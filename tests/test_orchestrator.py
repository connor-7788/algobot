import os
import time
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from kalshi.agents.base import AgentDecision
from kalshi.agents.edge_finder import EdgeResult
from kalshi.agents.execution import Execution
from kalshi.agents.monitor import MonitorReport
from kalshi.agents.risk_manager import RiskManager
from kalshi.agents.skeptic import Skeptic
from kalshi.history import MarketHistory
from kalshi.orchestrator import Orchestrator
from kalshi.proposal import Status

from tests import helpers as h


class FakeScout:
    def __init__(self, n=1):
        self.n = n
        self.calls = 0
        self.last_stats = {"markets": n}

    def scan(self):
        self.calls += 1
        return [SimpleNamespace(market=SimpleNamespace(ticker=f"T{i}")) for i in range(self.n)]


class FakeEdge:
    def evaluate(self, cand):
        now = datetime.now(timezone.utc)
        p = h.proposal(ticker=cand.market.ticker, event=f"E-{cand.market.ticker}", count=5)
        p.book_fetched_at = now
        for s in p.sources:
            s.fetched_at = s.observed_at = now
        return EdgeResult(p, "proposed")


class SlowAgent:
    def __init__(self, delay):
        self.delay = delay

    def review(self, p):
        time.sleep(self.delay)
        return AgentDecision.approve("too late")


class SlowExecution:
    def execute(self, p):
        time.sleep(1.0)
        return p

    def reconcile(self, p):
        return p


class FakeMonitor:
    def run_once(self):
        return MonitorReport()


class OrchBase(unittest.TestCase):
    def setUp(self):
        os.environ.pop("KILL_SWITCH", None)
        self.s = h.settings(timeout_skeptic_s=0.2, timeout_execution_s=0.2, monitor_interval_s=0,
                            risk_stake_floor_usd=5.0)
        self.j = h.journal()
        self.kill = h.kill(self.s, self.j)
        self.risk = RiskManager(self.s, self.j, self.kill)
        self.history = MarketHistory()

    def orch(self, **kw):
        parts = dict(scout=FakeScout(), edge_finder=FakeEdge(),
                     skeptic=Skeptic(self.s, self.history, self.j), risk=self.risk,
                     execution=Execution(self.s, self.j, self.kill, None, None),
                     monitor=FakeMonitor(), journal=self.j)
        parts.update(kw)
        return Orchestrator(self.s, **parts)


class TestOrchestrator(OrchBase):
    def test_paper_happy_path_journals_every_verdict(self):
        o = self.orch()
        stats = o.run_cycle()
        self.assertEqual(stats.executed, 1)
        rows = self.j.rows()
        self.assertEqual(len(rows), 1)
        p = self.j.get(rows[0]["proposal_id"])
        self.assertEqual(p.status, Status.PAPER_FILLED)
        self.assertEqual([v.agent for v in p.verdicts], ["edge_finder", "skeptic", "risk_manager", "execution"])

    def test_timeout_counts_as_reject_and_is_journaled(self):
        o = self.orch(skeptic=SlowAgent(1.0))
        stats = o.run_cycle()
        self.assertEqual(stats.rejected_by, {"skeptic": 1})
        row = self.j.rows()[0]
        self.assertEqual(row["status"], "REJECTED")
        self.assertEqual(row["rejected_by"], "skeptic")
        self.assertIn("timed out", row["reject_reason"])

    def test_execution_timeout_holds_slot_as_uncertain(self):
        o = self.orch(execution=SlowExecution())
        o.run_cycle()
        p = self.j.get(self.j.rows()[0]["proposal_id"])
        self.assertEqual(p.status, Status.UNCERTAIN)
        self.assertEqual(len(self.j.open_positions()), 1)

    def test_kill_switch_skips_scan(self):
        scout = FakeScout()
        o = self.orch(scout=scout)
        self.kill.set(True)
        stats = o.run_cycle()
        self.assertEqual(scout.calls, 0)
        self.assertIn("kill switch", stats.skipped)

    def test_rejects_eleventh_through_pipeline(self):
        for i in range(10):
            h.open_position(self.j, f"OPEN-{i}", event=f"EV-{i}")
        o = self.orch()
        stats = o.run_cycle()
        self.assertEqual(stats.rejected_by, {"risk_manager": 1})

    def test_status_board_shows_each_agent(self):
        o = self.orch(scout=FakeScout(n=2))
        o.run_cycle()
        st = {r["agent"]: r for r in self.j.agent_statuses()}
        for agent in ("orchestrator", "scout", "edge_finder", "skeptic", "risk_manager", "execution", "monitor"):
            self.assertIn(agent, st)
        self.assertEqual(st["execution"]["last_decision"], "APPROVE")
        self.assertIn("paper fill", st["execution"]["last_message"])
        self.assertIn("sleeping until", st["orchestrator"]["activity"])
        self.assertIn("open_positions", st["risk_manager"]["detail"])
        feed = self.j.agent_feed(limit=50)
        self.assertTrue(any(f["agent"] == "skeptic" for f in feed))


if __name__ == "__main__":
    unittest.main()
