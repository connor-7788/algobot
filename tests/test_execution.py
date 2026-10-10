import copy
import logging
import os
import unittest
from decimal import Decimal

from kalshi.agents.execution import Execution, verify_approvals
from kalshi.client import OrderState
from kalshi.http import KalshiError, KalshiTransportError
from kalshi.orderbook import Book
from kalshi.orders import PlaceResult
from kalshi.proposal import Agent, Decision, Status, Verdict

from tests import helpers as h


class FakeData:
    def __init__(self, yes_ask_cents=45, depth=100):
        self.yes_ask = yes_ask_cents
        self.depth = depth
        self.http = type("H", (), {"authenticated": True})()

    def get_orderbook(self, ticker, depth=10):
        # YES ask = 1 - best NO bid
        no_bid = Decimal(100 - self.yes_ask) / 100
        return Book(ticker, yes_bids=[(Decimal("0.40"), Decimal(self.depth))],
                    no_bids=[(no_bid, Decimal(self.depth))])


class FakeOrders:
    """Records calls. Simulates partial fills over successive get_order polls."""

    def __init__(self, fills_per_poll=(0,), place_error=None, lookup_result=None, final_status="resting"):
        self.placed = []
        self.cancelled = []
        self.fills_per_poll = list(fills_per_poll)
        self.place_error = place_error
        self.lookup_result = lookup_result
        self.final_status = final_status
        self.filled = 0
        self.count = 0

    def place_limit_order(self, *, ticker, side, price_cents, count, client_order_id):
        self.placed.append(dict(ticker=ticker, side=side, price_cents=price_cents, count=count,
                                client_order_id=client_order_id))
        if self.place_error:
            raise self.place_error
        self.count = count
        return PlaceResult("ord-1", client_order_id, 0, count, None, None)

    def _state(self):
        rem = 0 if self.final_status == "canceled" else self.count - self.filled
        return OrderState("ord-1", "cid", "T", self.final_status if self.filled < self.count else "executed",
                          self.filled, rem, self.filled * 0.45, self.filled * 0.02)

    def get_order(self, order_id):
        if self.lookup_result is not None and self.count == 0:
            return self.lookup_result
        if self.fills_per_poll:
            self.filled = min(self.count, self.filled + self.fills_per_poll.pop(0))
        return self._state()

    def cancel_order(self, order_id, ticker):
        self.cancelled.append(order_id)
        rem = self.count - self.filled
        self.final_status = "canceled"
        return float(rem)

    def lookup(self, client_order_id, ticker):
        return self.lookup_result


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += max(s, 1.0)


class ExecBase(unittest.TestCase):
    def setUp(self):
        os.environ.pop("KILL_SWITCH", None)
        self.j = h.journal()

    def make(self, paper=True, orders=None, data=None, **kw):
        s = h.settings(paper_mode=paper, **kw)
        clock = Clock()
        ex = Execution(s, self.j, h.kill(s, self.j), data or FakeData(), orders,
                       sleep=clock.sleep, clock=clock)
        return ex, s


class TestApprovalChain(ExecBase):
    def assert_refused(self, p, orders=None, contains=""):
        orders = orders or FakeOrders()
        ex, _ = self.make(paper=False, orders=orders)
        out = ex.execute(p)
        self.assertTrue(out.is_rejected)
        self.assertEqual(out.rejected_by, Agent.EXECUTION.value)
        self.assertEqual(orders.placed, [], "nothing may be sent")
        if contains:
            self.assertIn(contains, out.verdicts[-1].reason)
        saved = self.j.get(p.proposal_id)
        self.assertEqual(saved.status, Status.REJECTED)

    def test_missing_edge_finder(self):
        p = h.proposal(with_edge_verdict=False)
        p.add_verdict(Agent.SKEPTIC.value, Decision.APPROVE, "ok")
        p.add_verdict(Agent.RISK.value, Decision.APPROVE, "ok")
        p.status = Status.APPROVED
        self.assert_refused(p, contains="edge_finder")

    def test_missing_skeptic(self):
        p = h.proposal()
        p.add_verdict(Agent.RISK.value, Decision.APPROVE, "ok")
        p.status = Status.APPROVED
        self.assert_refused(p, contains="skeptic")

    def test_missing_risk(self):
        p = h.proposal()
        p.add_verdict(Agent.SKEPTIC.value, Decision.APPROVE, "ok")
        p.status = Status.APPROVED
        self.assert_refused(p, contains="risk_manager")

    def test_rejected_anywhere(self):
        p = h.proposal()
        p.add_verdict(Agent.SKEPTIC.value, Decision.REJECT, "stale")
        ex, _ = self.make(paper=False, orders=FakeOrders())
        out = ex.execute(p)
        self.assertTrue(out.is_rejected)
        self.assertEqual(out.rejected_by, Agent.SKEPTIC.value)

    def test_wrong_order(self):
        p = h.proposal()
        p.add_verdict(Agent.RISK.value, Decision.APPROVE, "ok")
        p.add_verdict(Agent.SKEPTIC.value, Decision.APPROVE, "ok")
        p.status = Status.APPROVED
        self.assert_refused(p, contains="out of order")

    def test_skeptic_modify_not_accepted(self):
        p = h.proposal()
        p.verdicts.append(Verdict(Agent.SKEPTIC.value, Decision.MODIFY, "forged", changes={}))
        p.add_verdict(Agent.RISK.value, Decision.APPROVE, "ok")
        p.status = Status.APPROVED
        self.assert_refused(p, contains="skeptic verdict is MODIFY")

    def test_status_not_approved(self):
        p = h.fully_approved()
        p.status = Status.PROPOSED
        self.assert_refused(p, contains="status")

    def test_tampered_size(self):
        p = h.fully_approved(count=10)
        p.contract_count = 20          # bypassing add_verdict
        self.assert_refused(p, contains="size grew")

    def test_tampered_side(self):
        p = h.fully_approved()
        p.side = "no"
        self.assert_refused(p, contains="side changed")

    def test_tampered_price(self):
        p = h.fully_approved(price=45)
        p.price_cents = 30
        self.assert_refused(p, contains="price changed")

    def test_kill_switch_blocks(self):
        p = h.fully_approved()
        ex, s = self.make(paper=True)
        ex.kill.set(True)
        out = ex.execute(p)
        self.assertTrue(out.is_rejected)

    def test_full_chain_passes_verify(self):
        ok, why = verify_approvals(h.fully_approved())
        self.assertTrue(ok, why)

    def test_risk_modify_down_passes(self):
        p = h.proposal(count=50)
        p.add_verdict(Agent.SKEPTIC.value, Decision.APPROVE, "ok")
        p.add_verdict(Agent.RISK.value, Decision.MODIFY, "size", changes={"contract_count": 10})
        p.status = Status.APPROVED
        ok, why = verify_approvals(p)
        self.assertTrue(ok, why)


class TestPaper(ExecBase):
    def test_paper_simulates_fill_at_ask_and_sends_nothing(self):
        orders = FakeOrders()
        ex, s = self.make(paper=True, orders=orders)
        p = h.fully_approved(price=45, count=10)
        logging.disable(logging.NOTSET)
        try:
            with self.assertLogs("agent.execution", level="INFO") as logs:
                out = ex.execute(p)
        finally:
            logging.disable(logging.WARNING)
        self.assertEqual(orders.placed, [])
        self.assertEqual(out.status, Status.PAPER_FILLED)
        self.assertEqual(out.filled_count, 10)
        self.assertEqual(out.avg_fill_price_cents, 45.0)
        self.assertAlmostEqual(out.fees_paid_usd, 0.18)      # ceil_cent(0.07*10*.45*.55)=0.18
        self.assertTrue(any("PAPER ORDER" in m and p.proposal_id in m for m in logs.output))
        self.assertEqual(self.j.get(p.proposal_id).status, Status.PAPER_FILLED)

    def test_original_proposal_not_mutated(self):
        ex, _ = self.make(paper=True)
        p = h.fully_approved()
        before = copy.deepcopy(p.to_dict())
        ex.execute(p)
        self.assertEqual(p.to_dict(), before)


class TestLive(ExecBase):
    def test_uses_proposal_id_as_client_order_id_and_limit(self):
        orders = FakeOrders(fills_per_poll=[10])
        ex, s = self.make(paper=False, orders=orders, exec_slippage_cap_cents=1)
        p = h.fully_approved(price=45, count=10)
        out = ex.execute(p)
        self.assertEqual(len(orders.placed), 1)
        self.assertEqual(orders.placed[0]["client_order_id"], p.proposal_id)
        self.assertEqual(orders.placed[0]["price_cents"], 46)
        self.assertEqual(out.status, Status.OPEN)
        self.assertEqual(out.filled_count, 10)

    def test_partial_fill_cancels_remainder_after_timeout(self):
        orders = FakeOrders(fills_per_poll=[3, 1, 0, 0, 0, 0, 0, 0, 0, 0])
        ex, s = self.make(paper=False, orders=orders, exec_order_timeout_s=5.0)
        p = h.fully_approved(count=10)
        out = ex.execute(p)
        self.assertEqual(orders.cancelled, ["ord-1"])
        self.assertEqual(out.filled_count, 4)
        self.assertEqual(out.status, Status.OPEN)

    def test_nothing_filled_is_cancelled_unfilled(self):
        orders = FakeOrders(fills_per_poll=[0] * 20)
        ex, _ = self.make(paper=False, orders=orders, exec_order_timeout_s=3.0)
        out = ex.execute(h.fully_approved(count=10))
        self.assertEqual(out.status, Status.CANCELLED_UNFILLED)
        self.assertEqual(orders.cancelled, ["ord-1"])

    def test_refuses_when_ask_moved_past_slippage_cap(self):
        orders = FakeOrders()
        ex, _ = self.make(paper=False, orders=orders, data=FakeData(yes_ask_cents=48),
                          exec_slippage_cap_cents=1)
        out = ex.execute(h.fully_approved(price=45))
        self.assertTrue(out.is_rejected)
        self.assertIn("above limit", out.verdicts[-1].reason)
        self.assertEqual(orders.placed, [])

    def test_transport_error_with_no_order_found_is_uncertain(self):
        orders = FakeOrders(place_error=KalshiTransportError("timeout"), lookup_result=None)
        ex, _ = self.make(paper=False, orders=orders)
        out = ex.execute(h.fully_approved())
        self.assertEqual(out.status, Status.UNCERTAIN)
        self.assertFalse(out.is_rejected)

    def test_transport_error_resolved_by_client_id_lookup(self):
        found = OrderState("ord-9", "x", "T", "executed", 10, 0, 4.5, 0.2)
        orders = FakeOrders(place_error=KalshiTransportError("timeout"), lookup_result=found)
        ex, _ = self.make(paper=False, orders=orders)
        out = ex.execute(h.fully_approved(count=10))
        self.assertEqual(out.order_id, "ord-9")
        self.assertEqual(out.filled_count, 10)
        self.assertEqual(out.status, Status.OPEN)
        self.assertEqual(len(orders.placed), 1, "no second placement")

    def test_exchange_rejection_is_reject(self):
        orders = FakeOrders(place_error=KalshiError(400, "insufficient balance"))
        ex, _ = self.make(paper=False, orders=orders)
        out = ex.execute(h.fully_approved())
        self.assertTrue(out.is_rejected)
        self.assertIn("insufficient balance", out.verdicts[-1].reason)

    def test_live_without_order_client_refuses(self):
        ex, _ = self.make(paper=False, orders=None)
        out = ex.execute(h.fully_approved())
        self.assertTrue(out.is_rejected)


if __name__ == "__main__":
    unittest.main()
