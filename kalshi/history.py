"""
In-memory recent history per market: Kalshi asks seen by Scout, fair values
and edges seen by Edge Finder. Skeptic reads it to spot sharp line moves;
Edge Finder reads it to count confirmations for the confidence model.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class MarketHistory:
    def __init__(self, max_age_s: float = 3600.0, clock=time.time):
        self.max_age_s = max_age_s
        self.clock = clock
        self._lock = threading.Lock()
        self._asks: dict[tuple[str, str], deque] = defaultdict(lambda: deque(maxlen=500))
        self._fair: dict[str, deque] = defaultdict(lambda: deque(maxlen=500))
        self._edges: dict[tuple[str, str], deque] = defaultdict(lambda: deque(maxlen=500))

    def _trim(self, dq: deque, now: float) -> None:
        while dq and now - dq[0][0] > self.max_age_s:
            dq.popleft()

    def record_ask(self, ticker: str, side: str, ask_cents: int | None, ts: float | None = None) -> None:
        if ask_cents is None:
            return
        now = ts if ts is not None else self.clock()
        with self._lock:
            dq = self._asks[(ticker, side)]
            dq.append((now, float(ask_cents)))
            self._trim(dq, now)

    def record_fair(self, ticker: str, fair_yes: float, ts: float | None = None) -> None:
        now = ts if ts is not None else self.clock()
        with self._lock:
            dq = self._fair[ticker]
            dq.append((now, float(fair_yes)))
            self._trim(dq, now)

    def record_edge(self, ticker: str, side: str, edge_cents: float, ts: float | None = None) -> None:
        now = ts if ts is not None else self.clock()
        with self._lock:
            dq = self._edges[(ticker, side)]
            dq.append((now, float(edge_cents)))
            self._trim(dq, now)

    @staticmethod
    def _range(dq, now: float, window_s: float) -> float:
        vals = [v for t, v in dq if now - t <= window_s]
        return (max(vals) - min(vals)) if len(vals) >= 2 else 0.0

    def ask_move_cents(self, ticker: str, side: str, window_s: float) -> float:
        now = self.clock()
        with self._lock:
            return self._range(list(self._asks.get((ticker, side), ())), now, window_s)

    def fair_move(self, ticker: str, window_s: float) -> float:
        now = self.clock()
        with self._lock:
            return self._range(list(self._fair.get(ticker, ())), now, window_s)

    def edge_confirmations(self, ticker: str, side: str, window_s: float, min_edge: float) -> int:
        """Observations already recorded in the window where the edge cleared
        the minimum. Call this BEFORE record_edge() for the current scan."""
        now = self.clock()
        with self._lock:
            obs = [v for t, v in self._edges.get((ticker, side), ()) if now - t <= window_s]
        return sum(1 for v in obs if v >= min_edge)
