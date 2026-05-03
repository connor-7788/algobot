"""
Combined AlgoTrader dashboard.

Run:
  streamlit run dashboard_combined.py --server.port 8500
"""

import json
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import config


STATE_FILE = Path("logs/engine_state.json")
PLOT_BG = "#0b0f14"
PANEL_BG = "#151b23"
GRID = "#29323d"
TEXT = "#d9e2ec"
MUTED = "#8594a6"
GREEN = "#2fbf71"
RED = "#ef476f"
BLUE = "#4ea8de"
YELLOW = "#f2c14e"


st.set_page_config(
    page_title="AlgoTrader Command Center",
    page_icon="AT",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
    <style>
      .block-container { padding: 1rem 1.25rem 2rem; }
      .stApp { background: #0b0f14; color: #d9e2ec; }
      .metric-card {
        background: #151b23;
        border: 1px solid #29323d;
        border-radius: 8px;
        padding: 11px 13px;
        min-height: 76px;
      }
      .metric-label {
        color: #8594a6;
        font-size: 11px;
        text-transform: uppercase;
        letter-spacing: .08em;
        margin-bottom: 6px;
      }
      .metric-value { color: #d9e2ec; font-size: 22px; font-weight: 750; }
      .section {
        color: #9cc9ff;
        font-size: 12px;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: .11em;
        margin: 14px 0 7px;
      }
      div[data-testid="stDataFrame"] {
        border: 1px solid #29323d;
        border-radius: 8px;
      }
    </style>
    """,
    unsafe_allow_html=True,
)


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {
        "ts": datetime.now().isoformat(),
        "mode": "WAITING",
        "risk": {},
        "pnl": {},
        "equity_positions": [],
        "kalshi_positions": [],
        "signals": {},
        "arb_table": [],
        "trade_log": [],
        "kalshi_trades": [],
        "orderbook": {},
        "velocity": {},
        "odds_status": {"markets_tracked": 0, "books": []},
    }


def money(value) -> str:
    value = float(value or 0)
    sign = "+" if value > 0 else ""
    return f"{sign}${value:,.2f}"


def pct(value) -> str:
    value = float(value or 0)
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.2f}%"


def metric(col, label: str, value: str, color: str | None = None):
    style = f"color:{color};" if color else ""
    col.markdown(
        f"""
        <div class="metric-card">
          <div class="metric-label">{label}</div>
          <div class="metric-value" style="{style}">{value}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def base_layout(height=230, title=""):
    return dict(
        height=height,
        title=dict(text=title, font=dict(size=13, color=TEXT)),
        paper_bgcolor=PLOT_BG,
        plot_bgcolor=PLOT_BG,
        font=dict(color=MUTED, size=11),
        margin=dict(l=35, r=18, t=36, b=28),
        xaxis=dict(gridcolor=GRID, zeroline=False),
        yaxis=dict(gridcolor=GRID, zeroline=False),
    )


def pnl_chart(pnl: dict) -> go.Figure:
    labels = ["Stocks Open", "Stocks Realized", "Sports Open", "Sports Realized"]
    values = [
        pnl.get("equity_open", 0),
        pnl.get("equity_realized", 0),
        pnl.get("kalshi_open", 0),
        pnl.get("kalshi_realized", 0),
    ]
    colors = [GREEN if v >= 0 else RED for v in values]
    fig = go.Figure(go.Bar(x=labels, y=values, marker_color=colors))
    fig.update_layout(**base_layout(230, "Profit and Loss by Engine"))
    fig.update_yaxes(tickprefix="$")
    return fig


def exposure_chart(state: dict) -> go.Figure:
    stock_positions = state.get("equity_positions", [])
    kalshi_positions = state.get("kalshi_positions", [])
    labels = []
    values = []

    for p in stock_positions:
        labels.append(p.get("symbol", "stock"))
        values.append(abs(float(p.get("qty", 0)) * float(p.get("current", p.get("entry", 0)))))
    for p in kalshi_positions:
        labels.append(p.get("market_id", "kalshi")[:18])
        values.append(abs(float(p.get("contracts", 0)) * float(p.get("entry_price", 0)) * 100))

    if not labels:
        fig = go.Figure()
        fig.update_layout(**base_layout(230, "Open Exposure"))
        return fig

    fig = go.Figure(go.Treemap(
        labels=labels,
        parents=[""] * len(labels),
        values=values,
        marker=dict(colors=[BLUE] * len(labels), line=dict(color=PLOT_BG, width=2)),
        texttemplate="%{label}<br>$%{value:,.0f}",
    ))
    fig.update_layout(**base_layout(230, "Open Exposure"))
    return fig


def velocity_chart(state: dict, market_id: str) -> go.Figure:
    rows = state.get("velocity", {}).get(market_id, [])
    fig = go.Figure()
    if rows:
        fig.add_trace(go.Scatter(
            x=[r.get("t", i) for i, r in enumerate(rows)],
            y=[r.get("price", 0) for r in rows],
            mode="lines",
            line=dict(color=BLUE, width=2),
            fill="tozeroy",
            fillcolor="rgba(78,168,222,0.08)",
            name="Price",
        ))
    fig.update_layout(**base_layout(220, f"Sports Price Velocity {market_id[:24]}"))
    fig.update_yaxes(range=[0, 1], tickformat=".2f")
    return fig


def orderbook_chart(state: dict, market_id: str) -> go.Figure:
    ob = state.get("orderbook", {}).get(market_id, {})
    bids = ob.get("yes_bids", [])
    asks = ob.get("yes_asks", [])
    fig = go.Figure()
    if bids:
        fig.add_trace(go.Bar(x=[b[0] for b in bids], y=[b[1] for b in bids], marker_color=GREEN, name="Bids"))
    if asks:
        fig.add_trace(go.Bar(x=[a[0] for a in asks], y=[a[1] for a in asks], marker_color=RED, name="Asks"))
    fig.update_layout(**base_layout(220, "Kalshi Order Book"), barmode="overlay")
    return fig


@st.fragment(run_every=max(1, config.DASH_REFRESH_MS // 1000))
def render():
    state = load_state()
    risk = state.get("risk", {})
    pnl = state.get("pnl", {})
    arb = state.get("arb_table", [])
    ts = state.get("ts", "")

    left, right = st.columns([3, 2])
    with left:
        st.markdown("## AlgoTrader Command Center")
    with right:
        if st.button("Refresh now", key="combined_refresh"):
            st.rerun()
        st.markdown(
            f"<div style='text-align:right;color:{MUTED};padding-top:13px;'>"
            f"{state.get('mode', 'WAITING')} | Last update {ts[-8:] if ts else 'waiting'} | Updates every {max(1, config.DASH_REFRESH_MS // 1000)}s</div>",
            unsafe_allow_html=True,
        )

    total = pnl.get("total", 0)
    day_pct = risk.get("daily_pnl_pct", 0)
    m = st.columns(8)
    metric(m[0], "Portfolio", f"${float(risk.get('current_equity') or 0):,.0f}")
    metric(m[1], "Total P&L", money(total), GREEN if total >= 0 else RED)
    metric(m[2], "Day P&L", pct(day_pct), GREEN if day_pct >= 0 else RED)
    metric(m[3], "Stock Open", money(pnl.get("equity_open", 0)), GREEN if pnl.get("equity_open", 0) >= 0 else RED)
    metric(m[4], "Sports Open", money(pnl.get("kalshi_open", 0)), GREEN if pnl.get("kalshi_open", 0) >= 0 else RED)
    metric(m[5], "Open Stocks", str(risk.get("open_equity", 0)))
    metric(m[6], "Open Sports", str(risk.get("open_kalshi", 0)))
    metric(m[7], "Best Edge", f"{max((a.get('edge_pct', 0) for a in arb), default=0) * 100:.1f}%")

    if risk.get("kill_switch"):
        st.error("Kill switch active. New orders are halted.")
    if risk.get("sentiment_halt"):
        st.warning(f"Sentiment halt: {risk.get('sentiment_reason', '')}")

    c1, c2 = st.columns([3, 2])
    with c1:
        st.plotly_chart(pnl_chart(pnl), use_container_width=True)
    with c2:
        st.plotly_chart(exposure_chart(state), use_container_width=True)

    stock_col, sports_col = st.columns(2)

    with stock_col:
        st.markdown('<div class="section">Stocks</div>', unsafe_allow_html=True)
        positions = state.get("equity_positions", [])
        if positions:
            st.dataframe(pd.DataFrame([{
                "Symbol": p.get("symbol"),
                "Side": p.get("side", "").upper(),
                "Qty": p.get("qty"),
                "Entry": f"${p.get('entry', 0):.2f}",
                "Now": f"${p.get('current', 0):.2f}",
                "P&L": money(p.get("pnl_usd", 0)),
                "P&L %": pct(p.get("pnl_pct", 0)),
                "Held": f"{p.get('mins_held', 0)}m",
            } for p in positions]), use_container_width=True, hide_index=True)
        else:
            st.caption("No open stock positions.")

        signals = state.get("signals", {})
        if signals:
            st.dataframe(pd.DataFrame([{
                "Symbol": sym,
                "Price": sig.get("price"),
                "RSI": sig.get("rsi"),
                "EMA8": sig.get("ema_fast"),
                "EMA21": sig.get("ema_slow"),
                "ADX": sig.get("adx"),
                "Trend": sig.get("trend"),
            } for sym, sig in signals.items()]), use_container_width=True, hide_index=True)

    with sports_col:
        st.markdown('<div class="section">Sports Arbitrage</div>', unsafe_allow_html=True)
        if arb:
            st.dataframe(pd.DataFrame([{
                "Event": a.get("event", "")[:36],
                "YES Objective": a.get("yes_objective", "")[:44],
                "To Win": a.get("win_condition", "")[:52],
                "Sport": a.get("sport"),
                "Side": a.get("side", "").upper(),
                "Price": a.get("price"),
                "K Odds": a.get("kalshi_american_odds", ""),
                "Book Home": a.get("book_home_odds", ""),
                "Book Away": a.get("book_away_odds", ""),
                "Book Prob": a.get("book_prob"),
                "Edge": pct(a.get("edge_pct", 0) * 100),
                "Score": a.get("confidence_score", ""),
                "Contracts": a.get("contracts") if a.get("tradeable") else "-",
                "Status": "TRADE" if a.get("tradeable") else "WATCH",
            } for a in arb[:25]]), use_container_width=True, hide_index=True)
        else:
            st.caption("No sports opportunities yet.")

        kalshi_positions = state.get("kalshi_positions", [])
        if kalshi_positions:
            st.dataframe(pd.DataFrame([{
                "Market": p.get("market_id", "")[:22],
                "To Win": p.get("win_condition", "")[:52],
                "Side": p.get("side", "").upper(),
                "Contracts": p.get("contracts"),
                "Entry": f"{p.get('entry_price', 0):.3f}",
                "K Odds": p.get("kalshi_american_odds", ""),
                "Now": f"{p.get('current_price', p.get('entry_price', 0)):.3f}",
                "P&L": money(p.get("pnl", 0)),
            } for p in kalshi_positions]), use_container_width=True, hide_index=True)

    focus = arb[0].get("market_id") if arb else ""
    v1, v2 = st.columns(2)
    with v1:
        st.plotly_chart(velocity_chart(state, focus), use_container_width=True)
    with v2:
        st.plotly_chart(orderbook_chart(state, focus), use_container_width=True)

    t1, t2 = st.columns(2)
    with t1:
        st.markdown('<div class="section">Stock Trades</div>', unsafe_allow_html=True)
        trades = state.get("trade_log", [])
        if trades:
            st.dataframe(pd.DataFrame(trades[:20]), use_container_width=True, hide_index=True)
        else:
            st.caption("No stock trades yet.")
    with t2:
        st.markdown('<div class="section">Sports Trades</div>', unsafe_allow_html=True)
        sports_trades = state.get("kalshi_trades", [])
        if sports_trades:
            st.dataframe(pd.DataFrame(sports_trades[:20]), use_container_width=True, hide_index=True)
        else:
            st.caption("No sports trades yet.")


if __name__ == "__main__":
    render()
