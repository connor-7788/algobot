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
import json
import logging
import threading
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from kalshi.agents.base import AgentDecision
from kalshi.pnl import held_positions, position_pnl
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
LEARNER = "learner"
SHADOW = "shadow"


def pass_category(reason: str) -> str:
    """Bucket an Edge Finder PASS so the dashboard can say why nothing traded."""
    r = reason.lower()
    if r.startswith("learner has paused"):
        return "Learner paused entries (no proven edge)"
    if r.startswith("edge ") and "confidence" in r.split(". ")[0]:
        return "edge too small and low confidence"
    if r.startswith("edge "):
        return "edge too small after spread + fees"
    if r.startswith("confidence"):
        return "confidence below minimum"
    if "outside" in r and "priced" in r:
        return "price outside 30-97c band"
    if "averaging window" in r:
        return "too close to settlement"
    if "fetch failed" in r or "no " in r[:12]:
        return "missing data"
    if "depth" in r:
        return "order book too thin"
    if "books disagree" in r:
        return "price sources disagree"
    return "other"


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
    def __init__(self, settings, *, scout, edge_finder, skeptic, risk, execution, monitor, journal,
                 learner=None, shadow=None):
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
        # Monitor (settlement) and exits (mark feed thread) both close positions.
        self.position_lock = threading.Lock()
        self._cycle_no = 0
        self.last_cycle: CycleStats | None = None
        self.learner = learner
        self.shadow = shadow
        self._last_learn = 0.0
        self._learning = threading.Lock()
        self._backfilled = False
        self._passes: deque = deque()           # (monotonic ts, category) for the last hour

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
            self._note_pass(res.reason)
            self.board.said(EDGE, "PASS", f"{ticker}: {res.reason}", ticker=ticker)
            if res.diagnostic is not None:
                self.journal.save(res.diagnostic)
                stats.bump(EDGE)
            return None

        p = res.proposal
        stats.proposals += 1
        self.journal.save(p)
        self._note_pass("PROPOSED")
        self.board.said(EDGE, "APPROVE", f"{ticker}: {p.verdicts[-1].reason}",
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
            if self.shadow is not None and p.filled_count:
                try:
                    self.shadow.record_bot_trade(p)
                except Exception as e:
                    log.debug("bot trade not recorded for post-mortem: %s", e)
        self.board.said(EXEC, v.decision.value, f"{ticker}: {v.reason} [{p.status.value}]",
                        ticker=ticker, detail=v.data_snapshot)
        self.journal.save(p)
        return p

    # ── Cycle ────────────────────────────────────────────────────────────────

    def run_exits(self) -> int:
        """Called every second by the mark feed: Risk checks take-profit /
        stop-loss on live prices, Execution closes what Risk flags."""
        if not self.s.risk_exits_enabled:
            return 0
        closed = 0
        with self.position_lock:
            marks = self.journal.get_marks()
            # Only markets still trading: a closed market's last bid is stale and can't be sold into.
            rows = [position_pnl(p, marks.get(p.market_ticker), self.s)
                    for p in held_positions(self.journal.open_positions())
                    if (marks.get(p.market_ticker) or {}).get("status") in ("active", "open")]
            for order in self.risk.check_exits(rows):
                self.board.said(RISK, "INFO", f"{order.ticker} {order.side.upper()}: {order.reason}",
                                ticker=order.ticker, detail={"kind": order.kind, "pnl_usd": order.pnl_usd})
                self.board.working(EXEC, f"closing {order.ticker} ({order.kind.lower()})", order.ticker)
                try:
                    p = self.execution.close_position(order)
                except Exception as e:
                    log.exception("exit failed for %s", order.ticker)
                    self.board.said(EXEC, "ERROR", f"exit {order.ticker} failed: {e}", ticker=order.ticker)
                    continue
                if p is not None:
                    closed += 1
                    self.board.said(EXEC, "APPROVE", f"{order.ticker}: {p.verdicts[-1].reason}",
                                    ticker=order.ticker, detail=p.verdicts[-1].data_snapshot)
        return closed

    def run_monitor(self) -> None:
        self.board.working(MONITOR, "checking open positions for settlement")
        try:
            with self.position_lock:
                rep = self._call(self.monitor.run_once, self.s.timeout_monitor_s)
        except Exception as e:
            log.warning("monitor failed: %s", e)
            self.board.said(MONITOR, "ERROR", f"monitor failed: {e}")
            return
        msg = (f"{rep.open_positions} open, {len(rep.settled_now)} settled this pass, "
               f"realized P&L ${rep.realized_total:.2f}")
        notes = getattr(rep, "settled_notes", [])
        if notes:
            msg += ". " + "; ".join(notes)
        if rep.mismatches:
            msg += f", {len(rep.mismatches)} position mismatch(es)"
        self.board.said(MONITOR, "INFO", msg, detail={"settled": rep.settled_now, "mismatches": rep.mismatches,
                                                      "reconcile": [p.proposal_id for p in rep.needs_reconcile]})
        self._settle_shadows()
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

    def _settle_shadows(self) -> None:
        if self.shadow is None:
            return
        try:
            done = self.shadow.settle()
        except Exception as e:
            log.warning("shadow settle failed: %s", e)
            self.board.said(SHADOW, "ERROR", f"settling shadow trades failed: {e}")
            return
        if not done:
            return
        tally: dict[str, list[int]] = {}
        for d in done:
            tally.setdefault(d["strategy"], [0, 0])[0 if d["won"] else 1] += 1
        summary = ", ".join(f"{k} {w}W/{l}L" for k, (w, l) in sorted(tally.items()))
        self.board.said(SHADOW, "INFO", f"settled {len(done)}: {summary}. Latest: {done[-1]['text']}",
                        detail={"settled": len(done), "by_strategy": tally},
                        activity="waiting for the next 15-minute markets to close")

    # ── Why-no-trades tally ──────────────────────────────────────────────────

    def _note_pass(self, reason: str) -> None:
        now = time.monotonic()
        self._passes.append((now, "proposed" if reason == "PROPOSED" else pass_category(reason)))
        while self._passes and now - self._passes[0][0] > 3600:
            self._passes.popleft()

    def pass_summary(self) -> dict:
        counts = Counter(c for _, c in self._passes)
        return {"window_s": 3600, "evaluations": sum(counts.values()), "counts": dict(counts.most_common()),
                "updated_at": datetime.now(timezone.utc).isoformat()}

    # ── Learner ──────────────────────────────────────────────────────────────

    def _learn_once(self) -> None:
        if not self._learning.acquire(blocking=False):
            return
        try:
            if not self._backfilled and self.s.learner_backfill_h > 0:
                self._backfilled = True
                self.board.working(LEARNER, f"rebuilding memory from the last {self.s.learner_backfill_h:.0f}h "
                                            "of settled markets")
                try:
                    from kalshi import backfill
                    rep = backfill.run(self.s, self.journal, self.learner.data, shadow=self.shadow)
                    msg = (f"loaded {rep['observations']:,} labelled snapshots from {rep['markets']} settled "
                           f"markets ({rep['skipped']} already known) and replayed {rep['shadow_trades']:,} "
                           f"shadow trades on them")
                    if rep["errors"]:
                        msg += f"; {len(rep['errors'])} problem(s): {rep['errors'][0]}"
                    self.journal.log_event("learner", msg)
                    self.board.said(LEARNER, "INFO", msg, detail=rep, activity="refitting")
                except Exception as e:
                    log.exception("backfill failed")
                    self.board.said(LEARNER, "ERROR", f"history backfill failed, learning from live data only: {e}")
            self.board.working(LEARNER, "labelling settled markets and refitting")
            labelled = self.learner.label_pending()
            report = self.learner.refit()
            try:
                self.journal.prune_observations(int(time.time() - self.s.learner_window_h * 3600 * 2))
            except Exception:
                pass
            counts = self.journal.observation_counts()
            p = self.learner.store.get()
            trade_lessons = []
            try:
                from kalshi.agents.shadow import lessons
                trade_lessons = lessons(self.journal.shadow_rows(limit=5000))
            except Exception as e:
                log.debug("lessons failed: %s", e)
            explore = (" Paper exploration is ON: the bot keeps taking small paper trades on the chart model "
                       "so it learns from its own results." if (p.paused and self.s.paper_mode and
                                                                  self.s.paper_explore and p.chart) else "")
            if trade_lessons:
                report += " From settled trades: " + "; ".join(trade_lessons[:3]) + "."
            detail = {"version": p.version, "mode": p.mode, "sigma_scale": p.sigma_scale,
                      "model_weight": p.model_weight, "edge_min_cents": "PAUSED" if p.paused else p.edge_min_cents,
                      "chart_lessons": p.lessons, "trade_lessons": trade_lessons,
                      "observations": counts, "labelled_this_run": labelled, **p.metrics}
            self.board.said(LEARNER, "PAUSE" if p.paused else "INFO", report + explore, detail=detail,
                            activity=f"next refit in {self.s.learner_interval_s / 60:.0f} min")
        except Exception as e:
            log.exception("learner failed")
            self.board.said(LEARNER, "ERROR", f"learning pass failed: {e}")
        finally:
            self._learning.release()

    def _maybe_learn(self) -> None:
        if self.learner is None or not self.s.learner_enabled:
            return
        if time.monotonic() - self._last_learn < self.s.learner_interval_s:
            return
        self._last_learn = time.monotonic()
        # Own thread: a refit takes seconds and must never hold up a trade cycle.
        threading.Thread(target=self._learn_once, name="learner", daemon=True).start()

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
        self._maybe_learn()

        halt = self.risk.halt_reason()
        if halt:
            stats.skipped = halt
            log.info("cycle skipped: %s", halt)
            self.board.state(ORCH, "halted", f"not scanning: {halt}", message=f"halted: {halt}", decision="INFO")
            self.board.state(RISK, "halted", halt, message=halt, decision="REJECT")
            self.last_cycle = stats
            return stats

        series = self.s.crypto_series if self.s.market_focus == "crypto_15m" else self.s.sport_series
        self.board.working(SCOUT, f"scanning {', '.join(series)}")
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
        try:
            self.journal.set_flag("pass_summary", json.dumps(self.pass_summary()), by="orchestrator")
        except Exception as e:
            log.debug("pass summary write failed: %s", e)
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
