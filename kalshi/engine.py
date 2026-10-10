"""
Wires everything together from Settings and starts the orchestrator in a
background thread. launch.py calls start_engine_thread() after Streamlit has
bound the port.
"""

from __future__ import annotations

import logging
import sys
import threading

from kalshi.agents.edge_finder import EdgeFinder
from kalshi.agents.execution import Execution
from kalshi.agents.monitor import Monitor
from kalshi.agents.risk_manager import RiskManager
from kalshi.agents.scout import Scout
from kalshi.agents.skeptic import Skeptic
from kalshi.client import KalshiDataClient
from kalshi.history import MarketHistory
from kalshi.http import KalshiHTTP, KalshiSigner
from kalshi.journal import Journal
from kalshi.killswitch import KillSwitch
from kalshi.odds_api import OddsAPIClient
from kalshi.orchestrator import Orchestrator

log = logging.getLogger("engine")


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)-20s %(message)s",
        stream=sys.stdout,
        force=True,
    )


def build_orchestrator(settings) -> Orchestrator:
    journal = Journal(settings.journal_db_path, paper=settings.paper_mode)
    kill = KillSwitch(settings, journal)

    signer = None
    try:
        signer = KalshiSigner.from_settings(settings)
    except Exception as e:
        log.error("could not load Kalshi key: %s", e)
    http = KalshiHTTP(settings, signer)
    data = KalshiDataClient(http)

    if not settings.paper_mode:
        if signer is None:
            raise RuntimeError("PAPER_MODE=false requires KALSHI_API_KEY and a private key")
        # Fail fast on bad credentials before the first trade.
        bal = data.get_balance_usd()
        log.warning("LIVE TRADING ENABLED on %s, balance $%.2f", settings.kalshi_env, bal)

    history = MarketHistory()
    odds = OddsAPIClient(settings)
    if not settings.odds_api_key:
        log.warning("ODDS_API_KEY not set: Edge Finder will find no fair values")

    journal.log_event("startup", f"paper_mode={settings.paper_mode} env={settings.kalshi_env} "
                                 f"auth={'yes' if signer else 'no'}")
    return Orchestrator(
        settings,
        scout=Scout(settings, data, history),
        edge_finder=EdgeFinder(settings, odds, history),
        skeptic=Skeptic(settings, history, journal),
        risk=RiskManager(settings, journal, kill),
        execution=Execution(settings, journal, kill, data, http=http),
        monitor=Monitor(settings, journal, data),
        journal=journal,
    )


def start_engine_thread(settings) -> tuple[threading.Thread, threading.Event]:
    stop = threading.Event()

    def _run():
        try:
            orch = build_orchestrator(settings)
            orch.run_forever(stop)
        except Exception:
            log.exception("engine failed to start")

    t = threading.Thread(target=_run, name="kalshi-engine", daemon=True)
    t.start()
    return t, stop
