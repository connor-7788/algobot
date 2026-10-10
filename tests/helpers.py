"""Shared fixtures for the unittest suite (stdlib only, no network)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from config import Settings
from kalshi.journal import Journal
from kalshi.killswitch import KillSwitch
from kalshi.proposal import Agent, Decision, Source, Status, TradeProposal

NOW = datetime(2026, 10, 10, 18, 0, tzinfo=timezone.utc)


def settings(**kw) -> Settings:
    base = dict(paper_mode=True, kill_switch_env=False, journal_db_path=":memory:",
                exec_poll_interval_s=0.0, exec_order_timeout_s=5.0)
    base.update(kw)
    return Settings(**base)


def journal(paper: bool = True) -> Journal:
    return Journal(":memory:", paper=paper)


def sources(n: int = 2, prob: float = 0.55, fetched: datetime = NOW, observed: datetime = NOW) -> list[Source]:
    return [Source(name=f"book{i}", probability=prob, fetched_at=fetched, observed_at=observed)
            for i in range(n)]


def proposal(ticker: str = "KXNFLGAME-26OCT11DALNYG-DAL", event: str = "KXNFLGAME-26OCT11DALNYG",
             side: str = "yes", price: int = 45, count: int = 50, fair: float = 0.52,
             with_edge_verdict: bool = True, series: str = "KXNFLGAME") -> TradeProposal:
    p = TradeProposal(market_ticker=ticker, event_ticker=event, side=side, price_cents=price,
                      contract_count=count, fair_probability=fair, sources=sources(),
                      series_ticker=series, edge_cents=4.0, fee_cents_per_contract=2.0,
                      confidence_score=80, created_at=NOW, book_fetched_at=NOW)
    if with_edge_verdict:
        p.add_verdict(Agent.EDGE_FINDER.value, Decision.APPROVE, "edge ok",
                      {"side": side, "price_cents": price, "contract_count": count})
    return p


def fully_approved(**kw) -> TradeProposal:
    p = proposal(**kw)
    p.add_verdict(Agent.SKEPTIC.value, Decision.APPROVE, "fine")
    p.add_verdict(Agent.RISK.value, Decision.APPROVE, "within limits")
    p.status = Status.APPROVED
    return p


def settled(j: Journal, pnl: float, series: str = "KXNFLGAME", when: datetime = NOW,
            ticker: str | None = None) -> TradeProposal:
    """Write a settled proposal with the given realized P&L into the journal."""
    p = proposal(ticker=ticker or f"T-{pnl}-{when.timestamp()}", series=series)
    p.status = Status.SETTLED
    p.filled_count = p.contract_count
    p.avg_fill_price_cents = p.price_cents
    p.realized_pnl_usd = pnl
    p.settled_at = when
    j.save(p)
    return p


def open_position(j: Journal, ticker: str, event: str = "EV-OTHER", count: int = 10,
                  price: int = 40, status: Status = Status.PAPER_FILLED) -> TradeProposal:
    p = proposal(ticker=ticker, event=event, count=count, price=price)
    p.status = status
    p.filled_count = count if status in (Status.PAPER_FILLED, Status.OPEN) else 0
    p.avg_fill_price_cents = price
    j.save(p)
    return p


def kill(s: Settings, j: Journal) -> KillSwitch:
    return KillSwitch(s, j)


def minutes(n: float) -> timedelta:
    return timedelta(minutes=n)
