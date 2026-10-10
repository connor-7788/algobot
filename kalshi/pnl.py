"""
P&L math shared by the dashboard and tests.

Unrealized P&L marks each open position at what you'd actually pocket by
selling right now: the best bid for your side, minus the taker fee to sell,
minus what you paid (entry price and entry fees). That's deliberately the
conservative number; a fresh position starts slightly red by the spread plus
fees, which is the honest picture.

Your side's bid: YES -> yes_bid. NO -> 100 - yes_ask (a YES ask at a is a NO bid at 1 - a).
"""

from __future__ import annotations

from dataclasses import dataclass

from kalshi.fees import taker_fee_usd
from kalshi.proposal import Status, TradeProposal


@dataclass
class PositionPnL:
    proposal_id: str
    ticker: str
    title: str
    side: str
    contracts: int
    entry_cents: float
    bid_cents: float | None
    cost_usd: float
    value_usd: float | None
    pnl_usd: float | None
    pnl_pct: float | None
    mark_age_s: float | None


def side_bid_cents(side: str, mark: dict | None) -> float | None:
    if not mark:
        return None
    if side == "yes":
        return mark.get("yes_bid")
    ask = mark.get("yes_ask")
    return None if ask is None else 100.0 - ask


def position_pnl(p: TradeProposal, mark: dict | None, settings, mark_age_s: float | None = None) -> PositionPnL:
    n = int(p.filled_count)
    entry = float(p.avg_fill_price_cents or p.price_cents)
    cost = n * entry / 100.0 + float(p.fees_paid_usd or 0.0)
    bid = side_bid_cents(p.side, mark)
    if bid is None or n <= 0:
        value = pnl = pct = None
    else:
        bid_c = max(0, min(99, int(round(bid))))
        exit_fee = taker_fee_usd(bid_c, n, settings) if 1 <= bid_c <= 99 else 0.0
        value = n * bid / 100.0 - exit_fee
        pnl = value - cost
        pct = pnl / cost if cost > 0 else None
    return PositionPnL(p.proposal_id, p.market_ticker, p.title, p.side, n, entry, bid,
                       round(cost, 4), None if value is None else round(value, 4),
                       None if pnl is None else round(pnl, 4), pct, mark_age_s)


def held_positions(open_props: list[TradeProposal]) -> list[TradeProposal]:
    """Open proposals that actually hold contracts."""
    return [p for p in open_props
            if p.filled_count > 0 and p.status in (Status.OPEN, Status.PAPER_FILLED, Status.UNCERTAIN)]
