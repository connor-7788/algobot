"""
Kalshi ORDER client. Places and cancels orders.

Only kalshi/agents/execution.py may import this module. tests/test_import_boundary.py
fails the build if anything else does.

Endpoints (V2 order API, docs.kalshi.com, read Oct 10, 2026):
    POST   /portfolio/events/orders            create
    DELETE /portfolio/events/orders/{id}       cancel (?market_ticker= for routing)
V2 uses one YES book: side "bid" buys YES, side "ask" sells YES. Prices are
dollar strings ("0.4500"), counts are fixed-point strings ("10" or "10.00").

ASSUMPTION TO VERIFY IN DEMO: an "ask" on YES with no YES position opens a NO
position, so "buy NO at q" is sent as "ask YES at 1 - q". Kalshi reports NO
holdings as negative position_fp, which fits this, but place one 1-contract NO
order in demo and check the position before trusting it with size.

Idempotency: client_order_id = proposal_id. Kalshi rejects a duplicate
client_order_id with HTTP 409, so a retry after a timeout can never create a
second order. On 409 we look the original order up and return it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from kalshi.client import OrderState, count_value, normalize_order
from kalshi.http import KalshiError, KalshiHTTP

log = logging.getLogger("kalshi.orders")


@dataclass
class PlaceResult:
    order_id: str
    client_order_id: str
    fill_count: int
    remaining_count: int
    avg_fill_price_cents: float | None      # price paid for OUR side
    avg_fee_per_contract_usd: float | None
    duplicate: bool = False                  # True if resolved via 409 lookup


def to_v2_side_and_price(side: str, price_cents: int) -> tuple[str, str]:
    """Map (outcome side, price for that side) to V2 (book side, YES price)."""
    if side == "yes":
        return "bid", f"{Decimal(price_cents) / 100:.4f}"
    if side == "no":
        return "ask", f"{Decimal(100 - price_cents) / 100:.4f}"
    raise ValueError(side)


class KalshiOrderClient:
    def __init__(self, http: KalshiHTTP, settings):
        if not http.authenticated:
            raise RuntimeError("KalshiOrderClient needs API credentials")
        self.http = http
        self.settings = settings

    def place_limit_order(self, *, ticker: str, side: str, price_cents: int,
                          count: int, client_order_id: str) -> PlaceResult:
        if count < 1:
            raise ValueError("count must be >= 1")
        if not (1 <= price_cents <= 99):
            raise ValueError("price_cents must be 1..99")
        book_side, yes_price = to_v2_side_and_price(side, price_cents)
        body = {
            "ticker": ticker,
            "client_order_id": client_order_id,
            "side": book_side,
            "count": str(int(count)),
            "price": yes_price,
            "time_in_force": "good_till_canceled",
            "self_trade_prevention_type": self.settings.self_trade_prevention_type,
            "cancel_order_on_pause": True,
        }
        log.info("PLACE %s", body)
        try:
            data = self.http.request("POST", "/portfolio/events/orders", body=body, write=True)
        except KalshiError as e:
            if e.status == 409:
                log.warning("409 on %s: order already exists, looking it up", client_order_id)
                existing = self.lookup(client_order_id, ticker)
                if existing is None:
                    raise
                return PlaceResult(existing.order_id, client_order_id, existing.fill_count,
                                   existing.remaining_count, None, None, duplicate=True)
            raise

        avg_yes = data.get("average_fill_price")
        avg_cents = None
        if avg_yes not in (None, ""):
            yes_c = float(Decimal(str(avg_yes)) * 100)
            avg_cents = yes_c if side == "yes" else 100.0 - yes_c
        avg_fee = data.get("average_fee_paid")
        return PlaceResult(
            order_id=data.get("order_id", ""),
            client_order_id=data.get("client_order_id", client_order_id) or client_order_id,
            fill_count=int(count_value(data, "fill_count")),
            remaining_count=int(count_value(data, "remaining_count")),
            avg_fill_price_cents=avg_cents,
            avg_fee_per_contract_usd=float(Decimal(str(avg_fee))) if avg_fee not in (None, "") else None,
        )

    def get_order(self, order_id: str) -> OrderState:
        data = self.http.request("GET", f"/portfolio/orders/{order_id}")
        return normalize_order(data.get("order", data))

    def cancel_order(self, order_id: str, ticker: str) -> float:
        """Cancel the resting remainder. Returns contracts cancelled."""
        data = self.http.request("DELETE", f"/portfolio/events/orders/{order_id}",
                                 params={"market_ticker": ticker}, write=True,
                                 cost=self.settings.rate_cancel_cost)
        return float(Decimal(str(data.get("reduced_by", "0") or "0")))

    def lookup(self, client_order_id: str, ticker: str) -> OrderState | None:
        data = self.http.request("GET", "/portfolio/orders", params={"ticker": ticker, "limit": 1000})
        for o in data.get("orders", []):
            if (o.get("client_order_id") or "") == client_order_id:
                return normalize_order(o)
        return None
