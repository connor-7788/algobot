"""
Orchestrator: runs Scout on a loop and routes each candidate through
    Edge Finder -> Skeptic -> Risk Manager -> Execution
writing every proposal (rejected ones too) to the journal after each step.

Every agent call runs with a timeout. A timeout counts as REJECT, with one
exception that matters for money: if Execution times out, an order may already
be on the book, so the proposal is marked UNCERTAIN (it keeps its position
slot) and Execution reconciles it by client_order_id on a later cycle.

Agents get a deep copy of the proposal, so a late-finishing agent can't change
anything after its timeout fired. The orchestrator applies their decisions via
TradeProposal.add_verdict, which enforces MODIFY scope; a scope violation is
recorded as a REJECT.

It also keeps the StatusBoard current: before each agent call it marks that
agent "working" on the item, and afterward posts what the agent said.
"""

from __future__ import annotations

import copy
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from kalshi.agents.base import AgentDecision
from kalshi.proposal import Agent, Decision, ProposalClosed, ScopeViolation, Status, TradeProposal
from kalshi.status import StatusBoard

log = logging.getLogger("orchestrator")

ORCH = Agent.ORCHESTRATOR.value
SCOUT = Agent.SCOUT.value
EDGE = Agent.EDGE_FINDER.value
SKEPTIC = Agent.SKEPTIC.value
RISK = Agent.RISK.value
EXEC = Agent.EXECUTION.value
MONITOR = Agent.MONITOR.value


class AgentTimeout(Exception):
    pass


@dataclass
class CycleStats:
    cycle: int = 0
    skipped: str = ""
    candidates: int = 0
    proposals: int = 0
    rejected_by: dict = field(default_factory=dict)
    executed: int = 0
    errors: int = 0

    def bump(self, agent: str) -> None:
        self.rejected_by[agent] = self.rejected_by.get(agent, 0) + 1


class Orchestrator:
    def __init__(self, settings, *, scout, edge_finder, skeptic, risk, execution, monitor, journal):
        self.s = settings
        self.scout = scout
        self.edge = edge_finder
        self.skeptic = skeptic
        self.risk = risk
        self.execution = execution
        self.monitor = monitor
        self.journal = journal
        self.board = StatusBoard(journal)
        self.pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="agent")
        self._last_monitor = 0.0
        self._cycle_no = 0
        self.last_cycle: CycleStats | None = None

    # ── Plumbing ─────────────────────────────────────────────────────────────

    def _call(self, fn, timeout: float, *args):
        fut = self.pool.submit(fn, *args)
        try:
            return fut.result(timeout=timeout)
        except FutureTimeout:
            raise AgentTimeout(f"timed out after {timeout:.0f}s")

    def _apply(self, p: TradeProposal, agent: str, d) -> None:
        """Apply an AgentDecision to the proposal; bad output becomes a REJECT."""
        if not isinstance(d, AgentDecision):
            p.add_verdict(agent, Decision.REJECT, f"invalid agent output: {type(d).__name__}")
            return
        try:
            p.add_verdict(agent, d.decision, d.reason, d.data_snapshot, d.changes or None)
        except (ScopeViolation, ValueError) as e:
            p.add_verdict(agent, Decision.REJECT, f"scope violation: {e}")

    def _review(self, p: TradeProposal, agent: str, fn, timeout: float) -> bool:
        """Run Skeptic or Risk. Returns True if the proposal survives."""
        self.board.working(agent, f"reviewing {p.side.upper()} {p.market_ticker} @ {p.price_cents}c",
                           p.market_ticker)
        try:
            d = self._call(fn, timeout, copy.deepcopy(p))
        except AgentTimeout as e:
            p.add_verdict(agent, Decision.REJECT, str(e))
        except Exception as e:
            log.exception("%s crashed on %s", agent, p.proposal_id)
            p.add_verdict(agent, Decision.REJECT, f"agent error: {e}")
        else:
            self._apply(p, agent, d)
        self.journal.save(p)
        v = p.verdicts[-1]
        self.board.said(agent, v.decision.value, f"{p.market_ticker} {p.side.upper()}: {v.reason}",
                        ticker=p.market_ticker, detail=v.data_snapshot)
        return not p.is_rejected

    # ── One candidate ────────────────────────────────────────────────────────

    def process_candidate(self, cand, stats: CycleStats) -> TradeProposal | None:
        ticker = cand.market.ticker
        self.board.working(EDGE, f"pricing {ticker}", ticker)
        try:
            res = self._call(self.edge.evaluate, self.s.timeout_edge_s, cand)
        except AgentTimeout as e:
            log.info("edge_finder %s on %s", e, ticker)
            stats.bump(EDGE)
            self.board.said(EDGE, "REJECT", f"{ticker}: {e}", ticker=ticker)
            return None
        except Exception as e:
            log.exception("edge_finder crashed on %s", ticker)
            stats.errors += 1
            self.board.said(EDGE, "ERROR", f"{ticker}: {e}", ticker=ticker)
            return None
        if res.proposal is None:
            self.board.said(EDGE, "PASS", f"{ticker}: {res.reason}", ticker=ticker)
            if res.diagnostic is not None:
                self.journal.save(res.diagnostic)
                stats.bump(EDGE)
            return None

        p = res.proposal
        stats.proposals += 1
        self.journal.save(p)
        self.board.said(EDGE, "APPROVE",
                        f"{ticker}: proposing {p.side.upper()} @ {p.price_cents}c, fair "
                        f"{p.fair_probability*100:.1f}%, edge {p.edge_cents:.2f}c, confidence {p.confidence_score}",
                        ticker=ticker, detail=p.verdicts[-1].data_snapshot)

        for agent, fn, timeout in (
            (SKEPTIC, self.skeptic.review, self.s.timeout_skeptic_s),
            (RISK, self.risk.review, self.s.timeout_risk_s),
        ):
            if not self._review(p, agent, fn, timeout):
                stats.bump(agent)
                log.info("REJECTED %s %s by %s: %s", p.market_ticker, p.side, agent, p.verdicts[-1].reason)
                return p

        p.status = Status.APPROVED
        self.journal.save(p)

        self.board.working(EXEC, f"{'paper ' if self.s.paper_mode else ''}executing {p.contract_count}x "
                                 f"{p.side.upper()} {ticker} @ {p.price_cents}c", ticker)
        try:
            p = self._call(self.execution.execute, self.s.timeout_execution_s, copy.deepcopy(p))
        except AgentTimeout as e:
            # The order may be live. Hold the slot and let reconciliation sort it out.
            p.status = Status.UNCERTAIN
            self.journal.save(p)
            self.journal.log_event("execution_timeout", f"{p.proposal_id} {ticker}: {e}", "ERROR")
            stats.bump(EXEC)
            self.board.said(EXEC, "ERROR", f"{ticker}: {e}; order state UNCERTAIN, will reconcile",
                            ticker=ticker)
            return p
        except Exception as e:
            log.exception("execution crashed on %s", p.proposal_id)
            p.status = Status.UNCERTAIN
            self.journal.save(p)
            self.journal.log_event("execution_error", f"{p.proposal_id}: {e}", "ERROR")
            stats.errors += 1
            self.board.said(EXEC, "ERROR", f"{ticker}: {e}; order state UNCERTAIN", ticker=ticker)
            return p

        v = p.verdicts[-1]
        if p.is_rejected:
            stats.bump(EXEC)
        else:
            stats.executed += 1
        self.board.said(EXEC, v.decision.value, f"{ticker}: {v.reason} [{p.status.value}]",
                        ticker=ticker, detail=v.data_snapshot)
        self.journal.save(p)
        return p

    # ── Cycle ────────────────────────────────────────────────────────────────

    def run_monitor(self) -> None:
        self.board.working(MONITOR, "checking open positions for settlement")
        try:
            rep = self._call(self.monitor.run_once, self.s.timeout_monitor_s)
        except Exception as e:
            log.warning("monitor failed: %s", e)
            self.board.said(MONITOR, "ERROR", f"monitor failed: {e}")
            return
        msg = (f"{rep.open_positions} open, {len(rep.settled_now)} settled this pass, "
               f"realized P&L ${rep.realized_total:.2f}")
        if rep.mismatches:
            msg += f", {len(rep.mismatches)} position mismatch(es)"
        self.board.said(MONITOR, "INFO", msg, detail={"settled": rep.settled_now, "mismatches": rep.mismatches,
                                                      "reconcile": [p.proposal_id for p in rep.needs_reconcile]})
        for p in rep.needs_reconcile:
            self.board.working(EXEC, f"reconciling {p.market_ticker} ({p.status.value})", p.market_ticker)
            try:
                q = self._call(self.execution.reconcile, self.s.timeout_execution_s, p)
                self.board.said(EXEC, "INFO", f"reconciled {p.market_ticker}: {q.status.value}",
                                ticker=p.market_ticker)
            except Exception as e:
                log.error("reconcile %s failed: %s", p.proposal_id, e)
                self.board.said(EXEC, "ERROR", f"reconcile {p.market_ticker} failed: {e}", ticker=p.market_ticker)
        self._last_monitor = time.monotonic()

    def _post_risk_snapshot(self) -> None:
        try:
            open_n = len(self.journal.open_positions())
            today = self.risk.pnl_today()
            stake = self.risk.stake_usd()
            self.board.state(RISK, "idle", "waiting for next proposal", detail={
                "open_positions": f"{open_n}/{self.s.risk_max_open_positions}",
                "pnl_today_usd": round(today, 2),
                "daily_loss_limit_usd": self.s.risk_daily_loss_limit_usd,
                "stake_usd": round(stake, 2),
                "realized_total_usd": round(self.journal.realized_pnl_total(), 2),
            })
        except Exception as e:
            log.debug("risk snapshot failed: %s", e)

    def run_cycle(self) -> CycleStats:
        self._cycle_no += 1
        stats = CycleStats(cycle=self._cycle_no)
        self.board.working(ORCH, f"cycle {self._cycle_no} running")
        if time.monotonic() - self._last_monitor >= self.s.monitor_interval_s:
            self.run_monitor()
        self._post_risk_snapshot()

        halt = self.risk.halt_reason()
        if halt:
            stats.skipped = halt
            log.info("cycle skipped: %s", halt)
            self.board.state(ORCH, "halted", f"not scanning: {halt}", message=f"halted: {halt}", decision="INFO")
            self.board.state(RISK, "halted", halt, message=halt, decision="REJECT")
            self.last_cycle = stats
            return stats

        self.board.working(SCOUT, f"scanning {len(self.s.sport_series)} series")
        try:
            candidates = self._call(self.scout.scan, self.s.timeout_scout_s)
        except Exception as e:
            log.warning("scout failed: %s", e)
            stats.errors += 1
            self.board.said(SCOUT, "ERROR", f"scan failed: {e}")
            self.last_cycle = stats
            return stats
        stats.candidates = len(candidates)
        sstats = getattr(self.scout, "last_stats", {}) or {}
        self.board.said(SCOUT, "INFO", f"{len(candidates)} candidates from {sstats.get('markets', '?')} open markets",
                        detail=sstats)

        for cand in candidates:
            # Re-check between candidates so a kill switch or loss limit stops
            # the rest of this cycle, not just the next one.
            halt = self.risk.halt_reason()
            if halt:
                stats.skipped = halt
                break
            try:
                self.process_candidate(cand, stats)
            except ProposalClosed:
                pass
            except Exception:
                log.exception("unhandled error on %s", getattr(cand.market, "ticker", "?"))
                stats.errors += 1
        log.info("cycle: %s", stats)
        self.last_cycle = stats
        nxt = datetime.now(timezone.utc) + timedelta(seconds=self.s.scan_interval_s)
        self.board.state(ORCH, "idle", f"sleeping until {nxt:%H:%M:%S} UTC",
                         message=(f"cycle {stats.cycle}: {stats.candidates} candidates, {stats.proposals} proposals, "
                                  f"{stats.executed} executed, rejects {stats.rejected_by or '{}'}"),
                         decision="INFO", detail=asdict(stats))
        self._post_risk_snapshot()
        return stats

    def run_forever(self, stop: threading.Event) -> None:
        log.info("orchestrator started (paper_mode=%s, env=%s)", self.s.paper_mode, self.s.kalshi_env)
        self.board.state(ORCH, "idle", "starting", message=f"engine started, paper_mode={self.s.paper_mode}",
                         decision="INFO")
        while not stop.is_set():
            started = time.monotonic()
            try:
                self.run_cycle()
            except Exception as e:
                log.exception("cycle crashed")
                self.board.state(ORCH, "error", "cycle crashed", message=str(e), decision="ERROR")
            stop.wait(max(1.0, self.s.scan_interval_s - (time.monotonic() - started)))
        self.board.state(ORCH, "idle", "stopped", message="engine stopped", decision="INFO")
        self.pool.shutdown(wait=False, cancel_futures=True)
        log.info("orchestrator stopped")
