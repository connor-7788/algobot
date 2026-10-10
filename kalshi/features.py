"""
Chart features for 15-minute crypto markets, computed from the coin's own
1-minute chart (Coinbase candles) plus Kalshi's price.

Every feature is signed so that positive means "pointing toward YES" (for a
"price at or above the strike" market), and scaled by the coin's current
per-minute volatility so BTC, ETH and SOL are comparable.

  mkt_logit   Kalshi's mid price, as log-odds (what the crowd thinks)
  z           distance from spot to strike in volatility units over the time left
  mom1/3/5/15 move over the last 1/3/5/15 minutes, in sigmas
  range_pos   where spot sits in its last-15-minute high/low range (-0.5 low .. +0.5 high)
  z_vol       z x volatility regime: is the distance worth less when volatility is spiking?
  mkt_late    Kalshi's price x share of time elapsed: does the crowd get sharper near the end?

Also returned (not model inputs, they have no direction): vol_regime (last
5 minutes' volatility vs the last hour, log ratio) and t_frac (time left / 15 min).
"""

from __future__ import annotations

import math
import statistics

FEATURES = ["mkt_logit", "z", "mom1", "mom3", "mom5", "mom15", "range_pos", "z_vol", "mkt_late"]
EXTRA = ["vol_regime", "t_frac"]

LABELS = {
    "mkt_logit": "Kalshi's own price",
    "z": "distance from the strike (in volatility units)",
    "mom1": "1-minute momentum",
    "mom3": "3-minute momentum",
    "mom5": "5-minute momentum",
    "mom15": "15-minute momentum",
    "range_pos": "position in the 15-minute range",
    "z_vol": "distance from the strike while volatility is spiking",
    "mkt_late": "Kalshi's price late in the market",
    "vol_regime": "volatility spike vs the last hour",
    "t_frac": "time left",
}

# What a positive / negative learned weight means, in words.
MEANING = {
    "z": ("being past the strike is worth more than Kalshi prices in",
          "Kalshi already over-prices distance from the strike"),
    "mom1": ("1-minute moves tend to continue", "1-minute moves tend to snap back"),
    "mom3": ("3-minute moves tend to continue", "3-minute moves tend to snap back"),
    "mom5": ("5-minute moves tend to continue", "5-minute moves tend to snap back"),
    "mom15": ("15-minute trends tend to continue", "15-minute trends tend to reverse"),
    "range_pos": ("breaks to the edge of the 15-minute range tend to hold",
                  "moves to the edge of the 15-minute range tend to snap back"),
    "z_vol": ("distance still holds up when volatility spikes", "a volatility spike makes distance less safe"),
    "mkt_late": ("Kalshi's price gets more reliable near the close", "Kalshi's price over-reacts near the close"),
}

CLIP = 6.0


def _clip(x: float) -> float:
    return max(-CLIP, min(CLIP, x))


def logit(p: float) -> float:
    p = min(0.999, max(0.001, p))
    return math.log(p / (1 - p))


def _ref(closes: dict[int, float], now_ts: float, minutes: int) -> float | None:
    """Close of the 1-minute candle that ended about `minutes` minutes before now_ts."""
    start = int((now_ts - 60 * minutes) // 60) * 60 - 60
    return closes.get(start) or closes.get(start - 60)


def chart_features(*, closes: dict[int, float], now_ts: float, spot: float, strike: float, secs_left: float,
                   sigma: float, strike_type: str, bid: int, ask: int, window_s: float = 60.0) -> dict | None:
    """sigma is per-sqrt-second. Returns None if the inputs can't support the features."""
    if spot <= 0 or strike <= 0 or sigma <= 0 or secs_left < window_s or not (0 < bid < ask < 100):
        return None
    sign = 1.0 if strike_type in ("greater", "greater_or_equal") else -1.0
    sig_min = sigma * math.sqrt(60.0)
    f = {"mkt_logit": logit((bid + ask) / 200.0),
         "z": _clip(sign * math.log(spot / strike) / (sigma * math.sqrt(secs_left - 2 * window_s / 3))),
         "t_frac": min(1.0, secs_left / 900.0)}
    for k in (1, 3, 5, 15):
        ref = _ref(closes, now_ts, k)
        f[f"mom{k}"] = _clip(sign * math.log(spot / ref) / (sig_min * math.sqrt(k))) if ref else 0.0

    recent = [_ref(closes, now_ts, k) for k in range(15, 0, -1)]
    recent = [c for c in recent if c]
    rets = [math.log(b / a) for a, b in zip(recent[-6:], recent[-5:]) if a > 0 and b > 0]
    if len(rets) >= 3 and sig_min > 0:
        short = statistics.pstdev(rets) or sig_min * 0.1
        f["vol_regime"] = max(-2.0, min(2.0, math.log(short / sig_min)))
    else:
        f["vol_regime"] = 0.0
    if len(recent) >= 5:
        lo, hi = min(recent + [spot]), max(recent + [spot])
        f["range_pos"] = sign * ((spot - lo) / (hi - lo) - 0.5) if hi > lo else 0.0
    else:
        f["range_pos"] = 0.0
    f["z_vol"] = _clip(f["z"] * f["vol_regime"])
    f["mkt_late"] = f["mkt_logit"] * (1.0 - f["t_frac"])
    return {k: round(f[k], 5) for k in FEATURES + EXTRA}


def describe(f: dict, side: str | None = None) -> str:
    """Short plain-English read of the chart. side='yes'/'no' phrases momentum as with/against the bet."""
    if not f:
        return "no chart data"
    flip = -1.0 if side == "no" else 1.0

    def mom(k):
        v = f.get(f"mom{k}", 0.0) * flip
        if side:
            return f"{k}-min momentum {abs(v):.1f}σ {'with' if v >= 0 else 'against'} the bet"
        return f"{k}-min momentum {v:+.1f}σ toward YES"

    rp = f.get("range_pos", 0.0) * flip
    where = "near the top of" if rp > 0.25 else "near the bottom of" if rp < -0.25 else "mid"
    vol = f.get("vol_regime", 0.0)
    vtxt = "volatility spiking" if vol > 0.5 else "unusually calm" if vol < -0.5 else "normal volatility"
    return f"{mom(5)}, {mom(15)}, {where} its 15-min range{' for the bet' if side else ''}, {vtxt}"
