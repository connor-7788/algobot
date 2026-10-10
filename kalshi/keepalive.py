"""
Keep-alive: Render's free plan stops a web service after 15 minutes with no
inbound requests, which also stops the trading engine running inside it.
This thread requests our own public URL (through Render's proxy, so it counts
as inbound traffic) every keepalive_interval_s.

Free plans get 750 instance hours a month across the whole account; one
always-on service uses ~744.
"""

from __future__ import annotations

import logging
import threading

import requests

log = logging.getLogger("keepalive")


def run_forever(url: str, interval_s: float, stop: threading.Event, get=requests.get) -> None:
    target = url.rstrip("/") + "/_stcore/health"
    log.info("keep-alive pinging %s every %.0fs", target, interval_s)
    while not stop.wait(interval_s):
        try:
            r = get(target, timeout=15)
            log.debug("keep-alive %s", r.status_code)
        except Exception as e:
            log.warning("keep-alive ping failed: %s", e)
