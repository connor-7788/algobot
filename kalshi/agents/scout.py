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

    def _in_live_window(self, m, now) -> bool:
        """Cheap pre-filter so we don't fetch books for games days away. Kalshi's
        occurrence_datetime sits ~3h after kickoff; Edge Finder does the exact
        "has it started" check against the sportsbook's kickoff time."""
        t = m.occurrence_time or m.expected_expiration_time
        if t is None:
            return False
        hours = (t - now).total_seconds() / 3600
        return -self.s.scout_live_lookback_h <= hours <= self.s.scout_live_lookahead_h

    def scan(self) -> list[Candidate]:
        if self.s.market_focus == "crypto_15m":
            return self.scan_crypto()
        return self.scan_sports()

    def scan_crypto(self) -> list[Candidate]:
        """15-minute up/down markets: one open market per coin at a time."""
        now = datetime.now(timezone.utc)
        out: list[Candidate] = []
        stats = {"markets": 0, "closing_soon": 0, "no_strike": 0, "no_book": 0,
                 "wide_spread": 0, "thin": 0, "passed": 0, "errors": 0}
        for series, asset in self.s.crypto_series.items():
            try:
                markets = self.data.get_markets(series, status="open", max_pages=1)
            except Exception as e:
                stats["errors"] += 1
                log.warning("get_markets %s failed: %s", series, e)
                continue
            if not markets and series not in self._warned_empty:
                self._warned_empty.add(series)
                log.warning("series %s returned 0 open markets", series)
            stats["markets"] += len(markets)
            for m in markets:
                if m.status and m.status not in ("active", "open"):
                    continue
                if m.close_time is None or (m.close_time - now).total_seconds() < self.s.crypto_min_seconds_to_close:
                    stats["closing_soon"] += 1
                    continue
                if m.floor_strike is None or m.strike_type not in ("greater", "greater_or_equal", "less", "less_or_equal"):
                    stats["no_strike"] += 1
                    continue
                try:
                    book = self.data.get_orderbook(m.ticker, depth=self.s.scout_orderbook_depth)
                except Exception as e:
                    stats["errors"] += 1
                    log.debug("orderbook %s failed: %s", m.ticker, e)
                    continue
                self.history.record_ask(m.ticker, "yes", book.best_ask_cents("yes"))
                self.history.record_ask(m.ticker, "no", book.best_ask_cents("no"))
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
                out.append(Candidate(market=m, book=book, sport_key="", asset=asset, scanned_at=now))
        self.last_stats = stats
        log.info("scout: %s", stats)
        return out

    def scan_sports(self) -> list[Candidate]:
        now = datetime.now(timezone.utc)
        out: list[Candidate] = []
        stats = {"markets": 0, "closing_soon": 0, "not_live_window": 0, "low_volume": 0, "no_book": 0,
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
            # Kalshi game events hold one market per team ("Washington wins",
            # "San Francisco wins"), so the sibling's yes_sub_title is the opponent.
            by_event: dict[str, list[str]] = {}
            for m in markets:
                by_event.setdefault(m.event_ticker, []).append(m.yes_sub_title)

            # Cheap filters on market fields first, so we only fetch books we might use.
            shortlist = []
            for m in markets:
                if m.status and m.status not in ("active", "open"):
                    continue
                if m.close_time is None or (m.close_time - now).total_seconds() < self.s.scout_min_minutes_to_close * 60:
                    stats["closing_soon"] += 1
                    continue
                if self.s.edge_game_phase == "live_only" and not self._in_live_window(m, now):
                    stats["not_live_window"] += 1
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
                others = [n for n in by_event.get(m.event_ticker, []) if n != m.yes_sub_title]
                opponent = others[0] if len(others) == 1 else ""
                out.append(Candidate(market=m, book=book, sport_key=sport_key, opponent=opponent,
                                     scanned_at=now))
        self.last_stats = stats
        log.info("scout: %s", stats)
        return out
