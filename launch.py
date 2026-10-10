"""
launch.py: Render entrypoint.

Order matters for Render: the Streamlit dashboard binds $PORT FIRST, and only
once the port answers does the trading engine start, in a background thread
of this process. Render's health check sees an open port within seconds no
matter how slow the engine's startup is.

The main thread then supervises:
  * if Streamlit exits, stop the engine and exit non-zero so Render restarts us
  * if the engine thread dies, restart it after a pause
"""

from __future__ import annotations

import logging
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from config import load_settings
from kalshi.engine import setup_logging, start_engine_thread

log = logging.getLogger("launch")
ROOT = Path(__file__).resolve().parent


def wait_for_port(port: int, timeout_s: float = 90.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.5)
    return False


def main() -> int:
    settings = load_settings(str(ROOT / ".env"))
    setup_logging(settings.log_level)
    port = settings.port

    cmd = [sys.executable, "-m", "streamlit", "run", str(ROOT / "dashboard" / "app.py"),
           "--server.port", str(port), "--server.address", "0.0.0.0",
           "--server.headless", "true", "--browser.gatherUsageStats", "false"]
    log.info("starting dashboard on port %d", port)
    dashboard = subprocess.Popen(cmd, cwd=str(ROOT))

    if not wait_for_port(port):
        log.error("dashboard never bound port %d; exiting so the host restarts us", port)
        dashboard.terminate()
        return 1
    log.info("port %d is up; starting engine thread (PAPER_MODE=%s)", port, settings.paper_mode)

    engine, stop = start_engine_thread(settings)

    def shutdown(signum, _frame):
        log.info("signal %s: shutting down", signum)
        stop.set()
        dashboard.terminate()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    while True:
        rc = dashboard.poll()
        if rc is not None:
            log.error("dashboard exited with %s", rc)
            stop.set()
            return rc or 1
        if stop.is_set():
            dashboard.wait(timeout=10)
            return 0
        if not engine.is_alive():
            log.error("engine thread died; restarting in 30s")
            time.sleep(30)
            engine, stop = start_engine_thread(settings)
        time.sleep(5)


if __name__ == "__main__":
    sys.exit(main())
