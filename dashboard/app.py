"""Overview page. Other pages live in dashboard/pages/."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from common import AGENT_LABELS, PIPELINE, ago, get_journal, get_settings, sidebar

st.set_page_config(page_title="Kalshi Agents", layout="wide")
sidebar()

s = get_settings()
j = get_journal()

st.title("Kalshi agent pipeline")
st.caption("Scout -> Edge Finder -> Skeptic -> Risk Manager -> Execution, with Monitor booking P&L.")


@st.fragment(run_every=10)
def overview():
    tz = ZoneInfo(s.timezone)
    midnight = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    open_pos = j.open_positions()
    today = j.realized_pnl_since(midnight)
    total = j.realized_pnl_total()
    stake = min(s.risk_stake_ceiling_usd, max(s.risk_stake_floor_usd, s.risk_stake_pct_of_realized * max(total, 0)))

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Open positions", f"{len(open_pos)} / {s.risk_max_open_positions}")
    c2.metric("Realized P&L (all time)", f"${total:,.2f}")
    c3.metric("Realized today", f"${today:,.2f}", help=f"Daily loss limit: -${s.risk_daily_loss_limit_usd:.2f}")
    c4.metric("Next stake", f"${stake:,.2f}", help="2% of realized profit, clamped to floor/ceiling")

    st.subheader("Agents right now")
    rows = {r["agent"]: r for r in j.agent_statuses()}
    cols = st.columns(len(PIPELINE))
    for col, agent in zip(cols, PIPELINE):
        r = rows.get(agent)
        with col:
            st.markdown(f"**{AGENT_LABELS[agent]}**")
            if not r:
                st.caption("no report yet")
                continue
            st.caption(f"{r['state']} · {ago(r['updated_at'])}")
            st.write(r["activity"])

    st.subheader("Open positions")
    if open_pos:
        st.dataframe(pd.DataFrame([{
            "market": p.market_ticker, "side": p.side.upper(), "contracts": p.filled_count or p.contract_count,
            "price (c)": p.avg_fill_price_cents or p.price_cents, "fair %": round(p.fair_probability * 100, 1),
            "status": p.status.value, "opened": ago(p.created_at.isoformat()),
        } for p in open_pos]), hide_index=True, use_container_width=True)
    else:
        st.caption("None.")

    st.subheader("System events")
    ev = j.recent_events(30)
    if ev:
        st.dataframe(pd.DataFrame(ev)[["ts", "level", "kind", "message"]], hide_index=True, use_container_width=True)
    else:
        st.caption("None yet.")


overview()
