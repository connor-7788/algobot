"""Chart features, chart model learning, paper exploration, shadow trades + post-mortems, setup lessons."""

import math
import random
import unittest
from datetime import datetime, timezone

from kalshi.agents.base import Candidate
from kalshi.agents.edge_finder import EdgeFinder
from kalshi.agents.shadow import Shadow, lessons, postmortem, setup_key, side_feats
from kalshi.agents.skeptic import Skeptic
from kalshi.chart_model import ChartModel
from kalshi.features import FEATURES, chart_features
from kalshi.history import MarketHistory
from kalshi.learner import PAUSED, LearnedParams, Learner, ParamStore
from kalshi.proposal import Decision

from tests import helpers as h
from tests.test_crypto_exits import FakeSpot, book, btc_market
from tests.test_learner import Clock, T0

SIG = 0.0008 / math.sqrt(60)


def rising(n=80, start=100.0, step=0.0003, t_end=T0):
    return {t_end - 60 * (n - i): start * math.exp(step * i) for i in range(n)}


class TestFeatures(unittest.TestCase):
    def test_uptrend_points_toward_yes_and_flips_for_less(self):
        closes = rising()
        last = max(closes)
        kw = dict(closes=closes, now_ts=last + 60, spot=closes[last], strike=closes[last] * 0.999,
                  secs_left=400, sigma=SIG, bid=60, ask=62)
        up = chart_features(strike_type="greater_or_equal", **kw)
        down = chart_features(strike_type="less", **kw)
        self.assertGreater(up["mom5"], 0)
        self.assertGreater(up["z"], 0)
        self.assertGreater(up["range_pos"], 0)
        self.assertAlmostEqual(up["mom5"], -down["mom5"], places=6)
        self.assertEqual(set(FEATURES) - set(up), set())

    def test_refuses_bad_inputs(self):
        self.assertIsNone(chart_features(closes={}, now_ts=T0, spot=100, strike=100, secs_left=30, sigma=SIG,
                                         strike_type="greater", bid=50, ask=52))

    def test_side_feats_flip(self):
        f = {"z": 1.0, "mom5": 0.5, "mkt_logit": 0.2}
        self.assertEqual(side_feats(f, "no")["mom5"], -0.5)
        self.assertEqual(side_feats(f, "no")["mkt_logit"], 0.2)


def momentum_world(j, n_markets, seed=3, momentum_matters=True):
    """Markets where 5-minute momentum carries information Kalshi's price ignores."""
    rng = random.Random(seed)
    for k in range(n_markets):
        close = T0 + 900 * k
        mid0 = min(0.85, max(0.15, rng.gauss(0.5, 0.15)))
        rows = []
        for mins in range(13, 1, -1):
            f = {x: rng.gauss(0, 1) for x in FEATURES}
            mid = min(0.95, max(0.05, mid0 + rng.gauss(0, 0.02)))
            f["mkt_logit"] = math.log(mid / (1 - mid))
            rows.append((mins * 60.0, f, mid))
        # Outcome decided by the market price plus (optionally) momentum at the 8-minute mark.
        f8 = rows[5][1]
        x = f8["mkt_logit"] + (1.6 * f8["mom5"] if momentum_matters else 0.0)
        y = 1 if rng.random() < 1 / (1 + math.exp(-x)) else 0
        for secs, f, mid in rows:
            if momentum_matters:
                f["mom5"] = f8["mom5"]
            c = int(round(mid * 100))
            j.add_observation(ticker=f"KXBTC15M-M{k}", series="KXBTC15M", close_ts=close, secs_left=secs,
                              spot=100.0, strike=100.0, strike_type="greater_or_equal", sigma=SIG,
                              bid=max(1, c - 1), ask=min(99, c + 1), y=y, feats=f)
    return T0 + 900 * n_markets


def settings(**kw):
    base = dict(market_focus="crypto_15m", learner_min_observations=600, learner_min_trades=20,
                learner_window_h=10_000, learner_min_t=0.5)
    base.update(kw)
    return h.settings(**base)


class TestChartLearning(unittest.TestCase):
    def test_adopts_chart_model_when_chart_has_information(self):
        s, j = settings(), h.journal()
        end = momentum_world(j, 500)
        store = ParamStore(s)
        report = Learner(s, j, None, store, clock=Clock(end)).refit()
        p = store.get()
        self.assertEqual(p.mode, "chart", report)
        self.assertIn("ADOPTED", report)
        self.assertTrue(any("5-minute moves tend to continue" in x for x in p.lessons), p.lessons)
        self.assertFalse(p.paused, report)

    def test_keeps_market_when_chart_is_noise(self):
        s, j = settings(), h.journal()
        end = momentum_world(j, 500, momentum_matters=False)
        store = ParamStore(s)
        report = Learner(s, j, None, store, clock=Clock(end)).refit()
        p = store.get()
        self.assertEqual(p.mode, "blend", report)
        self.assertIn("not adopted", report)
        self.assertTrue(p.chart, "a chart model is still kept for paper exploration")


class ChartSpot(FakeSpot):
    def closes(self, asset):
        now = int(datetime.now(timezone.utc).timestamp()) // 60 * 60
        return {now - 60 * k: self.price * (1 - 0.0004 * k) for k in range(1, 70)}     # steady climb into now


class TestExploration(unittest.TestCase):
    def _chart(self, w_mom=2.0):
        coef = [1.0 if k == "mkt_logit" else (w_mom if k == "mom5" else 0.0) for k in FEATURES]
        return ChartModel(coef, 0.0, [0.0] * len(FEATURES), [1.0] * len(FEATURES)).to_dict()

    def test_paper_explores_on_chart_when_paused(self):
        s = h.settings(market_focus="crypto_15m", paper_mode=True, edge_min_confidence=99)
        store = ParamStore(s)
        store.set(LearnedParams(0.8, 0.0, PAUSED, version=2, mode="blend", chart=self._chart()))
        ef = EdgeFinder(s, None, MarketHistory(), ChartSpot(82990.0), params=store)
        res = ef.evaluate(Candidate(btc_market(), book(51, 52), "", asset="BTC"))
        self.assertIsNotNone(res.proposal, res.reason)
        why = res.proposal.verdicts[0].reason
        self.assertIn("EXPLORE (paper only", why)
        self.assertIn("Chart model says YES", why)
        self.assertIn("momentum", why)
        snap = res.proposal.verdicts[0].data_snapshot["crypto"]
        self.assertEqual(snap["mode"], "explore-chart")
        self.assertTrue(snap["exploring"])

    def test_live_mode_never_explores(self):
        s = h.settings(market_focus="crypto_15m", paper_mode=False)
        store = ParamStore(s)
        store.set(LearnedParams(0.8, 0.0, PAUSED, version=2, chart=self._chart()))
        ef = EdgeFinder(s, None, MarketHistory(), ChartSpot(82990.0), params=store)
        res = ef.evaluate(Candidate(btc_market(), book(51, 52), "", asset="BTC"))
        self.assertIsNone(res.proposal)
        self.assertIn("Learner has paused", res.reason)


class TestShadow(unittest.TestCase):
    def setUp(self):
        self.s = h.settings(market_focus="crypto_15m")
        self.j = h.journal()
        self.sh = Shadow(self.s, self.j)
        self.feats = {k: 0.0 for k in FEATURES}
        self.feats.update(z=0.8, mom5=1.2, mkt_logit=0.4)

    def open(self, **kw):
        args = dict(ticker="KXBTC15M-X-00", series="KXBTC15M", close_ts=T0, secs_left=480, spot=100.05,
                    strike=100.0, strike_type="greater_or_equal", bid=58, ask=60, feats=self.feats, p_chart=0.7)
        args.update(kw)
        return self.sh.consider(**args)

    def test_one_trade_per_strategy_per_market(self):
        self.assertEqual(self.open(), 5)
        self.assertEqual(self.open(secs_left=420), 0)
        rows = {r["strategy"]: r for r in self.j.shadow_rows(settled_only=False)}
        self.assertEqual(rows["momentum"]["side"], "yes")
        self.assertEqual(rows["fade"]["side"], "no")
        self.assertEqual(rows["favorite"]["side"], "yes")
        self.assertEqual(rows["underdog"]["side"], "no")
        self.assertEqual(rows["underdog"]["entry_cents"], 42)

    def test_waits_for_entry_window(self):
        self.assertEqual(self.open(secs_left=800), 0)
        self.assertEqual(self.open(secs_left=100), 0)

    def test_postmortem_explains_reversal(self):
        self.open()
        self.j.add_observation(ticker="KXBTC15M-X-00", series="KXBTC15M", close_ts=T0, secs_left=120, spot=100.02,
                               strike=100.0, strike_type="greater_or_equal", sigma=SIG, bid=70, ask=72)

        class M:
            ticker, result, expiration_value = "KXBTC15M-X-00", "no", 99.97

        class Data:
            def get_markets_by_tickers(self, tickers):
                return [M()]

        sh = Shadow(self.s, self.j, Data(), clock=lambda: T0 + 120)
        done = {d["strategy"]: d for d in sh.settle()}
        mom = done["momentum"]
        self.assertEqual(mom["won"], 0)
        self.assertAlmostEqual(mom["pnl"], -60 - 2.0, delta=0.5)
        self.assertIn("LOST", mom["text"])
        self.assertIn("reversed through the strike", mom["text"])
        self.assertIn("still on our side", mom["text"])
        self.assertIn("Setup: ahead of strike / momentum with / 60-79c", mom["text"])
        self.assertEqual(done["fade"]["won"], 1)
        self.assertIn("WON", done["fade"]["text"])

    def test_postmortem_pure(self):
        t = {"side": "yes", "entry_cents": 55, "fee_cents": 1.7, "strike_type": "greater_or_equal",
             "feats": {"mom5": -1.0, "z": -0.5}, "series": "KXETH15M", "spot": 99.9, "strike": 100.0,
             "secs_left": 500, "strategy": "underdog", "p_chart": 0.4, "p_market": 0.5}
        won, pnl, text = postmortem(t, "yes", 100.1, None)
        self.assertEqual(won, 1)
        self.assertIn("crossed to our side", text)
        self.assertIn("ETH", text)

    def test_lessons_and_skeptic_blocks_losing_setup(self):
        rng = random.Random(1)
        f = {k: 0.0 for k in FEATURES}
        f.update(z=-1.0, mom5=-1.0)                  # behind the strike, momentum against (for YES)
        for i in range(60):
            won = rng.random() < 0.25
            self.j.add_shadow({"ticker": f"T{i}", "series": "KXBTC15M", "strategy": "momentum", "side": "yes",
                               "entry_cents": 45, "fee_cents": 1.7, "secs_left": 400, "spot": 99.9, "strike": 100,
                               "strike_type": "greater_or_equal", "feats": f, "close_ts": T0, "won": int(won),
                               "pnl_cents": (55 if won else -45) - 1.7, "result": "yes" if won else "no"})
        self.assertIn("momentum", " ".join(lessons(self.j.shadow_rows(), min_n=30)))
        key = setup_key(f, "yes", 45)
        self.assertEqual(key, "behind strike / momentum against / 40-59c")

        p = h.proposal(ticker="KXBTC15M-NEW", event="KXBTC15M-NEW", price=45, series="KXBTC15M")
        p.verdicts[0].data_snapshot["crypto"] = {"feats": f, "exploring": True}
        p.book_fetched_at = h.NOW
        sk = Skeptic(self.s.with_overrides(skeptic_min_sources=1, skeptic_max_line_age_s=1e9,
                                           skeptic_max_data_age_s=1e9), MarketHistory(), self.j, now=lambda: h.NOW)
        d = sk.review(p)
        self.assertEqual(d.decision, Decision.REJECT, d.reason)
        self.assertIn("this setup keeps losing", d.reason)


if __name__ == "__main__":
    unittest.main()


class TestSelector(unittest.TestCase):
    def _rows(self, strategy, n, win_p, entry, seed, setup_feats=None, t0=T0):
        rng = random.Random(seed)
        f = setup_feats or {k: 0.0 for k in FEATURES}
        out = []
        for i in range(n):
            won = rng.random() < win_p
            out.append({"strategy": strategy, "side": "yes", "entry_cents": entry, "fee_cents": 1.5,
                        "pnl_cents": (100 - entry - 1.5) if won else (-entry - 1.5), "won": int(won),
                        "close_ts": t0 + 900 * i, "feats": f})
        return out

    def test_picks_best_return_per_dollar_and_validates(self):
        from kalshi import selector
        rows = (self._rows("underdog", 300, 0.40, 25, 1)        # +55% per $ expected
                + self._rows("favorite", 300, 0.90, 88, 2)      # wins more often, ~+0.6% per $
                + self._rows("momentum", 300, 0.50, 55, 3))     # loses
        best = selector.select(rows)
        self.assertEqual(best["strategy"], "underdog")
        self.assertTrue(best["validated"])
        self.assertIn("underdog", selector.describe(best))
        self.assertGreater(best["train"]["roi"], 0.2)

    def test_not_validated_when_newer_trades_lose(self):
        from kalshi import selector
        rows = self._rows("fade", 210, 0.62, 50, 4) + self._rows("fade", 90, 0.30, 50, 5, t0=T0 + 900 * 210)
        best = selector.select(rows)
        self.assertEqual(best["strategy"], "fade")
        self.assertFalse(best["validated"])
        self.assertIn("NOT validated", selector.describe(best))


class TestFollowStrategy(unittest.TestCase):
    def _store(self, s, validated):
        store = ParamStore(s)
        rec = {"n": 100, "roi": 0.05, "cents": 2.0, "win_rate": 0.6, "t": 1.5, "se": 0.02}
        store.set(LearnedParams(0.8, 0.0, PAUSED, version=3, strategy={
            "strategy": "momentum", "setup": None, "validated": validated, "train": rec, "valid": rec}))
        return store

    def test_paper_follows_momentum_inside_entry_window(self):
        s = h.settings(market_focus="crypto_15m", paper_mode=True)
        ef = EdgeFinder(s, None, MarketHistory(), ChartSpot(82990.0), params=self._store(s, False))
        res = ef.evaluate(Candidate(btc_market(seconds_left=420), book(51, 52), "", asset="BTC"))
        self.assertIsNotNone(res.proposal, res.reason)
        p = res.proposal
        self.assertEqual(p.side, "yes")                      # ChartSpot climbs into now: momentum toward YES
        self.assertIn("FOLLOWING BEST STRATEGY (paper only", p.verdicts[0].reason)
        self.assertEqual(p.verdicts[0].data_snapshot["crypto"]["strategy"], "momentum")

    def test_waits_outside_window_and_live_needs_validation(self):
        s = h.settings(market_focus="crypto_15m", paper_mode=True)
        ef = EdgeFinder(s, None, MarketHistory(), ChartSpot(82990.0), params=self._store(s, False))
        res = ef.evaluate(Candidate(btc_market(seconds_left=800), book(51, 52), "", asset="BTC"))
        self.assertIsNone(res.proposal)
        self.assertIn("following momentum", res.reason)
        live = h.settings(market_focus="crypto_15m", paper_mode=False)
        ef = EdgeFinder(live, None, MarketHistory(), ChartSpot(82990.0), params=self._store(live, False))
        res = ef.evaluate(Candidate(btc_market(seconds_left=420), book(51, 52), "", asset="BTC"))
        self.assertIsNone(res.proposal)
        self.assertNotIn("FOLLOWING", res.reason)
        ef = EdgeFinder(live, None, MarketHistory(), ChartSpot(82990.0), params=self._store(live, True))
        res = ef.evaluate(Candidate(btc_market(seconds_left=420), book(51, 52), "", asset="BTC"))
        self.assertIsNotNone(res.proposal, res.reason)
        self.assertIn("FOLLOWING BEST STRATEGY:", res.proposal.verdicts[0].reason)


class TestTradingModeNote(unittest.TestCase):
    def test_note_matches_what_edge_finder_does(self):
        from kalshi.orchestrator import Orchestrator
        rec = {"n": 100, "roi": 0.01, "cents": 1.0, "win_rate": 0.9, "t": 0.2, "se": 0.02}
        strat = {"strategy": "favorite", "setup": "ahead of strike / momentum with / 80c+", "validated": False,
                 "train": rec, "valid": rec}
        o = Orchestrator.__new__(Orchestrator)
        o.s = h.settings(market_focus="crypto_15m", paper_mode=True)
        note = o._trading_mode_note(LearnedParams(0.8, 0, PAUSED, strategy=strat, chart={"coef": [0]}))
        self.assertIn("following the best tested strategy (favorite when ahead", note)
        self.assertNotIn("exploration", note)
        note = o._trading_mode_note(LearnedParams(0.8, 0, PAUSED, chart={"coef": [0]}))
        self.assertIn("paper exploration", note)
        o.s = h.settings(market_focus="crypto_15m", paper_mode=False)
        self.assertIn("paused", o._trading_mode_note(LearnedParams(0.8, 0, PAUSED, strategy=strat)))
        self.assertIn("edge threshold 5c", o._trading_mode_note(LearnedParams(0.8, 0, 5.0)))
