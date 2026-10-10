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

Exits (check_exits, run every second by the mark feed):
  take-profit: net P&L (sell at the bid, minus exit fee, minus cost) reaches
               +risk_take_profit_pct of cost. Real profit, after every cost.
  stop-loss:   P&L has fallen risk_stop_loss_pct of cost below where it stood
               right after entry. A fresh position already shows a loss equal
               to the spread plus fees; measuring from zero would stop out
               cheap contracts the moment they fill.
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
        stake = min(self.s.risk_stake_ceiling_usd, max(self.s.risk_stake_floor_usd, raw))
        return stake * self.cooldown_factor()[0]

    def cooldown_factor(self) -> tuple[float, str]:
        """Learns from recent results: after a losing run, bet smaller until it recovers.
        Can only shrink the stake, never grow it."""
        n = self.s.risk_cooldown_lookback
        if n <= 0:
            return 1.0, ""
        recent = self.journal.recent_results(n)
        if len(recent) < n:
            return 1.0, ""
        net = sum(recent)
        if net < 0:
            wins = sum(1 for r in recent if r > 0)
            return self.s.risk_cooldown_factor, (f"last {n} trades went {wins}-{n - wins} (${net:+.2f}), "
                                                 f"stake cut to {self.s.risk_cooldown_factor:.0%}")
        return 1.0, ""

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
        if s.risk_one_entry_per_market and p.market_ticker in self.journal.traded_tickers():
            # Re-buying after a take-profit/stop-loss pays the spread and two fees again.
            return AgentDecision.reject(f"already traded {p.market_ticker} (one entry per market)")
        if len(open_pos) >= s.risk_max_open_positions:
            return AgentDecision.reject(
                f"{len(open_pos)} open positions, max {s.risk_max_open_positions}", open_positions=len(open_pos))

        # Worst-case cost per contract: Execution's limit (ask + slippage cap) plus fee.
        limit_c = min(99, p.price_cents + s.exec_slippage_cap_cents)
        per_contract = (limit_c + max(p.fee_cents_per_contract, 0.0)) / 100.0

        realized = self.journal.realized_pnl_total()
        stake = self.stake_usd()
        by_stake = math.floor(stake / per_contract + 1e-9)
        factor, _ = self.cooldown_factor()
        if by_stake == 0 and factor < 1 and stake / factor >= per_contract:
            by_stake = 1          # cool-down shrinks the bet, but never to nothing: keep learning at 1 contract

        event_cost = sum(self._position_cost(o) for o in open_pos if o.event_ticker == p.event_ticker)
        room = s.risk_max_event_exposure_usd - event_cost
        by_event = math.floor(max(room, 0.0) / per_contract + 1e-9)

        limits = {"proposed": p.contract_count, "stake": by_stake, "event_exposure": by_event}
        binding = min(limits, key=lambda k: limits[k])
        new_count = int(min(limits.values()))
        snap = {"realized_pnl": round(realized, 2), "stake_usd": round(stake, 2),
                "per_contract_usd": round(per_contract, 4), "event_cost_usd": round(event_cost, 2),
                "open_positions": len(open_pos), "limits": limits, "binding": binding}

        _, cool = self.cooldown_factor()
        snap["cooldown"] = cool
        today = self.pnl_today()
        snap["pnl_today"] = round(today, 2)
        why_binding = {
            "proposed": "Edge Finder's depth cap",
            "stake": (f"stake ${stake:.2f} / ${per_contract:.2f} per contract worst case"
                      + (f"; {cool}" if cool else "")),
            "event_exposure": f"${room:.2f} room left of ${s.risk_max_event_exposure_usd:.0f} in this event",
        }[binding]
        context = (f"{len(open_pos)}/{s.risk_max_open_positions} positions open, today ${today:+.2f} "
                   f"(stop at -${s.risk_daily_loss_limit_usd:.0f}), realized ${realized:+.2f}")
        if new_count < 1:
            return AgentDecision.reject(f"size rounds to 0: {why_binding}. {context}", **snap)
        cost = new_count * per_contract
        if new_count >= p.contract_count:
            return AgentDecision(Decision.APPROVE,
                                 f"{p.contract_count} contracts (${cost:.2f} max) fit every limit. {context}", snap)
        return AgentDecision(Decision.MODIFY,
                             f"sized {p.contract_count} -> {new_count} contracts (${cost:.2f} max), limited by "
                             f"{why_binding}. {context}",
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
            move = r.change_since_entry_pct
            if r.pnl_pct >= self.s.risk_take_profit_pct:
                kind, shown, limit = "TAKE_PROFIT", r.pnl_pct, self.s.risk_take_profit_pct
                desc = "net P&L"
            elif move is not None and move <= -self.s.risk_stop_loss_pct:
                kind, shown, limit = "STOP_LOSS", move, -self.s.risk_stop_loss_pct
                desc = "since entry"
            else:
                continue
            out.append(ExitOrder(r.proposal_id, r.ticker, r.side, r.contracts, bid, kind,
                                 round(r.pnl_usd, 4), r.pnl_pct,
                                 f"{kind.replace('_', ' ').lower()}: {shown * 100:+.1f}% {desc} "
                                 f"(limit {limit * 100:+.0f}%), sell {r.contracts} @ {bid}c"))
        return out
