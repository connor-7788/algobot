"""Learner (self-improvement), richer agent reasoning, losing-run cool-down, why-no-trades tally."""

import math
import random
import unittest
from datetime import timedelta

from kalshi.agents.base import Candidate
from kalshi.agents.edge_finder import EdgeFinder
from kalshi.agents.risk_manager import RiskManager
from kalshi.history import MarketHistory
from kalshi.learner import PAUSED, LearnedParams, Learner, ParamStore, blend, log_loss, raw_z
from kalshi.orchestrator import pass_category
from kalshi.proposal import Decision
from kalshi.spot import norm_cdf

from tests import helpers as h
from tests.test_crypto_exits import FakeSpot, book, btc_market

T0 = 1_791_600_000


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def synth(j, *, n_markets, model_is_truth, seed=1):
    """Write labelled observations. Each market: 12 looks at 13..2 minutes left.
    If model_is_truth, outcomes follow the model and Kalshi's price is a lazy,
    over-cautious copy. Otherwise outcomes follow Kalshi's price and the model is noise."""
    rng = random.Random(seed)
    sigma = 0.0008 / math.sqrt(60)
    for k in range(n_markets):
        close = T0 + 900 * k
        strike = 100.0
        drift = rng.gauss(0, 0.0015)
        mid0 = min(0.9, max(0.1, rng.gauss(0.5, 0.2)))
        rows = []
        for mins in range(13, 1, -1):
            secs = mins * 60.0
            spot = strike * math.exp(drift * (13 - mins) / 11 + rng.gauss(0, 0.0003))
            z = raw_z(spot, strike, secs, sigma, "greater_or_equal")
            p_true_model = norm_cdf(z / 0.8)
            if model_is_truth:
                p_true = p_true_model
                mid = 0.5 + (p_true - 0.5) * 0.45           # market under-reacts
            else:
                mid = min(0.95, max(0.05, mid0 + rng.gauss(0, 0.01)))
                p_true = mid0
            mid_c = int(round(100 * mid))
            bid, ask = max(1, mid_c - 1), min(99, mid_c + 1)
            rows.append((secs, spot, bid, ask, p_true))
        y = 1 if rng.random() < rows[-1][4] else 0
        for secs, spot, bid, ask, _ in rows:
            j.add_observation(ticker=f"KXBTC15M-T{k}", series="KXBTC15M", close_ts=close, secs_left=secs,
                              spot=spot, strike=strike, strike_type="greater_or_equal", sigma=sigma,
                              bid=bid, ask=ask)
        j.label_observations(f"KXBTC15M-T{k}", y)
    return T0 + 900 * n_markets


def learner_settings(**kw):
    base = dict(market_focus="crypto_15m", learner_min_observations=600, learner_min_trades=20,
                learner_window_h=10_000, learner_min_t=0.5)
    base.update(kw)
    return h.settings(**base)


class TestObserveAndLabel(unittest.TestCase):
    def test_one_observation_per_market_minute(self):
        s, j = learner_settings(), h.journal()
        lr = Learner(s, j, None, ParamStore(s), clock=Clock(T0))
        kw = dict(ticker="A", series="KXBTC15M", close_ts=T0, spot=1, strike=1, strike_type="greater_or_equal",
                  sigma=1e-4, bid=50, ask=52)
        self.assertTrue(lr.observe(secs_left=430, **kw))
        self.assertFalse(lr.observe(secs_left=425, **kw))      # same minute bucket
        self.assertTrue(lr.observe(secs_left=350, **kw))
        self.assertEqual(j.observation_counts()["total"], 2)

    def test_labels_from_settled_markets(self):
        s, j = learner_settings(), h.journal()

        class M:
            def __init__(self, t, r):
                self.ticker, self.result = t, r

        class Data:
            def get_markets_by_tickers(self, tickers):
                return [M(t, "yes" if t == "A" else "") for t in tickers]

        for t in ("A", "B"):
            j.add_observation(ticker=t, series="X", close_ts=T0, secs_left=300, spot=1, strike=1,
                              strike_type="greater_or_equal", sigma=1e-4, bid=50, ask=52)
        lr = Learner(s, j, Data(), ParamStore(s), clock=Clock(T0 + 600))
        self.assertEqual(lr.label_pending(), 1)
        self.assertEqual([r["y"] for r in j.labeled_observations()], [1])
        self.assertEqual(j.unlabeled_tickers(T0 + 600), ["B"])


class TestRefit(unittest.TestCase):
    def test_waits_for_enough_data(self):
        s, j = learner_settings(learner_min_observations=10_000), h.journal()
        end = synth(j, n_markets=50, model_is_truth=True)
        store = ParamStore(s)
        report = Learner(s, j, None, store, clock=Clock(end)).refit()
        self.assertIn("waiting for data", report)
        self.assertEqual(store.get().version, 0)

    def test_learns_to_trust_model_when_market_lags(self):
        s, j = learner_settings(), h.journal()
        end = synth(j, n_markets=400, model_is_truth=True)
        store = ParamStore(s)
        report = Learner(s, j, None, store, clock=Clock(end)).refit()
        p = store.get()
        self.assertGreater(p.model_weight, s.crypto_model_weight, report)
        self.assertFalse(p.paused, report)
        self.assertEqual(p.version, 1)
        self.assertLess(p.metrics["valid_logloss_new"], p.metrics["valid_logloss_market"])
        self.assertIn("calibration updated", report)
        # Persisted: a restarted engine picks it up.
        self.assertEqual(ParamStore(s, j).get().model_weight, p.model_weight)

    def test_pauses_when_model_has_no_edge(self):
        s, j = learner_settings(), h.journal()
        end = synth(j, n_markets=400, model_is_truth=False)
        store = ParamStore(s)
        report = Learner(s, j, None, store, clock=Clock(end)).refit()
        p = store.get()
        self.assertTrue(p.paused, report)
        self.assertLessEqual(p.model_weight, s.crypto_model_weight + 1e-9)
        self.assertIn("paused", report)

    def test_math_helpers(self):
        self.assertAlmostEqual(blend(0.8, 0.4, 1.0), 0.8, places=6)
        self.assertAlmostEqual(blend(0.8, 0.4, 0.0), 0.4, places=6)
        self.assertLess(log_loss([0.9], [1]), log_loss([0.6], [1]))
        self.assertIsNone(raw_z(100, 100, 30, 1e-4, "greater"))
        self.assertGreater(raw_z(101, 100, 300, 1e-4, "less"), -1e9)
        self.assertLess(raw_z(101, 100, 300, 1e-4, "less"), 0)


class TestEdgeFinderUsesLearnedParams(unittest.TestCase):
    def test_paused_means_pass_with_explanation(self):
        s = h.settings(market_focus="crypto_15m", edge_min_confidence=0)
        store = ParamStore(s)
        store.set(LearnedParams(0.8, 0.25, PAUSED, version=3, source="learned"))
        ef = EdgeFinder(s, None, MarketHistory(), FakeSpot(83150.0), params=store)
        res = ef.evaluate(Candidate(btc_market(), book(), "", asset="BTC"))
        self.assertIsNone(res.proposal)
        self.assertIn("Learner has paused", res.reason)
        self.assertEqual(pass_category(res.reason), "Learner paused entries (no proven edge)")

    def test_reason_shows_the_numbers(self):
        s = h.settings(market_focus="crypto_15m", edge_min_confidence=50)
        ef = EdgeFinder(s, None, MarketHistory(), FakeSpot(83150.0), params=ParamStore(s))
        res = ef.evaluate(Candidate(btc_market(), book(), "", asset="BTC"))
        self.assertIsNotNone(res.proposal, res.reason)
        why = res.proposal.verdicts[0].reason
        for bit in ("above the", "min left", "model says YES", "Kalshi mid", "fee", "edge +", "need +3c"):
            self.assertIn(bit, why)

    def test_records_observations(self):
        s, j = h.settings(market_focus="crypto_15m"), h.journal()
        store = ParamStore(s)
        ef = EdgeFinder(s, None, MarketHistory(), FakeSpot(82974.2), params=store,
                        learner=Learner(s, j, None, store))
        ef.evaluate(Candidate(btc_market(), book(49, 51), "", asset="BTC"))
        self.assertEqual(j.observation_counts()["total"], 1)


class TestCooldown(unittest.TestCase):
    def test_losing_run_halves_stake_and_says_why(self):
        s = h.settings(risk_stake_floor_usd=10.0, risk_cooldown_lookback=4, risk_cooldown_factor=0.5)
        j = h.journal()
        risk = RiskManager(s, j, h.kill(s, j), now=lambda: h.NOW)
        self.assertEqual(risk.stake_usd(), 10.0)
        for i in range(4):
            h.settled(j, -1.0 if i else 0.5, when=h.NOW - timedelta(days=2, minutes=i))
        self.assertEqual(risk.stake_usd(), 5.0)
        d = risk.review(h.proposal(count=500, price=45))
        self.assertEqual(d.decision, Decision.MODIFY)
        self.assertIn("stake cut to 50%", d.reason)
        self.assertIn("positions open", d.reason)

    def test_winning_run_keeps_full_stake(self):
        s = h.settings(risk_stake_floor_usd=10.0, risk_cooldown_lookback=4)
        j = h.journal()
        for i in range(4):
            h.settled(j, 1.0, when=h.NOW - timedelta(days=2, minutes=i))
        self.assertEqual(RiskManager(s, j, h.kill(s, j), now=lambda: h.NOW).stake_usd(), 10.0)


class TestPassCategory(unittest.TestCase):
    def test_buckets(self):
        self.assertEqual(pass_category("edge -5.81c < 3c. ETH ..."), "edge too small after spread + fees")
        self.assertEqual(pass_category("edge -5.81c < 3c; confidence 56 < 60. ETH"),
                         "edge too small and low confidence")
        self.assertEqual(pass_category("both sides priced outside 30-97c (YES ask 99)"), "price outside 30-97c band")
        self.assertEqual(pass_category("40s left: inside the averaging window"), "too close to settlement")


if __name__ == "__main__":
    unittest.main()


class TestBackfill(unittest.TestCase):
    def test_rebuilds_labelled_observations_from_history(self):
        from datetime import datetime, timezone
        from kalshi import backfill
        from kalshi.client import Market

        now = datetime.fromtimestamp(T0 + 7200, timezone.utc)
        close = T0 + 3600
        open_ = close - 900

        def mkt(ticker, result, c):
            return Market.from_api({
                "ticker": ticker, "event_ticker": ticker[:-3], "title": "BTC up?", "status": "finalized",
                "result": result, "close_time": datetime.fromtimestamp(c, timezone.utc).isoformat(),
                "open_time": datetime.fromtimestamp(c - 900, timezone.utc).isoformat(),
                "strike_type": "greater_or_equal", "floor_strike": 100.0}, "KXBTC15M")

        class Data:
            calls = []

            def get_markets(self, series, status="open", max_pages=10):
                return [mkt("KXBTC15M-A-00", "yes", close), mkt("KXBTC15M-B-00", "no", close - 900),
                        mkt("KXBTC15M-OLD", "yes", T0 - 500 * 3600)]

            def get_candlesticks(self, tickers, start, end, interval_min=1):
                self.calls.append(tickers)
                out = {}
                for t in tickers:
                    c = close if t.endswith("A-00") else close - 900
                    out[t] = [{"end_period_ts": c - 60 * k, "yes_bid": {"close_dollars": "0.55"},
                               "yes_ask": {"close_dollars": "0.57"}} for k in range(1, 15)]
                return out

        class Resp:
            def __init__(self, rows):
                self.rows = rows

            def raise_for_status(self):
                pass

            def json(self):
                return self.rows

        class Sess:
            def get(self, url, params=None, **kw):
                st = int(datetime.fromisoformat(params["start"]).timestamp())
                en = int(datetime.fromisoformat(params["end"]).timestamp())
                assert (en - st) // 60 + 1 <= 300
                return Resp([[t, 0, 0, 0, 100 * (1 + 0.0005 * math.sin(t / 60)), 0]
                             for t in range(en - en % 60, st - 1, -60)])

        s = h.settings(market_focus="crypto_15m", crypto_series={"KXBTC15M": "BTC"}, learner_backfill_h=72)
        j = h.journal()
        from kalshi.agents.shadow import Shadow
        rep = backfill.run(s, j, Data(), session=Sess(), now=now, pause_s=0, shadow=Shadow(s, j))
        self.assertEqual(rep["errors"], [])
        self.assertGreaterEqual(rep["shadow_trades"], 4)              # favorite + underdog on both markets
        settled = j.shadow_rows()
        self.assertEqual(len(settled), rep["shadow_trades"])
        self.assertTrue(all(r["postmortem"] for r in settled))
        self.assertTrue(all(r["feats"] for r in settled))
        self.assertEqual(rep["markets"], 2)
        rows = j.labeled_observations()
        self.assertEqual(len(rows), rep["observations"])
        self.assertGreater(len(rows), 20)
        self.assertTrue(all(r["secs_left"] >= s.crypto_min_seconds_to_close for r in rows))
        self.assertEqual({r["y"] for r in rows if r["ticker"].endswith("A-00")}, {1})
        self.assertEqual({(r["bid"], r["ask"]) for r in rows}, {(55, 57)})
        # Second run skips what it already has.
        again = backfill.run(s, j, Data(), session=Sess(), now=now, pause_s=0)
        self.assertEqual((again["markets"], again["skipped"]), (0, 2))
