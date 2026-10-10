"""
Shadow: simulates trades the bot would not normally take, then explains why
each one won or lost.

For every 15-minute market, at the first look with <= shadow_entry_max_secs
left, it places one pretend bet per strategy at the current ask (plus the
taker fee). Nothing is ever sent to Kalshi.

  chart     the side the chart model likes better than Kalshi's price does
  momentum  with the last 5 minutes' move
  fade      against the last 5 minutes' move
  favorite  the side priced above 50c
  underdog  the side priced below 50c

The bot's own (paper or live) fills are recorded the same way as strategy
"bot", so they get post-mortems too.

At settlement each bet gets a post-mortem: where price was against the strike
at entry, what the chart said, where it settled, and what happened in
between. Results are grouped by setup (ahead of / behind the strike x
momentum with / against x price band); Skeptic refuses setups that keep
losing money.
"""

from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timezone

from kalshi.features import describe
from kalshi.fees import worst_case_fee_per_contract_cents

log = logging.getLogger("agent.shadow")

DIRECTIONAL = ("z", "mom1", "mom3", "mom5", "mom15", "range_pos")


def side_feats(feats: dict, side: str) -> dict:
    """Flip directional features so positive means 'good for this side'."""
    if not feats:
        return {}
    if side == "yes":
        return dict(feats)
    out = dict(feats)
    for k in DIRECTIONAL:
        if k in out:
            out[k] = -out[k]
    return out


def asset_of(series: str) -> str:
    s = series or ""
    return s[2:-3] if s.startswith("KX") and s.endswith("15M") else s


def price_band(cents: float) -> str:
    return "<40c" if cents < 40 else "40-59c" if cents < 60 else "60-79c" if cents < 80 else "80c+"


def setup_key(feats: dict, side: str, entry_cents: float) -> str:
    f = side_feats(feats, side)
    z, m5 = f.get("z", 0.0), f.get("mom5", 0.0)
    place = "ahead of strike" if z > 0.25 else "behind strike" if z < -0.25 else "at the strike"
    mom = "momentum with" if m5 >= 0.3 else "momentum against" if m5 <= -0.3 else "no momentum"
    return f"{place} / {mom} / {price_band(entry_cents)}"


def stats(pnls: list[float]) -> dict:
    n = len(pnls)
    if not n:
        return {"n": 0, "win_rate": None, "avg_cents": None, "t": 0.0}
    mean = sum(pnls) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in pnls) / (n - 1)) if n > 1 else 0.0
    return {"n": n, "win_rate": sum(1 for x in pnls if x > 0) / n, "avg_cents": mean,
            "t": mean / (sd / math.sqrt(n)) if sd > 0 else 0.0}


def group(rows: list[dict], key) -> dict[str, dict]:
    buckets: dict[str, list[float]] = {}
    for r in rows:
        buckets.setdefault(key(r), []).append(float(r["pnl_cents"]))
    return {k: stats(v) for k, v in buckets.items()}


def postmortem(t: dict, result: str, settle_value: float | None, last_obs: dict | None) -> tuple[int, float, str]:
    """(won, pnl cents, explanation) for one settled shadow trade."""
    side, entry, fee = t["side"], float(t["entry_cents"]), float(t["fee_cents"])
    won = int(result == side)
    pnl = 100.0 * won - entry - fee
    above = t["strike_type"] in ("greater", "greater_or_equal")
    sign = (1 if above else -1) * (1 if side == "yes" else -1)      # +1: higher price helps this bet
    f = t.get("feats") or {}
    sf = side_feats(f, side)
    asset = asset_of(t.get("series", ""))
    dist0 = (t["spot"] / t["strike"] - 1) * 100
    ahead0 = sign * dist0 > 0
    m5 = sf.get("mom5", 0.0)

    head = (f"{'WON' if won else 'LOST'} {pnl:+.0f}c ({t['strategy']}): {side.upper()} at {entry:.0f}c "
            f"with {t['secs_left'] / 60:.1f} min left.")
    pc = t.get("p_chart")
    pm = t.get("p_market")
    odds = ""
    if pm is not None:
        ps = (lambda p: p if side == "yes" else 1 - p)
        odds = f" Kalshi gave it {ps(pm) * 100:.0f}%" + (f", chart model {ps(pc) * 100:.0f}%" if pc is not None else "") + "."
    entry_txt = (f" At entry {asset} was {abs(dist0):.3f}% {'above' if dist0 >= 0 else 'below'} the strike "
                 f"({'on our side' if ahead0 else 'on the wrong side'}); {describe(f, side)}.{odds}")

    end_txt = ""
    if settle_value:
        dist1 = (settle_value / t["strike"] - 1) * 100
        end_txt = f" Settled at {settle_value:,.2f}, {abs(dist1):.3f}% {'above' if dist1 >= 0 else 'below'} the strike."
    late = ""
    if last_obs and last_obs.get("secs_left", 1e9) < t["secs_left"] - 30:
        d = (last_obs["spot"] / t["strike"] - 1) * 100
        late_ahead = sign * d > 0
        late = (f" With {last_obs['secs_left'] / 60:.0f} min left it was "
                f"{'still on our side' if late_ahead else 'on the wrong side'} ({d:+.3f}%).")

    if won and ahead0:
        why = " Why: price held on our side of the strike."
    elif won:
        why = (" Why: price crossed to our side before the close"
               + (", as the momentum suggested." if m5 >= 0.3 else
                  ", even though momentum was against us (some luck)." if m5 <= -0.3 else "."))
    elif ahead0:
        why = (" Why: price reversed through the strike"
               + ("; momentum was already turning against the bet at entry." if m5 <= -0.3 else
                  "; nothing on the chart warned of it." if m5 >= 0.3 else "."))
    else:
        why = (" Why: price started on the wrong side and never got back"
               + ("; momentum was pointing the wrong way too." if m5 <= -0.3 else "."))
    lesson = f" Setup: {setup_key(f, side, entry)}."
    return won, pnl, head + entry_txt + end_txt + late + why + lesson


class Shadow:
    name = "shadow"

    def __init__(self, settings, journal, data=None, clock=time.time):
        self.s = settings
        self.journal = journal
        self.data = data
        self.clock = clock
        self._keys: set[tuple[str, str]] | None = None

    def _have(self, ticker: str, strategy: str) -> bool:
        if self._keys is None:
            self._keys = self.journal.shadow_keys()
        return (ticker, strategy) in self._keys

    # ── Open ─────────────────────────────────────────────────────────────────

    def _pick(self, strategy: str, feats: dict, p_chart: float | None, bid: int, ask: int):
        """(side, reason) for a strategy, or None if it has no opinion here."""
        no_ask = 100 - bid
        m5 = feats.get("mom5", 0.0)                     # + = toward YES
        if strategy == "chart":
            if p_chart is None:
                return None
            e_yes = 100 * p_chart - ask - worst_case_fee_per_contract_cents(ask, self.s)
            e_no = 100 * (1 - p_chart) - no_ask - worst_case_fee_per_contract_cents(no_ask, self.s)
            side = "yes" if e_yes >= e_no else "no"
            return side, f"chart model says YES {p_chart * 100:.0f}%, best value on {side.upper()} ({max(e_yes, e_no):+.1f}c)"
        if strategy in ("momentum", "fade"):
            if abs(m5) < 0.3:
                return None
            with_side = "yes" if m5 > 0 else "no"
            side = with_side if strategy == "momentum" else ("no" if with_side == "yes" else "yes")
            return side, f"5-min momentum {m5:+.1f}σ toward YES; {strategy} takes {side.upper()}"
        if strategy in ("favorite", "underdog"):
            fav = "yes" if (bid + ask) >= 100 else "no"
            side = fav if strategy == "favorite" else ("no" if fav == "yes" else "yes")
            return side, f"{strategy}: {side.upper()} ({'YES' if side == 'yes' else 'NO'} ask {ask if side == 'yes' else no_ask}c)"
        return None

    def consider(self, *, ticker: str, series: str, close_ts: int, secs_left: float, spot: float, strike: float,
                 strike_type: str, bid: int, ask: int, feats: dict | None, p_chart: float | None,
                 strategies=None) -> int:
        """Open any shadow trades due on this snapshot. Returns how many were opened."""
        if not self.s.shadow_enabled or not feats:
            return 0
        if not (self.s.shadow_entry_min_secs <= secs_left <= self.s.shadow_entry_max_secs):
            return 0
        if not (0 < bid < ask < 100):
            return 0
        opened = 0
        for strat in strategies or self.s.shadow_strategies:
            if self._have(ticker, strat):
                continue
            pick = self._pick(strat, feats, p_chart, bid, ask)
            self._keys.add((ticker, strat))      # one decision per strategy per market, even "no opinion"
            if pick is None:
                continue
            side, reason = pick
            price = ask if side == "yes" else 100 - bid
            if not (3 <= price <= 97):
                continue
            ok = self.journal.add_shadow({
                "ticker": ticker, "series": series, "strategy": strat, "side": side, "entry_cents": price,
                "fee_cents": round(worst_case_fee_per_contract_cents(price, self.s), 4), "secs_left": secs_left,
                "spot": spot, "strike": strike, "strike_type": strike_type, "p_chart": p_chart,
                "p_market": (bid + ask) / 200.0, "feats": feats, "reason": reason, "close_ts": int(close_ts)})
            opened += int(ok)
        return opened

    def record_bot_trade(self, p) -> bool:
        """Log the bot's own fill so it gets the same post-mortem."""
        snap = next((v.data_snapshot for v in p.verdicts if v.agent == "edge_finder"), {}) or {}
        k = snap.get("crypto") or {}
        if not k or not p.filled_count or p.close_time is None:
            return False
        fee = (p.fees_paid_usd * 100 / p.filled_count) if p.filled_count else 0.0
        book = snap.get("book") or {}
        bid, ask = book.get("yes_bid"), book.get("yes_ask")
        return self.journal.add_shadow({
            "ticker": p.market_ticker, "series": p.series_ticker, "strategy": "bot", "side": p.side,
            "entry_cents": int(round(p.avg_fill_price_cents or p.price_cents)), "fee_cents": round(fee, 4),
            "secs_left": k.get("seconds_left", 0.0), "spot": k.get("spot"), "strike": k.get("strike"),
            "strike_type": k.get("strike_type"), "p_chart": k.get("p_chart"),
            "p_market": (bid + ask) / 200.0 if bid is not None and ask is not None else None,
            "feats": k.get("feats") or {}, "reason": (p.verdicts[0].reason if p.verdicts else "")[:500],
            "close_ts": int(p.close_time.timestamp())})

    # ── Settle ───────────────────────────────────────────────────────────────

    def settle_one(self, t: dict, result: str, settle_value: float | None) -> dict:
        won, pnl, text = postmortem(t, result, settle_value, self.journal.last_observation(t["ticker"]))
        self.journal.settle_shadow(t["id"], result=result, won=won, pnl_cents=pnl, settle_value=settle_value,
                                   postmortem=text)
        return {"won": won, "pnl": pnl, "text": text}

    def settle(self) -> list[dict]:
        pending = self.journal.pending_shadow(before_ts=int(self.clock()) - 60)
        if not pending or self.data is None:
            return []
        by_ticker: dict[str, list[dict]] = {}
        for t in pending:
            by_ticker.setdefault(t["ticker"], []).append(t)
        tickers = list(by_ticker)
        done = []
        for i in range(0, len(tickers), 100):
            for m in self.data.get_markets_by_tickers(tickers[i:i + 100]):
                if m.result not in ("yes", "no"):
                    continue
                for t in by_ticker.get(m.ticker, []):
                    done.append({**self.settle_one(t, m.result, m.expiration_value), "strategy": t["strategy"]})
        return done

    # ── Replay history (startup backfill) ────────────────────────────────────

    def replay(self, market, observations: list[dict]) -> int:
        """Shadow-trade one already-settled market from its recorded snapshots."""
        if market.result not in ("yes", "no"):
            return 0
        obs = sorted((o for o in observations if o.get("feats")), key=lambda o: -o["secs_left"])
        entry = next((o for o in obs if self.s.shadow_entry_min_secs <= o["secs_left"]
                      <= self.s.shadow_entry_max_secs), None)
        if entry is None:
            return 0
        n = self.consider(ticker=market.ticker, series=market.series_ticker,
                          close_ts=int(market.close_time.timestamp()), secs_left=entry["secs_left"],
                          spot=entry["spot"], strike=entry["strike"], strike_type=entry["strike_type"],
                          bid=entry["bid"], ask=entry["ask"], feats=entry["feats"], p_chart=None,
                          strategies=[s for s in self.s.shadow_strategies if s != "chart"])
        for t in self.journal.pending_shadow(before_ts=int(market.close_time.timestamp()) + 1):
            if t["ticker"] == market.ticker:
                self.settle_one(t, market.result, market.expiration_value)
        return n


def lessons(rows: list[dict], min_n: int = 30) -> list[str]:
    """The clearest patterns in settled shadow + bot trades, in words."""
    out = []
    by_strat = group(rows, lambda r: r["strategy"])
    ranked = sorted(((k, v) for k, v in by_strat.items() if v["n"] >= min_n), key=lambda kv: -kv[1]["avg_cents"])
    for k, v in ranked[:2] + ranked[-1:] if len(ranked) > 2 else ranked:
        out.append(f"{k}: won {v['win_rate'] * 100:.0f}% of {v['n']} trades, {v['avg_cents']:+.1f}c per contract")
    by_setup = group(rows, lambda r: setup_key(r["feats"], r["side"], r["entry_cents"]))
    good = [(k, v) for k, v in by_setup.items() if v["n"] >= min_n]
    good.sort(key=lambda kv: -kv[1]["avg_cents"])
    if good:
        k, v = good[0]
        out.append(f"best setup: {k} won {v['win_rate'] * 100:.0f}% ({v['n']} trades, {v['avg_cents']:+.1f}c)")
        k, v = good[-1]
        out.append(f"worst setup: {k} won {v['win_rate'] * 100:.0f}% ({v['n']} trades, {v['avg_cents']:+.1f}c)")
    return out


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
