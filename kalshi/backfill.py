"""
Backfill: rebuild the Learner's memory from public history on startup.

Render's free plan has no disk, so every deploy or restart starts with an
empty journal. Instead of waiting ~10 hours to relearn, this pulls the last
learner_backfill_h hours of settled 15-minute markets:

  * Kalshi: settled markets (strike, close time, YES/NO result) and their
    1-minute candlesticks (yes_bid / yes_ask at the close of each minute),
    GET /markets/candlesticks?market_tickers=...&period_interval=1
  * Coinbase: 1-minute candles for spot and volatility, 300 per request,
    GET /products/{sym}/candles?granularity=60&start=..&end=..

and writes one labelled observation per market-minute, the same shape Edge
Finder records live (spot is Coinbase's close for that minute instead of the
three-exchange median). Markets already in the journal are skipped, so it is
safe to run on every start.
"""

from __future__ import annotations

import logging
import math
import statistics
import time
from datetime import datetime, timedelta, timezone

import requests

from kalshi.spot import SYMBOLS

log = logging.getLogger("backfill")

COINBASE = "https://api.exchange.coinbase.com/products/{sym}/candles"


def _cents(block: dict | None) -> int | None:
    if not block or block.get("close_dollars") is None:
        return None
    return int(round(float(block["close_dollars"]) * 100))


def coinbase_closes(sym: str, start: int, end: int, session=None, pause_s: float = 0.15) -> dict[int, float]:
    """{minute start ts: close} from 1-minute candles covering [start, end]."""
    sess = session or requests.Session()
    out: dict[int, float] = {}
    t = start
    while t < end:
        t2 = min(end, t + 299 * 60)          # Coinbase rejects requests over 300 candles
        r = sess.get(COINBASE.format(sym=sym), timeout=10, headers={"User-Agent": "algobot/1.0"},
                     params={"granularity": 60,
                             "start": datetime.fromtimestamp(t, timezone.utc).isoformat(),
                             "end": datetime.fromtimestamp(t2, timezone.utc).isoformat()})
        r.raise_for_status()
        for c in r.json():
            out[int(c[0])] = float(c[4])
        t = t2
        time.sleep(pause_s)
    return out


def sigma_at(closes: dict[int, float], minute_ts: int, lookback: int, floor_per_min: float) -> float | None:
    """Per-sqrt-second vol from the `lookback` minutes ending at minute_ts (what the live bot would have seen)."""
    seq = [closes.get(minute_ts - 60 * k) for k in range(lookback, -1, -1)]
    rets = [math.log(b / a) for a, b in zip(seq, seq[1:]) if a and b and a > 0 and b > 0]
    if len(rets) < 10:
        return None
    return max(statistics.pstdev(rets), floor_per_min) / math.sqrt(60.0)


def run(settings, journal, data, *, session=None, now=None, pause_s: float = 0.15) -> dict:
    """Returns {"markets": n, "observations": n, "skipped": n, "errors": [...]}."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=settings.learner_backfill_h)
    have = journal.observed_tickers()
    report = {"markets": 0, "observations": 0, "skipped": 0, "errors": []}

    for series, asset in settings.crypto_series.items():
        sym = SYMBOLS.get(asset, {}).get("coinbase")
        if not sym:
            continue
        try:
            pages = max(1, math.ceil(settings.learner_backfill_h * 4 / 200) + 1)
            markets = [m for m in data.get_markets(series, status="settled", max_pages=pages)
                       if m.result in ("yes", "no") and m.floor_strike and m.close_time and m.open_time
                       and m.close_time >= since]
        except Exception as e:
            report["errors"].append(f"{series} markets: {e}")
            continue
        todo = [m for m in markets if m.ticker not in have]
        report["skipped"] += len(markets) - len(todo)
        if not todo:
            continue
        todo.sort(key=lambda m: m.close_time)

        lo = int(todo[0].open_time.timestamp()) - 60 * (settings.crypto_vol_lookback_min + 2)
        hi = int(todo[-1].close_time.timestamp()) + 60
        try:
            closes = coinbase_closes(sym, lo, hi, session, pause_s)
        except Exception as e:
            report["errors"].append(f"{asset} spot candles: {e}")
            continue

        for i in range(0, len(todo), 20):          # 20 markets x ~15 candles, far under Kalshi's 10,000 cap
            chunk = todo[i:i + 20]
            try:
                candles = data.get_candlesticks([m.ticker for m in chunk],
                                                int(chunk[0].open_time.timestamp()),
                                                int(chunk[-1].close_time.timestamp()))
            except Exception as e:
                report["errors"].append(f"{series} candles: {e}")
                continue
            for m in chunk:
                close_ts = int(m.close_time.timestamp())
                y = 1 if m.result == "yes" else 0
                rows = 0
                for c in candles.get(m.ticker, []):
                    t = int(c["end_period_ts"])
                    secs = close_ts - t
                    if secs < settings.crypto_min_seconds_to_close or secs > 15 * 60:
                        continue
                    bid, ask = _cents(c.get("yes_bid")), _cents(c.get("yes_ask"))
                    spot = closes.get(t - 60)            # Coinbase candle that closed at t
                    sig = sigma_at(closes, t - 60, settings.crypto_vol_lookback_min,
                                   settings.crypto_vol_floor_per_min)
                    if not (bid and ask and 0 < bid < ask < 100 and spot and sig):
                        continue
                    journal.add_observation(ticker=m.ticker, series=series, close_ts=close_ts, secs_left=secs,
                                            spot=spot, strike=m.floor_strike, strike_type=m.strike_type,
                                            sigma=sig, bid=bid, ask=ask, y=y)
                    rows += 1
                if rows:
                    report["markets"] += 1
                    report["observations"] += rows
            time.sleep(pause_s)
    return report
