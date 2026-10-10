"""
config.py: every threshold for the Kalshi agent pipeline lives here.

Secrets and deploy switches come from environment variables (Render dashboard
or a local .env). Everything else is a plain default in the Settings dataclass.
Agents receive a Settings instance; none of them define their own constants.

Env vars read:
    PAPER_MODE                  "true" (default) | "false"
    KILL_SWITCH                 "false" (default) | "true"
    KALSHI_ENV                  "demo" (default) | "prod"
    KALSHI_API_KEY              Kalshi API key ID
    KALSHI_PRIVATE_KEY_CONTENT  PEM text (literal "\\n" sequences are accepted)
    KALSHI_PRIVATE_KEY_PATH     alternative: path to a PEM file (Render secret file)
    ODDS_API_KEY                the-odds-api.com key
    JOURNAL_DB_PATH             SQLite path (default data/journal.db)
    DASHBOARD_PASSWORD          optional; gates the kill switch toggle
    PORT                        port Streamlit binds (Render sets this)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path


def _load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader so local runs work without python-dotenv.
    Never overrides a variable that is already set in the environment."""
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# Kalshi series ticker -> the-odds-api sport key.
# VERIFY: these series tickers are my best knowledge of Kalshi's single-game
# winner series as of Oct 2026. Scout logs any series that returns zero open
# markets at startup, which is the signal that a ticker here is wrong.
DEFAULT_SPORT_SERIES = {
    "KXNFLGAME": "americanfootball_nfl",
    "KXNCAAFGAME": "americanfootball_ncaaf",
    "KXNBAGAME": "basketball_nba",
    "KXMLBGAME": "baseball_mlb",
    "KXNHLGAME": "icehockey_nhl",
}


@dataclass(frozen=True)
class Settings:
    # ── Deploy switches ──────────────────────────────────────────────────────
    paper_mode: bool = True
    kill_switch_env: bool = False
    kalshi_env: str = "demo"                      # "demo" | "prod"
    timezone: str = "America/New_York"            # defines "today" for the daily loss limit

    # ── Kalshi API ───────────────────────────────────────────────────────────
    # Base URLs from docs.kalshi.com (read Oct 10, 2026). Both are config so a
    # change on Kalshi's side is a config edit, not a code edit.
    kalshi_base_urls: dict = field(default_factory=lambda: {
        "demo": "https://external-api.demo.kalshi.co/trade-api/v2",
        "prod": "https://external-api.kalshi.com/trade-api/v2",
    })
    kalshi_api_key_id: str = ""
    kalshi_private_key_pem: str = ""
    kalshi_private_key_path: str = ""
    http_timeout_s: float = 10.0
    http_max_retries: int = 4
    http_backoff_base_s: float = 0.5
    http_backoff_max_s: float = 8.0
    # Basic tier budgets (tokens/sec). Most requests cost 10 tokens, cancels 2.
    rate_read_tokens_per_s: float = 200.0
    rate_write_tokens_per_s: float = 100.0
    rate_default_cost: int = 10
    rate_cancel_cost: int = 2
    self_trade_prevention_type: str = "taker_at_cross"

    # ── Fees (Kalshi fee schedule effective Jul 7, 2026) ─────────────────────
    # taker = ceil(M * 0.07 * C * P * (1-P)); maker = ceil(M_maker * 0.0175 * C * P * (1-P))
    # Rounding is to your account's balance precision: $0.01 for FCM-cleared
    # members, $0.0001 for direct members. $0.01 is the conservative choice
    # (it can only overstate fees). Change once you know your member type.
    fee_taker_coefficient: float = 0.07
    fee_maker_coefficient: float = 0.0175
    fee_taker_multiplier: float = 1.0
    fee_maker_multiplier: float = 1.0             # assume maker fees apply (safe side)
    fee_balance_precision: float = 0.01

    # ── Orchestrator ─────────────────────────────────────────────────────────
    scan_interval_s: float = 60.0
    timeout_scout_s: float = 45.0
    timeout_edge_s: float = 20.0
    timeout_skeptic_s: float = 5.0
    timeout_risk_s: float = 5.0
    timeout_execution_s: float = 90.0
    timeout_monitor_s: float = 45.0
    journal_edge_rejects: bool = False            # also journal candidates Edge Finder passed on

    # ── Scout ────────────────────────────────────────────────────────────────
    sport_series: dict = field(default_factory=lambda: dict(DEFAULT_SPORT_SERIES))
    scout_min_volume_24h: float = 500.0           # contracts
    scout_min_ask_depth: float = 25.0             # contracts resting at the best ask
    scout_max_spread_cents: int = 6               # X
    scout_min_minutes_to_close: int = 30          # N
    scout_max_candidates_per_series: int = 40
    scout_orderbook_depth: int = 10

    # ── Edge Finder ──────────────────────────────────────────────────────────
    odds_api_base: str = "https://api.the-odds-api.com/v4"
    odds_api_key: str = ""
    odds_bookmakers: str = "pinnacle,fanduel,draftkings,betmgm,williamhill_us"
    odds_cache_s: float = 120.0                   # each call spends Odds API credits
    edge_min_cents: float = 3.0                   # after fees and slippage cap
    edge_min_confidence: int = 60                 # confidence model score, 0-100
    edge_max_book_dispersion: float = 0.04        # max stdev of devigged probs across books
    edge_confirm_window_s: float = 300.0
    edge_max_contracts: int = 500                 # hard ceiling on the proposal's upper bound
    edge_max_depth_take: float = 0.5              # never propose more than this share of ask depth
    match_max_start_diff_h: float = 36.0          # Kalshi vs sportsbook start time tolerance
    edge_skip_in_play: bool = True                # pregame only; in-play lines move too fast

    # ── Skeptic ──────────────────────────────────────────────────────────────
    skeptic_max_data_age_s: float = 180.0
    skeptic_line_window_s: float = 300.0
    skeptic_max_line_move_cents: float = 5.0      # Kalshi ask move inside the window
    skeptic_max_fair_move: float = 0.04           # sportsbook fair prob move inside the window
    skeptic_min_sources: int = 2                  # independent books behind fair value
    skeptic_record_lookback: int = 20             # settled trades per market type
    skeptic_record_min_sample: int = 8            # losing record = net P&L < 0 over >= this many
    skeptic_max_line_age_s: float = 6 * 3600      # book says line unchanged this long = stale

    # ── Risk Manager ─────────────────────────────────────────────────────────
    risk_stake_pct_of_realized: float = 0.02      # 2% of realized profit
    risk_stake_floor_usd: float = 1.00
    risk_stake_ceiling_usd: float = 25.00
    risk_max_open_positions: int = 10
    risk_max_event_exposure_usd: float = 40.00
    risk_daily_loss_limit_usd: float = 30.00

    # ── Execution ────────────────────────────────────────────────────────────
    exec_slippage_cap_cents: int = 1              # limit = observed ask + cap
    exec_order_timeout_s: float = 45.0            # cancel unfilled remainder after this
    exec_poll_interval_s: float = 3.0

    # ── Monitor ──────────────────────────────────────────────────────────────
    monitor_interval_s: float = 60.0

    # ── Storage / dashboard ──────────────────────────────────────────────────
    journal_db_path: str = "data/journal.db"
    dashboard_password: str = ""
    port: int = 10000
    log_level: str = "INFO"

    @property
    def kalshi_base_url(self) -> str:
        if self.kalshi_env not in self.kalshi_base_urls:
            raise ValueError(f"KALSHI_ENV must be one of {list(self.kalshi_base_urls)}")
        return self.kalshi_base_urls[self.kalshi_env]

    def with_overrides(self, **kwargs) -> "Settings":
        return replace(self, **kwargs)


def load_settings(dotenv_path: str = ".env") -> Settings:
    _load_dotenv(dotenv_path)
    pem = os.getenv("KALSHI_PRIVATE_KEY_CONTENT", "").replace("\\n", "\n").strip()
    return Settings(
        paper_mode=_env_bool("PAPER_MODE", True),
        kill_switch_env=_env_bool("KILL_SWITCH", False),
        kalshi_env=os.getenv("KALSHI_ENV", "demo").strip().lower(),
        kalshi_api_key_id=os.getenv("KALSHI_API_KEY", "").strip(),
        kalshi_private_key_pem=pem,
        kalshi_private_key_path=os.getenv("KALSHI_PRIVATE_KEY_PATH", "").strip(),
        odds_api_key=os.getenv("ODDS_API_KEY", "").strip(),
        journal_db_path=os.getenv("JOURNAL_DB_PATH", "data/journal.db"),
        dashboard_password=os.getenv("DASHBOARD_PASSWORD", ""),
        port=int(os.getenv("PORT", "10000")),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
    )
