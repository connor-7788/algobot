"""Learning page (routed from app.py): what the agents have learned from the chart and from their trades."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from common import ago, get_journal, get_settings
from kalshi.agents.shadow import group, setup_key

GREEN, RED, MUTED = "#0ca30c", "#d03b3b", "#8b8a85"

s = get_settings()
j = get_journal()


def color(v) -> str:
    if v is None or pd.isna(v) or abs(v) < 0.05:
        return f"color: {MUTED}"
    return f"color: {GREEN if v > 0 else RED}; font-weight: 600"


def cents(v) -> str:
    return "n/a" if v is None or pd.isna(v) else f"{v:+.1f}c"


@st.cache_data(ttl=20, show_spinner=False)
def shadow_rows(_j, key: str) -> list[dict]:
    return _j.shadow_rows(limit=6000)


@st.cache_data(ttl=20, show_spinner=False)
def params(_j, key: str) -> dict:
    raw = _j.get_flag("learned_params", "")
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}


st.title("Learning")
st.caption("Every 15-minute market feeds the Learner (chart + outcome), and every market also gets pretend "
           "shadow trades from five simple strategies plus the bot's own trades, each explained after it "
           "settles. Win rate alone misleads: heavy favorites win most of the time and can still lose money, "
           "so the tables sort by profit per contract.")


@st.fragment(run_every=30)
def page():
    key = f"{s.journal_db_path}:{s.paper_mode}"
    p = params(j, key)
    rows = shadow_rows(j, key)
    counts = j.observation_counts()

    paused = (p.get("edge_min_cents") or 0) >= 999
    mode = p.get("mode", "blend")
    exploring = paused and s.paper_mode and s.paper_explore and bool(p.get("chart"))
    m = p.get("metrics") or {}
    c = st.columns(4)
    c[0].metric("Pricing with", {"chart": "Chart model", "blend": "Kalshi price + distance model"}.get(mode, mode),
                help="The chart model is used for real decisions only after it beats Kalshi's own price on "
                     "markets it didn't train on.")
    c[1].metric("Trading", "Exploring (paper)" if exploring else "Paused" if paused else
                f"Edge ≥ {p.get('edge_min_cents', s.edge_min_cents):g}c")
    if m.get("chart_valid_logloss") is not None:
        gap = m["market_valid_logloss_chart_rows"] - m["chart_valid_logloss"]
        c[2].metric("Chart model vs Kalshi", f"{gap:+.4f}",
                    help="Held-out log loss of Kalshi's price minus the chart model's. Positive = the chart "
                         "model predicted outcomes better.")
    else:
        c[2].metric("Chart model vs Kalshi", "n/a")
    c[3].metric("Snapshots learned from", f"{counts.get('labelled', 0):,}",
                f"{counts.get('markets', 0):,} markets", delta_color="off")
    if p.get("fitted_at"):
        st.caption(f"Model v{p.get('version', 0)}, refit {ago(p['fitted_at'])}. Refits every "
                   f"{s.learner_interval_s / 60:.0f} min on the last {s.learner_window_h:.0f}h.")

    st.markdown("#### What the chart has taught it")
    if p.get("lessons"):
        st.markdown("\n".join(f"- {x}" for x in p["lessons"]))
        st.caption("Weights are per standard deviation of each chart feature, after accounting for Kalshi's price. "
                   "Small weights mean the crowd already prices that pattern in.")
    else:
        st.caption("No chart model yet. It needs a few hundred labelled snapshots.")

    if not rows:
        st.info("No settled shadow trades yet. They appear a minute after each 15-minute market closes "
                "(and right after startup, from the last 72 hours of history).")
        return

    df = pd.DataFrame(rows)
    df["setup"] = [setup_key(r["feats"] or {}, r["side"], r["entry_cents"]) for r in rows]

    st.markdown("#### Strategy scoreboard")
    g = group(rows, lambda r: r["strategy"])
    sb = pd.DataFrame([{"Strategy": k, "Trades": v["n"], "Win rate": v["win_rate"],
                        "Avg per contract": v["avg_cents"], "Total per contract": v["avg_cents"] * v["n"],
                        "t-stat": v["t"]} for k, v in g.items()]).sort_values("Avg per contract", ascending=False)
    st.dataframe(sb.style.format({"Win rate": "{:.0%}", "Avg per contract": cents,
                                  "Total per contract": lambda v: f"{v / 100:+,.2f} $", "t-stat": "{:+.1f}"})
                 .map(color, subset=["Avg per contract", "Total per contract"]),
                 hide_index=True, use_container_width=True)
    st.caption("'bot' is the bot's own trades. t-stat above +2 or below -2 is unlikely to be luck; "
               "between them, keep watching.")

    st.markdown("#### Setups: where price was, which way it was moving, what it cost")
    min_n = st.select_slider("Minimum trades per setup", options=[10, 20, 40, 80], value=20, key="setup_min_n")
    gs = group([dict(r, _k=k) for r, k in zip(rows, df["setup"])], lambda r: r["_k"])
    sd = pd.DataFrame([{"Setup": k, "Trades": v["n"], "Win rate": v["win_rate"], "Avg per contract": v["avg_cents"],
                        "t-stat": v["t"]} for k, v in gs.items() if v["n"] >= min_n])
    if sd.empty:
        st.caption("Not enough trades per setup yet.")
    else:
        sd = sd.sort_values("Avg per contract", ascending=False)
        st.dataframe(sd.style.format({"Win rate": "{:.0%}", "Avg per contract": cents, "t-stat": "{:+.1f}"})
                     .map(color, subset=["Avg per contract"]), hide_index=True, use_container_width=True)
        st.caption(f"Skeptic refuses a setup once it has lost money over {s.skeptic_setup_min_n}+ trades "
                   f"with t ≤ {s.skeptic_setup_max_t:g}.")

    st.markdown("#### Why each trade won or lost")
    f1, f2 = st.columns(2)
    strat = f1.multiselect("Strategy", sorted(df["strategy"].unique()), default=[], placeholder="All",
                           key="pm_strat")
    outcome = f2.radio("Outcome", ["All", "Won", "Lost"], horizontal=True, key="pm_outcome")
    v = df
    if strat:
        v = v[v["strategy"].isin(strat)]
    if outcome != "All":
        v = v[v["won"] == (1 if outcome == "Won" else 0)]
    v = v.sort_values(["close_ts", "id"], ascending=False).head(200)
    show = pd.DataFrame({
        "Closed": v["close_ts"].map(lambda t: ago(datetime.fromtimestamp(int(t), timezone.utc).isoformat())),
        "Market": v["ticker"], "Strategy": v["strategy"], "Bet": v["side"].str.upper() + " @ " +
        v["entry_cents"].astype(int).astype(str) + "c", "P&L": v["pnl_cents"], "Why": v["postmortem"]})
    st.dataframe(show.style.format({"P&L": cents}).map(color, subset=["P&L"]), hide_index=True,
                 use_container_width=True, height=460,
                 column_config={"Why": st.column_config.TextColumn(width="large")})


page()
