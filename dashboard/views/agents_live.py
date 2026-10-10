"""Agents Live page (routed from app.py). Live view: what each agent is doing and saying right now. Refreshes itself."""

from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from common import AGENT_LABELS, PIPELINE, ago, get_journal, get_settings, seconds_since

s = get_settings()
j = get_journal()

st.title("Agents live")
refresh = st.select_slider("Refresh every", options=[2, 5, 10, 30], value=5, format_func=lambda v: f"{v}s")

STATE_COLOR = {"working": "green", "idle": "gray", "halted": "orange", "error": "red"}
DECISION_COLOR = {"APPROVE": "green", "MODIFY": "orange", "REJECT": "red", "PASS": "gray",
                  "ERROR": "red", "INFO": "blue", "PAUSE": "orange"}
QUIET_NOTE = {
    "skeptic": "Quiet until Edge Finder proposes a trade.",
    "risk_manager": "Quiet until Skeptic approves a trade.",
    "execution": "Quiet until Risk Manager approves a trade.",
    "learner": "Runs on the first engine cycle, then every 30 minutes.",
    "shadow": "Reports when the first shadow trades settle (a minute after a 15-minute market closes).",
}


def card(agent: str, r: dict | None) -> None:
    with st.container(border=True):
        st.markdown(f"### {AGENT_LABELS[agent]}")
        if not r:
            st.caption(QUIET_NOTE.get(agent, "No report yet. The engine may still be starting."))
            return
        # Only the agents that report every cycle can go stale; Skeptic etc. are
        # legitimately quiet until there's a proposal to look at.
        stale = agent in ("orchestrator", "scout", "monitor") and \
            seconds_since(r["updated_at"]) > max(3 * s.scan_interval_s, 180)
        color = "violet" if stale else STATE_COLOR.get(r["state"], "gray")
        label = "STALE" if stale else r["state"].upper()
        st.markdown(f":{color}[**{label}**] · updated {ago(r['updated_at'])}")
        if stale:
            st.warning("No update in a while. Check the engine logs on Render.")
        st.markdown(f"**Doing:** {r['activity']}")
        if r.get("last_message"):
            color = DECISION_COLOR.get(r.get("last_decision") or "", "gray")
            st.markdown(f"**Last said:** :{color}[{r.get('last_decision') or ''}] {r['last_message']}")
        if r.get("detail"):
            with st.expander("Data behind it"):
                st.json(r["detail"], expanded=False)


def why_no_bets() -> None:
    raw = j.get_flag("pass_summary", "")
    try:
        summ = json.loads(raw) if raw else {}
    except ValueError:
        summ = {}
    counts = summ.get("counts") or {}
    total = summ.get("evaluations") or 0
    with st.container(border=True):
        st.markdown("#### Why it is or isn't betting (last hour)")
        if not total:
            st.caption("No evaluations yet this hour. The tally resets when the engine restarts.")
            return
        proposed = counts.get("proposed", 0)
        st.markdown(f"Edge Finder priced **{total:,}** market snapshots and proposed **{proposed}** trades "
                    f"({proposed / total * 100:.1f}%). Everything else was passed for these reasons:")
        df = pd.DataFrame([{"Reason": k, "Count": v, "Share": v / total}
                           for k, v in counts.items() if k != "proposed"])
        if not df.empty:
            st.dataframe(df.style.format({"Share": "{:.0%}"}), hide_index=True, use_container_width=True)


@st.fragment(run_every=refresh)
def board():
    rows = {r["agent"]: r for r in j.agent_statuses()}
    top = st.columns(3)
    for col, agent in zip(top, ["orchestrator", "scout", "monitor"]):
        with col:
            card(agent, rows.get(agent))
    learn = st.columns(2)
    for col, agent in zip(learn, ["learner", "shadow"]):
        with col:
            card(agent, rows.get(agent))
    why_no_bets()
    st.markdown("#### Trade pipeline")
    mid = st.columns(4)
    for col, agent in zip(mid, ["edge_finder", "skeptic", "risk_manager", "execution"]):
        with col:
            card(agent, rows.get(agent))

    st.markdown("#### Live feed")
    pick = st.multiselect("Agents", PIPELINE, default=[], format_func=lambda a: AGENT_LABELS[a],
                          placeholder="All agents", key="feed_filter")
    hide_pass = st.checkbox("Hide Edge Finder PASS messages", value=True, key="hide_pass")
    feed = j.agent_feed(limit=400)
    df = pd.DataFrame(feed)
    if df.empty:
        st.caption("Nothing yet.")
        return
    if pick:
        df = df[df["agent"].isin(pick)]
    if hide_pass:
        df = df[df["decision"] != "PASS"]
    df["agent"] = df["agent"].map(lambda a: AGENT_LABELS.get(a, a))
    df["when"] = df["ts"].map(ago)
    st.dataframe(df[["when", "agent", "decision", "ticker", "message"]].head(150),
                 hide_index=True, use_container_width=True, height=420)


board()
