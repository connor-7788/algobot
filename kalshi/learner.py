"""
Learner: the agents improve from results, with guard rails.

What it learns (crypto 15-minute markets):
  * sigma_scale   how much to trust the volatility estimate (model confidence)
  * model_weight  how much to trust the model vs Kalshi's own price
  * edge_min_cents the smallest edge worth trading, or PAUSED when no
                   threshold shows positive expected value

How, without fooling itself:
  1. Every minute Edge Finder looks at a market, it records an observation:
     spot, strike, seconds left, volatility, Kalshi bid/ask. That is ~15 per
     market whether or not the bot trades, so the learner sees thousands of
     labelled examples a day instead of a handful of trades.
  2. After each market settles, the observation gets its outcome (YES/NO).
  3. Every learner_interval_s it refits on the last learner_window_h hours,
     split by time: fit on the older 70%, judge on the newer 30%. New
     parameters are adopted only if they beat the current ones on the
     held-out 30% by at least learner_min_improvement in log loss.
  4. The entry threshold is picked on the older 70% and must ALSO show
     positive expected value on the newer 30% (t >= learner_min_t). If no
     threshold passes, Edge Finder is paused until the evidence changes.
  5. Every parameter is bounded, every change is versioned in the journal
     and announced on the Agents Live board. Risk limits are never touched.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from kalshi.fees import worst_case_fee_per_contract_cents
from kalshi.spot import norm_cdf

log = logging.getLogger("learner")

PAUSED = 999.0
FLAG_KEY = "learned_params"


@dataclass
class LearnedParams:
    sigma_scale: float
    model_weight: float
    edge_min_cents: float
    version: int = 0
    fitted_at: str = ""
    source: str = "config defaults"
    metrics: dict = field(default_factory=dict)

    @property
    def paused(self) -> bool:
        return self.edge_min_cents >= PAUSED


class ParamStore:
    """Thread-safe holder Edge Finder reads on every evaluation."""

    def __init__(self, settings, journal=None):
        self._lock = threading.Lock()
        self._p = LearnedParams(settings.crypto_sigma_scale, settings.crypto_model_weight,
                                float(settings.edge_min_cents))
        if journal is not None:
            raw = journal.get_flag(FLAG_KEY, "")
            if raw:
                try:
                    self._p = LearnedParams(**json.loads(raw))
                except (TypeError, ValueError):
                    log.warning("ignoring unreadable learned params")

    def get(self) -> LearnedParams:
        with self._lock:
            return self._p

    def set(self, p: LearnedParams, journal=None) -> None:
        with self._lock:
            self._p = p
        if journal is not None:
            journal.set_flag(FLAG_KEY, json.dumps(asdict(p)), by="learner")


def blend(p_model: float, p_market: float, w: float) -> float:
    def logit(p):
        p = min(0.999, max(0.001, p))
        return math.log(p / (1 - p))
    x = w * logit(p_model) + (1 - w) * logit(p_market)
    return 1 / (1 + math.exp(-x))


def raw_z(spot: float, strike: float, secs: float, sigma: float, strike_type: str, window_s: float = 60.0):
    """Unscaled z-score of finishing at/above the strike (sign flipped for "less" strikes)."""
    if spot <= 0 or strike <= 0 or sigma <= 0 or secs < window_s:
        return None
    z = math.log(spot / strike) / (sigma * math.sqrt(secs - 2 * window_s / 3))
    return z if strike_type in ("greater", "greater_or_equal") else -z


def log_loss(ps: list[float], ys: list[int]) -> float:
    tot = 0.0
    for p, y in zip(ps, ys):
        p = min(0.999, max(0.001, p))
        tot -= math.log(p) if y else math.log(1 - p)
    return tot / max(1, len(ps))


class Learner:
    name = "learner"
    SCALES = [0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 1.0, 1.1, 1.2, 1.3]
    WEIGHTS = [0.0, 0.1, 0.2, 0.25, 0.3, 0.4, 0.5, 0.6, 0.75]     # never 100% model: Kalshi's price always counts
    THRESHOLDS = [3.0, 5.0, 8.0, 12.0]

    def __init__(self, settings, journal, data, store: ParamStore, clock=time.time):
        self.s = settings
        self.journal = journal
        self.data = data
        self.store = store
        self.clock = clock
        self._seen: set[tuple[str, int]] = set()
        self._lock = threading.Lock()
        self.last_report = ""

    # ── Step 1: observe ──────────────────────────────────────────────────────

    def observe(self, *, ticker: str, series: str, close_ts: int, secs_left: float, spot: float,
                strike: float, strike_type: str, sigma: float, bid: int, ask: int) -> bool:
        """Record at most one observation per market per minute-to-close."""
        key = (ticker, int(secs_left // 60))
        with self._lock:
            if key in self._seen:
                return False
            self._seen.add(key)
            if len(self._seen) > 20000:
                self._seen.clear()
        self.journal.add_observation(ticker=ticker, series=series, close_ts=close_ts, secs_left=secs_left,
                                     spot=spot, strike=strike, strike_type=strike_type, sigma=sigma,
                                     bid=bid, ask=ask)
        return True

    # ── Step 2: label ────────────────────────────────────────────────────────

    def label_pending(self) -> int:
        tickers = self.journal.unlabeled_tickers(before_ts=int(self.clock()) - 90)
        labelled = 0
        for i in range(0, len(tickers), 100):
            for m in self.data.get_markets_by_tickers(tickers[i:i + 100]):
                if m.result in ("yes", "no"):
                    labelled += self.journal.label_observations(m.ticker, 1 if m.result == "yes" else 0)
        return labelled

    # ── Step 3/4: refit ──────────────────────────────────────────────────────

    def _simulate(self, rows: list[dict], scale: float, w: float, thr: float) -> tuple[int, float, float]:
        """First entry per market where edge >= thr, held to settlement.
        Returns (trades, mean P&L cents/contract, t-stat)."""
        by_mkt: dict[str, list[dict]] = {}
        for r in rows:
            by_mkt.setdefault(r["ticker"], []).append(r)
        pnl = []
        for obs in by_mkt.values():
            obs.sort(key=lambda r: -r["secs_left"])
            for r in obs:
                p = blend(norm_cdf(r["z"] / scale), (r["bid"] + r["ask"]) / 200.0, w)
                choice = None
                for side, ask, fair in (("yes", r["ask"], p), ("no", 100 - r["bid"], 1 - p)):
                    if not (self.s.edge_min_price_cents <= ask <= self.s.edge_max_price_cents):
                        continue
                    lim = min(99, ask + self.s.exec_slippage_cap_cents)
                    edge = 100 * fair - lim - worst_case_fee_per_contract_cents(lim, self.s)
                    if edge >= thr and (choice is None or edge > choice[2]):
                        choice = (side, lim, edge)
                if choice is None:
                    continue
                side, lim, _ = choice
                won = (r["y"] == 1) == (side == "yes")
                pnl.append(100 * won - lim - worst_case_fee_per_contract_cents(lim, self.s))
                break
        n = len(pnl)
        if n < 2:
            return n, (pnl[0] if pnl else 0.0), 0.0
        mean = sum(pnl) / n
        sd = math.sqrt(sum((x - mean) ** 2 for x in pnl) / (n - 1)) or 1e-9
        return n, mean, mean / (sd / math.sqrt(n))

    def refit(self) -> str:
        since = int(self.clock() - self.s.learner_window_h * 3600)
        rows = []
        for r in self.journal.labeled_observations(since_ts=since):
            z = raw_z(r["spot"], r["strike"], r["secs_left"], r["sigma"], r["strike_type"])
            if z is None or r["bid"] is None or r["ask"] is None or not (0 < r["bid"] < r["ask"] < 100):
                continue
            r = dict(r)
            r["z"] = z
            rows.append(r)
        cur = self.store.get()
        if len(rows) < self.s.learner_min_observations:
            self.last_report = (f"waiting for data: {len(rows)} labelled observations, "
                                f"need {self.s.learner_min_observations}. Using {cur.source}.")
            return self.last_report

        closes = sorted({r["close_ts"] for r in rows})
        cut = closes[int(len(closes) * 0.7)]
        train = [r for r in rows if r["close_ts"] < cut]
        valid = [r for r in rows if r["close_ts"] >= cut]
        ys_t, ys_v = [r["y"] for r in train], [r["y"] for r in valid]

        def preds(rs, sc, w):
            return [blend(norm_cdf(r["z"] / sc), (r["bid"] + r["ask"]) / 200.0, w) for r in rs]

        best = min(((log_loss(preds(train, sc, w), ys_t), sc, w) for sc in self.SCALES for w in self.WEIGHTS))
        _, sc, w = best
        ll_new = log_loss(preds(valid, sc, w), ys_v)
        ll_cur = log_loss(preds(valid, cur.sigma_scale, cur.model_weight), ys_v)
        ll_mkt = log_loss([(r["bid"] + r["ask"]) / 200.0 for r in valid], ys_v)
        adopt_cal = ll_new <= ll_cur - self.s.learner_min_improvement
        sc_use, w_use = (sc, w) if adopt_cal else (cur.sigma_scale, cur.model_weight)

        # Entry threshold: chosen on train, must hold up on validation.
        thr_use, thr_note = PAUSED, "no threshold showed positive value on both halves: Edge Finder paused"
        for thr in self.THRESHOLDS:
            n_t, m_t, _ = self._simulate(train, sc_use, w_use, thr)
            n_v, m_v, t_v = self._simulate(valid, sc_use, w_use, thr)
            if (n_t >= self.s.learner_min_trades and m_t > 0 and n_v >= self.s.learner_min_trades // 2
                    and m_v > 0 and t_v >= self.s.learner_min_t):
                thr_use = thr
                thr_note = (f"entry threshold {thr:.0f}c: +{m_t:.2f}c/contract on {n_t} older trades, "
                            f"+{m_v:.2f}c on {n_v} newer (t {t_v:.1f})")
                break

        metrics = {"n_obs": len(rows), "n_train": len(train), "n_valid": len(valid),
                   "valid_logloss_new": round(ll_new, 5), "valid_logloss_current": round(ll_cur, 5),
                   "valid_logloss_market": round(ll_mkt, 5), "candidate_scale": sc, "candidate_weight": w}
        changed = adopt_cal or thr_use != cur.edge_min_cents
        new = LearnedParams(sc_use, w_use, thr_use, cur.version + (1 if changed else 0),
                            datetime.now(timezone.utc).isoformat(), "learned from settled markets", metrics)
        self.store.set(new, self.journal)
        cal_note = (f"calibration updated: volatility x{cur.sigma_scale:g} -> x{sc:g}, model weight "
                    f"{cur.model_weight:g} -> {w:g} (held-out log loss {ll_cur:.4f} -> {ll_new:.4f}; "
                    f"market alone {ll_mkt:.4f})" if adopt_cal else
                    f"calibration kept (x{cur.sigma_scale:g}, weight {cur.model_weight:g}); best alternative "
                    f"x{sc:g}/{w:g} scored {ll_new:.4f} vs {ll_cur:.4f} on held-out data, not enough to switch")
        self.last_report = f"v{new.version} on {len(rows):,} observations: {cal_note}. {thr_note}."
        if changed:
            self.journal.log_event("learner", self.last_report)
        log.info(self.last_report)
        return self.last_report
