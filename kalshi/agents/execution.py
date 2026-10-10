"""
Execution: the ONLY module allowed to import the Kalshi order client.

Before anything is sent it verifies the verdict chain:
    edge_finder APPROVE  ->  skeptic APPROVE  ->  risk_manager APPROVE|MODIFY
in that order, exactly one of each, no REJECT anywhere, status APPROVED, and
side / price / size consistent with what Edge Finder proposed (size may only
have gone down). Anything off and Execution appends a REJECT and stops.

Then:
  * Paper mode (default): logs the exact order it would have sent and
    simulates a full fill at the observed ask.
  * Live: re-reads the book, refuses if the ask is now above
    limit = proposal ask + exec_slippage_cap_cents, places a limit order with
    client_order_id = proposal_id, polls for fills, and cancels whatever is
    still resting after exec_order_timeout_s (or immediately if the kill
    switch flips while waiting).
"""

from __future__ import annotations

import copy
import json
import logging
import time

from kalshi.client import KalshiDataClient
from kalshi.agents.risk_manager import ExitOrder
from kalshi.fees import taker_fee_usd
from kalshi.http import KalshiError, KalshiTransportError
from kalshi.journal import Journal
from kalshi.killswitch import KillSwitch
from kalshi.orders import KalshiOrderClient, to_v2_side_and_price
from datetime import datetime, timezone

from kalshi.proposal import Agent, Decision, Status, TradeProposal

log = logging.getLogger("agent.execution")

REQUIRED_CHAIN = [
    (Agent.EDGE_FINDER.value, {Decision.APPROVE}),
    (Agent.SKEPTIC.value, {Decision.APPROVE}),
    (Agent.RISK.value, {Decision.APPROVE, Decision.MODIFY}),
]


def verify_approvals(p: TradeProposal) -> tuple[bool, str]:
    if p.is_rejected:
        return False, f"proposal was rejected by {p.rejected_by}"
    if p.status != Status.APPROVED:
        return False, f"status is {p.status.value}, expected APPROVED"
    positions = []
    for agent, allowed in REQUIRED_CHAIN:
        idx = [i for i, v in enumerate(p.verdicts) if v.agent == agent]
        if not idx:
            return False, f"missing verdict from {agent}"
        if len(idx) > 1:
            return False, f"{agent} has {len(idx)} verdicts, expected 1"
        v = p.verdicts[idx[0]]
        if v.decision not in allowed:
            return False, f"{agent} verdict is {v.decision.value}"
        positions.append(idx[0])
    if positions != sorted(positions):
        return False, "verdicts are out of order (need edge_finder, skeptic, risk_manager)"

    edge = p.verdicts[positions[0]].data_snapshot
    if edge.get("side") != p.side:
        return False, f"side changed after Edge Finder ({edge.get('side')} -> {p.side})"
    if edge.get("price_cents") != p.price_cents:
        return False, f"price changed after Edge Finder ({edge.get('price_cents')} -> {p.price_cents})"
    proposed = int(edge.get("contract_count", p.original_count))
    if p.contract_count > proposed or p.contract_count > p.original_count:
        return False, f"size grew after Edge Finder ({proposed} -> {p.contract_count})"
    if p.contract_count < 1:
        return False, "contract_count < 1"
    risk = p.verdicts[positions[2]]
    if risk.decision == Decision.MODIFY:
        if set(risk.changes) - {"contract_count"}:
            return False, f"risk modified fields outside its scope: {sorted(risk.changes)}"
        old, new = risk.changes.get("contract_count", [0, 0])
        if new > old:
            return False, "risk MODIFY increased size"
    return True, "approval chain verified"


class Execution:
    name = Agent.EXECUTION.value

    def __init__(self, settings, journal: Journal, killswitch: KillSwitch,
                 data: KalshiDataClient | None, orders: KalshiOrderClient | None = None,
                 http=None, sleep=time.sleep, clock=time.monotonic):
        self.s = settings
        self.journal = journal
        self.kill = killswitch
        self.data = data
        # Execution owns the order client. In live mode it builds one from the
        # signed transport; tests inject a fake through `orders`.
        if orders is None and http is not None and not settings.paper_mode and http.authenticated:
            orders = KalshiOrderClient(http, settings)
        self.orders = orders
        self.sleep = sleep
        self.clock = clock

    def _refuse(self, p: TradeProposal, reason: str, **snap) -> TradeProposal:
        log.warning("execution refused %s: %s", p.proposal_id, reason)
        if not p.is_rejected:
            p.add_verdict(self.name, Decision.REJECT, reason, snap)
        self.journal.save(p)
        return p

    def execute(self, proposal: TradeProposal) -> TradeProposal:
        p = copy.deepcopy(proposal)
        ok, why = verify_approvals(p)
        if not ok:
            return self._refuse(p, why)
        on, kill_why = self.kill.status()
        if on:
            return self._refuse(p, kill_why)

        limit = min(99, p.price_cents + self.s.exec_slippage_cap_cents)
        book_side, yes_price = to_v2_side_and_price(p.side, limit)
        order = {"ticker": p.market_ticker, "client_order_id": p.proposal_id, "outcome_side": p.side,
                 "count": p.contract_count, "limit_cents": limit, "v2_side": book_side,
                 "v2_yes_price": yes_price, "time_in_force": "good_till_canceled"}

        if self.s.paper_mode:
            return self._paper_fill(p, order)
        if self.orders is None or self.data is None:
            return self._refuse(p, "live mode but no Kalshi order client (missing credentials)")
        return self._live(p, order, limit)

    # ── Paper ────────────────────────────────────────────────────────────────

    def _paper_fill(self, p: TradeProposal, order: dict) -> TradeProposal:
        log.info("PAPER ORDER (not sent): %s", json.dumps(order))
        p.filled_count = p.contract_count
        p.avg_fill_price_cents = float(p.price_cents)          # simulated fill at the ask
        edge = p.verdict_for(Agent.EDGE_FINDER.value)
        book = (edge.data_snapshot.get("book") if edge else None) or {}
        if p.side == "yes" and book.get("yes_bid") is not None:
            p.entry_bid_cents = float(book["yes_bid"])
        elif p.side == "no" and book.get("yes_ask") is not None:
            p.entry_bid_cents = 100.0 - float(book["yes_ask"])
        p.fees_paid_usd = taker_fee_usd(p.price_cents, p.contract_count, self.s)
        p.order_id = f"paper-{p.proposal_id[:8]}"
        p.add_verdict(self.name, Decision.APPROVE,
                      f"paper fill {p.filled_count} @ {p.price_cents}c", {"order": order, "paper": True})
        p.status = Status.PAPER_FILLED
        self.journal.save(p)
        return p

    # ── Live ─────────────────────────────────────────────────────────────────

    def _live(self, p: TradeProposal, order: dict, limit: int) -> TradeProposal:
        try:
            book = self.data.get_orderbook(p.market_ticker, depth=self.s.scout_orderbook_depth)
        except Exception as e:
            return self._refuse(p, f"could not re-read book: {e}")
        ask = book.best_ask_cents(p.side)
        if ask is None:
            return self._refuse(p, "no ask on the book anymore", book=book.snapshot())
        if ask > limit:
            return self._refuse(p, f"ask moved to {ask}c, above limit {limit}c "
                                   f"(slippage cap {self.s.exec_slippage_cap_cents}c)", book=book.snapshot())

        p.entry_bid_cents = book.best_bid_cents(p.side)
        log.info("LIVE ORDER: %s", json.dumps(order))
        try:
            res = self.orders.place_limit_order(ticker=p.market_ticker, side=p.side, price_cents=limit,
                                                count=p.contract_count, client_order_id=p.proposal_id)
        except KalshiTransportError as e:
            # Unknown whether it reached Kalshi. Look it up by client_order_id.
            found = None
            try:
                found = self.orders.lookup(p.proposal_id, p.market_ticker)
            except Exception as e2:
                log.error("lookup after transport error failed: %s", e2)
            if found is None:
                return self._mark_uncertain(p, order, f"transport error, order state unknown: {e}")
            res_id, filled, remaining = found.order_id, found.fill_count, found.remaining_count
        except KalshiError as e:
            return self._refuse(p, f"exchange rejected order: {e}", order=order)
        else:
            res_id, filled, remaining = res.order_id, res.fill_count, res.remaining_count

        p.order_id = res_id
        p.status = Status.UNCERTAIN          # until we've read back a final state
        self.journal.save(p)

        state = None
        deadline = self.clock() + self.s.exec_order_timeout_s
        while remaining > 0 and self.clock() < deadline:
            if self.kill.is_on():
                log.warning("kill switch on while waiting for fills on %s", p.proposal_id)
                break
            self.sleep(self.s.exec_poll_interval_s)
            try:
                state = self.orders.get_order(res_id)
            except Exception as e:
                log.warning("get_order %s failed: %s", res_id, e)
                continue
            filled, remaining = state.fill_count, state.remaining_count
            if state.status in ("canceled", "executed"):
                break

        cancelled = 0.0
        if remaining > 0:
            try:
                cancelled = self.orders.cancel_order(res_id, p.market_ticker)
            except Exception as e:
                log.error("cancel of %s failed: %s", res_id, e)
                return self._mark_uncertain(p, order, f"cancel of unfilled remainder failed: {e}")
        try:
            state = self.orders.get_order(res_id)
            filled = state.fill_count
        except Exception as e:
            # Fall back to arithmetic: whatever wasn't cancelled was filled.
            log.warning("final get_order %s failed (%s); deriving fills from cancel", res_id, e)
            filled = p.contract_count - int(cancelled) if remaining > 0 else p.contract_count
            state = None

        p.filled_count = int(filled)
        p.avg_fill_price_cents = self._avg_price(state, filled, limit)
        p.fees_paid_usd = state.fees_usd if state else 0.0
        snap = {"order": order, "order_id": res_id, "filled": p.filled_count,
                "cancelled": cancelled, "avg_fill_cents": p.avg_fill_price_cents}
        if p.filled_count == 0:
            p.add_verdict(self.name, Decision.APPROVE, "sent; nothing filled before timeout, cancelled", snap)
            p.status = Status.CANCELLED_UNFILLED
        else:
            p.add_verdict(self.name, Decision.APPROVE,
                          f"filled {p.filled_count}/{p.contract_count} @ {p.avg_fill_price_cents:.2f}c", snap)
            p.status = Status.OPEN
        self.journal.save(p)
        return p

    @staticmethod
    def _avg_price(state, filled: int, limit: int) -> float:
        """Average price paid for our side. Kalshi's fill-cost fields are used
        when they give a plausible per-contract price; otherwise assume we paid
        the limit, which is the worst case."""
        if state is not None and filled > 0 and state.fill_cost_usd > 0:
            per = state.fill_cost_usd / filled * 100
            if 1 <= per <= limit + 0.5:
                return round(per, 4)
        return float(limit)

    def _mark_uncertain(self, p: TradeProposal, order: dict, reason: str) -> TradeProposal:
        log.error("UNCERTAIN %s: %s", p.proposal_id, reason)
        if p.verdict_for(self.name) is None:
            p.add_verdict(self.name, Decision.APPROVE, f"sent; {reason}", {"order": order})
        p.status = Status.UNCERTAIN
        self.journal.save(p)
        self.journal.log_event("uncertain_order", f"{p.proposal_id} {p.market_ticker}: {reason}", "ERROR")
        return p

    # ── Reconciliation (called by the orchestrator for UNCERTAIN / stuck) ────

    def reconcile(self, proposal: TradeProposal) -> TradeProposal:
        p = copy.deepcopy(proposal)
        if self.s.paper_mode or self.orders is None:
            if p.status in (Status.APPROVED, Status.UNCERTAIN) and p.filled_count == 0:
                p.status = Status.CANCELLED_UNFILLED
                self.journal.save(p)
            return p
        found = self.orders.lookup(p.proposal_id, p.market_ticker)
        if found is None:
            p.status = Status.CANCELLED_UNFILLED
            self.journal.log_event("reconcile", f"{p.proposal_id}: no order on Kalshi, marking unfilled")
            self.journal.save(p)
            return p
        if found.status == "resting" and found.remaining_count > 0:
            self.orders.cancel_order(found.order_id, p.market_ticker)
            found = self.orders.get_order(found.order_id)
        p.order_id = found.order_id
        p.filled_count = found.fill_count
        limit = min(99, p.price_cents + self.s.exec_slippage_cap_cents)
        p.avg_fill_price_cents = self._avg_price(found, found.fill_count, limit)
        p.fees_paid_usd = found.fees_usd
        p.status = Status.OPEN if found.fill_count > 0 else Status.CANCELLED_UNFILLED
        self.journal.log_event("reconcile", f"{p.proposal_id}: resolved to {p.status.value} "
                                            f"({found.fill_count} filled)")
        self.journal.save(p)
        return p

    # ── Exits (take-profit / stop-loss from Risk) ────────────────────────────

    def close_position(self, order: ExitOrder) -> TradeProposal | None:
        if not isinstance(order, ExitOrder):
            raise TypeError("close_position only accepts an ExitOrder from RiskManager.check_exits")
        p = self.journal.get(order.proposal_id)
        if p is None or p.status not in (Status.OPEN, Status.PAPER_FILLED) or p.filled_count <= 0:
            return None
        n = min(order.contracts, p.filled_count)
        if self.s.paper_mode:
            sold, price = n, order.bid_cents
            log.info("PAPER EXIT (not sent): sell %d %s %s @ %dc [%s]", n, p.side.upper(),
                     p.market_ticker, price, order.kind)
        else:
            if self.orders is None:
                return None
            res = self.orders.place_exit_order(ticker=p.market_ticker, side=p.side, bid_cents=order.bid_cents,
                                               count=n, client_order_id=f"{p.proposal_id[:24]}-x{int(time.time())}")
            sold, price = res.fill_count, order.bid_cents        # IOC at >= bid: worst case is the bid
            if sold <= 0:
                log.info("exit IOC for %s filled nothing at %dc", p.market_ticker, price)
                return None

        entry = float(p.avg_fill_price_cents or p.price_cents)
        entry_fee_part = (p.fees_paid_usd or 0.0) * sold / p.filled_count
        exit_fee = taker_fee_usd(price, sold, self.s)
        pnl = sold * (price - entry) / 100.0 - entry_fee_part - exit_fee
        snap = {"kind": order.kind, "sold": sold, "price_cents": price, "exit_fee_usd": exit_fee,
                "pnl_usd": round(pnl, 4), "paper": self.s.paper_mode}
        p.add_verdict(Agent.RISK.value, Decision.APPROVE, f"EXIT {order.reason}", {"exit": order.kind})
        p.add_verdict(self.name, Decision.APPROVE,
                      f"closed {sold} @ {price}c ({order.kind.replace('_', ' ').lower()}), P&L ${pnl:+.2f}", snap)
        prior = p.realized_pnl_usd or 0.0
        if sold >= p.filled_count:
            p.status = Status.CLOSED
            p.realized_pnl_usd = round(prior + pnl, 4)
            p.settled_at = datetime.now(timezone.utc)
        else:
            # Partial: book the sold part as a separate closed record would be cleaner, but
            # keeping one row per trade, we shrink the open size and carry realized P&L.
            p.fees_paid_usd = (p.fees_paid_usd or 0.0) - entry_fee_part
            p.filled_count -= sold
            p.realized_pnl_usd = round(prior + pnl, 4)
        self.journal.save(p)
        return p
