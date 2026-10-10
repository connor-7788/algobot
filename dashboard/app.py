"""Dashboard entrypoint: page config, shared sidebar, and page routing."""

from __future__ import annotations

import streamlit as st

from common import sidebar

st.set_page_config(page_title="AlgoTrader", layout="wide")

# Streamlit fades every element to ~30% opacity while a rerun is in flight.
# With pages that refresh every second that reads as the screen darkening on
# every tick. Keep elements fully opaque and hide the "Running..." widget.
st.markdown("""
<style>
[data-stale="true"], .stale-element, [data-testid="stale-element"],
div[data-testid="stVerticalBlock"] > div[data-stale="true"] {
  opacity: 1 !important; filter: none !important; transition: none !important;
}
div[data-testid="stStatusWidget"] { visibility: hidden; height: 0; }
div[data-testid="stSkeleton"] { display: none; }
.stApp, .stApp * { transition-property: none !important; }
.live-dot { transition-property: opacity !important; }
</style>
""", unsafe_allow_html=True)
sidebar()

pages = st.navigation([
    st.Page("views/pnl.py", title="P&L", default=True),
    st.Page("views/agents_live.py", title="Agents Live"),
    st.Page("views/journal.py", title="Journal"),
])
pages.run()
