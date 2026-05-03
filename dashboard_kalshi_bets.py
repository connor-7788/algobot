"""
Standalone Kalshi high-confidence betting dashboard.

Run:
  streamlit run dashboard_kalshi_bets.py --server.port 8502
"""

import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import config


STATE_FILE = Path("logs/engine_state.json")
BG = "#0b0f14"
PANEL = "#151b23"
GRID = "#29323d"
TEXT = "#d9e2ec"
MUTED = "#8594a6"
GREEN = "#2fbf71"
RED = "#ef476f"
BLUE = "#4ea8de"
YELLOW = "#f2c14e"


st.set_page_config(
    page_title="Kalshi High-Confidence Bets",
    page_icon="K",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
    <style>
      .stApp { background: #0b0f14; color: #d9e2ec; }
      .block-container { padding: 1rem 1.25rem 2rem; }
      .metric-card {
        background: #151b23;
        border: 1px solid #29323d;
        border-radius: 8px;
        padding: 12px 14px;
        min-height: 76px;
      }
      .metric-label {
        color: #8594a6;
        font-size: 11px;
        text-transform: uppercase;
        letter-spacing: .08em;
      }
      .metric-value { color: #d9e2ec; font-size: 22px; font-weight: 750; margin-top: 6px; }
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
        "risk": {},
        "pnl": {},
        "confidence_table": [],
        "kalshi_positions": [],
        "kalshi_trades": [],
        "orderbook": {},
        "velocity": {},
    }


def money(value) -> str:
    value = float(value or 0)
    return f"{'+' if value > 0 else ''}${value:,.2f}"


def pct(value) -> str:
    value = float(value or 0)
    return f"{value:.2f}%"


def metric(col, label, value, color=None):
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
        paper_bgcolor=BG,
        plot_bgcolor=BG,
        font=dict(color=MUTED, size=11),
        margin=dict(l=35, r=18, t=36, b=28),
        xaxis=dict(gridcolor=GRID, zeroline=False),
        yaxis=dict(gridcolor=GRID, zeroline=False),
    )


def confidence_chart(rows: list[dict]) -> go.Figure:
    top = rows[:10]
    fig = go.Figure(go.Bar(
        x=[r.get("score", 0) for r in top],
        y=[r.get("market_id", "")[:28] for r in top],
        orientation="h",
        marker_color=[GREEN if r.get("approved") else BLUE for r in top],
    ))
    fig.update_layout(**base_layout(280, "Confidence Score Queue"))
    fig.update_xaxes(range=[0, 100])
    return fig


def velocity_chart(state: dict, market_id: str) -> go.Figure:
    rows = state.get("velocity", {}).get(market_id, [])
    fig = go.Figure()
    if rows:
        fig.add_trace(go.Scatter(
            x=[r.get("t", i) for i, r in enumerate(rows)],
            y=[r.get("price", 0) for r in rows],
            mode="lines",
            fill="tozeroy",
            fillcolor="rgba(78,168,222,0.08)",
            line=dict(color=BLUE, width=2),
        ))
    fig.update_layout(**base_layout(240, f"Price Backtrack {market_id[:24]}"))
    fig.update_yaxes(range=[0, 1], tickformat=".2f")
    return fig


@st.fragment(run_every=max(1, config.DASH_REFRESH_MS // 1000))
def render():
    state = load_state()
    risk = state.get("risk", {})
    pnl = state.get("pnl", {})
    confidence = state.get("confidence_table", [])
    positions = state.get("kalshi_positions", [])
    trades = state.get("kalshi_trades", [])
    ts = state.get("ts", "")

    c1, c2 = st.columns([3, 2])
    with c1:
        st.markdown("## Kalshi High-Confidence Bets")
    with c2:
        if st.button("Refresh now", key="kalshi_refresh"):
            st.rerun()
        st.markdown(
            f"<div style='text-align:right;color:{MUTED};padding-top:13px;'>"
            f"Last update {ts[-8:] if ts else 'waiting'} | Updates every {max(1, config.DASH_REFRESH_MS // 1000)}s | Max 3 bets | 5% stake</div>",
            unsafe_allow_html=True,
        )

    approved = [r for r in confidence if r.get("approved")]
    best = max((r.get("score", 0) for r in confidence), default=0)
    open_pnl = pnl.get("kalshi_open", 0)
    realized = pnl.get("kalshi_realized", 0)

    cols = st.columns(7)
    metric(cols[0], "Open Bets", f"{len(positions)}/3")
    metric(cols[1], "Open P&L", money(open_pnl), GREEN if open_pnl >= 0 else RED)
    metric(cols[2], "Realized P&L", money(realized), GREEN if realized >= 0 else RED)
    metric(cols[3], "Bet Size", "5%")
    metric(cols[4], "Queued Approved", str(len(approved)))
    metric(cols[5], "Best Score", f"{best}/100", GREEN if best >= config.HIGH_CONF_MIN_SCORE else YELLOW)
    metric(cols[6], "Min Edge", pct(config.HIGH_CONF_MIN_EDGE_PCT * 100))

    if risk.get("kill_switch"):
        st.error("Kill switch active. New Kalshi bets are halted.")

    left, right = st.columns([3, 2])
    with left:
        st.markdown('<div class="section">Confidence Queue</div>', unsafe_allow_html=True)
        if confidence:
            st.dataframe(pd.DataFrame([{
                "Market": r.get("market_id", "")[:24],
                "Event": r.get("event", "")[:42],
                "YES Objective": r.get("yes_objective", "")[:56],
                "To Win": r.get("win_condition", "")[:64],
                "Sport": r.get("sport"),
                "Side": r.get("side", "").upper(),
                "Score": r.get("score"),
                "Kalshi": f"{r.get('price', 0):.3f}",
                "K Odds": r.get("kalshi_american_odds", ""),
                "Book Home": r.get("book_home_odds", ""),
                "Book Away": r.get("book_away_odds", ""),
                "Edge": pct(r.get("edge_pct", 0) * 100),
                "Spread": pct(r.get("spread", 0) * 100),
                "Books": r.get("book_count"),
                "Confirm": r.get("confirmations"),
                "Stake": money(r.get("stake_usd", 0)),
                "Status": "READY" if r.get("approved") else r.get("reason", ""),
            } for r in confidence[:30]]), use_container_width=True, hide_index=True)
        else:
            st.caption("Waiting for confirmed opportunities.")

    with right:
        st.plotly_chart(confidence_chart(confidence), use_container_width=True)

    st.markdown('<div class="section">Open High-Confidence Bets</div>', unsafe_allow_html=True)
    if positions:
        st.dataframe(pd.DataFrame([{
            "Market": p.get("market_id", "")[:24],
            "Event": p.get("event", "")[:42],
            "YES Objective": p.get("yes_objective", "")[:56],
            "To Win": p.get("win_condition", "")[:64],
            "Sport": p.get("sport"),
            "Side": p.get("side", "").upper(),
            "Contracts": p.get("contracts"),
            "Entry": f"{p.get('entry_price', 0):.3f}",
            "K Odds": p.get("kalshi_american_odds", ""),
            "Book Home": p.get("book_home_odds", ""),
            "Book Away": p.get("book_away_odds", ""),
            "Now": f"{p.get('current_price', p.get('entry_price', 0)):.3f}",
            "Notional": money(p.get("notional", 0)),
            "P&L": money(p.get("pnl", 0)),
            "Edge": pct(p.get("edge_pct", 0) * 100),
            "Score": p.get("confidence_score", ""),
        } for p in positions]), use_container_width=True, hide_index=True)
    else:
        st.caption("No open Kalshi bets.")

    focus = positions[0].get("market_id") if positions else (confidence[0].get("market_id") if confidence else "")
    st.plotly_chart(velocity_chart(state, focus), use_container_width=True)

    st.markdown('<div class="section">Bet Log</div>', unsafe_allow_html=True)
    if trades:
        st.dataframe(pd.DataFrame(trades[:30]), use_container_width=True, hide_index=True)
    else:
        st.caption("No Kalshi bet history yet.")


if __name__ == "__main__":
    render()
