"""
dashboards/dashboard_a.py — Wall Street Engine Dashboard.

Run: streamlit run dashboards/dashboard_a.py --server.port 8501

Panels:
  • Account overview bar (equity, buying power, day P&L, VIX, ADX regime)
  • Equity curve vs SPY (Plotly line chart from DB / in-memory)
  • Sharpe Ratio gauge
  • Risk Exposure treemap (position sizes by symbol)
  • Open Positions table (live P&L, ATR stop, trail active)
  • Signal scanner (EMA/RSI/ADX/VOL per symbol)
  • Black Box trade log (every entry/exit with full reasoning)
  • Risk Firewall status (kill-switch, sentiment, latency)
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time
import json
import math
from datetime import datetime
from pathlib import Path

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px

import config

st.set_page_config(
    page_title="AlgoTrader — Wall Street Engine",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── Dark theme CSS ────────────────────────────────────────────────────────────
st.markdown("""
<style>
  .main { background: #0d1117; color: #e6edf3; }
  .block-container { padding: 1rem 1.5rem; }
  .metric-card {
    background: #161b22; border: 1px solid #30363d;
    border-radius: 8px; padding: 12px 16px; text-align: center;
  }
  .metric-label { color: #8b949e; font-size: 11px; text-transform: uppercase; letter-spacing: 1px; }
  .metric-value { color: #e6edf3; font-size: 22px; font-weight: 700; margin-top: 4px; }
  .metric-positive { color: #3fb950; }
  .metric-negative { color: #f85149; }
  .kill-switch { background: #3d1a1a; border: 2px solid #f85149; border-radius: 8px; padding: 12px; }
  .sentiment-halt { background: #2d2a0a; border: 2px solid #e3b341; border-radius: 8px; padding: 12px; }
  .section-header { color: #58a6ff; font-size: 13px; font-weight: 600;
                    text-transform: uppercase; letter-spacing: 1.5px; margin: 12px 0 6px; }
  div[data-testid="stDataFrame"] { border: 1px solid #30363d; border-radius: 6px; }
  .stPlotlyChart { border: 1px solid #21262d; border-radius: 8px; }
</style>
""", unsafe_allow_html=True)

# ── Load shared state ─────────────────────────────────────────────────────────
# Dashboards read from a JSON state file written by the main engine.
# This decouples the dashboard from engine internals.

STATE_FILE = Path("logs/engine_state.json")

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except:
            pass
    return _mock_state()

def _mock_state() -> dict:
    """Demo data when engine isn't running."""
    import random
    random.seed(int(time.time() / 30))
    return {
        "ts":              datetime.now().isoformat(),
        "mode":            "PAPER",
        "risk": {
            "kill_switch":      False,
            "sentiment_halt":   False,
            "sentiment_reason": "",
            "starting_equity":  100_000,
            "current_equity":   102_340,
            "daily_pnl_pct":    2.34,
            "open_equity":      2,
            "open_kalshi":      3,
            "equity_allocated": 18_000,
            "kalshi_allocated": 5_000,
            "current_vix":      17.3,
            "size_multiplier":  1.0,
        },
        "equity_positions": [
            {"symbol":"NVDA","side":"long","qty":18,"entry":875.20,"current":889.50,
             "pnl_usd":257.4,"pnl_pct":1.63,"mins_held":22.0,"trail_active":True,"atr_stop":861.1},
            {"symbol":"TSLA","side":"short","qty":12,"entry":242.80,"current":239.10,
             "pnl_usd":44.4,"pnl_pct":1.52,"mins_held":8.5,"trail_active":False,"atr_stop":249.3},
        ],
        "signals": {
            "AAPL":  {"price":189.50,"ema_fast":189.2,"ema_slow":188.1,"rsi":52.1,"adx":28.4,"vol_r":1.4,"trend":"BULL","regime":"trending"},
            "TSLA":  {"price":239.10,"ema_fast":239.8,"ema_slow":241.2,"rsi":44.2,"adx":31.0,"vol_r":1.8,"trend":"BEAR","regime":"trending"},
            "NVDA":  {"price":889.50,"ema_fast":889.0,"ema_slow":878.2,"rsi":55.3,"adx":42.1,"vol_r":2.1,"trend":"BULL","regime":"trending"},
            "AMZN":  {"price":185.20,"ema_fast":184.9,"ema_slow":185.5,"rsi":48.9,"adx":18.2,"vol_r":0.9,"trend":"BEAR","regime":"ranging"},
            "MSFT":  {"price":415.80,"ema_fast":415.5,"ema_slow":414.1,"rsi":51.0,"adx":22.7,"vol_r":1.1,"trend":"BULL","regime":"ranging"},
        },
        "trade_log": [
            {"ts":"10:32:15","symbol":"NVDA","side":"long","action":"OPEN","qty":18,"entry":875.20,"exit":None,"pnl":0,"asset":"equity"},
            {"ts":"10:18:44","symbol":"AAPL","side":"long","action":"TP","qty":22,"entry":188.10,"exit":192.80,"pnl":103.4,"asset":"equity"},
            {"ts":"09:58:02","symbol":"TSLA","side":"short","action":"OPEN","qty":12,"entry":242.80,"exit":None,"pnl":0,"asset":"equity"},
            {"ts":"09:44:30","symbol":"MSFT","side":"long","action":"TRAIL","qty":8,"entry":412.00,"exit":417.20,"pnl":41.6,"asset":"equity"},
        ],
        "equity_curve": _mock_equity_curve(),
        "spy_curve":    _mock_spy_curve(),
        "sentiment_events": [],
        "latency_ms": {"alpaca_ws": 42, "kalshi_ws": 78, "odds_api": 210},
    }

def _mock_equity_curve() -> list:
    import random
    equity = 100_000.0
    result = []
    for i in range(78):
        equity *= 1 + random.gauss(0.0003, 0.002)
        result.append({"i": i, "equity": round(equity, 2)})
    return result

def _mock_spy_curve() -> list:
    import random
    spy = 100_000.0
    result = []
    for i in range(78):
        spy *= 1 + random.gauss(0.00015, 0.0015)
        result.append({"i": i, "equity": round(spy, 2)})
    return result


# ── Computed metrics ──────────────────────────────────────────────────────────

def calc_sharpe(returns: list[float], risk_free: float = 0.0) -> float:
    if len(returns) < 2:
        return 0.0
    import statistics
    mean_r = sum(returns) / len(returns) - risk_free
    std_r  = statistics.stdev(returns)
    if std_r == 0:
        return 0.0
    return round(mean_r / std_r * math.sqrt(252 * 78 / len(returns)), 2)  # annualised

def calc_max_drawdown(equity_curve: list[float]) -> float:
    peak = equity_curve[0]
    max_dd = 0.0
    for v in equity_curve:
        if v > peak:
            peak = v
        dd = (peak - v) / peak
        if dd > max_dd:
            max_dd = dd
    return round(max_dd * 100, 2)


# ── Plotly helpers ────────────────────────────────────────────────────────────

PLOT_BG    = "#0d1117"
PLOT_PAPER = "#0d1117"
GRID_COLOR = "#21262d"
FONT_COLOR = "#8b949e"

def base_layout(**kwargs):
    return dict(
        paper_bgcolor = PLOT_PAPER,
        plot_bgcolor  = PLOT_BG,
        font          = dict(color=FONT_COLOR, size=11),
        margin        = dict(l=40, r=20, t=30, b=30),
        **kwargs
    )

def equity_curve_chart(state: dict) -> go.Figure:
    ec  = state.get("equity_curve", [])
    spy = state.get("spy_curve", [])
    if not ec:
        return go.Figure()

    x     = list(range(len(ec)))
    eq    = [p["equity"] for p in ec]
    spy_v = [p["equity"] for p in spy] if spy else []

    # Normalise to 100 for apples-to-apples
    eq_norm  = [v / eq[0]  * 100 for v in eq]
    spy_norm = [v / spy_v[0] * 100 for v in spy_v] if spy_v else []

    returns = [eq_norm[i] / eq_norm[i-1] - 1 for i in range(1, len(eq_norm))]
    sharpe  = calc_sharpe(returns)
    max_dd  = calc_max_drawdown(eq)

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=x, y=eq_norm, name="Portfolio",
        line=dict(color="#3fb950", width=2),
        fill="tozeroy", fillcolor="rgba(63,185,80,0.06)",
    ))
    if spy_norm:
        fig.add_trace(go.Scatter(
            x=x, y=spy_norm, name="SPY",
            line=dict(color="#58a6ff", width=1.5, dash="dash"),
        ))

    fig.update_layout(
        **base_layout(height=280),
        title=dict(
            text=f"Equity Curve vs SPY  |  Sharpe: {sharpe}  |  Max DD: {max_dd}%",
            font=dict(size=12, color="#e6edf3")
        ),
        legend=dict(orientation="h", y=1.05, bgcolor="rgba(0,0,0,0)"),
        xaxis=dict(showgrid=True, gridcolor=GRID_COLOR, zeroline=False),
        yaxis=dict(showgrid=True, gridcolor=GRID_COLOR, zeroline=False, ticksuffix=""),
    )
    return fig


def sharpe_gauge(sharpe: float) -> go.Figure:
    color = "#3fb950" if sharpe >= 1.5 else ("#e3b341" if sharpe >= 0.5 else "#f85149")
    fig = go.Figure(go.Indicator(
        mode  = "gauge+number",
        value = max(-3, min(4, sharpe)),
        title = {"text": "Sharpe Ratio", "font": {"size": 13, "color": "#8b949e"}},
        number= {"font": {"size": 28, "color": color}},
        gauge = {
            "axis":       {"range": [-2, 4], "tickcolor": FONT_COLOR, "tickwidth": 1},
            "bar":        {"color": color, "thickness": 0.25},
            "bgcolor":    "#161b22",
            "bordercolor": GRID_COLOR,
            "steps": [
                {"range": [-2, 0.5], "color": "#2d1a1a"},
                {"range": [0.5, 1.5], "color": "#2d2a0a"},
                {"range": [1.5, 4],   "color": "#1a2d1a"},
            ],
            "threshold": {"line": {"color": "#e6edf3", "width": 2}, "value": 1.5},
        }
    ))
    fig.update_layout(**base_layout(height=200))
    return fig


def risk_treemap(positions: list[dict]) -> go.Figure:
    if not positions:
        fig = go.Figure()
        fig.update_layout(**base_layout(height=200))
        return fig

    labels  = [p["symbol"] for p in positions]
    values  = [abs(p["qty"] * p["current"]) for p in positions]
    colors  = ["#3fb950" if p["side"] == "long" else "#f85149" for p in positions]
    parents = [""] * len(labels)

    fig = go.Figure(go.Treemap(
        labels  = labels,
        parents = parents,
        values  = values,
        marker  = dict(colors=colors, line=dict(width=2, color="#0d1117")),
        texttemplate = "%{label}<br>$%{value:,.0f}",
        textfont     = dict(size=12, color="#e6edf3"),
    ))
    fig.update_layout(**base_layout(height=200), title=dict(
        text="Risk Exposure", font=dict(size=12, color="#e6edf3")
    ))
    return fig


# ── Main layout ───────────────────────────────────────────────────────────────

@st.fragment(run_every=max(1, config.DASH_REFRESH_MS // 1000))
def render():
    state  = load_state()
    risk   = state.get("risk", {})
    ts     = state.get("ts", "")

    # ── Header ────────────────────────────────────────────────────────────────
    col_logo, col_mode, col_ts = st.columns([3, 1, 2])
    with col_logo:
        st.markdown("## 📈 Wall Street Engine")
    with col_mode:
        mode_color = "#e3b341" if state.get("mode") == "PAPER" else "#f85149"
        st.markdown(f"<div style='color:{mode_color};font-size:14px;font-weight:700;"
                    f"padding-top:14px;'>{state.get('mode','?')}</div>",
                    unsafe_allow_html=True)
    with col_ts:
        st.markdown(f"<div style='color:#8b949e;font-size:11px;padding-top:18px;"
                    f"text-align:right;'>Last update: {ts[-8:] if ts else '—'}</div>",
                    unsafe_allow_html=True)

    # ── Kill-switch / sentiment alert banners ──────────────────────────────────
    if risk.get("kill_switch"):
        st.markdown('<div class="kill-switch">⛔ <b>KILL SWITCH ACTIVE</b> — '
                    'Daily loss limit exceeded. All new orders halted.</div>',
                    unsafe_allow_html=True)
    if risk.get("sentiment_halt"):
        st.markdown(f'<div class="sentiment-halt">⚠️ <b>SENTIMENT HALT</b> — '
                    f'{risk.get("sentiment_reason","")[:100]}</div>',
                    unsafe_allow_html=True)

    # ── Metric strip ─────────────────────────────────────────────────────────
    st.markdown("")
    m1, m2, m3, m4, m5, m6, m7 = st.columns(7)
    def metric(col, label, value, color=None):
        val_class = f"metric-{'positive' if color=='green' else 'negative' if color=='red' else ''}"
        col.markdown(f"""
        <div class="metric-card">
          <div class="metric-label">{label}</div>
          <div class="metric-value {val_class}">{value}</div>
        </div>""", unsafe_allow_html=True)

    eq     = risk.get("current_equity", 0)
    pnl_p  = risk.get("daily_pnl_pct", 0)
    pnl_c  = "green" if pnl_p >= 0 else "red"
    vix    = risk.get("current_vix", 0)
    vix_c  = "red" if vix >= 30 else ("yellow" if vix >= 20 else "green")
    latency = state.get("latency_ms", {})

    metric(m1, "Portfolio",    f"${eq:,.0f}")
    metric(m2, "Day P&L",      f"{'+' if pnl_p>=0 else ''}{pnl_p:.2f}%", pnl_c)
    metric(m3, "VIX",          f"{vix:.1f}", vix_c)
    metric(m4, "Open Stocks",  risk.get("open_equity", 0))
    metric(m5, "Open Kalshi",  risk.get("open_kalshi", 0))
    metric(m6, "Size ×",       risk.get("size_multiplier", 1.0))
    metric(m7, "Feed Lag",     f"{max(latency.values(), default=0)}ms")

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Row 1: Equity curve + Sharpe + Treemap ────────────────────────────────
    col_curve, col_sharpe, col_tree = st.columns([5, 2, 2])

    ec      = state.get("equity_curve", [])
    spy_c   = state.get("spy_curve", [])
    eq_vals = [p["equity"] for p in ec] if ec else [100_000]
    returns = [eq_vals[i] / eq_vals[i-1] - 1 for i in range(1, len(eq_vals))]
    sharpe  = calc_sharpe(returns)

    with col_curve:
        st.plotly_chart(equity_curve_chart(state), use_container_width=True)
    with col_sharpe:
        st.plotly_chart(sharpe_gauge(sharpe), use_container_width=True)
    with col_tree:
        positions = state.get("equity_positions", [])
        st.plotly_chart(risk_treemap(positions), use_container_width=True)

    # ── Row 2: Open Positions + Signal Scanner ────────────────────────────────
    col_pos, col_sig = st.columns([3, 3])

    with col_pos:
        st.markdown('<div class="section-header">Open Positions</div>', unsafe_allow_html=True)
        positions = state.get("equity_positions", [])
        if positions:
            rows = []
            for p in positions:
                rows.append({
                    "Symbol":   p["symbol"],
                    "Side":     p["side"].upper(),
                    "Qty":      p["qty"],
                    "Entry":    f"${p['entry']:.2f}",
                    "Price":    f"${p['current']:.2f}",
                    "P&L $":    f"{'+'if p['pnl_usd']>=0 else ''}${p['pnl_usd']:.2f}",
                    "P&L %":    f"{'+'if p['pnl_pct']>=0 else ''}{p['pnl_pct']:.2f}%",
                    "ATR Stop": f"${p['atr_stop']:.2f}",
                    "Trail":    "▲ ON" if p.get("trail_active") else "off",
                    "Held":     f"{p['mins_held']}m",
                })
            df = pd.DataFrame(rows)
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.markdown("<div style='color:#8b949e;padding:12px;'>No open positions</div>",
                        unsafe_allow_html=True)

    with col_sig:
        st.markdown('<div class="section-header">Live Signal Scanner</div>', unsafe_allow_html=True)
        signals = state.get("signals", {})
        if signals:
            rows = []
            for sym, sig in signals.items():
                rows.append({
                    "Symbol": sym,
                    "Price":  f"${sig.get('price', '—')}",
                    "RSI":    sig.get("rsi", "—"),
                    "EMA8":   sig.get("ema_fast", "—"),
                    "EMA21":  sig.get("ema_slow", "—"),
                    "ADX":    sig.get("adx", "—"),
                    "Vol×":   sig.get("vol_r", "—"),
                    "Trend":  sig.get("trend", "—"),
                    "Regime": sig.get("regime", "—"),
                })
            df = pd.DataFrame(rows)
            st.dataframe(df, use_container_width=True, hide_index=True)

    # ── Row 3: Black Box Trade Log + Risk Firewall ────────────────────────────
    col_log, col_firewall = st.columns([4, 2])

    with col_log:
        st.markdown('<div class="section-header">Black Box — Trade Reasons</div>',
                    unsafe_allow_html=True)
        trades = state.get("trade_log", [])
        equity_trades = [t for t in trades if t.get("asset") == "equity"]
        if equity_trades:
            rows = []
            for t in equity_trades[:15]:
                rows.append({
                    "Time":   t["ts"],
                    "Symbol": t["symbol"],
                    "Side":   t["side"].upper(),
                    "Action": t["action"],
                    "Qty":    t["qty"],
                    "Entry":  f"${t['entry']:.2f}",
                    "Exit":   f"${t['exit']:.2f}" if t.get("exit") else "—",
                    "P&L":    f"{'+'if t['pnl']>=0 else ''}${t['pnl']:.2f}" if t.get("exit") else "—",
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        else:
            st.markdown("<div style='color:#8b949e;padding:12px;'>No trades yet</div>",
                        unsafe_allow_html=True)

    with col_firewall:
        st.markdown('<div class="section-header">Risk Firewall</div>', unsafe_allow_html=True)

        starting = risk.get("starting_equity", 100_000)
        current  = risk.get("current_equity",  100_000)
        loss_pct = (starting - current) / starting * 100 if starting else 0
        limit_pct = config.MAX_DAILY_LOSS_PCT * 100

        fig = go.Figure(go.Indicator(
            mode  = "gauge+number+delta",
            value = abs(min(loss_pct, 0)),
            title = {"text": "Daily Loss Used", "font": {"size": 12, "color": "#8b949e"}},
            delta = {"reference": 0, "valueformat": ".2f", "suffix": "%"},
            number= {"suffix": "%", "font": {"size": 22}},
            gauge = {
                "axis":  {"range": [0, limit_pct * 1.5]},
                "bar":   {"color": "#f85149" if abs(loss_pct) > limit_pct * 0.75 else "#e3b341"},
                "bgcolor": "#161b22",
                "threshold": {"line": {"color": "#f85149", "width": 3}, "value": limit_pct},
            }
        ))
        fig.update_layout(**base_layout(height=180))
        st.plotly_chart(fig, use_container_width=True)

        # Feed latency
        latency = state.get("latency_ms", {})
        for feed, ms in latency.items():
            color = "🟢" if ms < 200 else ("🟡" if ms < 500 else "🔴")
            st.markdown(f"<div style='font-size:12px;color:#8b949e;'>"
                        f"{color} {feed}: {ms}ms</div>", unsafe_allow_html=True)


if __name__ == "__main__":
    render()
