"""Dashboard entrypoint: page config, shared sidebar, and page routing."""

from __future__ import annotations

import streamlit as st

from common import sidebar

st.set_page_config(page_title="AlgoTrader", layout="wide")
sidebar()

pages = st.navigation([
    st.Page("views/pnl.py", title="P&L", default=True),
    st.Page("views/agents_live.py", title="Agents Live"),
    st.Page("views/journal.py", title="Journal"),
])
pages.run()
