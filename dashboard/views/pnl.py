"""
P&L page. Green = profit, red = loss, and every number also carries a sign
and an arrow, so it still reads for red-green colorblind viewers.

Live section reruns every second: open positions are marked at the price you
could sell at right now (your side's best bid, minus the exit fee), using the
prices the engine's mark feed writes to the journal once a second.
History section reruns every 15 seconds: equity curve from settled trades,
P&L by market type, recent results.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import altair as alt
import pandas as pd
import streamlit as st

from common import ago, get_journal, get_settings, seconds_since
from kalshi.pnl import held_positions, position_pnl
from kalshi.proposal import REALIZED_STATUSES

REALIZED = {st.value for st in REALIZED_STATUSES}

GREEN = "#0ca30c"     # status "good"
RED = "#d03b3b"       # status "critical"
MUTED = "#8b8a85"

s = get_settings()
j = get_journal()

st.markdown(f"""
<style>
.pnl-tile {{ border: 1px solid rgba(128,128,128,.25); border-radius: 12px; padding: 14px 16px; height: 100%; }}
.pnl-tile .lbl {{ font-size: .80rem; opacity: .75; letter-spacing: .02em; }}
.pnl-tile .val {{ font-size: clamp(1.05rem, 2.1vw, 1.85rem); font-weight: 650; font-variant-numeric: tabular-nums;
                   line-height: 1.25; margin-top: 2px; white-space: nowrap; }}
.pnl-tile .sub {{ font-size: .80rem; opacity: .7; margin-top: 2px; font-variant-numeric: tabular-nums; }}
.pnl-tile.hero .val {{ font-size: clamp(1.3rem, 2.8vw, 2.4rem); }}
.live-dot {{ display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:6px;
             background:{GREEN}; animation: pulse 1.2s ease-in-out infinite; vertical-align: middle; }}
.live-dot.stale {{ background:{MUTED}; animation:none; }}
@keyframes pulse {{ 0%,100% {{ opacity:1 }} 50% {{ opacity:.25 }} }}
</style>
""", unsafe_allow_html=True)


# ── Formatting ────────────────────────────────────────────────────────────────

def color_of(v: float | None) -> str:
    if v is None or abs(v) < 0.005:
        return MUTED
    return GREEN if v > 0 else RED


def money(v: float | None) -> str:
    if v is None:
        return "n/a"
    if abs(v) < 0.005:
        return "$0.00"
    return f"{'▲ +' if v > 0 else '▼ -'}${abs(v):,.2f}"


def pct(v: float | None) -> str:
    if v is None:
        return ""
    return f"{'+' if v >= 0 else '-'}{abs(v) * 100:.1f}%"


def short_name(ticker: str) -> str:
    """KXBTC15M-26OCT101900-00 -> "BTC 19:00"; KXNFLGAME-26OCT11DALNYG-DAL -> "DAL"."""
    parts = ticker.split("-")
    series = parts[0]
    if series.endswith("15M") and len(parts) >= 2:
        asset = series[2:-3] if series.startswith("KX") else series[:-3]
        return f"{asset} {parts[1][-4:-2]}:{parts[1][-2:]}"
    return parts[-1]


def tile(label: str, value: float | None, sub: str = "", hero: bool = False, text: str | None = None) -> str:
    shown = text if text is not None else money(value)
    color = MUTED if text is not None else color_of(value)
    return (f"<div class='pnl-tile{' hero' if hero else ''}'><div class='lbl'>{label}</div>"
            f"<div class='val' style='color:{color}'>{shown}</div><div class='sub'>{sub}</div></div>")


def signed_area(df: pd.DataFrame, x: str, y: str, y_title: str, height: int, tooltip: list) -> alt.Chart:
    """Line over a green area above zero and a red area below, with a hover crosshair."""
    df = df.copy()
    df["_pos"] = df[y].clip(lower=0)
    df["_neg"] = df[y].clip(upper=0)
    base = alt.Chart(df).encode(x=alt.X(f"{x}:T", title=None))
    hover = alt.selection_point(on="mouseover", nearest=True, fields=[x], empty=False, clear="mouseout")
    pos = base.mark_area(color=GREEN, opacity=0.22, interpolate="monotone").encode(y=alt.Y("_pos:Q", title=y_title))
    neg = base.mark_area(color=RED, opacity=0.22, interpolate="monotone").encode(y="_neg:Q")
    line = base.mark_line(strokeWidth=2, color=MUTED, interpolate="monotone").encode(y=f"{y}:Q")
    zero = alt.Chart(pd.DataFrame({"z": [0]})).mark_rule(color=MUTED, strokeDash=[4, 4]).encode(y="z:Q")
    rule = base.mark_rule(color=MUTED).encode(opacity=alt.condition(hover, alt.value(0.6), alt.value(0)),
                                             tooltip=tooltip).add_params(hover)
    dots = base.mark_circle(size=80, stroke="white", strokeWidth=2).encode(
        y=f"{y}:Q",
        color=alt.condition(f"datum['{y}'] >= 0", alt.value(GREEN), alt.value(RED)),
        opacity=alt.condition(hover, alt.value(1), alt.value(0)))
    return (pos + neg + zero + line + rule + dots).properties(height=height)


def diverging_bars(df: pd.DataFrame, label: str, value: str, tooltip: list, height: int) -> alt.Chart:
    base = alt.Chart(df).encode(y=alt.Y(f"{label}:N", sort="-x", title=None))
    bars = base.mark_bar(cornerRadiusEnd=4, size=16).encode(
        x=alt.X(f"{value}:Q", title="P&L ($)"),
        color=alt.condition(f"datum['{value}'] >= 0", alt.value(GREEN), alt.value(RED)),
        tooltip=tooltip)
    lab_pos = base.transform_filter(f"datum['{value}'] >= 0").mark_text(align="left", dx=6).encode(
        x=f"{value}:Q", text="_label:N")
    lab_neg = base.transform_filter(f"datum['{value}'] < 0").mark_text(align="right", dx=-6).encode(
        x=f"{value}:Q", text="_label:N")
    zero = alt.Chart(pd.DataFrame({"z": [0]})).mark_rule(color=MUTED).encode(x="z:Q")
    return (bars + zero + lab_pos + lab_neg).properties(height=height)


# ── Header ────────────────────────────────────────────────────────────────────

st.title("P&L")
st.caption(f"{'Paper trading' if s.paper_mode else 'LIVE trading'} on Kalshi {s.kalshi_env}, "
           f"{'15-minute crypto markets' if s.market_focus == 'crypto_15m' else 'live sports'}. "
           + (f"Take-profit +{s.risk_take_profit_pct * 100:.0f}%, stop-loss -{s.risk_stop_loss_pct * 100:.0f}%. "
              if s.risk_exits_enabled else "Positions are held to settlement (take-profit/stop-loss off). ")
           + 
           "Open positions are valued at what you'd get selling right now (your side's bid, minus the exit fee).")

if "live_hist" not in st.session_state:
    st.session_state.live_hist = deque(maxlen=600)       # ~10 minutes at 1/s


# ── Live: refreshes every second ──────────────────────────────────────────────

@st.fragment(run_every=1)
def live():
    now = datetime.now(timezone.utc)
    marks = j.get_marks()
    held = held_positions(j.open_positions())
    rows = []
    for p in held:
        m = marks.get(p.market_ticker)
        rows.append(position_pnl(p, m, s, seconds_since(m["updated_at"]) if m else None))
    unreal = sum(r.pnl_usd for r in rows if r.pnl_usd is not None)
    priced = [r for r in rows if r.pnl_usd is not None]

    realized = j.realized_pnl_total()
    tz = ZoneInfo(s.timezone)
    midnight = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    today = j.realized_pnl_since(midnight)
    settled = [r for r in j.rows(limit=5000)
               if r["status"] in REALIZED and r["paper"] == (1 if s.paper_mode else 0)]
    wins = sum(1 for r in settled if (r["realized_pnl_usd"] or 0) > 0)

    ages = [r.mark_age_s for r in rows if r.mark_age_s is not None]
    freshest = min(ages) if ages else None
    if not held:
        status = "<span class='live-dot stale'></span>No open positions right now"
    elif freshest is not None and freshest <= 5:
        status = f"<span class='live-dot'></span>LIVE · prices {freshest:.1f}s old"
    else:
        status = ("<span class='live-dot stale'></span>Waiting for prices"
                  if freshest is None else f"<span class='live-dot stale'></span>Prices {freshest:.0f}s old (engine may be asleep)")
    st.markdown(status, unsafe_allow_html=True)

    c = st.columns([1.35, 1, 1, 1, 1])
    c[0].markdown(tile("Total P&L", realized + unreal, "realized + open", hero=True), unsafe_allow_html=True)
    c[1].markdown(tile("Open P&L (live)", unreal if held else 0.0,
                       f"{len(held)} position{'s' if len(held) != 1 else ''}"), unsafe_allow_html=True)
    c[2].markdown(tile("Realized", realized, f"{len(settled)} settled"), unsafe_allow_html=True)
    c[3].markdown(tile("Today", today, f"limit -${s.risk_daily_loss_limit_usd:,.0f}"), unsafe_allow_html=True)
    rate = f"{wins / len(settled) * 100:.0f}%" if settled else "n/a"
    c[4].markdown(tile("Win rate", None, f"{wins}W / {len(settled) - wins}L", text=rate), unsafe_allow_html=True)

    hist = st.session_state.live_hist
    if held:
        hist.append({"ts": pd.Timestamp(now), "open_pnl": round(unreal, 4)})
    left, right = st.columns([3, 2])
    with left:
        st.markdown("##### Open P&L, live")
        if len(hist) >= 2:
            df = pd.DataFrame(list(hist))
            st.altair_chart(signed_area(df, "ts", "open_pnl", "Open P&L ($)", 230,
                                        [alt.Tooltip("ts:T", title="Time", format="%H:%M:%S"),
                                         alt.Tooltip("open_pnl:Q", title="Open P&L", format="+$,.2f")]),
                            use_container_width=True)
        else:
            st.caption("The line starts drawing once there's an open position and this page has been open a few seconds.")
    with right:
        st.markdown("##### By position")
        if priced:
            bdf = pd.DataFrame([{"label": f"{short_name(r.ticker)} {r.side.upper()}", "pnl": r.pnl_usd,
                                 "_label": money(r.pnl_usd), "market": r.title or r.ticker,
                                 "pct": pct(r.pnl_pct)} for r in priced])
            st.altair_chart(diverging_bars(bdf, "label", "pnl",
                                           [alt.Tooltip("market:N", title="Market"),
                                            alt.Tooltip("pnl:Q", title="P&L", format="+$,.2f"),
                                            alt.Tooltip("pct:N", title="Return")],
                                           max(120, 34 * len(bdf))),
                            use_container_width=True)
        else:
            st.caption("No priced positions.")

    st.markdown("##### Open positions")
    if rows:
        tdf = pd.DataFrame([{
            "Market": r.title or r.ticker, "Side": r.side.upper(), "Contracts": r.contracts,
            "Entry": f"{r.entry_cents:.0f}c", "Bid now": "n/a" if r.bid_cents is None else f"{r.bid_cents:.0f}c",
            "Cost": r.cost_usd, "Value now": r.value_usd, "P&L": r.pnl_usd, "Return": r.pnl_pct,
            "Price age": "n/a" if r.mark_age_s is None else f"{r.mark_age_s:.0f}s",
        } for r in sorted(rows, key=lambda r: (r.pnl_usd is None, -(r.pnl_usd or 0)))])
        styler = (tdf.style
                  .format({"Cost": "${:,.2f}", "Value now": lambda v: "n/a" if pd.isna(v) else f"${v:,.2f}",
                           "P&L": lambda v: money(None if pd.isna(v) else v),
                           "Return": lambda v: pct(None if pd.isna(v) else v)})
                  .map(lambda v: f"color: {color_of(None if pd.isna(v) else v)}; font-weight: 600",
                       subset=["P&L", "Return"]))
        st.dataframe(styler, hide_index=True, use_container_width=True)
    else:
        st.caption("Nothing open right now." + (" New 15-minute markets open every quarter hour."
                                                if s.market_focus == "crypto_15m" else
                                                " The bot only trades games in progress."))


live()
st.divider()


# ── History: refreshes every 15 seconds ───────────────────────────────────────

@st.fragment(run_every=15)
def history():
    settled = [r for r in j.rows(limit=5000)
               if r["status"] in REALIZED and r["paper"] == (1 if s.paper_mode else 0)]
    if not settled:
        st.markdown("##### Equity curve")
        st.info("No closed trades yet. Positions count here once they settle or hit take-profit / stop-loss.")
        return
    df = pd.DataFrame(settled)
    df["settled_at"] = pd.to_datetime(df["settled_at"], utc=True)
    df = df.sort_values("settled_at")
    df["pnl"] = df["realized_pnl_usd"].fillna(0.0)
    df["cum"] = df["pnl"].cumsum()
    df["trade"] = df["market_ticker"].map(short_name) + " " + df["side"].str.upper()

    st.markdown("##### Equity curve (realized)")
    st.altair_chart(signed_area(df, "settled_at", "cum", "Cumulative P&L ($)", 280,
                                [alt.Tooltip("settled_at:T", title="Settled", format="%b %d %H:%M"),
                                 alt.Tooltip("trade:N", title="Trade"),
                                 alt.Tooltip("pnl:Q", title="Trade P&L", format="+$,.2f"),
                                 alt.Tooltip("cum:Q", title="Running total", format="+$,.2f")]),
                    use_container_width=True)

    left, right = st.columns(2)
    with left:
        st.markdown("##### By market type")
        g = df.groupby("series_ticker").agg(pnl=("pnl", "sum"), n=("pnl", "size"),
                                             wins=("pnl", lambda x: int((x > 0).sum()))).reset_index()
        g["_label"] = g["pnl"].map(money)
        g["record"] = g["wins"].astype(str) + "W / " + (g["n"] - g["wins"]).astype(str) + "L"
        st.altair_chart(diverging_bars(g, "series_ticker", "pnl",
                                       [alt.Tooltip("series_ticker:N", title="Series"),
                                        alt.Tooltip("pnl:Q", title="P&L", format="+$,.2f"),
                                        alt.Tooltip("record:N", title="Record")],
                                       max(120, 34 * len(g))),
                        use_container_width=True)
    with right:
        st.markdown("##### Recent results")
        recent = df.sort_values("settled_at", ascending=False).head(15)
        rdf = pd.DataFrame({
            "When": recent["settled_at"].map(lambda t: ago(t.isoformat())),
            "Market": recent["title"].fillna(recent["market_ticker"]),
            "Side": recent["side"].str.upper(),
            "Result": recent.apply(lambda r: "EXIT (TP/SL)" if r["status"] == "CLOSED"
                                   else str(r["result"]).upper(), axis=1),
            "P&L": recent["pnl"],
        })
        st.dataframe(rdf.style.format({"P&L": money})
                     .map(lambda v: f"color: {color_of(v)}; font-weight: 600", subset=["P&L"]),
                     hide_index=True, use_container_width=True)


history()
