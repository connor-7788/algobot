"""
Order book parsing.

GET /markets/{ticker}/orderbook returns only bids (docs read Oct 10, 2026):
    {"orderbook_fp": {"yes_dollars": [["0.4500", "120.00"], ...],
                      "no_dollars":  [["0.5300", "80.00"], ...]}}
A NO bid at q is a YES ask at 1 - q, so:
    best YES ask = 1 - best NO bid, size = size of that NO bid
    best NO  ask = 1 - best YES bid

Older responses used {"orderbook": {"yes": [[45, 120], ...], "no": [...]}} with
integer cents. Both are accepted, and the format seen is logged once.

Some markets now tick below a cent. We trade whole cents only (whole-cent
prices are valid in every Kalshi price structure), so asks are rounded UP to
the next cent and bids DOWN. That never makes a book look better than it is.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

log = logging.getLogger("kalshi.orderbook")
_seen_formats: set[str] = set()


def _note_format(fmt: str) -> None:
    if fmt not in _seen_formats:
        _seen_formats.add(fmt)
        log.info("Kalshi orderbook format detected: %s", fmt)


@dataclass
class Book:
    ticker: str
    yes_bids: list[tuple[Decimal, Decimal]] = field(default_factory=list)   # (price $, size)
    no_bids: list[tuple[Decimal, Decimal]] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @staticmethod
    def _best(levels):
        return max(levels, key=lambda lv: lv[0]) if levels else None

    def best_bid_cents(self, side: str) -> int | None:
        lv = self._best(self.yes_bids if side == "yes" else self.no_bids)
        return None if lv is None else int(math.floor(lv[0] * 100))

    def best_ask_cents(self, side: str) -> int | None:
        """Cheapest price to BUY `side`, in whole cents (rounded up)."""
        opposite = self._best(self.no_bids if side == "yes" else self.yes_bids)
        if opposite is None:
            return None
        return int(math.ceil((Decimal(1) - opposite[0]) * 100))

    def ask_depth(self, side: str) -> float:
        """Contracts available at the best ask for `side`."""
        opposite = self._best(self.no_bids if side == "yes" else self.yes_bids)
        return float(opposite[1]) if opposite else 0.0

    def depth_at_or_below(self, side: str, limit_cents: int) -> float:
        """Contracts purchasable for `side` at prices <= limit_cents."""
        levels = self.no_bids if side == "yes" else self.yes_bids
        total = Decimal(0)
        for price, size in levels:
            if (Decimal(1) - price) * 100 <= limit_cents:
                total += size
        return float(total)

    def spread_cents(self) -> int | None:
        bid = self.best_bid_cents("yes")
        ask = self.best_ask_cents("yes")
        if bid is None or ask is None:
            return None
        return ask - bid

    def snapshot(self) -> dict:
        return {
            "yes_bid": self.best_bid_cents("yes"),
            "yes_ask": self.best_ask_cents("yes"),
            "no_ask": self.best_ask_cents("no"),
            "yes_ask_depth": self.ask_depth("yes"),
            "no_ask_depth": self.ask_depth("no"),
            "fetched_at": self.fetched_at.isoformat(),
        }


def _levels_dollars(raw) -> list[tuple[Decimal, Decimal]]:
    out = []
    for lv in raw or []:
        price, size = Decimal(str(lv[0])), Decimal(str(lv[1]))
        if size > 0:
            out.append((price, size))
    return out


def _levels_cents(raw) -> list[tuple[Decimal, Decimal]]:
    out = []
    for lv in raw or []:
        price, size = Decimal(int(lv[0])) / 100, Decimal(str(lv[1]))
        if size > 0:
            out.append((price, size))
    return out


def parse_orderbook(ticker: str, payload: dict) -> Book:
    if "orderbook_fp" in payload:
        ob = payload["orderbook_fp"] or {}
        _note_format("orderbook_fp (dollar strings)")
        return Book(ticker, _levels_dollars(ob.get("yes_dollars")), _levels_dollars(ob.get("no_dollars")))
    ob = payload.get("orderbook") or {}
    if "yes_dollars" in ob or "no_dollars" in ob:
        _note_format("orderbook with *_dollars keys")
        return Book(ticker, _levels_dollars(ob.get("yes_dollars")), _levels_dollars(ob.get("no_dollars")))
    _note_format("legacy orderbook (integer cents)")
    return Book(ticker, _levels_cents(ob.get("yes")), _levels_cents(ob.get("no")))
