import os
import random
import unittest
from datetime import timedelta

from kalshi.agents.risk_manager import RiskManager
from kalshi.proposal import Agent, Decision, ScopeViolation, Status

from tests import helpers as h


class RiskTestBase(unittest.TestCase):
    def setUp(self):
        os.environ.pop("KILL_SWITCH", None)
        self.s = h.settings(risk_stake_floor_usd=5.0, risk_stake_ceiling_usd=25.0,
                            risk_max_event_exposure_usd=1000.0, risk_daily_loss_limit_usd=30.0)
        self.j = h.journal()
        self.k = h.kill(self.s, self.j)
        self.risk = RiskManager(self.s, self.j, self.k, now=lambda: h.NOW)

    def apply(self, p, d):
        p.add_verdict(Agent.RISK.value, d.decision, d.reason, d.data_snapshot, d.changes or None)
        return p


class TestMaxOpenPositions(RiskTestBase):
    def test_tenth_position_allowed(self):
        for i in range(9):
            h.open_position(self.j, f"OPEN-{i}", event=f"EV-{i}")
        d = self.risk.review(h.proposal(ticker="NEW", event="EV-NEW"))
        self.assertIn(d.decision, (Decision.APPROVE, Decision.MODIFY))

    def test_refuses_eleventh_position(self):
        for i in range(10):
            h.open_position(self.j, f"OPEN-{i}", event=f"EV-{i}")
        d = self.risk.review(h.proposal(ticker="NEW", event="EV-NEW"))
        self.assertEqual(d.decision, Decision.REJECT)
        self.assertIn("10 open positions", d.reason)

    def test_uncertain_and_approved_hold_slots(self):
        for i in range(5):
            h.open_position(self.j, f"U-{i}", event=f"EU-{i}", status=Status.UNCERTAIN)
        for i in range(5):
            h.open_position(self.j, f"A-{i}", event=f"EA-{i}", status=Status.APPROVED)
        d = self.risk.review(h.proposal(ticker="NEW", event="EV-NEW"))
        self.assertEqual(d.decision, Decision.REJECT)

    def test_settled_and_cancelled_free_slots(self):
        for i in range(10):
            h.open_position(self.j, f"C-{i}", event=f"EC-{i}", status=Status.CANCELLED_UNFILLED)
        d = self.risk.review(h.proposal(ticker="NEW", event="EV-NEW"))
        self.assertNotEqual(d.decision, Decision.REJECT)

    def test_refuses_second_position_same_market(self):
        h.open_position(self.j, "SAME", event="EV-1")
        d = self.risk.review(h.proposal(ticker="SAME", event="EV-1"))
        self.assertEqual(d.decision, Decision.REJECT)
        self.assertIn("already holding", d.reason)


class TestDailyLossLimit(RiskTestBase):
    def test_below_limit_allows(self):
        h.settled(self.j, -29.99, when=h.NOW - timedelta(hours=1))
        self.assertEqual(self.risk.halt_reason(), "")
        self.assertNotEqual(self.risk.review(h.proposal()).decision, Decision.REJECT)

    def test_at_limit_refuses(self):
        h.settled(self.j, -30.00, when=h.NOW - timedelta(hours=1))
        d = self.risk.review(h.proposal())
        self.assertEqual(d.decision, Decision.REJECT)
        self.assertIn("daily loss limit", d.reason)

    def test_above_limit_refuses(self):
        h.settled(self.j, -20.0, when=h.NOW - timedelta(hours=2))
        h.settled(self.j, -15.0, when=h.NOW - timedelta(hours=1))
        self.assertIn("daily loss limit", self.risk.halt_reason())
        self.assertEqual(self.risk.review(h.proposal()).decision, Decision.REJECT)

    def test_yesterdays_losses_do_not_count(self):
        # NOW is 14:00 New York; 30h earlier is the previous local day.
        h.settled(self.j, -100.0, when=h.NOW - timedelta(hours=30))
        self.assertEqual(self.risk.halt_reason(), "")


class TestKillSwitch(RiskTestBase):
    def test_dashboard_flag_rejects(self):
        self.k.set(True, by="test")
        self.assertEqual(self.risk.review(h.proposal()).decision, Decision.REJECT)
        self.k.set(False, by="test")
        self.assertNotEqual(self.risk.review(h.proposal()).decision, Decision.REJECT)

    def test_env_var_rejects(self):
        os.environ["KILL_SWITCH"] = "true"
        try:
            d = self.risk.review(h.proposal())
            self.assertEqual(d.decision, Decision.REJECT)
            self.assertIn("KILL_SWITCH", d.reason)
        finally:
            os.environ.pop("KILL_SWITCH", None)


class TestSizing(RiskTestBase):
    def test_floor_when_no_profit(self):
        # stake floor $5; worst-case cost per contract = (45 + 1 cap + 2 fee) / 100 = $0.48 -> 10
        d = self.risk.review(h.proposal(count=50, price=45))
        self.assertEqual(d.decision, Decision.MODIFY)
        self.assertEqual(d.changes["contract_count"], 10)
        self.assertEqual(d.data_snapshot["binding"], "stake")

    def test_two_percent_of_realized(self):
        h.settled(self.j, 1000.0, when=h.NOW - timedelta(days=3))     # 2% = $20
        d = self.risk.review(h.proposal(count=500, price=45))
        self.assertEqual(d.changes["contract_count"], 41)               # floor(20 / 0.48)

    def test_ceiling(self):
        h.settled(self.j, 100000.0, when=h.NOW - timedelta(days=3))   # 2% = $2000 -> capped $25
        d = self.risk.review(h.proposal(count=500, price=45))
        self.assertEqual(d.changes["contract_count"], 52)               # floor(25 / 0.48)

    def test_event_exposure_counts_same_game(self):
        s = h.settings(risk_stake_floor_usd=25.0, risk_max_event_exposure_usd=10.0)
        risk = RiskManager(s, self.j, h.kill(s, self.j), now=lambda: h.NOW)
        h.open_position(self.j, "GAME-HOME", event="GAME", count=10, price=60)   # $6 in the event
        d = risk.review(h.proposal(ticker="GAME-AWAY", event="GAME", count=100, price=45))
        self.assertEqual(d.decision, Decision.MODIFY)
        self.assertEqual(d.changes["contract_count"], 8)                # floor(4 / 0.48)
        self.assertEqual(d.data_snapshot["binding"], "event_exposure")

    def test_event_full_rejects(self):
        s = h.settings(risk_max_event_exposure_usd=6.0)
        risk = RiskManager(s, self.j, h.kill(s, self.j), now=lambda: h.NOW)
        h.open_position(self.j, "GAME-HOME", event="GAME", count=10, price=60)
        self.assertEqual(risk.review(h.proposal(ticker="GAME-AWAY", event="GAME")).decision, Decision.REJECT)


class TestNeverIncreases(RiskTestBase):
    def test_small_proposal_is_not_upsized(self):
        h.settled(self.j, 100000.0, when=h.NOW - timedelta(days=3))   # stake allows 52
        d = self.risk.review(h.proposal(count=3, price=45))
        self.assertEqual(d.decision, Decision.APPROVE)
        self.assertEqual(d.changes, {})

    def test_randomized_never_increases_or_touches_side_price(self):
        rng = random.Random(7)
        for i in range(300):
            j = h.journal()
            if rng.random() < 0.5:
                h.settled(j, rng.uniform(-20, 5000), when=h.NOW - timedelta(days=2))
            for k in range(rng.randint(0, 9)):
                h.open_position(j, f"O{k}", event=rng.choice(["E1", "E2", "E3"]),
                                count=rng.randint(1, 40), price=rng.randint(5, 95))
            s = h.settings(risk_stake_floor_usd=rng.uniform(0.5, 10), risk_stake_ceiling_usd=rng.uniform(10, 100),
                           risk_max_event_exposure_usd=rng.uniform(1, 80))
            risk = RiskManager(s, j, h.kill(s, j), now=lambda: h.NOW)
            p = h.proposal(ticker=f"N{i}", event=rng.choice(["E1", "E2", "E4"]),
                           side=rng.choice(["yes", "no"]), price=rng.randint(2, 97), count=rng.randint(1, 500))
            before = (p.contract_count, p.side, p.price_cents)
            d = risk.review(p)
            if d.decision == Decision.MODIFY:
                self.assertEqual(set(d.changes), {"contract_count"})
                self.assertLess(d.changes["contract_count"], before[0])
                self.assertGreaterEqual(d.changes["contract_count"], 1)
            self.assertEqual((p.contract_count, p.side, p.price_cents), before, "review must not mutate")

    def test_proposal_refuses_risk_increase_or_side_change(self):
        p = h.proposal(count=10)
        with self.assertRaises(ScopeViolation):
            p.add_verdict(Agent.RISK.value, Decision.MODIFY, "up", changes={"contract_count": 11})
        with self.assertRaises(ScopeViolation):
            p.add_verdict(Agent.RISK.value, Decision.MODIFY, "flip", changes={"side": "no"})
        with self.assertRaises(ScopeViolation):
            p.add_verdict(Agent.RISK.value, Decision.MODIFY, "reprice", changes={"price_cents": 40})
        self.assertEqual(p.contract_count, 10)


if __name__ == "__main__":
    unittest.main()
