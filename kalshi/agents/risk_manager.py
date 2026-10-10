"""
Risk Manager: deterministic Python only. No LLM calls, no model outputs.
(It reads the proposal's price and fee, which are market data, and journal
P&L, which is accounting. It never reads fair_probability or edge.)

Order of checks, first failure rejects:
  1. Kill switch (env var or dashboard flag)
  2. Daily loss limit: realized P&L since local midnight <= -limit halts new
     proposals for the rest of the day
  3. Already holding this market
  4. Max concurrent open positions (approved, filled, or uncertain all count)
  5. Sizing: stake = clamp(2% of realized profit, floor, ceiling)
  6. Per-event exposure: open cost in the same event + this trade <= cap

Outcome: REJECT, APPROVE (size unchanged), or MODIFY contract_count down.

Exits (check_exits, run every second by the mark feed): close a position when
its net P&L (sell at the bid, minus exit fee, minus cost) reaches
+risk_take_profit_pct (take-profit) or -risk_stop_loss_pct (stop-loss).
It cannot raise size or touch side or price; TradeProposal.add_verdict would
refuse that anyway.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from kalshi.agents.base import AgentDecision
from kalshi.journal import Journal
from kalshi.killswitch import KillSwitch
from kalshi.pnl import PositionPnL
from kalshi.proposal import Agent, Decision, TradeProposal

log = logging.getLogger("agent.risk")


@dataclass(frozen=True)
class ExitOrder:
    """Created only by RiskManager.check_exits; Execution.close_position
    refuses anything else."""
    proposal_id: str
    ticker: str
    side: str
    contracts: int
    bid_cents: int
    kind: str                 # "TAKE_PROFIT" | "STOP_LOSS"
    pnl_usd: float
    pnl_pct: float
    reason: str


class RiskManager:
    name = Agent.RISK.value

    def __init__(self, settings, journal: Journal, killswitch: KillSwitch, now=None):
        self.s = settings
        self.journal = journal
        self.kill = killswitch
        self._now = now or (lambda: datetime.now(timezone.utc))

    # ── Shared helpers (orchestrator uses halt_reason before scanning) ───────

    def start_of_day(self) -> datetime:
        tz = ZoneInfo(self.s.timezone)
        local = self._now().astimezone(tz)
        return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)

    def pnl_today(self) -> float:
        return self.journal.realized_pnl_since(self.start_of_day())

    def halt_reason(self) -> str:
        on, why = self.kill.status()
        if on:
            return why
        today = self.pnl_today()
        if today <= -abs(self.s.risk_daily_loss_limit_usd):
            return f"daily loss limit hit (${today:.2f} <= -${self.s.risk_daily_loss_limit_usd:.2f})"
        return ""

    def stake_usd(self) -> float:
        realized = self.journal.realized_pnl_total()
        raw = self.s.risk_stake_pct_of_realized * max(realized, 0.0)
        return min(self.s.risk_stake_ceiling_usd, max(self.s.risk_stake_floor_usd, raw))

    @staticmethod
    def _position_cost(p: TradeProposal) -> float:
        if p.filled_count > 0:
            return p.filled_count * (p.avg_fill_price_cents or p.price_cents) / 100.0
        return p.contract_count * p.price_cents / 100.0

    # ── Review ───────────────────────────────────────────────────────────────

    def review(self, p: TradeProposal) -> AgentDecision:
        s = self.s
        halt = self.halt_reason()
        if halt:
            return AgentDecision.reject(halt)

        open_pos = [o for o in self.journal.open_positions() if o.proposal_id != p.proposal_id]
        if any(o.market_ticker == p.market_ticker for o in open_pos):
            return AgentDecision.reject(f"already holding {p.market_ticker}")
        if len(open_pos) >= s.risk_max_open_positions:
            return AgentDecision.reject(
                f"{len(open_pos)} open positions, max {s.risk_max_open_positions}", open_positions=len(open_pos))

        # Worst-case cost per contract: Execution's limit (ask + slippage cap) plus fee.
        limit_c = min(99, p.price_cents + s.exec_slippage_cap_cents)
        per_contract = (limit_c + max(p.fee_cents_per_contract, 0.0)) / 100.0

        realized = self.journal.realized_pnl_total()
        stake = self.stake_usd()
        by_stake = math.floor(stake / per_contract + 1e-9)

        event_cost = sum(self._position_cost(o) for o in open_pos if o.event_ticker == p.event_ticker)
        room = s.risk_max_event_exposure_usd - event_cost
        by_event = math.floor(max(room, 0.0) / per_contract + 1e-9)

        limits = {"proposed": p.contract_count, "stake": by_stake, "event_exposure": by_event}
        binding = min(limits, key=lambda k: limits[k])
        new_count = int(min(limits.values()))
        snap = {"realized_pnl": round(realized, 2), "stake_usd": round(stake, 2),
                "per_contract_usd": round(per_contract, 4), "event_cost_usd": round(event_cost, 2),
                "open_positions": len(open_pos), "limits": limits, "binding": binding}

        if new_count < 1:
            return AgentDecision.reject(f"size rounds to 0 (binding: {binding})", **snap)
        if new_count >= p.contract_count:
            return AgentDecision(Decision.APPROVE, f"{p.contract_count} contracts within limits", snap)
        return AgentDecision(Decision.MODIFY,
                             f"sized {p.contract_count} -> {new_count} (binding: {binding})",
                             snap, {"contract_count": new_count})

    # ── Exits ────────────────────────────────────────────────────────────────

    def check_exits(self, positions: list[PositionPnL]) -> list[ExitOrder]:
        if not self.s.risk_exits_enabled:
            return []
        out = []
        for r in positions:
            if r.pnl_pct is None or r.bid_cents is None or r.contracts <= 0:
                continue
            bid = int(round(r.bid_cents))
            if not (1 <= bid <= 99):
                continue
            if r.pnl_pct >= self.s.risk_take_profit_pct:
                kind = "TAKE_PROFIT"
            elif r.pnl_pct <= -self.s.risk_stop_loss_pct:
                kind = "STOP_LOSS"
            else:
                continue
            limit = self.s.risk_take_profit_pct if kind == "TAKE_PROFIT" else -self.s.risk_stop_loss_pct
            out.append(ExitOrder(r.proposal_id, r.ticker, r.side, r.contracts, bid, kind,
                                 round(r.pnl_usd, 4), r.pnl_pct,
                                 f"{kind.replace('_', ' ').lower()}: {r.pnl_pct * 100:+.1f}% "
                                 f"(limit {limit * 100:+.0f}%), sell {r.contracts} @ {bid}c"))
        return out
