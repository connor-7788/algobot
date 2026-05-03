"""
AlgoTrader cross-asset runner — v2.2

Changes vs v2.1:
  • Both equity AND sports engines ALWAYS run concurrently when enabled.
    Each runs in its own daemon thread with auto-restart on crash.
  • Watchdog thread monitors both engines every 10s and restarts any that died.
  • Added multi-source live data support:
      - Alpaca (existing)
      - Polygon.io (real-time US equities + options)
      - Finnhub (global stocks, forex, crypto, alt data)
      - CoinGecko (crypto prices — free, no key)
      - OpenBB (open-source Bloomberg — unified data SDK)
      - PrizePicks / Underdog (via scraper helpers)
      - DraftKings odds (via theOddsAPI — existing)
      - ActionNetwork (sharp money / line movement)
      - Sportradar (professional sports data — key required)
"""

import json
import logging
import os
import signal
import subprocess
import sys
import time
import threading
from getpass import getpass
from datetime import datetime
from pathlib import Path


def _ensure_package(import_name: str, package_name: str | None = None):
    try:
        __import__(import_name)
    except ImportError:
        pkg = package_name or import_name
        print(f"Installing {pkg}...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", pkg, "--quiet"])


for _import_name, _package_name in [
    ("alpaca_trade_api", "alpaca-trade-api"),
    ("pandas", "pandas"),
    ("numpy", "numpy"),
    ("requests", "requests"),
    ("cryptography", "cryptography"),
    ("rich", "rich"),
]:
    _ensure_package(_import_name, _package_name)


import alpaca_trade_api as tradeapi
import requests
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

import config
from equity_engine import EquityEngine
from kalshi_engine import KalshiClient, KalshiEngine, OddsAPIBridge
from risk_gatekeeper import RiskGatekeeper


console = Console()
log = logging.getLogger(__name__)
STATE_FILE = Path("logs/engine_state.json")
CONFIG_PATH = Path(os.path.expanduser("~/.algotrader_config.json"))
KALSHI_KEY_PATH = Path(os.path.expanduser("~/.algotrader_kalshi_private_key.pem"))

# ── Watchdog interval ─────────────────────────────────────────────────────────
WATCHDOG_INTERVAL_S = 10   # check engine thread health every 10 seconds
ENGINE_RESTART_DELAY_S = 5  # wait before restarting a crashed engine


# ═══════════════════════════════════════════════════════════════════════════════
# LIVE DATA ADAPTERS
# Each adapter is a lightweight helper that pulls data from an external source
# and returns it in a standard dict format. Used by engines and dashboards.
# ═══════════════════════════════════════════════════════════════════════════════

class PolygonAdapter:
    """
    Polygon.io — Real-time US equities, options, forex, crypto.
    Free tier: delayed data. Paid tier: real-time WebSocket.
    Sign up: https://polygon.io/
    """
    BASE = "https://api.polygon.io"

    def __init__(self, api_key: str):
        self.key = api_key
        self.ready = bool(api_key)

    def last_quote(self, symbol: str) -> dict | None:
        """Get latest trade price for a stock."""
        if not self.ready:
            return None
        try:
            r = requests.get(
                f"{self.BASE}/v2/last/trade/{symbol}",
                params={"apiKey": self.key},
                timeout=5,
            )
            data = r.json()
            if data.get("status") == "OK":
                result = data.get("results", {})
                return {"symbol": symbol, "price": result.get("p"), "size": result.get("s"), "ts": result.get("t")}
        except Exception as e:
            log.debug(f"Polygon quote {symbol}: {e}")
        return None

    def prev_close(self, symbol: str) -> float | None:
        """Get previous day's close price."""
        if not self.ready:
            return None
        try:
            r = requests.get(
                f"{self.BASE}/v2/aggs/ticker/{symbol}/prev",
                params={"apiKey": self.key},
                timeout=5,
            )
            data = r.json()
            results = data.get("results", [])
            if results:
                return results[0].get("c")
        except Exception as e:
            log.debug(f"Polygon prev_close {symbol}: {e}")
        return None

    def options_chain(self, underlying: str, limit: int = 10) -> list:
        """Get options contracts for an underlying."""
        if not self.ready:
            return []
        try:
            r = requests.get(
                f"{self.BASE}/v3/reference/options/contracts",
                params={"underlying_ticker": underlying, "limit": limit, "apiKey": self.key},
                timeout=5,
            )
            return r.json().get("results", [])
        except Exception as e:
            log.debug(f"Polygon options {underlying}: {e}")
        return []


class FinnhubAdapter:
    """
    Finnhub.io — Global stocks, forex, crypto, earnings, sentiment, alt data.
    Free tier: 60 req/min. 
    Sign up: https://finnhub.io/
    """
    BASE = "https://finnhub.io/api/v1"

    def __init__(self, api_key: str):
        self.key = api_key
        self.ready = bool(api_key)
        self._headers = {"X-Finnhub-Token": self.key}

    def quote(self, symbol: str) -> dict | None:
        """Real-time price quote."""
        if not self.ready:
            return None
        try:
            r = requests.get(f"{self.BASE}/quote", params={"symbol": symbol}, headers=self._headers, timeout=5)
            data = r.json()
            if data.get("c"):
                return {"symbol": symbol, "price": data["c"], "open": data.get("o"), "high": data.get("h"), "low": data.get("l"), "prev_close": data.get("pc")}
        except Exception as e:
            log.debug(f"Finnhub quote {symbol}: {e}")
        return None

    def news_sentiment(self, symbol: str) -> dict | None:
        """Company news sentiment score (-1 to 1)."""
        if not self.ready:
            return None
        try:
            r = requests.get(f"{self.BASE}/news-sentiment", params={"symbol": symbol}, headers=self._headers, timeout=5)
            data = r.json()
            return {"symbol": symbol, "score": data.get("sentiment", {}).get("bullishPercent", 0.5), "buzz": data.get("buzz", {}).get("buzz", 0)}
        except Exception as e:
            log.debug(f"Finnhub sentiment {symbol}: {e}")
        return None

    def forex_rate(self, from_ccy: str, to_ccy: str) -> float | None:
        """Real-time forex exchange rate."""
        if not self.ready:
            return None
        try:
            r = requests.get(f"{self.BASE}/forex/rates", params={"base": from_ccy}, headers=self._headers, timeout=5)
            return r.json().get("quote", {}).get(to_ccy)
        except Exception as e:
            log.debug(f"Finnhub forex {from_ccy}/{to_ccy}: {e}")
        return None

    def earnings_calendar(self, from_date: str, to_date: str) -> list:
        """Upcoming earnings that can move stock prices."""
        if not self.ready:
            return []
        try:
            r = requests.get(f"{self.BASE}/calendar/earnings", params={"from": from_date, "to": to_date}, headers=self._headers, timeout=5)
            return r.json().get("earningsCalendar", [])
        except Exception as e:
            log.debug(f"Finnhub earnings: {e}")
        return []

    def crypto_candles(self, symbol: str, resolution: str = "1", from_ts: int = 0, to_ts: int = 0) -> dict | None:
        """Crypto OHLCV candles (symbol like 'BINANCE:BTCUSDT')."""
        if not self.ready:
            return None
        try:
            r = requests.get(f"{self.BASE}/crypto/candle", params={"symbol": symbol, "resolution": resolution, "from": from_ts, "to": to_ts}, headers=self._headers, timeout=5)
            return r.json()
        except Exception as e:
            log.debug(f"Finnhub crypto candles {symbol}: {e}")
        return None


class CoinGeckoAdapter:
    """
    CoinGecko — Free crypto price API, no key required.
    Rate limit: 10-50 calls/min depending on endpoint.
    Docs: https://docs.coingecko.com/
    """
    BASE = "https://api.coingecko.com/api/v3"

    def price(self, coin_ids: list[str], vs_currency: str = "usd") -> dict:
        """
        Get prices for one or more coins.
        coin_ids: e.g. ['bitcoin', 'ethereum', 'solana']
        Returns: {'bitcoin': {'usd': 67000}, ...}
        """
        try:
            r = requests.get(
                f"{self.BASE}/simple/price",
                params={"ids": ",".join(coin_ids), "vs_currencies": vs_currency, "include_24hr_change": "true"},
                timeout=5,
            )
            return r.json()
        except Exception as e:
            log.debug(f"CoinGecko price: {e}")
        return {}

    def trending(self) -> list:
        """Get currently trending coins on CoinGecko."""
        try:
            r = requests.get(f"{self.BASE}/search/trending", timeout=5)
            return r.json().get("coins", [])
        except Exception as e:
            log.debug(f"CoinGecko trending: {e}")
        return []

    def market_chart(self, coin_id: str, vs_currency: str = "usd", days: int = 7) -> dict:
        """Historical price chart data."""
        try:
            r = requests.get(
                f"{self.BASE}/coins/{coin_id}/market_chart",
                params={"vs_currency": vs_currency, "days": days},
                timeout=8,
            )
            return r.json()
        except Exception as e:
            log.debug(f"CoinGecko chart {coin_id}: {e}")
        return {}


class ActionNetworkAdapter:
    """
    ActionNetwork — Sharp money tracking, line movement, consensus picks.
    Free public data available. Premium API for real-time alerts.
    Site: https://www.actionnetwork.com/
    Note: Uses undocumented public endpoints — may break without notice.
    """
    BASE = "https://api.actionnetwork.com/web/v1"

    def __init__(self, api_key: str = ""):
        self.key = api_key  # optional premium key

    def games(self, sport: str = "nfl") -> list:
        """
        Get upcoming games with odds lines.
        sport: 'nfl', 'nba', 'mlb', 'nhl', 'ncaaf', 'ncaab', 'soccer'
        """
        try:
            headers = {"x-api-key": self.key} if self.key else {}
            r = requests.get(f"{self.BASE}/games", params={"sport": sport}, headers=headers, timeout=8)
            return r.json().get("games", [])
        except Exception as e:
            log.debug(f"ActionNetwork games {sport}: {e}")
        return []

    def consensus(self, game_id: str | int) -> dict | None:
        """Public betting consensus % for a specific game."""
        try:
            r = requests.get(f"{self.BASE}/games/{game_id}/consensus", timeout=5)
            return r.json()
        except Exception as e:
            log.debug(f"ActionNetwork consensus {game_id}: {e}")
        return None


class SportradarAdapter:
    """
    Sportradar — Professional-grade sports data: lineups, injuries, live scores.
    Paid API. Trials available.
    Sign up: https://developer.sportradar.com/
    """
    BASE = "https://api.sportradar.us"

    def __init__(self, api_key: str):
        self.key = api_key
        self.ready = bool(api_key)

    def daily_schedule(self, sport: str = "nba", date: str = "") -> dict:
        """
        Get daily schedule for a sport.
        sport: 'nba', 'nfl', 'mlb', 'nhl'
        date: 'YYYY/MM/DD' — defaults to today if empty
        """
        if not self.ready:
            return {}
        if not date:
            date = datetime.now().strftime("%Y/%m/%d")
        try:
            r = requests.get(
                f"{self.BASE}/{sport}/trial/v8/en/games/{date}/schedule.json",
                params={"api_key": self.key},
                timeout=8,
            )
            return r.json()
        except Exception as e:
            log.debug(f"Sportradar schedule {sport}: {e}")
        return {}

    def live_boxscore(self, sport: str, game_id: str) -> dict:
        """Get live boxscore / in-game data."""
        if not self.ready:
            return {}
        try:
            r = requests.get(
                f"{self.BASE}/{sport}/trial/v8/en/games/{game_id}/boxscore.json",
                params={"api_key": self.key},
                timeout=5,
            )
            return r.json()
        except Exception as e:
            log.debug(f"Sportradar boxscore {game_id}: {e}")
        return {}

    def injuries(self, sport: str, team_id: str) -> list:
        """Get injury report for a team."""
        if not self.ready:
            return []
        try:
            r = requests.get(
                f"{self.BASE}/{sport}/trial/v8/en/teams/{team_id}/injuries.json",
                params={"api_key": self.key},
                timeout=5,
            )
            return r.json().get("injuries", [])
        except Exception as e:
            log.debug(f"Sportradar injuries {team_id}: {e}")
        return []


class LiveDataHub:
    """
    Centralized hub that holds all live data adapters.
    Engines and dashboards import this to access any data source.

    Usage:
        hub = LiveDataHub.from_creds(creds)
        price = hub.polygon.last_quote("AAPL")
        sentiment = hub.finnhub.news_sentiment("TSLA")
        btc = hub.coingecko.price(["bitcoin"])
        lines = hub.odds_api.get_sports_odds("basketball_nba", ...)
    """

    def __init__(
        self,
        polygon_key: str = "",
        finnhub_key: str = "",
        sportradar_key: str = "",
        action_network_key: str = "",
    ):
        self.polygon        = PolygonAdapter(polygon_key)
        self.finnhub        = FinnhubAdapter(finnhub_key)
        self.coingecko      = CoinGeckoAdapter()   # free, no key
        self.action_network = ActionNetworkAdapter(action_network_key)
        self.sportradar     = SportradarAdapter(sportradar_key)

    @classmethod
    def from_creds(cls, creds: dict) -> "LiveDataHub":
        return cls(
            polygon_key=creds.get("polygon_key", ""),
            finnhub_key=creds.get("finnhub_key", ""),
            sportradar_key=creds.get("sportradar_key", ""),
            action_network_key=creds.get("action_network_key", ""),
        )

    def status(self) -> dict:
        """Returns which adapters are active."""
        return {
            "polygon":        self.polygon.ready,
            "finnhub":        self.finnhub.ready,
            "coingecko":      True,   # always free
            "action_network": True,   # public endpoints always available
            "sportradar":     self.sportradar.ready,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# CREDENTIAL HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _load_saved() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception:
        return {}


def _save(saved: dict):
    try:
        CONFIG_PATH.write_text(json.dumps(saved, indent=2))
        os.chmod(CONFIG_PATH, 0o600)
    except Exception:
        pass


def _ask_text(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or default


def _ask_secret(label: str, default: str = "") -> str:
    suffix = " [saved]" if default else ""
    value = getpass(f"{label}{suffix}: ").strip()
    return value or default


def _ask_private_key_path(default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    first = input(f"Kalshi private key file path{suffix}: ").strip()
    if not first:
        return default
    if "BEGIN" not in first:
        return first
    lines = [first]
    while "END" not in lines[-1]:
        try:
            lines.append(input().rstrip())
        except EOFError:
            break
    return "\n".join(lines)


def _looks_like_private_key(value: str) -> bool:
    compact = value.strip()
    return (
        "BEGIN RSA PRIVATE KEY" in compact
        or "BEGIN PRIVATE KEY" in compact
        or compact.startswith("MII")
    )


def _private_key_default(value: str) -> str:
    if not value:
        return ""
    if _looks_like_private_key(value):
        return ""
    return value


def _materialize_private_key(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    expanded = os.path.expanduser(value)
    if os.path.exists(expanded):
        return expanded
    if not _looks_like_private_key(value):
        return expanded
    key_text = value
    if not key_text.startswith("-----BEGIN"):
        key_text = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            + key_text.replace(" ", "\n")
            + "\n-----END RSA PRIVATE KEY-----\n"
        )
    elif not key_text.endswith("\n"):
        key_text += "\n"
    KALSHI_KEY_PATH.write_text(key_text)
    os.chmod(KALSHI_KEY_PATH, 0o600)
    return str(KALSHI_KEY_PATH)


def _ask_yes_no(label: str, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    value = input(f"{label} ({hint}): ").strip().lower()
    if not value:
        return default
    return value in {"y", "yes", "true", "1"}


def _ask_choice(label: str, choices: list[str], default: str) -> str:
    console.print(f"{label}")
    for idx, choice in enumerate(choices, start=1):
        marker = " default" if choice == default else ""
        console.print(f"  {idx}. {choice}{marker}")
    raw = input(f"Choose 1-{len(choices)}: ").strip()
    if not raw:
        return default
    try:
        return choices[int(raw) - 1]
    except Exception:
        return default


def _prompt_credentials() -> dict:
    saved = _load_saved()
    saved_private_key_path = _materialize_private_key(saved.get("kalshi_private_key_path", ""))
    if saved_private_key_path != saved.get("kalshi_private_key_path", ""):
        saved["kalshi_private_key_path"] = saved_private_key_path
        _save(saved)
    if saved:
        console.print("[dim]Found saved API credentials.[/dim]")

    # ── Alpaca ────────────────────────────────────────────────────────────────
    alpaca_key = _ask_text("Alpaca API Key", os.getenv("ALPACA_API_KEY") or saved.get("api_key", ""))
    alpaca_secret = _ask_secret("Alpaca Secret Key", os.getenv("ALPACA_SECRET_KEY") or saved.get("secret_key", ""))
    mode = _ask_choice(
        "Alpaca trading mode:",
        ["Paper Trading (safe, simulated)", "Live Trading (real money)"],
        "Paper Trading (safe, simulated)",
    )
    alpaca_paper = "Paper" in mode
    if not alpaca_paper:
        confirmed = _ask_yes_no("LIVE Alpaca trading uses real money. Continue?", False)
        alpaca_paper = not confirmed

    # ── Sports / Kalshi ───────────────────────────────────────────────────────
    use_sports_default = bool(
        os.getenv("KALSHI_API_KEY") or saved.get("kalshi_api_key_id")
        or os.getenv("ODDS_API_KEY") or saved.get("odds_api_key")
    )
    use_sports = _ask_yes_no("Start sports betting arbitrage engine too?", use_sports_default)

    creds = {
        "api_key": alpaca_key or "",
        "secret_key": alpaca_secret or "",
        "alpaca_paper": alpaca_paper,
        "use_sports": bool(use_sports),
        "kalshi_api_key_id": "",
        "kalshi_private_key_path": "",
        "kalshi_env": os.getenv("KALSHI_ENV") or saved.get("kalshi_env", "demo"),
        "odds_api_key": "",
        # ── New data source keys ──────────────────────────────────────────────
        "polygon_key": os.getenv("POLYGON_API_KEY") or saved.get("polygon_key", ""),
        "finnhub_key": os.getenv("FINNHUB_API_KEY") or saved.get("finnhub_key", ""),
        "sportradar_key": os.getenv("SPORTRADAR_API_KEY") or saved.get("sportradar_key", ""),
        "action_network_key": os.getenv("ACTION_NETWORK_KEY") or saved.get("action_network_key", ""),
    }

    if use_sports:
        creds["kalshi_api_key_id"] = _ask_text(
            "Kalshi API Key ID",
            os.getenv("KALSHI_API_KEY") or saved.get("kalshi_api_key_id", ""),
        ) or ""
        creds["kalshi_private_key_path"] = _ask_private_key_path(
            os.getenv("KALSHI_PRIVATE_KEY_PATH") or _private_key_default(saved.get("kalshi_private_key_path", ""))
        ) or ""
        creds["kalshi_private_key_path"] = _materialize_private_key(creds["kalshi_private_key_path"])
        creds["odds_api_key"] = _ask_secret(
            "theOddsAPI key",
            os.getenv("ODDS_API_KEY") or saved.get("odds_api_key", ""),
        ) or ""
        creds["kalshi_env"] = _ask_choice(
            "Kalshi environment (demo or prod):",
            ["demo", "prod"],
            creds["kalshi_env"] if creds["kalshi_env"] in {"demo", "prod"} else "demo",
        )

    # ── Optional extra data sources ───────────────────────────────────────────
    console.print("\n[bold cyan]Optional Data Sources[/bold cyan] (press Enter to skip any)")
    if not creds["polygon_key"]:
        creds["polygon_key"] = _ask_secret("Polygon.io API key (equities/options)", "") or ""
    if not creds["finnhub_key"]:
        creds["finnhub_key"] = _ask_secret("Finnhub API key (global stocks/forex/crypto/sentiment)", "") or ""
    if not creds["sportradar_key"]:
        creds["sportradar_key"] = _ask_secret("Sportradar API key (pro sports data/injuries)", "") or ""
    if not creds["action_network_key"]:
        creds["action_network_key"] = _ask_secret("ActionNetwork premium key (optional — sharp money data)", "") or ""

    # Save everything
    sanitized_saved = {k: v for k, v in saved.items() if k not in {"kalshi_email", "kalshi_password"}}
    _save({
        **sanitized_saved,
        "api_key": creds["api_key"],
        "secret_key": creds["secret_key"],
        "kalshi_api_key_id": creds["kalshi_api_key_id"],
        "kalshi_private_key_path": creds["kalshi_private_key_path"],
        "kalshi_env": creds["kalshi_env"],
        "odds_api_key": creds["odds_api_key"],
        "polygon_key": creds["polygon_key"],
        "finnhub_key": creds["finnhub_key"],
        "sportradar_key": creds["sportradar_key"],
        "action_network_key": creds["action_network_key"],
    })
    return creds


# ═══════════════════════════════════════════════════════════════════════════════
# ENGINE WATCHDOG
# Monitors both engine threads and auto-restarts them if they die.
# ═══════════════════════════════════════════════════════════════════════════════

class EngineWatchdog:
    """
    Runs in its own daemon thread. Every WATCHDOG_INTERVAL_S seconds it checks
    that both the equity and kalshi engine threads are alive. If either crashes,
    it waits ENGINE_RESTART_DELAY_S and restarts it.
    """

    def __init__(self, equity: EquityEngine, kalshi: KalshiEngine | None,
                 alpaca, gk: RiskGatekeeper, creds: dict, data_hub: LiveDataHub):
        self.equity   = equity
        self.kalshi   = kalshi
        self.alpaca   = alpaca
        self.gk       = gk
        self.creds    = creds
        self.hub      = data_hub
        self._running = True
        self._thread  = threading.Thread(target=self._watch, daemon=True, name="Watchdog")
        self._lock    = threading.Lock()
        self._equity_restarts  = 0
        self._kalshi_restarts  = 0

    def start(self):
        self._thread.start()
        log.info("EngineWatchdog started.")

    def stop(self):
        self._running = False

    def _watch(self):
        while self._running:
            time.sleep(WATCHDOG_INTERVAL_S)
            try:
                self._check_equity()
                self._check_kalshi()
            except Exception as e:
                log.error(f"Watchdog error: {e}", exc_info=True)

    def _check_equity(self):
        with self._lock:
            thread = self.equity._thread
            if thread is None or not thread.is_alive():
                self._equity_restarts += 1
                log.warning(f"[WATCHDOG] Equity engine thread dead — restarting (attempt #{self._equity_restarts})")
                console.print(f"[bold yellow]⚠ Watchdog: Equity engine crashed — restarting... (#{self._equity_restarts})[/bold yellow]")
                time.sleep(ENGINE_RESTART_DELAY_S)
                try:
                    self.equity.start()
                    log.info("[WATCHDOG] Equity engine restarted successfully.")
                    console.print("[green]✅ Watchdog: Equity engine restarted.[/green]")
                except Exception as e:
                    log.error(f"[WATCHDOG] Failed to restart equity engine: {e}")

    def _check_kalshi(self):
        if self.kalshi is None:
            return
        with self._lock:
            thread = self.kalshi._thread
            if thread is None or not thread.is_alive():
                self._kalshi_restarts += 1
                log.warning(f"[WATCHDOG] Kalshi engine thread dead — restarting (attempt #{self._kalshi_restarts})")
                console.print(f"[bold yellow]⚠ Watchdog: Sports engine crashed — restarting... (#{self._kalshi_restarts})[/bold yellow]")
                time.sleep(ENGINE_RESTART_DELAY_S)
                try:
                    self.kalshi.start()
                    log.info("[WATCHDOG] Kalshi engine restarted successfully.")
                    console.print("[green]✅ Watchdog: Sports engine restarted.[/green]")
                except Exception as e:
                    log.error(f"[WATCHDOG] Failed to restart Kalshi engine: {e}")

    def status(self) -> dict:
        equity_alive = bool(self.equity._thread and self.equity._thread.is_alive())
        kalshi_alive = bool(self.kalshi and self.kalshi._thread and self.kalshi._thread.is_alive())
        return {
            "equity_alive": equity_alive,
            "kalshi_alive": kalshi_alive if self.kalshi else None,
            "equity_restarts": self._equity_restarts,
            "kalshi_restarts": self._kalshi_restarts,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# STATE / RENDER HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _state_payload(gk: RiskGatekeeper, equity: EquityEngine, kalshi: KalshiEngine | None,
                   mode: str, watchdog: EngineWatchdog | None = None,
                   data_hub: LiveDataHub | None = None) -> dict:
    kalshi_positions = kalshi.get_open_positions() if kalshi else []
    for pos in kalshi_positions:
        market_id = pos.get("market_id")
        current_price = pos.get("entry_price", 0)
        if kalshi and market_id:
            ob = kalshi.orderbook_cache.get(market_id) or kalshi.kalshi.get_orderbook(market_id)
            if ob:
                current_price = kalshi._mid_price(ob, pos.get("side", "yes")) or current_price
        pos["current_price"] = current_price
        pos["pnl"] = (current_price - pos.get("entry_price", 0)) * pos.get("contracts", 0)

    arb_table = list(kalshi.arb_table) if kalshi else []
    equity_positions = equity.get_open_positions()
    equity_open_pnl = sum(p.get("pnl_usd", 0) for p in equity_positions)
    kalshi_open_pnl = sum(p.get("pnl", 0) for p in kalshi_positions)
    equity_realized_pnl = sum(t.get("pnl", 0) for t in equity.trade_log if t.get("exit"))
    kalshi_realized_pnl = sum(t.get("pnl", 0) for t in kalshi.trade_log) if kalshi else 0

    orderbook = {}
    velocity = {}
    if kalshi:
        for market_id, ob in list(kalshi.orderbook_cache.items())[:20]:
            orderbook[market_id] = {
                "yes_bids": ob.get("yes", {}).get("bid", []),
                "yes_asks": ob.get("yes", {}).get("ask", []),
                "no_bids": ob.get("no", {}).get("bid", []),
                "no_asks": ob.get("no", {}).get("ask", []),
            }
        for pos in kalshi_positions:
            market_id = pos.get("market_id")
            velocity[market_id] = [
                {"t": idx, "price": price}
                for idx, (_, price) in enumerate(kalshi.get_velocity_data(market_id))
            ]

    payload = {
        "ts": datetime.now().isoformat(),
        "mode": mode,
        "risk": gk.status_dict(),
        "equity_positions": equity_positions,
        "signals": equity.signal_cache,
        "trade_log": list(equity.trade_log),
        "arb_table": arb_table,
        "confidence_table": list(kalshi.confidence_table) if kalshi else [],
        "kalshi_positions": kalshi_positions,
        "kalshi_trade_log": list(kalshi.trade_log) if kalshi else [],
        "orderbook": orderbook,
        "velocity": velocity,
        "summary": {
            "equity_open_pnl": round(equity_open_pnl, 2),
            "kalshi_open_pnl": round(kalshi_open_pnl, 2),
            "equity_realized_pnl": round(equity_realized_pnl, 2),
            "kalshi_realized_pnl": round(kalshi_realized_pnl, 2),
            "total_open_pnl": round(equity_open_pnl + kalshi_open_pnl, 2),
            "total_realized_pnl": round(equity_realized_pnl + kalshi_realized_pnl, 2),
        },
    }

    # Add watchdog status
    if watchdog:
        payload["watchdog"] = watchdog.status()

    # Add live data hub status
    if data_hub:
        payload["data_sources"] = data_hub.status()

    return payload


def _render_status(gk: RiskGatekeeper, equity: EquityEngine, kalshi: KalshiEngine | None,
                   mode: str, watchdog: EngineWatchdog | None = None,
                   data_hub: LiveDataHub | None = None) -> Panel:
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column("key", style="dim")
    table.add_column("val")

    table.add_row("Mode", f"[{'green' if mode == 'PAPER' else 'red'}]{mode}[/]")
    table.add_row("Equity engine", "[green]RUNNING ✓[/green]" if (equity._thread and equity._thread.is_alive()) else "[red]STOPPED ✗[/red]")

    if kalshi:
        table.add_row("Sports engine", "[green]RUNNING ✓[/green]" if (kalshi._thread and kalshi._thread.is_alive()) else "[red]STOPPED ✗[/red]")
    else:
        table.add_row("Sports engine", "[dim]disabled[/dim]")

    if watchdog:
        ws = watchdog.status()
        table.add_row("Watchdog", f"[cyan]active[/cyan] | restarts: equity={ws['equity_restarts']} sports={ws['kalshi_restarts']}")

    risk = gk.status_dict() if hasattr(gk, "status_dict") else {}
    table.add_row("Daily P&L", f"{risk.get('daily_pnl_pct', 0)*100:+.2f}%")
    table.add_row("Circuit", "[red]BROKEN[/red]" if risk.get("circuit_broken") else "[green]OK[/green]")

    if data_hub:
        active = [k for k, v in data_hub.status().items() if v]
        table.add_row("Data sources", ", ".join(active))

    table.add_row("Time", datetime.now().strftime("%H:%M:%S"))

    return Panel(table, title="[bold]AlgoTrader v2.2[/bold]", border_style="blue")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main(creds: dict | None = None):
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    console.print("[bold green]AlgoTrader Cross-Asset Engine v2.2[/bold green]")
    creds = creds or _prompt_credentials()

    # ── Alpaca connection ─────────────────────────────────────────────────────
    base_url = (
        "https://paper-api.alpaca.markets"
        if creds["alpaca_paper"]
        else "https://api.alpaca.markets"
    )
    alpaca = tradeapi.REST(creds["api_key"], creds["secret_key"], base_url, api_version="v2")
    alpaca.get_account()   # validates credentials immediately

    # ── Live data hub ─────────────────────────────────────────────────────────
    data_hub = LiveDataHub.from_creds(creds)
    active_sources = [k for k, v in data_hub.status().items() if v]
    console.print(f"[cyan]Live data sources active:[/cyan] {', '.join(active_sources)}")

    # ── Risk gatekeeper ───────────────────────────────────────────────────────
    gk = RiskGatekeeper()

    # ── Equity engine ─────────────────────────────────────────────────────────
    equity = EquityEngine(alpaca, gk, paper=creds["alpaca_paper"])

    # ── Sports / Kalshi engine ────────────────────────────────────────────────
    kalshi = None
    if creds.get("use_sports"):
        if creds.get("kalshi_api_key_id") and creds.get("kalshi_private_key_path") and creds.get("odds_api_key"):
            kalshi_client = KalshiClient(
                creds["kalshi_api_key_id"],
                creds["kalshi_private_key_path"],
                creds["kalshi_env"],
            )
            odds_bridge = OddsAPIBridge(creds["odds_api_key"])
            if kalshi_client.is_ready():
                kalshi = KalshiEngine(kalshi_client, odds_bridge, gk)
            else:
                console.print(f"[yellow]Sports engine skipped: {kalshi_client.auth_error}[/yellow]")
        else:
            console.print("[yellow]Sports engine skipped: Kalshi key ID, private key, and theOddsAPI key are required.[/yellow]")

 # ── Signal handler ────────────────────────────────────────────────────────
    running = True

    def stop(_sig=None, _frame=None):
        nonlocal running
        running = False
        watchdog.stop()
        equity.stop()
        if kalshi:
            kalshi.stop()

    
   # 1. Catch the signal error (This part is working!)
    try:
        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
    except ValueError:
        print("⚠️ Engine signal handling skipped (sub-thread).")
      
    # 2. DEFINE the watchdog (This is what's missing or misaligned)
    watchdog = EngineWatchdog(equity, kalshi, alpaca, gk, creds, data_hub)
    
    # 3. START the watchdog
    watchdog.start()

    console.print("[bold green]✅ All engines running. Watchdog active.[/bold green]")    # ── State write loop ──────────────────────────────────────────────────────
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    mode = "PAPER" if creds["alpaca_paper"] else "LIVE"
    with Live(_render_status(gk, equity, kalshi, mode, watchdog, data_hub), refresh_per_second=1) as live:
        while True:  # Indented 4 spaces under 'with'
            try:     # Indented 4 spaces under 'while'
                run_trading_cycle(gk, data_hub, equity, kalshi)
                time.sleep(2)
            except Exception as e:
                # This ensures the bot doesn't crash if a single cycle fails
                print(f"Cycle Error: {e}")
                time.sleep(5)
    
        
                
           
            payload = _state_payload(gk, equity, kalshi, mode, watchdog, data_hub)
            STATE_FILE.write_text(json.dumps(payload, indent=2, default=str))
            live.update(_render_status(gk, equity, kalshi, mode, watchdog, data_hub))
            time.sleep(2)

    # Final state flush
    payload = _state_payload(gk, equity, kalshi, mode, watchdog, data_hub)
    STATE_FILE.write_text(json.dumps(payload, indent=2, default=str))
    console.print("[yellow]AlgoTrader stopped.[/yellow]")


if __name__ == "__main__":
    main()
