"""End-to-end paper cycle with real Scout, Edge Finder, Skeptic, Risk, Execution,
Monitor and orchestrator. Only the Kalshi and Odds API network calls are faked."""

import os
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from kalshi.agents.edge_finder import EdgeFinder
from kalshi.agents.execution import Execution
from kalshi.agents.monitor import Monitor
from kalshi.agents.risk_manager import RiskManager
from kalshi.agents.scout import Scout
from kalshi.agents.skeptic import Skeptic
from kalshi.client import Market
from kalshi.history import MarketHistory
from kalshi.odds_api import parse_odds_events
from kalshi.orchestrator import Orchestrator
from kalshi.orderbook import Book
from kalshi.proposal import Status

from tests import helpers as h


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeData:
    def __init__(self, now):
        self.now = now
        self.settled = False
        self.http = type("H", (), {"authenticated": False})()

    def _market(self):
        return Market.from_api({
            "ticker": "KXNFLGAME-26OCT11DALNYG-DAL", "event_ticker": "KXNFLGAME-26OCT11DALNYG",
            "title": "Dallas at New York G Winner?", "yes_sub_title": "Dallas",
            "status": "finalized" if self.settled else "active",
            "close_time": iso(self.now + timedelta(days=1)),
            "occurrence_datetime": iso(self.now + timedelta(hours=23)),
            "volume_24h_fp": "15000.00", "result": "yes" if self.settled else "",
        }, "KXNFLGAME")

    def get_markets(self, series, status="open"):
        return [self._market()] if series == "KXNFLGAME" else []

    def get_market(self, ticker):
        return self._market()

    def get_orderbook(self, ticker, depth=10):
        # YES bid 54c, YES ask 56c (NO bid 44c), 300 deep each side.
        return Book(ticker, yes_bids=[(Decimal("0.54"), Decimal(300))], no_bids=[(Decimal("0.44"), Decimal(300))])


class FakeOdds:
    def __init__(self, now):
        self.now = now

    def events(self, sport_key):
        if sport_key != "americanfootball_nfl":
            return []
        payload = [{
            "id": "ev1", "commence_time": iso(self.now + timedelta(hours=23)),
            "home_team": "New York Giants", "away_team": "Dallas Cowboys",
            "bookmakers": [
                {"key": b, "last_update": iso(self.now - timedelta(minutes=1)), "markets": [
                    {"key": "h2h", "outcomes": [{"name": "New York Giants", "price": 170},
                                                {"name": "Dallas Cowboys", "price": -200}]}]}
                for b in ("pinnacle", "fanduel", "draftkings")]}]
        return parse_odds_events(payload, sport_key, fetched_at=datetime.now(timezone.utc))


class TestPaperPipeline(unittest.TestCase):
    def test_one_full_cycle_then_settlement(self):
        os.environ.pop("KILL_SWITCH", None)
        now = datetime.now(timezone.utc)
        s = h.settings(edge_min_confidence=50, risk_stake_floor_usd=10.0, monitor_interval_s=0)
        j = h.journal()
        kill = h.kill(s, j)
        hist = MarketHistory()
        data = FakeData(now)
        orch = Orchestrator(
            s, scout=Scout(s, data, hist), edge_finder=EdgeFinder(s, FakeOdds(now), hist),
            skeptic=Skeptic(s, hist, j), risk=RiskManager(s, j, kill),
            execution=Execution(s, j, kill, data, None), monitor=Monitor(s, j, data), journal=j)

        stats = orch.run_cycle()
        self.assertEqual(stats.candidates, 1)
        self.assertEqual(stats.executed, 1, stats)
        p = j.get(j.rows()[0]["proposal_id"])
        # devigged Dallas ~0.638 vs 56c ask + 1c cap + 2c fee -> ~4.8c edge on YES
        self.assertEqual(p.side, "yes")
        self.assertEqual(p.status, Status.PAPER_FILLED)
        self.assertGreater(p.edge_cents, 3.0)
        self.assertEqual(p.contract_count, 16)          # floor($10 / $0.59)
        self.assertLess(p.contract_count, p.original_count)

        # Second cycle: already holding this market, so Risk rejects the repeat.
        stats2 = orch.run_cycle()
        self.assertEqual(stats2.rejected_by.get("risk_manager"), 1)

        # Market settles YES; Monitor books the P&L.
        data.settled = True
        orch.run_monitor()
        p = j.get(p.proposal_id)
        self.assertEqual(p.status, Status.SETTLED)
        expected = 16 * 1.0 - 16 * 0.56 - p.fees_paid_usd
        self.assertAlmostEqual(p.realized_pnl_usd, expected, places=4)
        self.assertAlmostEqual(j.realized_pnl_total(), expected, places=4)


if __name__ == "__main__":
    unittest.main()
