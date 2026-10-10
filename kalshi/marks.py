"""
MarkFeed: keeps live prices for every open position, refreshed every
settings.marks_interval_s (default 1 s).

One GET /markets?tickers=... per tick covers all open positions (at most 10),
which is 10 read tokens a second against a 200/s Basic-tier budget. Prices go
into the journal's marks table, so any number of dashboard viewers read them
without adding API calls. After each refresh it calls on_tick (the
orchestrator's take-profit / stop-loss check), which routes any exit through
Risk and Execution. Nothing in this module talks to the order API itself.
"""

from __future__ import annotations

import logging
import threading
import time

from kalshi.client import KalshiDataClient
from kalshi.journal import Journal
from kalshi.pnl import held_positions

log = logging.getLogger("marks")


class MarkFeed:
    def __init__(self, settings, journal: Journal, data: KalshiDataClient, on_tick=None):
        self.s = settings
        self.journal = journal
        self.data = data
        self.on_tick = on_tick          # orchestrator.run_exits: take-profit / stop-loss

    def tick(self) -> int:
        tickers = sorted({p.market_ticker for p in held_positions(self.journal.open_positions())})
        if not tickers:
            return 0
        markets = self.data.get_markets_by_tickers(tickers)
        self.journal.upsert_marks([{
            "ticker": m.ticker, "yes_bid": m.yes_bid_cents, "yes_ask": m.yes_ask_cents,
            "last_price": m.last_price_cents, "status": m.status, "result": m.result,
        } for m in markets])
        if self.on_tick is not None:
            self.on_tick()
        return len(markets)

    def run_forever(self, stop: threading.Event) -> None:
        backoff = self.s.marks_interval_s
        while not stop.is_set():
            started = time.monotonic()
            try:
                self.tick()
                backoff = self.s.marks_interval_s
            except Exception as e:
                backoff = min(30.0, backoff * 2)
                log.warning("mark refresh failed (%s); next try in %.0fs", e, backoff)
            stop.wait(max(0.05, backoff - (time.monotonic() - started)))
