"""Crypto 15-minute model, Scout/Edge Finder crypto path, take-profit / stop-loss exits."""

import os
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from kalshi.agents.base import Candidate
from kalshi.agents.edge_finder import EdgeFinder
from kalshi.agents.execution import Execution
from kalshi.agents.risk_manager import ExitOrder, RiskManager
from kalshi.agents.scout import Scout
from kalshi.client import Market
from kalshi.history import MarketHistory
from kalshi.orchestrator import Orchestrator
from kalshi.orderbook import Book
from kalshi.pnl import PositionPnL
from kalshi.proposal import Status
from kalshi.spot import (SpotQuote, parse_bitstamp, parse_coinbase, parse_kraken, prob_avg_at_or_above,
                         sigma_from_candles)

from tests import helpers as h

NOW = datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class TestModel(unittest.TestCase):
    def test_at_the_money_is_half(self):
        self.assertAlmostEqual(prob_avg_at_or_above(100.0, 100.0, 600, 0.0001), 0.5, places=6)

    def test_direction_and_time(self):
        sig = 0.0001
        above = prob_avg_at_or_above(100.1, 100.0, 600, sig)
        below = prob_avg_at_or_above(99.9, 100.0, 600, sig)
        self.assertGreater(above, 0.5)
        self.assertLess(below, 0.5)
        # Same gap with less time left is more certain.
        self.assertGreater(prob_avg_at_or_above(100.1, 100.0, 120, sig), above)

    def test_inside_averaging_window_refuses(self):
        self.assertIsNone(prob_avg_at_or_above(100.0, 100.0, 59, 0.0001))

    def test_sigma_from_candles(self):
        # newest first; alternating +-0.1% moves
        closes = [100 * (1.001 if i % 2 else 1.0) for i in range(61)][::-1]
        candles = [[0, 0, 0, 0, c, 0] for c in closes]
        sig = sigma_from_candles(candles, 60)
        self.assertGreater(sig, 0)
        self.assertLess(sig, 0.001)

    def test_parsers(self):
        f = NOW
        q = parse_coinbase({"price": "82990.33", "time": "2026-10-10T22:35:39.282173995Z"}, f)
        self.assertEqual(q.price, 82990.33)
        self.assertEqual(q.observed_at.second, 39)
        self.assertEqual(parse_kraken({"error": [], "result": {"XXBTZUSD": {"c": ["82991.1", "0.01"]}}}, f).price,
                         82991.1)
        self.assertEqual(parse_bitstamp({"last": "82989.0", "timestamp": "1791671739"}, f).price, 82989.0)


class FakeSpot:
    def __init__(self, price, sigma=0.00008):
        self.price, self.sig = price, sigma

    def quotes(self, asset):
        now = datetime.now(timezone.utc)
        return [SpotQuote(src, self.price * (1 + i * 1e-5), now, now)
                for i, src in enumerate(("coinbase", "kraken", "bitstamp"))]

    def sigma(self, asset):
        return self.sig


def btc_market(seconds_left=420, strike=82974.2):
    return Market.from_api({
        "ticker": "KXBTC15M-26OCT101845-45", "event_ticker": "KXBTC15M-26OCT101845",
        "title": "BTC price up in next 15 mins?", "yes_sub_title": f"Target Price: ${strike:,.2f}",
        "status": "active", "close_time": iso(datetime.now(timezone.utc) + timedelta(seconds=seconds_left)),
        "strike_type": "greater_or_equal", "floor_strike": strike, "volume_24h_fp": "187814.81",
    }, "KXBTC15M")


def book(yes_bid=51, yes_ask=52, depth=300):
    return Book("KXBTC15M-26OCT101845-45", yes_bids=[(Decimal(yes_bid) / 100, Decimal(depth))],
                no_bids=[(Decimal(100 - yes_ask) / 100, Decimal(depth))])


class TestCryptoEdge(unittest.TestCase):
    def test_proposes_when_spot_far_above_strike_and_market_lags(self):
        s = h.settings(market_focus="crypto_15m", edge_min_confidence=50)
        ef = EdgeFinder(s, None, MarketHistory(), FakeSpot(83150.0))       # +0.21% above strike, 7 min left
        res = ef.evaluate(Candidate(btc_market(), book(), "", asset="BTC"))
        self.assertIsNotNone(res.proposal, res.reason)
        p = res.proposal
        self.assertEqual(p.side, "yes")
        self.assertGreater(p.fair_probability, 0.75)
        self.assertEqual({src.name for src in p.sources}, {"coinbase", "kraken", "bitstamp"})
        self.assertEqual(p.verdicts[0].data_snapshot["crypto"]["asset"], "BTC")

    def test_passes_when_market_is_fair(self):
        s = h.settings(market_focus="crypto_15m")
        ef = EdgeFinder(s, None, MarketHistory(), FakeSpot(82974.2))       # at the strike: ~50%
        res = ef.evaluate(Candidate(btc_market(), book(49, 51), "", asset="BTC"))
        self.assertIsNone(res.proposal)

    def test_refuses_inside_averaging_window(self):
        s = h.settings(market_focus="crypto_15m")
        ef = EdgeFinder(s, None, MarketHistory(), FakeSpot(83500.0))
        res = ef.evaluate(Candidate(btc_market(seconds_left=40), book(), "", asset="BTC"))
        self.assertIsNone(res.proposal)
        self.assertIn("averaging window", res.reason)

    def test_scout_crypto_skips_markets_closing_soon(self):
        s = h.settings(market_focus="crypto_15m", crypto_series={"KXBTC15M": "BTC"})

        class Data:
            def __init__(self, secs):
                self.secs = secs

            def get_markets(self, series, status="open", max_pages=10):
                return [btc_market(seconds_left=self.secs)]

            def get_orderbook(self, ticker, depth=10):
                return book()

        self.assertEqual(len(Scout(s, Data(420), MarketHistory()).scan()), 1)
        self.assertEqual(len(Scout(s, Data(60), MarketHistory()).scan()), 0)


def pos(pnl_pct, bid=60, contracts=10):
    return PositionPnL("pid", "T", "", "yes", contracts, 50.0, bid, 5.0, None, pnl_pct * 5.0, pnl_pct, 0.5)


class TestExits(unittest.TestCase):
    def setUp(self):
        os.environ.pop("KILL_SWITCH", None)
        self.s = h.settings(risk_take_profit_pct=0.25, risk_stop_loss_pct=0.20)
        self.j = h.journal()
        self.risk = RiskManager(self.s, self.j, h.kill(self.s, self.j))

    def test_take_profit_at_and_above(self):
        self.assertEqual(self.risk.check_exits([pos(0.249)]), [])
        self.assertEqual(self.risk.check_exits([pos(0.25)])[0].kind, "TAKE_PROFIT")
        self.assertEqual(self.risk.check_exits([pos(0.60)])[0].kind, "TAKE_PROFIT")

    def test_stop_loss_at_and_below(self):
        self.assertEqual(self.risk.check_exits([pos(-0.199)]), [])
        self.assertEqual(self.risk.check_exits([pos(-0.20)])[0].kind, "STOP_LOSS")
        self.assertEqual(self.risk.check_exits([pos(-0.90, bid=5)])[0].kind, "STOP_LOSS")

    def test_no_exit_without_a_price(self):
        r = pos(0.5)
        r.bid_cents = None
        self.assertEqual(self.risk.check_exits([r]), [])

    def test_disabled(self):
        s = h.settings(risk_exits_enabled=False)
        self.assertEqual(RiskManager(s, self.j, h.kill(s, self.j)).check_exits([pos(0.9)]), [])

    def test_paper_close_books_pnl(self):
        p = h.open_position(self.j, "T-WIN", count=10, price=50)
        p.fees_paid_usd = 0.18
        self.j.save(p)
        ex = Execution(self.s, self.j, h.kill(self.s, self.j), None, None)
        out = ex.close_position(ExitOrder(p.proposal_id, "T-WIN", "yes", 10, 70, "TAKE_PROFIT", 1.6, 0.3, "tp"))
        self.assertEqual(out.status, Status.CLOSED)
        # 10 * (0.70 - 0.50) - 0.18 entry fees - ceil_cent(0.07*10*.7*.3)=0.15 exit fee
        self.assertAlmostEqual(out.realized_pnl_usd, 1.67, places=4)
        self.assertAlmostEqual(self.j.realized_pnl_total(), 1.67, places=4)
        self.assertEqual(self.j.open_positions(), [])

    def test_close_refuses_anything_but_exit_order(self):
        ex = Execution(self.s, self.j, h.kill(self.s, self.j), None, None)
        with self.assertRaises(TypeError):
            ex.close_position({"proposal_id": "x"})

    def test_orchestrator_exits_on_live_marks_only_when_trading(self):
        p = h.open_position(self.j, "T-RUN", count=10, price=50)
        orch = Orchestrator(self.s, scout=None, edge_finder=None, skeptic=None, risk=self.risk,
                            execution=Execution(self.s, self.j, h.kill(self.s, self.j), None, None),
                            monitor=None, journal=self.j)
        self.j.upsert_marks([{"ticker": "T-RUN", "yes_bid": 80, "yes_ask": 82, "last_price": 81,
                              "status": "closed", "result": ""}])
        self.assertEqual(orch.run_exits(), 0)                 # market closed: stale bid, no exit
        self.j.upsert_marks([{"ticker": "T-RUN", "yes_bid": 80, "yes_ask": 82, "last_price": 81,
                              "status": "active", "result": ""}])
        self.assertEqual(orch.run_exits(), 1)
        self.assertEqual(self.j.get(p.proposal_id).status, Status.CLOSED)
        msgs = {r["agent"]: r["last_message"] for r in self.j.agent_statuses()}
        self.assertIn("take profit", msgs["risk_manager"])


if __name__ == "__main__":
    unittest.main()


class TestStopLossBaseline(unittest.TestCase):
    def test_fresh_cheap_position_does_not_stop_out(self):
        from kalshi.pnl import position_pnl
        s = h.settings(risk_take_profit_pct=0.25, risk_stop_loss_pct=0.20)
        j = h.journal()
        risk = RiskManager(s, j, h.kill(s, j))
        p = h.proposal(side="yes", price=33, count=2)
        p.filled_count, p.avg_fill_price_cents, p.fees_paid_usd = 2, 33.0, 0.02
        p.entry_bid_cents = 29.0                     # 4c spread at entry
        fresh = position_pnl(p, {"yes_bid": 29, "yes_ask": 33}, s)
        self.assertLess(fresh.pnl_pct, -0.15)          # already ~-20% on paper
        self.assertEqual(risk.check_exits([fresh]), [])
        dropped = position_pnl(p, {"yes_bid": 21, "yes_ask": 24}, s)
        self.assertEqual(risk.check_exits([dropped])[0].kind, "STOP_LOSS")
        up = position_pnl(p, {"yes_bid": 45, "yes_ask": 47}, s)
        self.assertEqual(risk.check_exits([up])[0].kind, "TAKE_PROFIT")

    def test_paper_fill_records_entry_bid(self):
        j = h.journal()
        s = h.settings()
        p = h.fully_approved(side="yes", price=45, count=5)
        p.verdicts[0].data_snapshot["book"] = {"yes_bid": 41, "yes_ask": 45}
        out = Execution(s, j, h.kill(s, j), None, None).execute(p)
        self.assertEqual(out.entry_bid_cents, 41.0)
        q = h.fully_approved(side="no", price=57, count=5)
        q.verdicts[0].data_snapshot["book"] = {"yes_bid": 41, "yes_ask": 45}
        self.assertEqual(Execution(s, j, h.kill(s, j), None, None).execute(q).entry_bid_cents, 55.0)
