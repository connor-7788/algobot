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
from kalshi import keepalive
from kalshi.killswitch import KillSwitch
from kalshi.learner import Learner, ParamStore
from kalshi.marks import MarkFeed
from kalshi.odds_api import OddsAPIClient
from kalshi.spot import SpotClient
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
    spot = SpotClient(settings)
    if settings.market_focus == "sports_live" and not settings.odds_api_key:
        log.warning("ODDS_API_KEY not set: Edge Finder will find no fair values")
    log.info("market focus: %s", settings.market_focus)

    journal.log_event("startup", f"paper_mode={settings.paper_mode} env={settings.kalshi_env} "
                                 f"auth={'yes' if signer else 'no'}")
    params = ParamStore(settings, journal)
    learner = Learner(settings, journal, data, params)
    p = params.get()
    log.info("learned params v%d (%s): sigma x%g, model weight %g, edge min %s", p.version, p.source,
             p.sigma_scale, p.model_weight, "PAUSED" if p.paused else p.edge_min_cents)
    orch = Orchestrator(
        settings,
        scout=Scout(settings, data, history),
        edge_finder=EdgeFinder(settings, odds, history, spot, params=params,
                               learner=learner if settings.market_focus == "crypto_15m" else None),
        skeptic=Skeptic(settings, history, journal),
        risk=RiskManager(settings, journal, kill),
        execution=Execution(settings, journal, kill, data, http=http),
        monitor=Monitor(settings, journal, data),
        journal=journal,
        learner=learner if settings.market_focus == "crypto_15m" else None,
    )
    orch.mark_feed = MarkFeed(settings, journal, data, on_tick=orch.run_exits)
    return orch


def start_engine_thread(settings) -> tuple[threading.Thread, threading.Event]:
    stop = threading.Event()

    def _run():
        try:
            orch = build_orchestrator(settings)
            threading.Thread(target=orch.mark_feed.run_forever, args=(stop,),
                             name="mark-feed", daemon=True).start()
            if settings.keepalive_url:
                threading.Thread(target=keepalive.run_forever,
                                 args=(settings.keepalive_url, settings.keepalive_interval_s, stop),
                                 name="keepalive", daemon=True).start()
            orch.run_forever(stop)
        except Exception:
            log.exception("engine failed to start")

    t = threading.Thread(target=_run, name="kalshi-engine", daemon=True)
    t.start()
    return t, stop
