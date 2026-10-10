"""
TradeProposal: the single object that moves through the agent pipeline.

Rules enforced here, not by convention:
  * Agents record decisions only through add_verdict().
  * A REJECT closes the proposal. Nothing can be appended after it except by
    the orchestrator's bookkeeping (status changes).
  * MODIFY may only touch fields in that agent's scope (MODIFY_SCOPE), and
    contract_count may only go down. Anything else raises ScopeViolation,
    which the orchestrator treats as a REJECT.
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Decision(str, Enum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    MODIFY = "MODIFY"


class Status(str, Enum):
    PROPOSED = "PROPOSED"                    # Edge Finder created it
    REJECTED = "REJECTED"                    # some agent said no
    APPROVED = "APPROVED"                    # passed Risk, waiting on Execution
    PAPER_FILLED = "PAPER_FILLED"            # paper mode simulated fill (open)
    OPEN = "OPEN"                            # live fill, position open
    CANCELLED_UNFILLED = "CANCELLED_UNFILLED"
    UNCERTAIN = "UNCERTAIN"                  # order state unknown, Monitor must reconcile
    SETTLED = "SETTLED"


# Statuses that occupy a position slot for Risk.
OPEN_STATUSES = {Status.APPROVED, Status.PAPER_FILLED, Status.OPEN, Status.UNCERTAIN}


class Agent(str, Enum):
    SCOUT = "scout"
    EDGE_FINDER = "edge_finder"
    SKEPTIC = "skeptic"
    RISK = "risk_manager"
    EXECUTION = "execution"
    MONITOR = "monitor"
    ORCHESTRATOR = "orchestrator"


# Trade fields each agent may change with MODIFY. Skeptic gets nothing.
MODIFY_SCOPE: dict[str, set[str]] = {
    Agent.SKEPTIC.value: set(),
    Agent.RISK.value: {"contract_count"},
    Agent.EXECUTION.value: set(),
}

# Fields that may only decrease under MODIFY.
DECREASE_ONLY = {"contract_count"}


class ScopeViolation(Exception):
    pass


class ProposalClosed(Exception):
    pass


@dataclass
class Verdict:
    agent: str
    decision: Decision
    reason: str
    timestamp: datetime = field(default_factory=utcnow)
    data_snapshot: dict = field(default_factory=dict)
    changes: dict = field(default_factory=dict)   # field -> [old, new] for MODIFY

    def to_dict(self) -> dict:
        return {
            "agent": self.agent,
            "decision": self.decision.value,
            "reason": self.reason,
            "timestamp": self.timestamp.isoformat(),
            "data_snapshot": self.data_snapshot,
            "changes": self.changes,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Verdict":
        return cls(
            agent=d["agent"],
            decision=Decision(d["decision"]),
            reason=d.get("reason", ""),
            timestamp=datetime.fromisoformat(d["timestamp"]),
            data_snapshot=d.get("data_snapshot", {}) or {},
            changes=d.get("changes", {}) or {},
        )


@dataclass
class Source:
    """One input behind fair_probability."""
    name: str                 # e.g. "pinnacle"
    probability: float        # devigged probability for the proposal's YES outcome
    fetched_at: datetime      # when we pulled it
    observed_at: datetime     # when the provider says the line was last updated
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "probability": self.probability,
            "fetched_at": self.fetched_at.isoformat(),
            "observed_at": self.observed_at.isoformat(),
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Source":
        return cls(
            name=d["name"],
            probability=float(d["probability"]),
            fetched_at=datetime.fromisoformat(d["fetched_at"]),
            observed_at=datetime.fromisoformat(d["observed_at"]),
            detail=d.get("detail", {}) or {},
        )


@dataclass
class TradeProposal:
    market_ticker: str
    event_ticker: str
    side: str                              # "yes" | "no"
    price_cents: int                       # observed ask for `side`, whole cents
    contract_count: int                    # upper bound set by Edge Finder; Risk sizes down
    fair_probability: float                # probability that `side` wins
    sources: list[Source] = field(default_factory=list)
    verdicts: list[Verdict] = field(default_factory=list)
    status: Status = Status.PROPOSED
    proposal_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    # Context carried for later agents and the journal.
    series_ticker: str = ""
    title: str = ""
    edge_cents: float = 0.0
    fee_cents_per_contract: float = 0.0
    confidence_score: int = 0
    created_at: datetime = field(default_factory=utcnow)
    book_fetched_at: datetime | None = None
    close_time: datetime | None = None

    # Execution / Monitor results.
    original_count: int = 0
    order_id: str = ""
    filled_count: int = 0
    avg_fill_price_cents: float = 0.0
    fees_paid_usd: float = 0.0
    result: str = ""                        # "yes" | "no" once the market settles
    realized_pnl_usd: float | None = None
    settled_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.side not in ("yes", "no"):
            raise ValueError(f"side must be yes/no, got {self.side!r}")
        if not (1 <= int(self.price_cents) <= 99):
            raise ValueError(f"price_cents out of range: {self.price_cents}")
        if self.contract_count < 0:
            raise ValueError("contract_count cannot be negative")
        if not self.original_count:
            self.original_count = int(self.contract_count)

    # ── Verdict handling ─────────────────────────────────────────────────────

    @property
    def is_rejected(self) -> bool:
        return any(v.decision == Decision.REJECT for v in self.verdicts)

    @property
    def rejected_by(self) -> str:
        for v in self.verdicts:
            if v.decision == Decision.REJECT:
                return v.agent
        return ""

    def add_verdict(self, agent: str, decision: Decision, reason: str,
                    data_snapshot: dict | None = None,
                    changes: dict[str, Any] | None = None) -> Verdict:
        """Append a verdict. For MODIFY, `changes` maps field -> new value and is
        validated against the agent's scope before anything is applied."""
        if self.is_rejected:
            raise ProposalClosed(f"proposal {self.proposal_id} already rejected by {self.rejected_by}")
        decision = Decision(decision)
        applied: dict[str, list] = {}
        if decision == Decision.MODIFY:
            if not changes:
                raise ScopeViolation(f"{agent} sent MODIFY with no changes")
            allowed = MODIFY_SCOPE.get(agent, set())
            for name, new in changes.items():
                if name not in allowed:
                    raise ScopeViolation(f"{agent} may not modify {name!r}")
                old = getattr(self, name)
                if name in DECREASE_ONLY and new > old:
                    raise ScopeViolation(f"{agent} tried to raise {name} from {old} to {new}")
                if name == "contract_count" and new < 0:
                    raise ScopeViolation("contract_count cannot go negative")
            for name, new in changes.items():
                applied[name] = [getattr(self, name), new]
                setattr(self, name, new)
        elif changes:
            raise ScopeViolation(f"{agent} passed changes with decision {decision.value}")

        v = Verdict(agent=agent, decision=decision, reason=reason,
                    data_snapshot=copy.deepcopy(data_snapshot or {}), changes=applied)
        self.verdicts.append(v)
        if decision == Decision.REJECT:
            self.status = Status.REJECTED
        return v

    def verdict_for(self, agent: str) -> Verdict | None:
        for v in self.verdicts:
            if v.agent == agent:
                return v
        return None

    # ── Derived values ───────────────────────────────────────────────────────

    @property
    def cost_usd(self) -> float:
        """Capital at risk if fully filled at price_cents (excludes fees)."""
        return self.contract_count * self.price_cents / 100.0

    @property
    def filled_cost_usd(self) -> float:
        return self.filled_count * self.avg_fill_price_cents / 100.0

    # ── Serialization (journal) ──────────────────────────────────────────────

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        d["verdicts"] = [v.to_dict() for v in self.verdicts]
        d["sources"] = [s.to_dict() for s in self.sources]
        for k in ("created_at", "book_fetched_at", "close_time", "settled_at"):
            val = getattr(self, k)
            d[k] = val.isoformat() if val else None
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "TradeProposal":
        d = dict(d)
        verdicts = [Verdict.from_dict(v) for v in d.pop("verdicts", [])]
        sources = [Source.from_dict(s) for s in d.pop("sources", [])]
        status = Status(d.pop("status", Status.PROPOSED.value))
        for k in ("created_at", "book_fetched_at", "close_time", "settled_at"):
            if d.get(k):
                d[k] = datetime.fromisoformat(d[k])
        p = cls(**d)
        p.verdicts = verdicts
        p.sources = sources
        p.status = status
        return p
