"""
Monitor: tracks open positions, settles them, books realized P&L.

Feedback loop: realized P&L goes into the journal, which is where Risk reads
it for sizing (2% of realized profit, daily loss limit) and where Skeptic
reads per-market-type results for its losing-record check. Monitor also
returns proposals that need Execution's reconciliation (UNCERTAIN, or
APPROVED but never executed) so the orchestrator can route them.

Realized P&L for a filled proposal at settlement:
    payout = filled * $1 if the market result equals our side, else 0
    pnl    = payout - filled * avg_fill_price - fees_paid
No settlement fee (fee schedule effective Jul 7, 2026).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from kalshi.client import KalshiDataClient
from kalshi.journal import Journal
from kalshi.proposal import Status, TradeProposal

log = logging.getLogger("agent.monitor")


def realized_pnl(p: TradeProposal, result: str) -> float:
    payout = p.filled_count * (1.0 if result == p.side else 0.0)
    cost = p.filled_count * p.avg_fill_price_cents / 100.0
    return round(payout - cost - p.fees_paid_usd, 4)


@dataclass
class MonitorReport:
    open_positions: int = 0
    settled_now: list[str] = field(default_factory=list)
    settled_notes: list[str] = field(default_factory=list)
    needs_reconcile: list[TradeProposal] = field(default_factory=list)
    realized_total: float = 0.0
    mismatches: list[str] = field(default_factory=list)


class Monitor:
    name = "monitor"

    def __init__(self, settings, journal: Journal, data: KalshiDataClient, now=None):
        self.s = settings
        self.journal = journal
        self.data = data
        self._now = now or (lambda: datetime.now(timezone.utc))

    def run_once(self) -> MonitorReport:
        rep = MonitorReport()
        now = self._now()
        stale_after = self.s.timeout_execution_s * 2

        for p in self.journal.by_status([Status.APPROVED, Status.UNCERTAIN]):
            age = (now - p.created_at).total_seconds()
            if p.status == Status.UNCERTAIN or age > stale_after:
                rep.needs_reconcile.append(p)

        holding = self.journal.by_status([Status.PAPER_FILLED, Status.OPEN])
        rep.open_positions = len(holding)
        for p in holding:
            try:
                m = self.data.get_market(p.market_ticker)
            except Exception as e:
                log.warning("get_market %s failed: %s", p.market_ticker, e)
                continue
            if not m.is_settled:
                continue
            p.result = m.result
            # Add to anything already realized by a partial take-profit/stop-loss exit.
            p.realized_pnl_usd = round((p.realized_pnl_usd or 0.0) + realized_pnl(p, m.result), 4)
            p.settled_at = now
            p.status = Status.SETTLED
            self.journal.save(p)
            rep.settled_now.append(p.proposal_id)
            won = m.result == p.side
            rep.settled_notes.append(f"{p.market_ticker} {p.side.upper()} x{p.filled_count} @ "
                                     f"{p.avg_fill_price_cents:.0f}c {'WON' if won else 'LOST'} "
                                     f"(${p.realized_pnl_usd:+.2f})")
            log.info("SETTLED %s %s %s x%d -> %s, P&L $%.2f", p.market_ticker, p.side.upper(),
                     p.proposal_id[:8], p.filled_count, m.result, p.realized_pnl_usd)

        # Live only: compare what we think we hold with what Kalshi says.
        if not self.s.paper_mode and self.data.http.authenticated:
            try:
                live = self.data.get_positions()
                for p in self.journal.by_status([Status.OPEN]):
                    held = live.get(p.market_ticker, 0.0)
                    expected = p.filled_count if p.side == "yes" else -p.filled_count
                    if abs(held - expected) > 1e-6:
                        msg = f"{p.market_ticker}: journal {expected:+g}, Kalshi {held:+g}"
                        rep.mismatches.append(msg)
                        self.journal.log_event("position_mismatch", msg, "WARNING")
            except Exception as e:
                log.warning("position reconciliation failed: %s", e)

        rep.realized_total = self.journal.realized_pnl_total()
        return rep
