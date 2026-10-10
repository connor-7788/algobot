"""
Edge Finder: estimates fair probability and proposes trades with real edge.

fair probability = mean of devigged sportsbook lines for the team YES pays on
edge (cents)     = 100 * fair - (ask + slippage cap) - worst-case fee per contract

The slippage cap is included because Execution's limit price is ask + cap, so
the edge has to survive paying that. The fee is the larger of taker and maker
for a 1-contract fill, which bounds the per-contract fee from above.

A proposal needs edge >= edge_min_cents AND confidence >= edge_min_confidence.

Size: Edge Finder does not size. It sets contract_count to an upper bound
(the lesser of edge_max_contracts and edge_max_depth_take of the depth at the
ask). Risk sizes down from there and is never allowed to go up.
"""

from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone

from kalshi.agents.base import Candidate
from kalshi.confidence import confidence_score
from kalshi.fees import worst_case_fee_per_contract_cents
from kalshi.history import MarketHistory
from kalshi.odds_api import OddsAPIClient, consensus, match_market
from kalshi.proposal import Agent, Decision, Source, TradeProposal
from kalshi.spot import prob_avg_at_or_above

log = logging.getLogger("agent.edge_finder")


@dataclass
class EdgeResult:
    proposal: TradeProposal | None      # set only when Edge Finder approves
    reason: str
    diagnostic: TradeProposal | None = None   # a REJECTed proposal for the journal, if one could be built


class EdgeFinder:
    name = Agent.EDGE_FINDER.value

    def __init__(self, settings, odds: OddsAPIClient, history: MarketHistory, spot=None):
        self.s = settings
        self.odds = odds
        self.history = history
        self.spot = spot            # SpotClient for crypto 15m markets

    # ── Fair value: sports (devigged sportsbook consensus) ───────────────────

    def _fair_sports(self, c: Candidate):
        m = c.market
        yes_name = m.yes_sub_title or m.title
        try:
            events = self.odds.events(c.sport_key)
        except Exception as e:
            return f"odds fetch failed: {e}"
        start = m.occurrence_time or m.expected_expiration_time
        match = match_market(yes_name, c.opponent, start, events, self.s.match_max_start_diff_h, m.title)
        if match is None:
            return f"no unambiguous sportsbook match for {yes_name} vs {c.opponent or '?'}"
        event, team = match
        now = datetime.now(timezone.utc)
        phase = self.s.edge_game_phase
        if event.commence_time is None:
            return "sportsbook event has no start time"
        started = event.commence_time <= now
        if phase == "live_only":
            if not started:
                return f"not live yet (starts {event.commence_time:%m-%d %H:%M} UTC)"
            age_h = (now - event.commence_time).total_seconds() / 3600
            if age_h > self.s.edge_max_game_age_h:
                return f"game started {age_h:.1f}h ago, likely over"
        elif phase == "pregame_only" and started:
            return "event already started"
        sources = event.sources_for(team)
        if not sources:
            return "matched event has no usable lines"
        fair_yes, dispersion = consensus(sources)
        ctx = {"matched_event": {"id": event.event_id, "home": event.home_team, "away": event.away_team,
                                 "commence": event.commence_time.isoformat() if event.commence_time else None,
                                 "yes_team": team}}
        return sources, fair_yes, dispersion, ctx

    # ── Fair value: crypto 15m (spot vs strike, short-term volatility) ────────

    def _fair_crypto(self, c: Candidate):
        m = c.market
        if self.spot is None:
            return "no spot price client configured"
        now = datetime.now(timezone.utc)
        secs = (m.close_time - now).total_seconds() if m.close_time else 0
        try:
            quotes = self.spot.quotes(c.asset)
            sigma = self.spot.sigma(c.asset)
        except Exception as e:
            return f"spot/vol fetch failed: {e}"
        if not quotes:
            return f"no {c.asset} spot quotes"
        if not sigma:
            return f"no {c.asset} volatility estimate"
        sources = []
        for q in quotes:
            p_above = prob_avg_at_or_above(q.price, m.floor_strike, secs, sigma)
            if p_above is None:
                return f"{secs:.0f}s left: inside the averaging window"
            p_yes = p_above if m.strike_type in ("greater", "greater_or_equal") else 1.0 - p_above
            sources.append(Source(name=q.source, probability=p_yes, fetched_at=q.fetched_at,
                                  observed_at=q.observed_at, detail={"spot": q.price}))
        fair_yes, dispersion = consensus(sources)
        spot_mid = statistics.median(q.price for q in quotes)
        ctx = {"crypto": {"asset": c.asset, "spot": spot_mid, "strike": m.floor_strike,
                          "strike_type": m.strike_type, "seconds_left": round(secs, 1),
                          "sigma_per_min": round(sigma * 60 ** 0.5, 6),
                          "move_needed_pct": round((m.floor_strike / spot_mid - 1) * 100, 4)}}
        return sources, fair_yes, dispersion, ctx

    def evaluate(self, c: Candidate) -> EdgeResult:
        m, book = c.market, c.book
        yes_name = m.yes_sub_title or m.title
        fair = self._fair_crypto(c) if c.asset else self._fair_sports(c)
        if isinstance(fair, str):
            return EdgeResult(None, fair)
        sources, fair_yes, dispersion, ctx = fair
        self.history.record_fair(m.ticker, fair_yes)

        best = None
        for side in ("yes", "no"):
            ask = book.best_ask_cents(side)
            if ask is None or not (1 <= ask <= 99):
                continue
            fair_side = fair_yes if side == "yes" else 1.0 - fair_yes
            limit = min(99, ask + self.s.exec_slippage_cap_cents)
            fee_c = worst_case_fee_per_contract_cents(limit, self.s)
            edge_c = 100.0 * fair_side - limit - fee_c
            if best is None or edge_c > best["edge"]:
                best = {"side": side, "ask": ask, "limit": limit, "fee": fee_c,
                        "edge": edge_c, "fair": fair_side}
        if best is None:
            return EdgeResult(None, "no ask on either side")

        side = best["side"]
        confirmations = self.history.edge_confirmations(m.ticker, side, self.s.edge_confirm_window_s,
                                                        self.s.edge_min_cents)
        self.history.record_edge(m.ticker, side, best["edge"])
        score, parts = confidence_score(n_books=len(sources), dispersion=dispersion,
                                        edge_cents=best["edge"], confirmations=confirmations,
                                        spread_cents=book.spread_cents(), settings=self.s)
        depth = book.ask_depth(side)
        upper = int(min(self.s.edge_max_contracts, math.floor(depth * self.s.edge_max_depth_take)))

        snapshot = {
            "side": side, "price_cents": best["ask"], "limit_cents": best["limit"],
            "fee_cents_per_contract": round(best["fee"], 4), "edge_cents": round(best["edge"], 3),
            "fair_yes": round(fair_yes, 5), "fair_side": round(best["fair"], 5),
            "book_dispersion": round(dispersion, 5), "n_books": len(sources),
            "confidence": score, "confidence_parts": parts, "confirmations": confirmations,
            "ask_depth": depth, "contract_count": upper, "book": book.snapshot(),
            **ctx,
        }

        problems = []
        if best["edge"] < self.s.edge_min_cents:
            problems.append(f"edge {best['edge']:.2f}c < {self.s.edge_min_cents}c")
        if score < self.s.edge_min_confidence:
            problems.append(f"confidence {score} < {self.s.edge_min_confidence}")
        if dispersion > self.s.edge_max_book_dispersion:
            problems.append(f"books disagree (stdev {dispersion:.3f})")
        if upper < 1:
            problems.append(f"ask depth {depth:g} too thin")

        def build() -> TradeProposal:
            return TradeProposal(
                market_ticker=m.ticker, event_ticker=m.event_ticker, side=side,
                price_cents=best["ask"], contract_count=max(upper, 0),
                fair_probability=best["fair"], sources=sources,
                series_ticker=m.series_ticker, title=f"{m.title} | YES={yes_name}",
                edge_cents=round(best["edge"], 3), fee_cents_per_contract=best["fee"],
                confidence_score=score, book_fetched_at=book.fetched_at, close_time=m.close_time,
            )

        if problems:
            diag = None
            if self.s.journal_edge_rejects:
                diag = build()
                diag.add_verdict(self.name, Decision.REJECT, "; ".join(problems), snapshot)
            return EdgeResult(None, "; ".join(problems), diag)

        p = build()
        p.add_verdict(self.name, Decision.APPROVE,
                      f"edge {best['edge']:.2f}c after fees, confidence {score}", snapshot)
        return EdgeResult(p, "proposed")
