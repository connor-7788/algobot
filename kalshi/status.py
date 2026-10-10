"""
StatusBoard: what each agent is doing and saying right now.

The orchestrator calls working() before each agent call and said() after it.
Rows land in the journal's agent_status / agent_feed tables, which the
dashboard's Agents page polls. A failure here is logged and swallowed: the
status board must never be able to stop a trade decision or a cancel.
"""

from __future__ import annotations

import logging

log = logging.getLogger("status")

AGENT_ORDER = ["orchestrator", "scout", "edge_finder", "skeptic", "risk_manager", "execution", "monitor"]


class StatusBoard:
    def __init__(self, journal):
        self.journal = journal

    def _safe(self, *args, **kwargs) -> None:
        try:
            self.journal.set_agent_status(*args, **kwargs)
        except Exception as e:
            log.debug("status board write failed: %s", e)

    def working(self, agent: str, activity: str, ticker: str | None = None) -> None:
        self._safe(agent, "working", activity, ticker=ticker)

    def said(self, agent: str, decision: str, message: str, *, ticker: str | None = None,
             detail: dict | None = None, activity: str = "waiting for next item") -> None:
        self._safe(agent, "idle", activity, message=message, decision=decision, ticker=ticker, detail=detail)

    def state(self, agent: str, state: str, activity: str, *, message: str | None = None,
              decision: str | None = None, detail: dict | None = None) -> None:
        self._safe(agent, state, activity, message=message, decision=decision, detail=detail)
