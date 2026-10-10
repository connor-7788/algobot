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
    def __init__(self, now, kickoff):
        self.now = now
        self.kickoff = kickoff
        self.settled = False
        self.http = type("H", (), {"authenticated": False})()

    # Same shape as live KXNFLGAME data (Oct 10, 2026): one market per team,
    # titled "<Team> wins", occurrence_datetime ~3h after kickoff.
    TEAMS = {"DAL": ("Dallas", "yes"), "NYG": ("New York G", "no")}   # code -> (name, settled result)

    def _market(self, code):
        name, result = self.TEAMS[code]
        return Market.from_api({
            "ticker": f"KXNFLGAME-26OCT11DALNYG-{code}", "event_ticker": "KXNFLGAME-26OCT11DALNYG",
            "title": f"{name} wins", "yes_sub_title": name, "no_sub_title": name,
            "status": "finalized" if self.settled else "active",
            "close_time": iso(self.now + timedelta(days=3)),
            "occurrence_datetime": iso(self.kickoff + timedelta(hours=3)),
            "volume_24h_fp": "15000.00", "result": result if self.settled else "",
        }, "KXNFLGAME")

    def get_markets(self, series, status="open"):
        return [self._market(c) for c in self.TEAMS] if series == "KXNFLGAME" else []

    def get_market(self, ticker):
        return self._market(ticker.rsplit("-", 1)[1])

    def get_orderbook(self, ticker, depth=10):
        # DAL: YES bid 54c / ask 56c. NYG mirrors it: YES bid 44c / ask 46c. 300 deep.
        if ticker.endswith("DAL"):
            return Book(ticker, yes_bids=[(Decimal("0.54"), Decimal(300))], no_bids=[(Decimal("0.44"), Decimal(300))])
        return Book(ticker, yes_bids=[(Decimal("0.44"), Decimal(300))], no_bids=[(Decimal("0.54"), Decimal(300))])


class FakeOdds:
    def __init__(self, now, kickoff):
        self.now = now
        self.kickoff = kickoff

    def events(self, sport_key):
        if sport_key != "americanfootball_nfl":
            return []
        payload = [{
            "id": "ev1", "commence_time": iso(self.kickoff),
            "home_team": "New York Giants", "away_team": "Dallas Cowboys",
            "bookmakers": [
                {"key": b, "last_update": iso(self.now - timedelta(minutes=1)), "markets": [
                    {"key": "h2h", "outcomes": [{"name": "New York Giants", "price": 170},
                                                {"name": "Dallas Cowboys", "price": -200}]}]}
                for b in ("pinnacle", "fanduel", "draftkings")]}]
        return parse_odds_events(payload, sport_key, fetched_at=datetime.now(timezone.utc))


def build(kickoff_in_h: float, **overrides):
    os.environ.pop("KILL_SWITCH", None)
    now = datetime.now(timezone.utc)
    kickoff = now + timedelta(hours=kickoff_in_h)
    s = h.settings(edge_min_confidence=50, risk_stake_floor_usd=10.0, monitor_interval_s=0, **overrides)
    j = h.journal()
    kill = h.kill(s, j)
    hist = MarketHistory()
    data = FakeData(now, kickoff)
    edge = EdgeFinder(s, FakeOdds(now, kickoff), hist)
    orch = Orchestrator(
        s, scout=Scout(s, data, hist), edge_finder=edge,
        skeptic=Skeptic(s, hist, j), risk=RiskManager(s, j, kill),
        execution=Execution(s, j, kill, data, None), monitor=Monitor(s, j, data), journal=j)
    return orch, j, data


class TestGamePhase(unittest.TestCase):
    def test_live_only_ignores_games_hours_away(self):
        orch, j, _ = build(kickoff_in_h=23)
        stats = orch.run_cycle()
        self.assertEqual(stats.candidates, 0)          # Scout's live window drops it
        self.assertEqual(j.rows(), [])

    def test_live_only_passes_on_game_starting_soon(self):
        orch, j, _ = build(kickoff_in_h=1)             # inside Scout's window, not started
        stats = orch.run_cycle()
        self.assertEqual(stats.candidates, 2)
        self.assertEqual(stats.proposals, 0)
        msg = {r["agent"]: r for r in j.agent_statuses()}["edge_finder"]["last_message"]
        self.assertIn("not live yet", msg)

    def test_live_only_skips_finished_games(self):
        orch, j, _ = build(kickoff_in_h=-5)
        self.assertEqual(orch.run_cycle().proposals, 0)

    def test_pregame_mode_still_available(self):
        orch, j, _ = build(kickoff_in_h=23, edge_game_phase="pregame_only")
        self.assertEqual(orch.run_cycle().executed, 2)


class TestPaperPipeline(unittest.TestCase):
    def test_one_full_cycle_then_settlement(self):
        orch, j, data = build(kickoff_in_h=-1)         # live: kicked off an hour ago

        stats = orch.run_cycle()
        self.assertEqual(stats.candidates, 2)
        self.assertEqual(stats.executed, 2, stats)
        props = {p.market_ticker[-3:]: p for p in (j.get(r["proposal_id"]) for r in j.rows())}
        # Devigged Dallas ~0.638. DAL YES at 56c and NYG NO at 56c are the same bet:
        # 63.8 - (56 + 1c cap) - 2c fee ~ 4.8c edge each.
        self.assertEqual((props["DAL"].side, props["NYG"].side), ("yes", "no"))
        for p in props.values():
            self.assertEqual(p.status, Status.PAPER_FILLED)
            self.assertGreater(p.edge_cents, 3.0)
            self.assertEqual(p.contract_count, 16)          # floor($10 / $0.59)
            self.assertLess(p.contract_count, p.original_count)
            self.assertEqual(p.verdicts[0].data_snapshot["matched_event"]["yes_team"],
                             "Dallas Cowboys" if p.market_ticker.endswith("DAL") else "New York Giants")

        # Second cycle: already holding both markets, so Risk rejects the repeats.
        stats2 = orch.run_cycle()
        self.assertEqual(stats2.rejected_by.get("risk_manager"), 2)

        # Dallas wins: DAL settles YES, NYG settles NO. Both positions pay.
        data.settled = True
        orch.run_monitor()
        total = 0.0
        for p in props.values():
            p = j.get(p.proposal_id)
            self.assertEqual(p.status, Status.SETTLED)
            expected = 16 * 1.0 - 16 * 0.56 - p.fees_paid_usd
            self.assertAlmostEqual(p.realized_pnl_usd, expected, places=4)
            total += expected
        self.assertAlmostEqual(j.realized_pnl_total(), total, places=4)


if __name__ == "__main__":
    unittest.main()
