"""Shared helpers for the Streamlit pages: journal access, sidebar, kill switch."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402

from config import load_settings  # noqa: E402
from kalshi.journal import Journal  # noqa: E402
from kalshi.killswitch import KillSwitch  # noqa: E402

AGENT_LABELS = {
    "orchestrator": "Orchestrator",
    "scout": "Scout",
    "edge_finder": "Edge Finder",
    "skeptic": "Skeptic",
    "risk_manager": "Risk Manager",
    "execution": "Execution",
    "monitor": "Monitor",
}
PIPELINE = list(AGENT_LABELS)


@st.cache_resource
def get_settings():
    return load_settings(str(ROOT / ".env"))


@st.cache_resource
def get_journal() -> Journal:
    s = get_settings()
    path = s.journal_db_path
    if not Path(path).is_absolute():
        path = str(ROOT / path)
    return Journal(path, paper=s.paper_mode)


def ago(iso: str | None) -> str:
    if not iso:
        return "never"
    try:
        then = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    secs = (datetime.now(timezone.utc) - then).total_seconds()
    if secs < 60:
        return f"{secs:.0f}s ago"
    if secs < 3600:
        return f"{secs/60:.0f}m ago"
    return f"{secs/3600:.1f}h ago"


def seconds_since(iso: str | None) -> float:
    if not iso:
        return float("inf")
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds()
    except ValueError:
        return float("inf")


def sidebar() -> None:
    s = get_settings()
    j = get_journal()
    kill = KillSwitch(s, j)
    with st.sidebar:
        if s.paper_mode:
            st.success("PAPER MODE: no real orders")
        else:
            st.error(f"LIVE TRADING on {s.kalshi_env.upper()}")
        st.caption(f"Kalshi env: {s.kalshi_env}")

        st.subheader("Kill switch")
        on, why = kill.status()
        env_forced = "env var" in why
        unlocked = True
        if s.dashboard_password:
            pw = st.text_input("Dashboard password", type="password", key="kill_pw")
            unlocked = pw == s.dashboard_password
            if not unlocked:
                st.caption("Enter the password to change the kill switch.")
        new = st.toggle("Stop all new trading", value=on, disabled=env_forced or not unlocked, key="kill_toggle")
        if env_forced:
            st.warning("Forced ON by the KILL_SWITCH env var. Change it in Render to turn it off.")
        elif new != on and unlocked:
            kill.set(new, by="dashboard")
            st.rerun()
        if on:
            st.error(f"ON: {why}")
        else:
            st.caption("Off. Risk and Execution re-check it on every trade.")
