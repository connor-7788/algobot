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
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from kalshi.agents.base import Candidate
from kalshi.agents.shadow import pick_side, setup_key
from kalshi.confidence import confidence_score
from kalshi.features import chart_features, describe
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


def blend_with_market(p_model: float, p_market: float, model_weight: float) -> float:
    """Logit-space blend. model_weight 1.0 = model only, 0.0 = market only."""
    def logit(p):
        p = min(0.999, max(0.001, p))
        return math.log(p / (1 - p))
    x = model_weight * logit(p_model) + (1 - model_weight) * logit(p_market)
    return 1 / (1 + math.exp(-x))


class EdgeFinder:
    name = Agent.EDGE_FINDER.value

    def __init__(self, settings, odds: OddsAPIClient, history: MarketHistory, spot=None,
                 params=None, learner=None, shadow=None):
        self.s = settings
        self.odds = odds
        self.history = history
        self.spot = spot            # SpotClient for crypto 15m markets
        self.params = params        # learner.ParamStore: calibration + entry threshold learned from results
        self.learner = learner      # learner.Learner: records what we saw so outcomes can teach us
        self.shadow = shadow        # agents.shadow.Shadow: pretend trades it wouldn't take, explained later

    def _learned(self):
        if self.params is not None:
            return self.params.get()
        return None

    def _sigma_scale(self) -> float:
        p = self._learned()
        return p.sigma_scale if p else self.s.crypto_sigma_scale

    def _model_weight(self) -> float:
        p = self._learned()
        return p.model_weight if p else self.s.crypto_model_weight

    def _edge_min(self, crypto: bool) -> float:
        p = self._learned()
        return p.edge_min_cents if (p and crypto) else self.s.edge_min_cents

    def _exploring(self) -> bool:
        """Paper only: keep trading (on the chart model) while Learner has paused, to learn from trades."""
        p = self._learned()
        return bool(p and p.paused and self.s.paper_mode and self.s.paper_explore)

    # ── Follow the best tested strategy ──────────────────────────────────────

    def _following(self, ctx: dict) -> dict | None:
        """The selected strategy to follow on this crypto market, or None to use edge pricing."""
        if "crypto" not in ctx or not self.s.follow_best_strategy:
            return None
        p = self._learned()
        if p is None or not p.strategy or not p.paused:
            return None                     # a validated edge threshold beats any shadow strategy
        if not (self.s.paper_mode or p.strategy.get("validated")):
            return None                     # live mode only follows a strategy proven on newer markets
        return p.strategy

    def _follow_strategy(self, c: Candidate, ctx: dict, sources, st: dict) -> EdgeResult:
        m, book = c.market, c.book
        k = ctx["crypto"]
        name, want_setup = st["strategy"], st.get("setup")
        label = name + (f" ({want_setup})" if want_setup else "")
        feats, secs = k.get("feats"), k["seconds_left"]
        lo, hi = self.s.shadow_entry_min_secs, self.s.shadow_entry_max_secs
        if not feats:
            return EdgeResult(None, f"following {label}: no chart data for this market yet")
        if not (lo <= secs <= hi):
            return EdgeResult(None, f"following {label}: it enters with {lo / 60:.1f}-{hi / 60:.0f} min left, "
                                    f"now {secs / 60:.1f} min")
        bid, ask = book.best_bid_cents("yes"), book.best_ask_cents("yes")
        pick = pick_side(name, feats, k.get("p_chart"), bid, ask, self.s)
        if pick is None:
            return EdgeResult(None, f"following {label}: no signal on this market (5-min momentum "
                                    f"{feats.get('mom5', 0):+.1f}σ)")
        side, why = pick
        price = book.best_ask_cents(side)
        if price is None or not (3 <= price <= 97):
            return EdgeResult(None, f"following {label}: {side.upper()} ask {price} is outside 3-97c")
        here = setup_key(feats, side, price)
        if want_setup and here != want_setup:
            return EdgeResult(None, f"following {label}: this market's setup is '{here}', skip")
        limit = min(99, price + self.s.exec_slippage_cap_cents)
        fee = worst_case_fee_per_contract_cents(limit, self.s)
        va, tr = st["valid"], st["train"]
        exp_cents = va["roi"] * (limit + fee)
        fair_side = min(0.99, max(0.01, (limit + fee + exp_cents) / 100.0))
        depth = book.ask_depth(side)
        upper = int(min(self.s.edge_max_contracts, math.floor(depth * self.s.edge_max_depth_take)))
        if upper < 1:
            return EdgeResult(None, f"following {label}: ask depth {depth:g} too thin")
        score, parts = confidence_score(n_books=len(sources), dispersion=0.0, edge_cents=exp_cents,
                                        confirmations=0, spread_cents=book.spread_cents(), settings=self.s)
        k = dict(k, strategy=name, strategy_setup=want_setup, strategy_validated=bool(st.get("validated")))
        snapshot = {"side": side, "price_cents": price, "limit_cents": limit, "fee_cents_per_contract": round(fee, 4),
                    "edge_cents": round(exp_cents, 3), "fair_side": round(fair_side, 5), "confidence": score,
                    "confidence_parts": parts, "ask_depth": depth, "contract_count": upper,
                    "book": book.snapshot(), "crypto": k,
                    "strategy_record": {"older": tr, "newer": va}}
        dist = -k["move_needed_pct"]
        tag = "FOLLOWING BEST STRATEGY" + ("" if st.get("validated") else " (paper only: not yet validated on newer markets)")
        reason = (f"{tag}: {label}. {why}. {k['asset']} is {abs(dist):.3f}% {'above' if dist >= 0 else 'below'} "
                  f"the strike with {secs / 60:.1f} min left; chart: {describe(feats, side)}. Buy {side.upper()} at "
                  f"{price}c (+{self.s.exec_slippage_cap_cents}c slippage, +{fee:.1f}c fee). Its record: "
                  f"{tr['roi'] * 100:+.1f}% per $ on {tr['n']} older trades ({tr['win_rate'] * 100:.0f}% wins), "
                  f"{va['roi'] * 100:+.1f}% on {va['n']} newer ({va['win_rate'] * 100:.0f}% wins).")
        p = TradeProposal(
            market_ticker=m.ticker, event_ticker=m.event_ticker, side=side, price_cents=price,
            contract_count=upper, fair_probability=fair_side, sources=sources, series_ticker=m.series_ticker,
            title=f"{m.title} | {label}", edge_cents=round(exp_cents, 3), fee_cents_per_contract=fee,
            confidence_score=score, book_fetched_at=book.fetched_at, close_time=m.close_time)
        p.add_verdict(self.name, Decision.APPROVE, reason, snapshot)
        return EdgeResult(p, "proposed")

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
        bid, ask = c.book.best_bid_cents("yes"), c.book.best_ask_cents("yes")
        if bid is None or ask is None:
            return "no two-sided market to anchor fair value"
        market_mid = (bid + ask) / 200.0
        scale, weight = self._sigma_scale(), self._model_weight()
        learned = self._learned()
        chart = learned.chart_model() if learned is not None else None
        exploring = self._exploring()
        mode = "chart" if (learned is not None and learned.mode == "chart" and chart) else "blend"
        if exploring and chart:
            mode = "explore-chart"
        try:
            closes = self.spot.closes(c.asset) if hasattr(self.spot, "closes") else {}
        except Exception as e:
            log.debug("chart closes failed: %s", e)
            closes = {}
        now_ts = time.time()

        def feats_for(spot):
            if not closes:
                return None
            return chart_features(closes=closes, now_ts=now_ts, spot=spot, strike=m.floor_strike, secs_left=secs,
                                  sigma=sigma, strike_type=m.strike_type, bid=bid, ask=ask)

        sources, models, charts = [], [], []
        for q in quotes:
            p_above = prob_avg_at_or_above(q.price, m.floor_strike, secs, sigma * scale)
            if p_above is None:
                return f"{secs:.0f}s left: inside the averaging window, the result is mostly locked in"
            p_model = p_above if m.strike_type in ("greater", "greater_or_equal") else 1.0 - p_above
            models.append(p_model)
            p_yes = blend_with_market(p_model, market_mid, weight)
            f = feats_for(q.price) if chart else None
            p_chart = chart.predict(f) if (chart and f) else None
            if p_chart is not None:
                charts.append(p_chart)
                if mode != "blend":
                    p_yes = p_chart
            sources.append(Source(name=q.source, probability=p_yes, fetched_at=q.fetched_at,
                                  observed_at=q.observed_at,
                                  detail={"spot": q.price, "model": round(p_model, 5),
                                          "chart": None if p_chart is None else round(p_chart, 5)}))
        if mode != "blend" and len(charts) < len(quotes):
            mode = "blend"                  # chart features unavailable: fall back for every source
            for src, p_model in zip(sources, models):
                src.probability = blend_with_market(p_model, market_mid, weight)
        fair_yes, dispersion = consensus(sources)
        spot_mid = statistics.median(q.price for q in quotes)
        feats = feats_for(spot_mid)
        p_chart_mid = chart.predict(feats) if (chart and feats) else None
        if m.close_time and m.floor_strike:
            close_ts = int(m.close_time.timestamp())
            try:
                if self.learner is not None:
                    self.learner.observe(ticker=m.ticker, series=m.series_ticker, close_ts=close_ts,
                                         secs_left=secs, spot=spot_mid, strike=m.floor_strike,
                                         strike_type=m.strike_type, sigma=sigma, bid=bid, ask=ask, feats=feats)
                if self.shadow is not None:
                    self.shadow.consider(ticker=m.ticker, series=m.series_ticker, close_ts=close_ts, secs_left=secs,
                                         spot=spot_mid, strike=m.floor_strike, strike_type=m.strike_type,
                                         bid=bid, ask=ask, feats=feats, p_chart=p_chart_mid)
            except Exception as e:          # learning must never block trading decisions
                log.warning("observation/shadow not recorded: %s", e)
        ctx = {"crypto": {"asset": c.asset, "spot": spot_mid, "strike": m.floor_strike,
                          "strike_type": m.strike_type, "seconds_left": round(secs, 1),
                          "sigma_per_min": round(sigma * 60 ** 0.5, 6),
                          "move_needed_pct": round((m.floor_strike / spot_mid - 1) * 100, 4),
                          "p_model": round(statistics.median(models), 5), "market_mid": round(market_mid, 4),
                          "sigma_scale": scale, "model_weight": weight, "mode": mode, "exploring": exploring,
                          "p_chart": None if p_chart_mid is None else round(p_chart_mid, 5), "feats": feats}}
        return sources, fair_yes, dispersion, ctx

    # ── Reasoning text ───────────────────────────────────────────────────────

    def _explain(self, c: Candidate, ctx: dict, fair_yes: float, best: dict, thr: float, score: int,
                 n_sources: int, dispersion: float) -> str:
        """One readable paragraph with the numbers that drove the call."""
        side = best["side"].upper()
        cost = best["limit"] + best["fee"]
        trade = (f"Buy {side} at {best['ask']}c (+{self.s.exec_slippage_cap_cents}c slippage, "
                 f"+{best['fee']:.1f}c fee = {cost:.1f}c) vs fair {100 * best['fair']:.1f}c: "
                 f"edge {best['edge']:+.1f}c, need {thr:+.0f}c.")
        if "crypto" in ctx:
            k = ctx["crypto"]
            dist = -k["move_needed_pct"]
            where = "above" if dist >= 0 else "below"
            chart = (f" Chart: {describe(k['feats'], best['side'])}." if k.get("feats") else "")
            if k.get("mode") in ("chart", "explore-chart"):
                how = (f"Chart model says YES {100 * fair_yes:.1f}% (distance model {100 * k['p_model']:.0f}%, "
                       f"Kalshi mid {100 * k['market_mid']:.0f}%).")
            else:
                pc = f", chart model {100 * k['p_chart']:.0f}%" if k.get("p_chart") is not None else ""
                how = (f"Distance model says YES {100 * k['p_model']:.0f}%, Kalshi mid {100 * k['market_mid']:.0f}%"
                       f"{pc}; fair {100 * fair_yes:.1f}% (model weight {k['model_weight']:g}).")
            return (f"{k['asset']} {k['spot']:,.2f} is {abs(dist):.3f}% {where} the {k['strike']:,.2f} strike "
                    f"with {k['seconds_left'] / 60:.1f} min left (vol {100 * k['sigma_per_min']:.3f}%/min).{chart} "
                    f"{how} {trade} Confidence {score}.")
        ev = ctx.get("matched_event", {})
        return (f"{ev.get('yes_team', c.market.yes_sub_title)}: {n_sources} books average "
                f"{100 * fair_yes:.1f}% (spread between books {100 * dispersion:.1f} pts). {trade} "
                f"Confidence {score}.")

    def evaluate(self, c: Candidate) -> EdgeResult:
        m, book = c.market, c.book
        yes_name = m.yes_sub_title or m.title
        fair = self._fair_crypto(c) if c.asset else self._fair_sports(c)
        if isinstance(fair, str):
            return EdgeResult(None, fair)
        sources, fair_yes, dispersion, ctx = fair
        self.history.record_fair(m.ticker, fair_yes)
        follow = self._following(ctx)
        if follow is not None:
            return self._follow_strategy(c, ctx, sources, follow)

        best = None
        for side in ("yes", "no"):
            ask = book.best_ask_cents(side)
            if ask is None or not (self.s.edge_min_price_cents <= ask <= self.s.edge_max_price_cents):
                continue
            fair_side = fair_yes if side == "yes" else 1.0 - fair_yes
            limit = min(99, ask + self.s.exec_slippage_cap_cents)
            fee_c = worst_case_fee_per_contract_cents(limit, self.s)
            edge_c = 100.0 * fair_side - limit - fee_c
            if best is None or edge_c > best["edge"]:
                best = {"side": side, "ask": ask, "limit": limit, "fee": fee_c,
                        "edge": edge_c, "fair": fair_side}
        if best is None:
            lo, hi = self.s.edge_min_price_cents, self.s.edge_max_price_cents
            asks = [book.best_ask_cents(sd) for sd in ("yes", "no")]
            return EdgeResult(None, f"both sides priced outside {lo}-{hi}c (YES ask {asks[0]}, NO ask {asks[1]}); "
                                    f"cheap long shots lost money in the backtest")

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

        thr = self._edge_min(bool(c.asset))
        exploring = bool(ctx.get("crypto", {}).get("exploring")) and ctx["crypto"].get("mode") == "explore-chart"
        if exploring:
            thr = self.s.explore_edge_min_cents
        snapshot["edge_min_cents"] = thr
        problems = []
        if thr >= 999:
            problems.append("Learner has paused entries: no edge threshold made money on recent settled markets"
                            + ("" if not self.s.paper_mode else " (no chart model yet to explore with)"))
        elif best["edge"] < thr:
            problems.append(f"edge {best['edge']:.2f}c < {thr:g}c")
        min_conf = self.s.explore_min_confidence if exploring else self.s.edge_min_confidence
        if score < min_conf:
            problems.append(f"confidence {score} < {min_conf}")
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

        story = self._explain(c, ctx, fair_yes, best, min(thr, 99), score, len(sources), dispersion)
        if problems:
            diag = None
            why = "; ".join(problems) + ". " + story
            if self.s.journal_edge_rejects:
                diag = build()
                diag.add_verdict(self.name, Decision.REJECT, why, snapshot)
            return EdgeResult(None, why, diag)

        p = build()
        tag = ("EXPLORE (paper only: Learner found no proven edge, so this trade tests the chart model). "
               if exploring else "")
        p.add_verdict(self.name, Decision.APPROVE,
                      f"{tag}edge {best['edge']:.2f}c after fees, confidence {score}. {story} "
                      f"Upper bound {upper} contracts ({depth:g} resting at the ask).", snapshot)
        return EdgeResult(p, "proposed")
