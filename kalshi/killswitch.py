"""
Global kill switch. ON if either:
  * env var KILL_SWITCH is truthy (checked live on every call), or
  * the journal flag "kill_switch" is "on" (the dashboard toggle writes it).
Risk rejects everything while it's on, the orchestrator skips scanning, and
Execution checks it again right before sending and while waiting on fills.
"""

from __future__ import annotations

import os

FLAG_KEY = "kill_switch"


class KillSwitch:
    def __init__(self, settings, journal):
        self.settings = settings
        self.journal = journal

    def status(self) -> tuple[bool, str]:
        raw = os.getenv("KILL_SWITCH")
        env_on = (raw.strip().lower() in ("1", "true", "yes", "on")) if raw else self.settings.kill_switch_env
        if env_on:
            return True, "KILL_SWITCH env var is set"
        try:
            if self.journal.get_flag(FLAG_KEY, "off") == "on":
                return True, "kill switch toggled on in dashboard"
        except Exception as e:  # unreadable flag store: fail closed
            return True, f"kill switch flag unreadable ({e})"
        return False, ""

    def is_on(self) -> bool:
        return self.status()[0]

    def set(self, on: bool, by: str = "dashboard") -> None:
        self.journal.set_flag(FLAG_KEY, "on" if on else "off", by)
        self.journal.log_event("kill_switch", f"set {'ON' if on else 'OFF'} by {by}",
                               "WARNING" if on else "INFO")
