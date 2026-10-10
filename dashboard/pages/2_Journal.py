"""Journal: every proposal, every agent's verdict and reason, final outcome."""

from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from common import AGENT_LABELS, get_journal, sidebar

st.set_page_config(page_title="Journal", layout="wide")
sidebar()
j = get_journal()

st.title("Trade journal")

limit = st.select_slider("Load most recent", options=[100, 250, 500, 1000, 2500], value=500)
rows = j.rows(limit=limit)
if not rows:
    st.info("No proposals yet. Edge Finder only journals a proposal once it finds edge above the minimum.")
    st.stop()

df = pd.DataFrame(rows)
df["created_at"] = pd.to_datetime(df["created_at"], utc=True)
df["rejected_by"] = df["rejected_by"].fillna("")
df["outcome"] = df.apply(
    lambda r: (f"settled {r['result']} ${r['realized_pnl_usd']:+.2f}" if r["status"] == "SETTLED"
               else r["status"]), axis=1)

# ── Who kills the most trades ────────────────────────────────────────────────
st.subheader("Rejections by agent")
kills = df[df["rejected_by"] != ""].groupby("rejected_by").size().rename("rejections")
if kills.empty:
    st.caption("No rejections in this window.")
else:
    kills.index = [AGENT_LABELS.get(a, a) for a in kills.index]
    c1, c2 = st.columns([2, 1])
    c1.bar_chart(kills)
    c2.dataframe(kills.sort_values(ascending=False), use_container_width=True)
    reasons = (df[df["rejected_by"] != ""]
               .assign(reason=lambda d: d["reject_reason"].str.replace(r"[\d.]+", "#", regex=True))
               .groupby(["rejected_by", "reason"]).size().rename("count").reset_index()
               .sort_values("count", ascending=False).head(15))
    with st.expander("Most common rejection reasons"):
        reasons["rejected_by"] = reasons["rejected_by"].map(lambda a: AGENT_LABELS.get(a, a))
        st.dataframe(reasons, hide_index=True, use_container_width=True)

# ── Filters ──────────────────────────────────────────────────────────────────
st.subheader("Proposals")
f1, f2, f3 = st.columns(3)
agent_opts = ["(any)", "(not rejected)"] + list(AGENT_LABELS)
rejected_by = f1.selectbox("Rejected by", agent_opts,
                           format_func=lambda a: AGENT_LABELS.get(a, a))
statuses = f2.multiselect("Status", sorted(df["status"].unique()))
series = f3.multiselect("Market type (series)", sorted(df["series_ticker"].dropna().unique()))

view = df
if rejected_by == "(not rejected)":
    view = view[view["rejected_by"] == ""]
elif rejected_by != "(any)":
    view = view[view["rejected_by"] == rejected_by]
if statuses:
    view = view[view["status"].isin(statuses)]
if series:
    view = view[view["series_ticker"].isin(series)]

show = view[["created_at", "market_ticker", "side", "price_cents", "original_count", "contract_count",
             "fair_probability", "edge_cents", "confidence_score", "rejected_by", "reject_reason", "outcome",
             "proposal_id"]].copy()
show["rejected_by"] = show["rejected_by"].map(lambda a: AGENT_LABELS.get(a, a) if a else "")
show["fair_probability"] = (show["fair_probability"] * 100).round(1)
st.caption(f"{len(show)} of {len(df)} proposals")
st.dataframe(show, hide_index=True, use_container_width=True, height=380,
             column_config={"created_at": st.column_config.DatetimeColumn("created", format="MMM D HH:mm:ss"),
                            "fair_probability": "fair %", "price_cents": "ask (c)",
                            "original_count": "proposed", "contract_count": "approved"})

# ── One proposal in detail ───────────────────────────────────────────────────
st.subheader("Verdict trail")
ids = view["proposal_id"].tolist()
if not ids:
    st.stop()
labels = {r["proposal_id"]: f"{r['created_at']:%m-%d %H:%M} {r['market_ticker']} {r['side'].upper()} [{r['outcome']}]"
          for _, r in view.iterrows()}
pid = st.selectbox("Proposal", ids, format_func=lambda i: labels.get(i, i))
p = j.get(pid)
if p:
    st.markdown(f"**{p.title}**  \n{p.side.upper()} @ {p.price_cents}c · fair {p.fair_probability*100:.1f}% · "
                f"edge {p.edge_cents:.2f}c · confidence {p.confidence_score} · status **{p.status.value}**")
    for v in p.verdicts:
        color = {"APPROVE": "green", "MODIFY": "orange", "REJECT": "red"}[v.decision.value]
        with st.container(border=True):
            st.markdown(f":{color}[**{v.decision.value}**] **{AGENT_LABELS.get(v.agent, v.agent)}** · "
                        f"{v.timestamp:%Y-%m-%d %H:%M:%S} UTC  \n{v.reason}")
            if v.changes:
                st.caption(f"changed: {v.changes}")
            if v.data_snapshot:
                with st.expander("Data snapshot"):
                    st.json(v.data_snapshot, expanded=False)
    with st.expander("Sources behind fair value"):
        st.dataframe(pd.DataFrame([s.to_dict() for s in p.sources]), hide_index=True, use_container_width=True)
    if p.status.value == "SETTLED":
        st.success(f"Settled {p.result.upper()}: {p.filled_count} contracts, P&L ${p.realized_pnl_usd:+.2f}")
    with st.expander("Raw proposal JSON"):
        st.code(json.dumps(p.to_dict(), indent=2, default=str), language="json")
