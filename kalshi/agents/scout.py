"""
Scout: pulls open markets for the configured sports series and drops anything
too thin, too wide, or too close to close. Emits Candidates. Never computes edge.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from kalshi.agents.base import Candidate
from kalshi.client import KalshiDataClient
from kalshi.history import MarketHistory

log = logging.getLogger("agent.scout")


class Scout:
    name = "scout"

    def __init__(self, settings, data: KalshiDataClient, history: MarketHistory):
        self.s = settings
        self.data = data
        self.history = history
        self._warned_empty: set[str] = set()
        self.last_stats: dict = {}

    def scan(self) -> list[Candidate]:
        now = datetime.now(timezone.utc)
        out: list[Candidate] = []
        stats = {"markets": 0, "closing_soon": 0, "low_volume": 0, "no_book": 0,
                 "wide_spread": 0, "thin": 0, "passed": 0, "errors": 0}
        for series, sport_key in self.s.sport_series.items():
            try:
                markets = self.data.get_markets(series, status="open")
            except Exception as e:
                stats["errors"] += 1
                log.warning("get_markets %s failed: %s", series, e)
                continue
            if not markets and series not in self._warned_empty:
                self._warned_empty.add(series)
                log.warning("series %s returned 0 open markets; check the ticker in config.sport_series", series)
            stats["markets"] += len(markets)

            # Cheap filters on market fields first, so we only fetch books we might use.
            shortlist = []
            for m in markets:
                if m.status and m.status not in ("active", "open"):
                    continue
                if m.close_time is None or (m.close_time - now).total_seconds() < self.s.scout_min_minutes_to_close * 60:
                    stats["closing_soon"] += 1
                    continue
                if m.volume_24h < self.s.scout_min_volume_24h:
                    stats["low_volume"] += 1
                    continue
                shortlist.append(m)
            shortlist.sort(key=lambda m: m.volume_24h, reverse=True)

            for m in shortlist[: self.s.scout_max_candidates_per_series]:
                try:
                    book = self.data.get_orderbook(m.ticker, depth=self.s.scout_orderbook_depth)
                except Exception as e:
                    stats["errors"] += 1
                    log.debug("orderbook %s failed: %s", m.ticker, e)
                    continue
                yes_ask, no_ask = book.best_ask_cents("yes"), book.best_ask_cents("no")
                self.history.record_ask(m.ticker, "yes", yes_ask)
                self.history.record_ask(m.ticker, "no", no_ask)
                spread = book.spread_cents()
                if spread is None:
                    stats["no_book"] += 1
                    continue
                if spread > self.s.scout_max_spread_cents:
                    stats["wide_spread"] += 1
                    continue
                if max(book.ask_depth("yes"), book.ask_depth("no")) < self.s.scout_min_ask_depth:
                    stats["thin"] += 1
                    continue
                stats["passed"] += 1
                out.append(Candidate(market=m, book=book, sport_key=sport_key, scanned_at=now))
        self.last_stats = stats
        log.info("scout: %s", stats)
        return out
