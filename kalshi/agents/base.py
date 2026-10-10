"""Shared types for agents."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from kalshi.client import Market
from kalshi.orderbook import Book
from kalshi.proposal import Decision


@dataclass
class AgentDecision:
    """What Skeptic and Risk return. The orchestrator applies it to the
    proposal through TradeProposal.add_verdict, which enforces MODIFY scope.
    Agents never mutate the proposal they're shown."""
    decision: Decision
    reason: str
    data_snapshot: dict = field(default_factory=dict)
    changes: dict = field(default_factory=dict)

    @classmethod
    def approve(cls, reason: str, **snap) -> "AgentDecision":
        return cls(Decision.APPROVE, reason, snap)

    @classmethod
    def reject(cls, reason: str, **snap) -> "AgentDecision":
        return cls(Decision.REJECT, reason, snap)


@dataclass
class Candidate:
    market: Market
    book: Book
    sport_key: str
    opponent: str = ""            # yes_sub_title of the other market in the same event
    asset: str = ""               # crypto 15m markets: "BTC", "ETH", ...
    scanned_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
