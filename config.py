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
# Verified against GET /series?category=Sports on Oct 10, 2026: all five exist,
# all are fee_type quadratic_with_maker_fees (multiplier 1, except KXMLBGAME 0.5).
# Scout logs any series with zero open markets (off-season or a renamed ticker).
DEFAULT_SPORT_SERIES = {
    "KXNFLGAME": "americanfootball_nfl",
    "KXNCAAFGAME": "americanfootball_ncaaf",
    "KXNBAGAME": "basketball_nba",
    "KXMLBGAME": "baseball_mlb",
    "KXNHLGAME": "icehockey_nhl",
}


# 15-minute "up or down" series (verified live Oct 10, 2026: fee_type quadratic,
# multiplier 1, settle on CF Benchmarks' real-time index averaged over the
# final 60 seconds vs the average over the 60 seconds before the open).
DEFAULT_CRYPTO_SERIES = {
    "KXBTC15M": "BTC",
    "KXETH15M": "ETH",
    "KXSOL15M": "SOL",
}

# Thresholds that depend on what the bot trades. load_settings() applies the
# preset for MARKET_FOCUS on top of the defaults below.
FOCUS_PRESETS = {
    "crypto_15m": {
        "scan_interval_s": 5.0,             # a whole market lasts 15 minutes
        "timeout_scout_s": 4.0,
        "timeout_edge_s": 4.0,
        "edge_confirm_window_s": 30.0,
        "skeptic_max_data_age_s": 10.0,     # spot older than 10 s is stale
        "skeptic_max_line_age_s": 30.0,     # an exchange print older than 30 s
        "skeptic_line_window_s": 30.0,
        "skeptic_max_line_move_cents": 8.0,
        "skeptic_max_fair_move": 0.10,
        "exec_order_timeout_s": 5.0,
        "timeout_execution_s": 20.0,
        "monitor_interval_s": 15.0,
        "vol_cache_s": 15.0,                # chart features want fresh 1-minute closes
    },
    "sports_live": {},
}


@dataclass(frozen=True)
class Settings:
    # ── Deploy switches ──────────────────────────────────────────────────────
    market_focus: str = "sports_live"             # "crypto_15m" or "sports_live" (env MARKET_FOCUS)
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
    scan_interval_s: float = 30.0                 # live prices move; scan often
    timeout_scout_s: float = 25.0
    timeout_edge_s: float = 20.0
    timeout_skeptic_s: float = 5.0
    timeout_risk_s: float = 5.0
    timeout_execution_s: float = 45.0
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
    odds_cache_s: float = 30.0                    # 1 Odds API credit per call; only sports with live candidates are fetched
    edge_min_cents: float = 3.0                   # after fees and slippage cap
    edge_min_confidence: int = 60                 # confidence model score, 0-100
    edge_max_book_dispersion: float = 0.04        # max stdev of devigged probs across books
    edge_confirm_window_s: float = 120.0
    edge_max_contracts: int = 500                 # hard ceiling on the proposal's upper bound
    edge_max_depth_take: float = 0.5              # never propose more than this share of ask depth
    match_max_start_diff_h: float = 8.0           # |Kalshi occurrence time - book kickoff|; occurrence is ~3h after kickoff
    # Which games to trade: "live_only" (in progress), "pregame_only", or "both".
    edge_game_phase: str = "live_only"
    edge_max_game_age_h: float = 4.5              # past this since kickoff, assume it's over / in OT chaos
    scout_live_lookahead_h: float = 4.0           # Scout keeps markets whose occurrence time is within
    scout_live_lookback_h: float = 3.0            #   [now - lookback, now + lookahead] (occurrence ~= kickoff + 3h)

    # ── Crypto 15-minute markets ─────────────────────────────────────────────
    crypto_series: dict = field(default_factory=lambda: dict(DEFAULT_CRYPTO_SERIES))
    crypto_min_seconds_to_close: float = 75.0     # model needs T >= the 60 s averaging window
    crypto_vol_lookback_min: int = 60             # minutes of 1-min candles for volatility
    crypto_vol_floor_per_min: float = 0.0004      # never assume calmer than 0.04%/min
    # Calibration from the Oct 2-10, 2026 backtest (2,400 settled BTC/ETH/SOL markets):
    # the raw model was underconfident (said 70-80%, hit 83%), so volatility is
    # scaled down; and Kalshi's own price was better calibrated than the model,
    # so fair value leans mostly on the market (logit blend, model weight 0.25).
    crypto_sigma_scale: float = 0.8
    crypto_model_weight: float = 0.25
    # Never buy contracts below this price: 10-35c contracts lost money in every
    # time window of the backtest (long shots are overpriced on Kalshi).
    edge_min_price_cents: int = 30
    edge_max_price_cents: int = 97
    spot_cache_s: float = 2.0
    vol_cache_s: float = 30.0

    # ── Skeptic ──────────────────────────────────────────────────────────────
    skeptic_max_data_age_s: float = 60.0          # live: a minute-old price is already old
    skeptic_line_window_s: float = 120.0
    skeptic_max_line_move_cents: float = 5.0      # Kalshi ask move inside the window
    skeptic_max_fair_move: float = 0.04           # sportsbook fair prob move inside the window
    skeptic_min_sources: int = 2                  # independent books behind fair value
    skeptic_record_lookback: int = 20             # settled trades per market type
    skeptic_record_min_sample: int = 8            # losing record = net P&L < 0 over >= this many
    skeptic_max_line_age_s: float = 120.0         # live line not updated in 2 min = suspended or stale

    # ── Risk Manager ─────────────────────────────────────────────────────────
    risk_stake_pct_of_realized: float = 0.02      # 2% of realized profit
    risk_stake_floor_usd: float = 1.00
    risk_stake_ceiling_usd: float = 25.00
    risk_max_open_positions: int = 10
    risk_max_event_exposure_usd: float = 40.00
    risk_daily_loss_limit_usd: float = 30.00
    # Exits, checked every second against live prices. Measured on net P&L
    # (what you'd pocket selling at the bid after the exit fee, minus cost).
    risk_take_profit_pct: float = 0.25            # close at +25%
    risk_stop_loss_pct: float = 0.20              # close at -20%
    # Off by default: in the backtest, take-profit/stop-loss exits turned
    # -3.3c/contract (hold to settlement) into -4.8c, because every exit pays
    # the spread and another fee. Set True to re-enable.
    risk_exits_enabled: bool = False
    risk_one_entry_per_market: bool = True        # no re-entry after an exit in the same market
    # Losing-run cool-down: if the last N settled trades net negative, stake x factor.
    risk_cooldown_lookback: int = 10
    risk_cooldown_factor: float = 0.5

    # ── Execution ────────────────────────────────────────────────────────────
    exec_slippage_cap_cents: int = 1              # limit = observed ask + cap
    exec_order_timeout_s: float = 15.0            # cancel unfilled remainder after this (live: don't rest long)
    exec_poll_interval_s: float = 3.0

    # ── Monitor ──────────────────────────────────────────────────────────────
    monitor_interval_s: float = 60.0
    marks_interval_s: float = 1.0                 # live prices for open positions (one request per tick)

    # ── Learner (self-improvement from settled markets) ─────────────────────
    learner_enabled: bool = True
    learner_interval_s: float = 1800.0            # refit every 30 min
    learner_window_h: float = 72.0                # learn from the last 3 days
    learner_min_observations: int = 1500          # ~100 settled markets before any change
    learner_min_improvement: float = 0.002        # held-out log loss must drop by this to switch
    learner_min_trades: int = 30                  # simulated trades needed on the older 70%
    learner_min_t: float = 1.0                    # newer 30% must be positive with t >= this
    timeout_learner_s: float = 120.0
    learner_chart_l2: float = 2.0                 # ridge penalty for the chart model (bigger = more cautious)

    # Paper exploration: when Learner pauses (no proven edge), paper mode keeps
    # trading on the chart model's price so the bot learns from its own trades.
    # Never applies in live mode.
    paper_explore: bool = True
    explore_edge_min_cents: float = 1.0
    explore_min_confidence: int = 40

    # Shadow trades: simulated bets the bot would not normally take, one per
    # strategy per market, settled and explained afterwards. Never sent.
    shadow_enabled: bool = True
    shadow_strategies: tuple = ("chart", "momentum", "fade", "favorite", "underdog")
    shadow_entry_max_secs: float = 600.0          # enter at the first look with <= 10 min left
    shadow_entry_min_secs: float = 150.0
    # Skeptic refuses setups (side of strike x momentum x price band) whose
    # shadow + real record over >= N trades loses money with t <= -1.
    # Follow the best tested strategy (kalshi/selector.py). Paper mode follows
    # the winner even if it hasn't proven itself on newer markets (so you can
    # watch it); live mode only follows a validated winner.
    follow_best_strategy: bool = True
    selector_min_t: float = 1.0
    skeptic_setup_min_n: int = 40
    skeptic_setup_max_t: float = -1.0
    # On startup, rebuild learning data from the last N hours of settled
    # markets (Kalshi candles + Coinbase candles), so a deploy or restart on
    # the free plan doesn't make the agents forget. 0 disables.
    learner_backfill_h: float = 72.0

    # ── Keep-alive ───────────────────────────────────────────────────────────
    # Render's free plan stops a service after 15 minutes without inbound
    # traffic, and the engine stops with it. Pinging our own public URL keeps
    # it awake (Render sets RENDER_EXTERNAL_URL). Empty disables it.
    keepalive_url: str = ""
    keepalive_interval_s: float = 600.0

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
    focus = os.getenv("MARKET_FOCUS", "crypto_15m").strip().lower()
    if focus not in FOCUS_PRESETS:
        raise ValueError(f"MARKET_FOCUS must be one of {list(FOCUS_PRESETS)}")
    base = Settings(market_focus=focus, **FOCUS_PRESETS[focus])
    return replace(base,
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
        keepalive_url=os.getenv("KEEPALIVE_URL", os.getenv("RENDER_EXTERNAL_URL", "")).strip(),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
    )
