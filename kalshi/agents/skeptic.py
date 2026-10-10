"""
Skeptic: looks for a reason to kill the trade. APPROVE or REJECT only.

Checks, in order (first failure rejects):
  1. Stale inputs: the Kalshi book or any sportsbook pull older than
     skeptic_max_data_age_s, or a book whose line hasn't changed in
     skeptic_max_line_age_s.
  2. Single source: fewer than skeptic_min_sources independent books.
  3. Sharp line movement: Kalshi ask moved more than skeptic_max_line_move_cents,
     or the sportsbook fair moved more than skeptic_max_fair_move, inside
     skeptic_line_window_s.
  4. Losing record: this market type (series) has net negative realized P&L
     over its last skeptic_record_lookback settled trades, given at least
     skeptic_record_min_sample of them. Monitor writes these results.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from kalshi.agents.base import AgentDecision
from kalshi.history import MarketHistory
from kalshi.journal import Journal
from kalshi.proposal import Agent, TradeProposal

log = logging.getLogger("agent.skeptic")


class Skeptic:
    name = Agent.SKEPTIC.value

    def __init__(self, settings, history: MarketHistory, journal: Journal, now=None):
        self.s = settings
        self.history = history
        self.journal = journal
        self._now = now or (lambda: datetime.now(timezone.utc))

    def review(self, p: TradeProposal) -> AgentDecision:
        now = self._now()
        s = self.s

        # 1. Staleness
        if p.book_fetched_at is None:
            return AgentDecision.reject("no order book timestamp")
        book_age = (now - p.book_fetched_at).total_seconds()
        if book_age > s.skeptic_max_data_age_s:
            return AgentDecision.reject(
                f"order book is {book_age:.0f}s old (limit {s.skeptic_max_data_age_s:.0f}s): the price "
                f"Edge Finder saw may be gone", book_age_s=book_age)
        for src in p.sources:
            age = (now - src.fetched_at).total_seconds()
            if age > s.skeptic_max_data_age_s:
                return AgentDecision.reject(f"{src.name} data is {age:.0f}s old (limit {s.skeptic_max_data_age_s:.0f}s)",
                                            source=src.name, age_s=age)
            line_age = (now - src.observed_at).total_seconds()
            if line_age > s.skeptic_max_line_age_s:
                shown = f"{line_age:.0f}s" if line_age < 600 else f"{line_age/3600:.1f}h"
                return AgentDecision.reject(f"{src.name} price last changed {shown} ago (limit "
                                            f"{s.skeptic_max_line_age_s:.0f}s): feed may be frozen",
                                            source=src.name, line_age_s=line_age)

        # 2. Single source
        names = {src.name for src in p.sources}
        if len(names) < s.skeptic_min_sources:
            return AgentDecision.reject(
                f"edge rests on {len(names)} source(s) ({', '.join(sorted(names)) or 'none'}), need "
                f"{s.skeptic_min_sources} that agree", sources=sorted(names))

        # 3. Line movement
        ask_move = self.history.ask_move_cents(p.market_ticker, p.side, s.skeptic_line_window_s)
        if ask_move > s.skeptic_max_line_move_cents:
            return AgentDecision.reject(
                f"Kalshi ask moved {ask_move:.0f}c in {s.skeptic_line_window_s:.0f}s (limit "
                f"{s.skeptic_max_line_move_cents:.0f}c): someone may know something", ask_move_cents=ask_move)
        fair_move = self.history.fair_move(p.market_ticker, s.skeptic_line_window_s)
        if fair_move > s.skeptic_max_fair_move:
            return AgentDecision.reject(
                f"fair value moved {fair_move*100:.1f} pts in {s.skeptic_line_window_s:.0f}s (limit "
                f"{s.skeptic_max_fair_move*100:.0f} pts): too jumpy to trust",
                fair_move=fair_move)

        # 4. Losing record on this market type
        results = self.journal.market_type_results(p.series_ticker, s.skeptic_record_lookback)
        net = sum(results)
        if len(results) >= s.skeptic_record_min_sample and net < 0:
            wins = sum(1 for r in results if r > 0)
            return AgentDecision.reject(
                f"{p.series_ticker} is {wins}-{len(results)-wins}, net ${net:.2f} over our last {len(results)} "
                f"settled trades: sitting this series out until the record turns positive",
                series=p.series_ticker, n=len(results), net_pnl=net)

        record = (f"{p.series_ticker} record {sum(1 for r in results if r > 0)}-"
                  f"{sum(1 for r in results if r <= 0)} (${net:+.2f})" if results else
                  f"no settled {p.series_ticker} trades yet")
        return AgentDecision.approve(
            f"checked and passed: book {book_age:.1f}s old (max {s.skeptic_max_data_age_s:.0f}s), "
            f"{len(names)} sources ({', '.join(sorted(names))}), ask moved {ask_move:.0f}c "
            f"(max {s.skeptic_max_line_move_cents:.0f}c), fair moved {fair_move*100:.1f} pts "
            f"(max {s.skeptic_max_fair_move*100:.0f}), {record}",
            book_age_s=round(book_age, 1), sources=sorted(names), ask_move_cents=ask_move,
            fair_move=round(fair_move, 4), series_record_n=len(results), series_net_pnl=round(net, 2))
