"""
Live crypto spot prices and short-term volatility for the 15-minute markets.

Kalshi's crypto 15M markets settle on CF Benchmarks' Real Time Index (BRTI and
siblings), which is built from major USD spot exchanges. BRTI itself isn't a
free public feed, so we read three of its constituent exchanges directly and
treat each as an independent source:
    Coinbase  GET api.exchange.coinbase.com/products/{BTC-USD}/ticker   (price, time)
    Kraken    GET api.kraken.com/0/public/Ticker?pair={XBTUSD}         (result.*.c[0])
    Bitstamp  GET www.bitstamp.net/api/v2/ticker/{btcusd}/             (last, timestamp)
Coinbase's shape was checked live on Oct 10, 2026; Kraken and Bitstamp are
parsed defensively and a source that fails is simply left out (Skeptic needs
at least two to agree).

Volatility: standard deviation of 1-minute log returns from Coinbase candles
(GET .../candles?granularity=60 returns [time, low, high, open, close, volume],
newest first), converted to per-sqrt-second.
"""

from __future__ import annotations

import logging
import math
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from kalshi.client import parse_ts

log = logging.getLogger("kalshi.spot")

# asset -> exchange symbol. Extend here to add coins.
SYMBOLS = {
    "BTC": {"coinbase": "BTC-USD", "kraken": "XBTUSD", "bitstamp": "btcusd"},
    "ETH": {"coinbase": "ETH-USD", "kraken": "ETHUSD", "bitstamp": "ethusd"},
    "SOL": {"coinbase": "SOL-USD", "kraken": "SOLUSD", "bitstamp": "solusd"},
    "XRP": {"coinbase": "XRP-USD", "kraken": "XRPUSD", "bitstamp": "xrpusd"},
    "DOGE": {"coinbase": "DOGE-USD", "kraken": "XDGUSD", "bitstamp": "dogeusd"},
}


@dataclass
class SpotQuote:
    source: str
    price: float
    observed_at: datetime
    fetched_at: datetime


def parse_coinbase(d: dict, fetched: datetime) -> SpotQuote:
    return SpotQuote("coinbase", float(d["price"]), parse_ts(d.get("time")) or fetched, fetched)


def parse_kraken(d: dict, fetched: datetime) -> SpotQuote:
    if d.get("error"):
        raise ValueError(f"kraken error: {d['error']}")
    (row,) = d["result"].values()
    return SpotQuote("kraken", float(row["c"][0]), fetched, fetched)   # no timestamp in Ticker


def parse_bitstamp(d: dict, fetched: datetime) -> SpotQuote:
    ts = d.get("timestamp")
    observed = datetime.fromtimestamp(int(ts), tz=timezone.utc) if ts else fetched
    return SpotQuote("bitstamp", float(d["last"]), observed, fetched)


def sigma_from_candles(candles: list, lookback: int) -> float | None:
    """Per-sqrt-second volatility from 1-minute candles (newest first)."""
    closes = [float(c[4]) for c in candles[:lookback + 1]][::-1]
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    if len(rets) < 10:
        return None
    return statistics.pstdev(rets) / math.sqrt(60.0)


class SpotClient:
    def __init__(self, settings, session: requests.Session | None = None, clock=time.time):
        self.s = settings
        self.session = session or requests.Session()
        self.clock = clock
        self._lock = threading.Lock()
        self._quotes: dict[str, tuple[float, list[SpotQuote]]] = {}
        self._vol: dict[str, tuple[float, float]] = {}
        self._pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="spot")

    def _get(self, url: str, params: dict | None = None):
        r = self.session.get(url, params=params, timeout=4, headers={"User-Agent": "algobot/1.0"})
        r.raise_for_status()
        return r.json()

    def _fetch(self, source: str, sym: str) -> SpotQuote:
        now = datetime.now(timezone.utc)
        if source == "coinbase":
            return parse_coinbase(self._get(f"https://api.exchange.coinbase.com/products/{sym}/ticker"), now)
        if source == "kraken":
            return parse_kraken(self._get("https://api.kraken.com/0/public/Ticker", {"pair": sym}), now)
        if source == "bitstamp":
            return parse_bitstamp(self._get(f"https://www.bitstamp.net/api/v2/ticker/{sym}/"), now)
        raise ValueError(source)

    def quotes(self, asset: str) -> list[SpotQuote]:
        now = self.clock()
        with self._lock:
            hit = self._quotes.get(asset)
            if hit and now - hit[0] < self.s.spot_cache_s:
                return hit[1]
        syms = SYMBOLS.get(asset, {})
        futs = {src: self._pool.submit(self._fetch, src, sym) for src, sym in syms.items()}
        out = []
        for src, f in futs.items():
            try:
                out.append(f.result(timeout=5))
            except Exception as e:
                log.debug("%s %s spot failed: %s", src, asset, e)
        with self._lock:
            self._quotes[asset] = (now, out)
        return out

    def sigma(self, asset: str) -> float | None:
        now = self.clock()
        with self._lock:
            hit = self._vol.get(asset)
            if hit and now - hit[0] < self.s.vol_cache_s:
                return hit[1]
        sym = SYMBOLS.get(asset, {}).get("coinbase")
        if not sym:
            return None
        candles = self._get(f"https://api.exchange.coinbase.com/products/{sym}/candles", {"granularity": 60})
        sig = sigma_from_candles(candles, self.s.crypto_vol_lookback_min)
        if sig is None:
            return None
        sig = max(sig, self.s.crypto_vol_floor_per_min / math.sqrt(60.0))
        with self._lock:
            self._vol[asset] = (now, sig)
        return sig


# ── Fair value for "average of the last 60 s >= strike" ──────────────────────

def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def prob_avg_at_or_above(spot: float, strike: float, seconds_left: float, sigma_per_sqrt_s: float,
                         avg_window_s: float = 60.0) -> float | None:
    """P(average of the index over the final avg_window_s >= strike), driftless
    log-Brownian motion. Averaging over the window cuts the variance: for
    T >= W the average has the variance of a point (T - 2W/3) seconds out.
    Returns None inside the averaging window, where part of the average is
    already fixed and we don't observe it."""
    if spot <= 0 or strike <= 0 or sigma_per_sqrt_s <= 0 or seconds_left < avg_window_s:
        return None
    t_eff = seconds_left - 2.0 * avg_window_s / 3.0
    z = math.log(spot / strike) / (sigma_per_sqrt_s * math.sqrt(t_eff))
    return min(0.999, max(0.001, norm_cdf(z)))
