"""
Read-only Kalshi client: markets, order books, positions, order status.

This module cannot place or cancel orders. Only kalshi/orders.py can, and only
kalshi/agents/execution.py may import that (tests/test_import_boundary.py
enforces it).

Field formats: Kalshi moved prices to fixed-point dollar strings (`*_dollars`,
up to 4 decimals) and counts to fixed-point strings (`*_fp`, 2 decimals).
Integer-cent fields may still appear on some responses. Every reader here
tries the new field first, falls back to the old one, and logs which format
it saw the first time.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from kalshi.http import KalshiHTTP
from kalshi.orderbook import Book, parse_orderbook

log = logging.getLogger("kalshi.client")
_seen: set[str] = set()


def _note(field_name: str, fmt: str) -> None:
    key = f"{field_name}:{fmt}"
    if key not in _seen:
        _seen.add(key)
        log.info("Kalshi field %s arrived as %s", field_name, fmt)


def price_dollars(obj: dict, base: str) -> Decimal | None:
    """Read a price as Decimal dollars from `{base}_dollars` or legacy `{base}` cents."""
    v = obj.get(f"{base}_dollars")
    if v not in (None, ""):
        _note(base, "dollar string")
        return Decimal(str(v))
    v = obj.get(base)
    if v not in (None, ""):
        _note(base, "integer cents")
        return Decimal(int(v)) / 100
    return None


def count_value(obj: dict, base: str) -> Decimal:
    v = obj.get(f"{base}_fp")
    if v not in (None, ""):
        _note(base, "fixed-point string")
        return Decimal(str(v))
    v = obj.get(base)
    if v not in (None, ""):
        _note(base, "integer")
        return Decimal(str(v))
    return Decimal(0)


def money_dollars(obj: dict, base: str) -> Decimal:
    """For cost/fee fields: `{base}_dollars` string, else `{base}` cents."""
    p = price_dollars(obj, base)
    return p if p is not None else Decimal(0)


def parse_ts(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        secs = value / 1000 if value > 1e11 else value
        return datetime.fromtimestamp(secs, tz=timezone.utc)
    s = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class Market:
    ticker: str
    event_ticker: str
    series_ticker: str
    title: str
    yes_sub_title: str
    no_sub_title: str
    status: str
    close_time: datetime | None
    expected_expiration_time: datetime | None
    occurrence_time: datetime | None
    volume_24h: float
    yes_bid_cents: int | None
    yes_ask_cents: int | None
    result: str
    rules_primary: str = ""
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, m: dict, series_ticker: str = "") -> "Market":
        yb = price_dollars(m, "yes_bid")
        ya = price_dollars(m, "yes_ask")
        return cls(
            ticker=m.get("ticker", ""),
            event_ticker=m.get("event_ticker", ""),
            series_ticker=series_ticker or m.get("series_ticker", "") or m.get("event_ticker", "").split("-")[0],
            title=m.get("title", "") or "",
            yes_sub_title=m.get("yes_sub_title", "") or "",
            no_sub_title=m.get("no_sub_title", "") or "",
            status=(m.get("status") or "").lower(),
            close_time=parse_ts(m.get("close_time")),
            expected_expiration_time=parse_ts(m.get("expected_expiration_time")),
            occurrence_time=parse_ts(m.get("occurrence_datetime")),
            volume_24h=float(count_value(m, "volume_24h")),
            yes_bid_cents=None if yb is None else int(math.floor(yb * 100)),
            yes_ask_cents=None if ya is None else int(math.ceil(ya * 100)),
            result=(m.get("result") or "").lower(),
            rules_primary=m.get("rules_primary", "") or "",
            raw=m,
        )

    @property
    def is_settled(self) -> bool:
        return self.result in ("yes", "no") and self.status in (
            "determined", "settled", "finalized", "amended", "closed")


@dataclass
class OrderState:
    order_id: str
    client_order_id: str
    ticker: str
    status: str                  # resting | canceled | executed | unknown
    fill_count: int
    remaining_count: int
    fill_cost_usd: float
    fees_usd: float
    raw: dict = field(default_factory=dict, repr=False)


def normalize_order(o: dict) -> OrderState:
    fill = count_value(o, "fill_count")
    remaining = count_value(o, "remaining_count")
    cost = money_dollars(o, "taker_fill_cost") + money_dollars(o, "maker_fill_cost")
    fees = money_dollars(o, "taker_fees") + money_dollars(o, "maker_fees")
    return OrderState(
        order_id=o.get("order_id", ""),
        client_order_id=o.get("client_order_id", "") or "",
        ticker=o.get("ticker", "") or o.get("market_ticker", ""),
        status=(o.get("status") or "unknown").lower(),
        fill_count=int(fill),
        remaining_count=int(remaining),
        fill_cost_usd=float(cost),
        fees_usd=float(fees),
        raw=o,
    )


class KalshiDataClient:
    def __init__(self, http: KalshiHTTP):
        self.http = http

    # ── Market data ──────────────────────────────────────────────────────────
    # Public endpoints, sent unsigned (confirmed Oct 10, 2026: /markets and
    # /series answer without auth headers). That lets paper mode read prod
    # prices with a demo key, since a demo key signing a prod request gets 401.

    def get_markets(self, series_ticker: str, status: str = "open", max_pages: int = 10) -> list[Market]:
        markets: list[Market] = []
        cursor = ""
        for _ in range(max_pages):
            params = {"series_ticker": series_ticker, "status": status, "limit": 200}
            if cursor:
                params["cursor"] = cursor
            data = self.http.request("GET", "/markets", params=params, auth=False)
            markets.extend(Market.from_api(m, series_ticker) for m in data.get("markets", []))
            cursor = data.get("cursor") or ""
            if not cursor:
                break
        return markets

    def get_market(self, ticker: str) -> Market:
        data = self.http.request("GET", f"/markets/{ticker}", auth=False)
        return Market.from_api(data.get("market", data))

    def get_event(self, event_ticker: str) -> dict:
        data = self.http.request("GET", f"/events/{event_ticker}", auth=False)
        return data.get("event", data)

    def get_orderbook(self, ticker: str, depth: int = 10) -> Book:
        data = self.http.request("GET", f"/markets/{ticker}/orderbook",
                                 params={"depth": depth}, auth=False)
        return parse_orderbook(ticker, data)

    # ── Portfolio (read-only, needs credentials) ─────────────────────────────

    def get_balance_usd(self) -> float:
        data = self.http.request("GET", "/portfolio/balance")
        if data.get("balance_dollars") not in (None, ""):
            return float(Decimal(str(data["balance_dollars"])))
        return float(data.get("balance", 0)) / 100

    def get_positions(self) -> dict[str, float]:
        """ticker -> signed contracts (positive YES, negative NO)."""
        out: dict[str, float] = {}
        cursor = ""
        for _ in range(20):
            params = {"limit": 1000, "count_filter": "position"}
            if cursor:
                params["cursor"] = cursor
            data = self.http.request("GET", "/portfolio/positions", params=params)
            for p in data.get("market_positions", []):
                out[p.get("ticker", "")] = float(count_value(p, "position"))
            cursor = data.get("cursor") or ""
            if not cursor:
                break
        return out

    def get_order(self, order_id: str) -> OrderState:
        # ASSUMPTION: GET /portfolio/orders/{order_id} returns {"order": {...}}
        # (the "Get Order" page is listed in the docs index).
        data = self.http.request("GET", f"/portfolio/orders/{order_id}")
        return normalize_order(data.get("order", data))

    def list_orders(self, ticker: str | None = None, status: str | None = None,
                    min_ts: int | None = None) -> list[OrderState]:
        params: dict = {"limit": 1000}
        if ticker:
            params["ticker"] = ticker
        if status:
            params["status"] = status
        if min_ts:
            params["min_ts"] = min_ts
        data = self.http.request("GET", "/portfolio/orders", params=params)
        return [normalize_order(o) for o in data.get("orders", [])]

    def find_order_by_client_id(self, client_order_id: str, ticker: str,
                                min_ts: int | None = None) -> OrderState | None:
        for o in self.list_orders(ticker=ticker, min_ts=min_ts):
            if o.client_order_id == client_order_id:
                return o
        return None
