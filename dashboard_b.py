"""
dashboards/dashboard_b.py — Sharp Sports Command Dashboard.

Run: streamlit run dashboards/dashboard_b.py --server.port 8502

Panels:
  • Live Arbitrage Table (edge %, Kelly fraction, highlighted if >5%)
  • Kalshi Market Depth / Spread chart (order book visualisation)
  • Price Velocity line chart (in-play price movement per market)
  • Open Kalshi Positions (contracts, entry price, current, P&L)
  • Completed Kalshi Trades log
  • Odds API bridge status (last refresh, markets tracked)
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import time
import math
from datetime import datetime
from pathlib import Path

import streamlit as st
import pandas as pd
import plotly.graph_objects as go

import config

st.set_page_config(
    page_title="AlgoTrader — Sharp Sports Command",
    page_icon="🏈",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── Dark theme ────────────────────────────────────────────────────────────────
st.markdown("""
<style>
  .main { background: #0d1117; color: #e6edf3; }
  .block-container { padding: 1rem 1.5rem; }
  .edge-high { background: rgba(63,185,80,0.15); border-left: 3px solid #3fb950; padding: 2px 6px; border-radius: 3px; }
  .edge-med  { background: rgba(227,179,65,0.12); border-left: 3px solid #e3b341; padding: 2px 6px; border-radius: 3px; }
  .section-header { color: #58a6ff; font-size: 13px; font-weight: 600;
                    text-transform: uppercase; letter-spacing: 1.5px; margin: 12px 0 6px; }
  .metric-card { background: #161b22; border: 1px solid #30363d;
                 border-radius: 8px; padding: 12px 16px; text-align: center; }
  .metric-label { color: #8b949e; font-size: 11px; text-transform: uppercase; }
  .metric-value { color: #e6edf3; font-size: 22px; font-weight: 700; margin-top: 4px; }
</style>
""", unsafe_allow_html=True)

# ── Load state ────────────────────────────────────────────────────────────────
STATE_FILE = Path("logs/engine_state.json")

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except:
            pass
    return _mock_state()

def _mock_state() -> dict:
    import random
    random.seed(int(time.time() / 15))
    arb = [
        {
            "market_id": "NFL-20241215-KC-BUF",
            "event": "Kansas City Chiefs vs Buffalo Bills",
            "sport": "NFL",
            "side": "yes",
            "price": 0.44,
            "book_prob": 0.512,
            "edge_pct": 0.072,
            "contracts": 34,
            "kelly": 0.082,
            "spread": 0.03,
            "tradeable": True,
        },
        {
            "market_id": "NBA-20241215-LAL-GSW",
            "event": "Lakers vs Warriors",
            "sport": "NBA",
            "side": "no",
            "price": 0.38,
            "book_prob": 0.433,
            "edge_pct": 0.053,
            "contracts": 21,
            "kelly": 0.061,
            "spread": 0.04,
            "tradeable": True,
        },
        {
            "market_id": "NFL-20241215-DAL-PHI",
            "event": "Cowboys vs Eagles",
            "sport": "NFL",
            "side": "yes",
            "price": 0.51,
            "book_prob": 0.538,
            "edge_pct": 0.028,
            "contracts": 0,
            "kelly": 0.028,
            "spread": 0.02,
            "tradeable": False,
        },
    ]
    velocity = {
        "NFL-20241215-KC-BUF": [
            {"t": i * 5, "price": 0.44 + random.gauss(0, 0.008)} for i in range(60)
        ],
        "NBA-20241215-LAL-GSW": [
            {"t": i * 5, "price": 0.38 + random.gauss(0, 0.005)} for i in range(60)
        ],
    }
    orderbook = {
        "NFL-20241215-KC-BUF": {
            "yes_bids": [(0.43, 120),(0.42, 80),(0.41, 200)],
            "yes_asks": [(0.45, 90),(0.46, 140),(0.47, 60)],
            "no_bids":  [(0.54, 100),(0.53, 75)],
            "no_asks":  [(0.56, 110),(0.57, 90)],
        }
    }
    return {
        "ts":           datetime.now().isoformat(),
        "arb_table":    arb,
        "velocity":     velocity,
        "orderbook":    orderbook,
        "kalshi_positions": [
            {"market_id":"NFL-20241215-KC-BUF","event":"KC vs BUF","sport":"NFL",
             "side":"yes","contracts":34,"entry_price":0.44,
             "current_price": 0.44 + random.gauss(0,0.01), "pnl": random.gauss(40,20)},
        ],
        "kalshi_trades": [
            {"ts":"11:20:05","market_id":"NBA-20241215-LAL-GSW","sport":"NBA","side":"yes",
             "contracts":15,"entry":0.41,"exit":0.87,"pnl":690,"action":"EARLY_EXIT"},
            {"ts":"10:55:18","market_id":"NFL-20241215-NE-NYJ","sport":"NFL","side":"no",
             "contracts":8,"entry":0.38,"exit":0.32,"pnl":48,"action":"TP"},
        ],
        "risk": {
            "kill_switch": False, "sentiment_halt": False,
            "open_kalshi": 1, "kalshi_allocated": 3400,
            "current_vix": 17.3,
        },
        "odds_status": {
            "last_refresh": datetime.now().strftime("%H:%M:%S"),
            "markets_tracked": 47,
            "books": ["FanDuel", "DraftKings"],
        }
    }

# ── Chart helpers ─────────────────────────────────────────────────────────────
PLOT_BG   = "#0d1117"
GRID_COLOR= "#21262d"
FONT_COLOR= "#8b949e"

def base_layout(**kw):
    return dict(
        paper_bgcolor=PLOT_BG, plot_bgcolor=PLOT_BG,
        font=dict(color=FONT_COLOR, size=11),
        margin=dict(l=40, r=20, t=30, b=30), **kw
    )


def velocity_chart(state: dict, market_id: str) -> go.Figure:
    vel_data = state.get("velocity", {}).get(market_id, [])
    if not vel_data:
        fig = go.Figure()
        fig.update_layout(**base_layout(height=220))
        return fig

    x      = [d["t"] for d in vel_data]
    prices = [d["price"] for d in vel_data]

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=x, y=prices, name="YES Price",
        line=dict(color="#58a6ff", width=2),
        fill="tozeroy", fillcolor="rgba(88,166,255,0.05)",
    ))

    # Detect velocity spikes
    if len(prices) > 5:
        window = config.VELOCITY_WINDOW_S
        for i in range(5, len(prices)):
            delta = abs(prices[i] - prices[i-5])
            if delta >= config.VELOCITY_THRESHOLD:
                fig.add_vline(
                    x=x[i], line_color="#e3b341", line_dash="dash", line_width=1,
                    annotation_text="⚡", annotation_font_color="#e3b341",
                )

    fig.update_layout(
        **base_layout(height=220),
        title=dict(text=f"Price Velocity — {market_id[:35]}",
                   font=dict(size=11, color="#e6edf3")),
        xaxis=dict(showgrid=True, gridcolor=GRID_COLOR, title="seconds"),
        yaxis=dict(showgrid=True, gridcolor=GRID_COLOR,
                   range=[0, 1], tickformat=".2f", title="Price"),
    )
    return fig


def orderbook_chart(state: dict, market_id: str) -> go.Figure:
    ob = state.get("orderbook", {}).get(market_id, {})
    if not ob:
        fig = go.Figure()
        fig.update_layout(**base_layout(height=220))
        return fig

    yes_bids = ob.get("yes_bids", [])
    yes_asks = ob.get("yes_asks", [])

    bid_prices = [b[0] for b in yes_bids]
    bid_sizes  = [b[1] for b in yes_bids]
    ask_prices = [a[0] for a in yes_asks]
    ask_sizes  = [a[1] for a in yes_asks]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=bid_prices, y=bid_sizes, name="Bids (YES)",
        marker_color="rgba(63,185,80,0.7)", orientation="v",
    ))
    fig.add_trace(go.Bar(
        x=ask_prices, y=ask_sizes, name="Asks (YES)",
        marker_color="rgba(248,81,73,0.7)", orientation="v",
    ))

    if yes_bids and yes_asks:
        mid = (max(bid_prices) + min(ask_prices)) / 2
        spread = min(ask_prices) - max(bid_prices)
        fig.add_vline(x=mid, line_color="#e6edf3", line_dash="dot", line_width=1,
                      annotation_text=f"mid {mid:.2f}",
                      annotation_font_color="#e6edf3")

    fig.update_layout(
        **base_layout(height=220),
        title=dict(text="Order Book Depth", font=dict(size=11, color="#e6edf3")),
        barmode="overlay",
        xaxis=dict(showgrid=True, gridcolor=GRID_COLOR, tickformat=".2f"),
        yaxis=dict(showgrid=True, gridcolor=GRID_COLOR, title="Contracts"),
        legend=dict(orientation="h", y=1.08, bgcolor="rgba(0,0,0,0)"),
    )
    return fig


# ── Main layout ───────────────────────────────────────────────────────────────

@st.fragment(run_every=max(1, config.DASH_REFRESH_MS // 1000))
def render():
    state = load_state()
    risk  = state.get("risk", {})
    ts    = state.get("ts", "")

    # ── Header ────────────────────────────────────────────────────────────────
    c1, c2, c3 = st.columns([3,1,2])
    with c1:
        st.markdown("## 🏈 Sharp Sports Command")
    with c2:
        st.markdown(f"<div style='color:#58a6ff;font-size:13px;padding-top:16px;'>"
                    f"Kalshi {'DEMO' if config.KALSHI_ENV == 'demo' else 'LIVE'}</div>",
                    unsafe_allow_html=True)
    with c3:
        odds = state.get("odds_status", {})
        st.markdown(f"<div style='color:#8b949e;font-size:11px;padding-top:18px;text-align:right;'>"
                    f"Odds API: {odds.get('markets_tracked',0)} markets | "
                    f"Books: {', '.join(odds.get('books',[]))}</div>",
                    unsafe_allow_html=True)

    if risk.get("kill_switch"):
        st.error("⛔ KILL SWITCH ACTIVE — Kalshi trading halted")
    if risk.get("sentiment_halt"):
        st.warning(f"⚠️ SENTIMENT HALT — {risk.get('sentiment_reason','')[:80]}")

    # ── Stats strip ───────────────────────────────────────────────────────────
    arb_table = state.get("arb_table", [])
    high_edge = [a for a in arb_table if a.get("edge_pct", 0) >= config.MIN_EDGE_PCT]

    cols = st.columns(5)
    def metric(col, label, value):
        col.markdown(f"""<div class="metric-card">
          <div class="metric-label">{label}</div>
          <div class="metric-value">{value}</div></div>""", unsafe_allow_html=True)

    metric(cols[0], "Open Positions",  risk.get("open_kalshi", 0))
    metric(cols[1], "Capital Deployed",f"${risk.get('kalshi_allocated',0):,.0f}")
    metric(cols[2], "Arb Opportunities",len(high_edge))
    metric(cols[3], "Best Edge",
           f"{max((a['edge_pct'] for a in arb_table), default=0)*100:.1f}%")
    metric(cols[4], "Markets Scanned", len(arb_table))

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Row 1: Arb table ──────────────────────────────────────────────────────
    st.markdown('<div class="section-header">Live Arbitrage Table</div>',
                unsafe_allow_html=True)
    st.markdown("<div style='color:#8b949e;font-size:11px;margin-bottom:6px;'>"
                "Green highlight = edge > 5% (tradeable). "
                "Edge = no-vig book probability − Kalshi mid-price.</div>",
                unsafe_allow_html=True)

    if arb_table:
        rows = []
        for a in arb_table:
            edge_pct  = a["edge_pct"]
            edge_str  = f"{edge_pct*100:.2f}%"
            rows.append({
                "Event":      a["event"][:40],
                "Sport":      a["sport"],
                "Side":       a["side"].upper(),
                "Kalshi":     f"{a['price']:.3f}",
                "Book Prob":  f"{a['book_prob']:.3f}",
                "Edge":       edge_str,
                "Kelly %":    f"{a['kelly']*100:.1f}%",
                "Contracts":  a["contracts"] if a["tradeable"] else "—",
                "Spread":     f"{a['spread']:.3f}",
                "Status":     "✅ TRADE" if a["tradeable"] else "⏳ WATCH",
            })
        df = pd.DataFrame(rows)
        st.dataframe(
            df.style.apply(
                lambda row: [
                    "background-color: rgba(63,185,80,0.12);" if row["Status"] == "✅ TRADE"
                    else "" for _ in row
                ], axis=1
            ),
            use_container_width=True, hide_index=True
        )
    else:
        st.markdown("<div style='color:#8b949e;padding:12px;'>Scanning for opportunities…</div>",
                    unsafe_allow_html=True)

    # ── Row 2: Velocity + Order Book ──────────────────────────────────────────
    col_vel, col_ob = st.columns(2)

    # Pick most interesting market for depth / velocity display
    focus_market = arb_table[0]["market_id"] if arb_table else ""

    with col_vel:
        st.plotly_chart(velocity_chart(state, focus_market), use_container_width=True)
    with col_ob:
        st.plotly_chart(orderbook_chart(state, focus_market), use_container_width=True)

    # ── Row 3: Open positions + Trade log ─────────────────────────────────────
    col_pos, col_log = st.columns(2)

    with col_pos:
        st.markdown('<div class="section-header">Open Kalshi Positions</div>',
                    unsafe_allow_html=True)
        positions = state.get("kalshi_positions", [])
        if positions:
            rows = []
            for p in positions:
                cur = p.get("current_price", p["entry_price"])
                pnl = p.get("pnl", (cur - p["entry_price"]) * p["contracts"] * 100)
                rows.append({
                    "Event":     p.get("event","")[:30],
                    "Sport":     p.get("sport",""),
                    "Side":      p["side"].upper(),
                    "Contracts": p["contracts"],
                    "Entry":     f"{p['entry_price']:.3f}",
                    "Current":   f"{cur:.3f}",
                    "P&L $":     f"{'+'if pnl>=0 else ''}${pnl:.2f}",
                    "Early?":    f"{cur:.0%}" if cur >= config.EARLY_EXIT_THRESH else "—",
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        else:
            st.markdown("<div style='color:#8b949e;padding:12px;'>No open positions</div>",
                        unsafe_allow_html=True)

    with col_log:
        st.markdown('<div class="section-header">Completed Kalshi Trades</div>',
                    unsafe_allow_html=True)
        trades = state.get("kalshi_trades", [])
        if trades:
            rows = []
            for t in trades:
                rows.append({
                    "Time":      t["ts"],
                    "Event":     t.get("market_id","")[:25],
                    "Sport":     t.get("sport",""),
                    "Side":      t.get("side","").upper(),
                    "Contracts": t.get("contracts",""),
                    "Entry":     f"{t.get('entry',0):.3f}",
                    "Exit":      f"{t.get('exit',0):.3f}" if t.get("exit") else "—",
                    "P&L":       f"{'+'if t.get('pnl',0)>=0 else ''}${t.get('pnl',0):.2f}",
                    "Exit Type": t.get("action",""),
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        else:
            st.markdown("<div style='color:#8b949e;padding:12px;'>No trades yet</div>",
                        unsafe_allow_html=True)


if __name__ == "__main__":
    render()
