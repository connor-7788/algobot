"""
Strategy selector: pick the best strategy the bot has tested.

Candidates are every shadow strategy (chart, momentum, fade, favorite,
underdog), each on all markets and restricted to a single setup (ahead of /
behind the strike x momentum with / against x price band).

Ranked by return per dollar staked (P&L / (entry + fee)), because Risk
Manager sizes bets in dollars: a 20c contract that wins 30% and a 90c
contract that wins 92% are compared on what each dollar earned.

To avoid crowning a lucky streak:
  * the choice is made on the older 70% of settled trades only, scored by
    mean return minus one standard error (a strategy needs a big sample to
    beat a slightly worse one with a small sample);
  * it is then checked on the newer 30%. "validated" means it made money
    there too (t >= selector_min_t). Live mode follows only a validated
    winner. Paper mode follows the winner either way, so you can watch it.
"""

from __future__ import annotations

import math

from kalshi.agents.shadow import setup_key


def _roi(r: dict) -> float:
    cost = float(r["entry_cents"]) + float(r["fee_cents"])
    return float(r["pnl_cents"]) / cost if cost > 0 else 0.0


def summarize(rows: list[dict]) -> dict:
    n = len(rows)
    if not n:
        return {"n": 0, "roi": None, "cents": None, "win_rate": None, "t": 0.0, "se": None}
    rois = [_roi(r) for r in rows]
    mean = sum(rois) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in rois) / (n - 1)) if n > 1 else 0.0
    se = sd / math.sqrt(n) if n > 1 else float("inf")
    return {"n": n, "roi": mean, "cents": sum(float(r["pnl_cents"]) for r in rows) / n,
            "win_rate": sum(1 for r in rows if r["won"]) / n, "t": mean / se if se and se != float("inf") else 0.0,
            "se": se}


def select(rows: list[dict], *, min_train: int = 40, min_setup_train: int = 40, min_valid: int = 10,
           min_t: float = 1.0, exclude=("bot",)) -> dict | None:
    rows = [r for r in rows if r.get("won") is not None and r["strategy"] not in exclude]
    if not rows:
        return None
    closes = sorted({r["close_ts"] for r in rows})
    if len(closes) < 10:
        return None
    cut = closes[int(len(closes) * 0.7)]
    for r in rows:
        r.setdefault("_setup", setup_key(r.get("feats") or {}, r["side"], r["entry_cents"]))

    cands: dict[tuple[str, str | None], tuple[list, list]] = {}
    for r in rows:
        half = 0 if r["close_ts"] < cut else 1
        for key in ((r["strategy"], None), (r["strategy"], r["_setup"])):
            cands.setdefault(key, ([], []))[half].append(r)

    scored = []
    for (strat, setup), (tr, va) in cands.items():
        need = min_train if setup is None else min_setup_train
        if len(tr) < need or len(va) < min_valid:
            continue
        st, sv = summarize(tr), summarize(va)
        scored.append({"strategy": strat, "setup": setup, "score": st["roi"] - st["se"], "train": st, "valid": sv,
                       "all": summarize(tr + va)})
    if not scored:
        return None
    scored.sort(key=lambda c: -c["score"])
    best = scored[0]
    best["validated"] = bool(best["train"]["roi"] > 0 and best["valid"]["roi"] > 0 and best["valid"]["t"] >= min_t)
    best["ranking"] = [{k: c[k] for k in ("strategy", "setup", "score", "train", "valid", "all")}
                       for c in scored[:12]]
    best["cut_ts"] = cut
    return best


def describe(sel: dict | None) -> str:
    if not sel:
        return "no strategy has enough settled trades to rank yet"
    name = sel["strategy"] + (f" when {sel['setup']}" if sel["setup"] else " on every market")
    tr, va = sel["train"], sel["valid"]
    return (f"best tested strategy: {name}. Older trades: {tr['roi'] * 100:+.1f}% return per $ "
            f"({tr['win_rate'] * 100:.0f}% wins, {tr['cents']:+.1f}c/contract, n={tr['n']}); newer trades: "
            f"{va['roi'] * 100:+.1f}% ({va['win_rate'] * 100:.0f}% wins, n={va['n']}, t {va['t']:.1f}). "
            + ("Validated on newer markets." if sel.get("validated") else
               "NOT validated: it lost money or wasn't clearly positive on the newer markets."))
