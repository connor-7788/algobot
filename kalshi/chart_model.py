"""
Chart model: L2-regularised logistic regression from chart features to
P(YES). Small on purpose: nine inputs, fitted by Newton's method in numpy,
so it trains in well under a second on a few thousand rows and can't
memorise noise the way a big model would.

Kalshi's own price is one of the inputs, so the model starts from the
crowd's view and only moves away from it where the chart has added
something in the training data.
"""

from __future__ import annotations

import math

import numpy as np

from kalshi.features import FEATURES, MEANING


class ChartModel:
    def __init__(self, coef: list[float], intercept: float, mean: list[float], std: list[float],
                 features: list[str] | None = None):
        self.features = list(features or FEATURES)
        self.coef = np.asarray(coef, dtype=float)
        self.intercept = float(intercept)
        self.mean = np.asarray(mean, dtype=float)
        self.std = np.asarray(std, dtype=float)

    # ── Fit / predict ────────────────────────────────────────────────────────

    @classmethod
    def fit(cls, rows: list[dict], ys: list[int], l2: float = 1.0, iters: int = 30) -> "ChartModel":
        X = np.array([[r[k] for k in FEATURES] for r in rows], dtype=float)
        y = np.asarray(ys, dtype=float)
        mean, std = X.mean(axis=0), X.std(axis=0)
        std[std < 1e-9] = 1.0
        Z = np.hstack([np.ones((len(X), 1)), (X - mean) / std])
        w = np.zeros(Z.shape[1])
        reg = np.full(Z.shape[1], l2)
        reg[0] = 0.0                              # don't shrink the intercept
        for _ in range(iters):
            p = 1 / (1 + np.exp(-np.clip(Z @ w, -30, 30)))
            g = Z.T @ (p - y) + reg * w
            H = (Z * (p * (1 - p))[:, None]).T @ Z + np.diag(reg) + 1e-9 * np.eye(len(w))
            step = np.linalg.solve(H, g)
            w -= step
            if np.max(np.abs(step)) < 1e-7:
                break
        return cls(w[1:].tolist(), float(w[0]), mean.tolist(), std.tolist())

    def predict(self, f: dict) -> float:
        x = (np.array([f[k] for k in self.features], dtype=float) - self.mean) / self.std
        s = float(self.intercept + x @ self.coef)
        return 1 / (1 + math.exp(-max(-30.0, min(30.0, s))))

    def predict_many(self, rows: list[dict]) -> list[float]:
        if not rows:
            return []
        X = (np.array([[r[k] for k in self.features] for r in rows], dtype=float) - self.mean) / self.std
        s = np.clip(self.intercept + X @ self.coef, -30, 30)
        return (1 / (1 + np.exp(-s))).tolist()

    # ── Explain ──────────────────────────────────────────────────────────────

    def importance(self) -> list[tuple[str, float]]:
        """(feature, weight per 1 standard deviation), biggest effect first."""
        return sorted(zip(self.features, self.coef.tolist()), key=lambda kv: -abs(kv[1]))

    def lessons(self, top: int = 4) -> list[str]:
        out = []
        for k, w in self.importance():
            if k == "mkt_logit" or abs(w) < 0.03:
                continue
            pos, neg = MEANING.get(k, (f"{k} up", f"{k} down"))
            out.append(f"{pos if w > 0 else neg} (weight {w:+.2f})")
            if len(out) >= top:
                break
        return out

    # ── Persist ──────────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {"features": self.features, "coef": [round(c, 6) for c in self.coef.tolist()],
                "intercept": round(self.intercept, 6), "mean": self.mean.tolist(), "std": self.std.tolist()}

    @classmethod
    def from_dict(cls, d: dict | None) -> "ChartModel | None":
        if not d or "coef" not in d:
            return None
        return cls(d["coef"], d["intercept"], d["mean"], d["std"], d.get("features"))
