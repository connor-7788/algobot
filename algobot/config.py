"""
config.py — Central configuration for the Cross-Asset Trading Engine.
All tunable parameters live here. Never hard-code values in logic files.
"""

import os
from dataclasses import dataclass, field
from typing import List

# ── API Credentials (load from environment — never commit keys) ──────────────
ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_PAPER      = os.getenv("ALPACA_PAPER", "true").lower() == "true"

KALSHI_API_KEY    = os.getenv("KALSHI_API_KEY", "")
KALSHI_EMAIL      = os.getenv("KALSHI_EMAIL", "")
KALSHI_PASSWORD   = os.getenv("KALSHI_PASSWORD", "")
KALSHI_ENV        = os.getenv("KALSHI_ENV", "demo")   # "demo" | "prod"

ODDS_API_KEY      = os.getenv("ODDS_API_KEY", "")    # theOddsAPI.com
BENZINGA_API_KEY  = os.getenv("BENZINGA_API_KEY", "")
# ── New Live Data Sources ─────────────────────────────────────────────────────
# Polygon.io — real-time US equities, options, forex, crypto
# Sign up: https://polygon.io/  (free tier: delayed | paid: real-time)
POLYGON_API_KEY   = os.getenv("POLYGON_API_KEY", "")

# Finnhub — global stocks, forex, crypto, earnings, news sentiment
# Sign up: https://finnhub.io/  (free tier: 60 req/min)
FINNHUB_API_KEY   = os.getenv("FINNHUB_API_KEY", "")

# Sportradar — professional sports data: lineups, injuries, live scores
# Sign up: https://developer.sportradar.com/  (trials available)
SPORTRADAR_API_KEY = os.getenv("SPORTRADAR_API_KEY", "")

# ActionNetwork — sharp money tracking, line movement, consensus picks
# Premium key optional — public endpoints work without a key
ACTION_NETWORK_KEY = os.getenv("ACTION_NETWORK_KEY", "")
# CoinGecko — crypto prices (FREE, no key needed — accessed directly)

# ── Database ──────────────────────────────────────────────────────────────────
POSTGRES_DSN   = os.getenv("POSTGRES_DSN", "postgresql://trader:trader@localhost:5432/tradedb")
INFLUX_URL     = os.getenv("INFLUX_URL",   "http://localhost:8086")
INFLUX_TOKEN   = os.getenv("INFLUX_TOKEN", "")
INFLUX_ORG     = os.getenv("INFLUX_ORG",  "trading")
INFLUX_BUCKET  = os.getenv("INFLUX_BUCKET","tickdata")

# ── Risk / Firewall ───────────────────────────────────────────────────────────
MAX_DAILY_LOSS_PCT      = 0.02    # 2%  — hard kill-switch across entire account
MAX_STOCK_POSITION_PCT  = 0.15    # 15% of equity per stock position
MAX_KALSHI_POSITION_USD = 0       # 0 = no fixed cap; high-confidence bets use KALSHI_BET_FRACTION
MAX_CONCURRENT_STOCKS   = 4
MAX_CONCURRENT_KALSHI   = 3
DATA_LATENCY_LIMIT_MS   = 500     # halt orders if feed age > 500ms
CAPITAL_SPLIT_STOCK_PCT = 0.60    # 60% capital reserved for equities
CAPITAL_SPLIT_KALSHI_PCT= 0.40    # 40% capital reserved for Kalshi


# ── Dump Protection ───────────────────────────────────────────────────────────
DUMP_THRESHOLD_PCT  = 0.015   # 1.5% drop triggers emergency sell
DUMP_LOOKBACK_BARS  = 2       # number of 1-min bars to look back for the drop

# ── Equity Strategy ───────────────────────────────────────────────────────────
STOCK_SYMBOLS      = ["AAPL", "TSLA", "NVDA", "AMZN", "MSFT", "AMD", "META", "GOOGL", "SPY", "QQQ"]
EMA_FAST           = 8
EMA_SLOW           = 21
RSI_PERIOD         = 14
RSI_LONG_MIN       = 38
RSI_LONG_MAX       = 58
RSI_SHORT_MIN      = 42
RSI_SHORT_MAX      = 65
ATR_PERIOD         = 14
ATR_MULTIPLIER     = 1.5          # stop = entry ± (ATR × multiplier)
TAKE_PROFIT_PCT    = 0.025
TRAILING_TRIGGER   = 0.008        # activate trail after +0.8% in favour
TRAILING_STOP_PCT  = 0.010
EARLY_STOP_PCT     = 0.007
EARLY_STOP_MINS    = 5
MAX_HOLD_MINS      = 90
CLOSE_EARLY_MINS   = 15           # force-exit N min before market close
EMA_SEP_MIN_PCT    = 0.001
VOLUME_MULT_MIN    = 1.2
ADX_TREND_MIN      = 25           # ADX > 25 = trending market, prefer directional
VIX_HIGH_THRESHOLD = 30           # VIX > 30 = reduce position size by 50%
POLL_SECONDS       = 30

# ── Kalshi / Arbitrage ────────────────────────────────────────────────────────
KALSHI_SPORTS      = ["NFL", "NBA", "MLB", "EPL", "NHL", "MMA", "NCAAF", "NCAAB"]
MIN_EDGE_PCT       = 0.03         # only trade arb if edge > 3% (was 5%)
MAX_SPREAD_PCT     = 0.12         # skip market if bid/ask spread > 12% (was 8%)
KALSHI_BET_FRACTION = 0.05        # stake 5% of Kalshi bankroll per high-confidence bet
HIGH_CONF_MIN_EDGE_PCT = 0.04     # require stronger edge than basic arb (was 0.08)
HIGH_CONF_MAX_SPREAD_PCT = 0.10   # spread allowed for executable confidence (was 0.04)
HIGH_CONF_MIN_BOOKS = 1           # only 1 book required — OddsAPI sometimes only returns 1 (was 2)
HIGH_CONF_CONFIRMATIONS = 1       # confirmations before placing (was 3 — caused 15s delay)
HIGH_CONF_LOOKBACK_SEC = 90       # confirmation window
HIGH_CONF_EDGE_DECAY_PCT = 0.02   # reject if edge fades too much while confirming
HIGH_CONF_MIN_SCORE = 50          # confidence score required to place order (was 80)
VELOCITY_WINDOW_S  = 60           # "in-play velocity" window in seconds
VELOCITY_THRESHOLD = 0.03         # 3% price move in window = major event
KALSHI_POLL_SEC    = 5            # poll Kalshi every 5s (faster than stocks)
EARLY_EXIT_THRESH  = 0.85         # take Kalshi profit if prob hits 85% (was 50%)

# ── Sentiment / News ─────────────────────────────────────────────────────────
SENTIMENT_HALT_KEYWORDS = [
    "FOMC", "Fed decision", "rate hike", "emergency", "circuit breaker",
    "trading halt", "injury report", "ejected", "suspended"
]
NEWS_LOOKBACK_MINS = 10           # check news published in last N minutes

# ── Dashboard ─────────────────────────────────────────────────────────────────
DASH_HOST          = "0.0.0.0"
DASH_PORT_A        = 8501         # Wall Street Engine dashboard
DASH_PORT_B        = 8502         # Sharp Sports Command dashboard
DASH_REFRESH_MS    = 1000         # 1 second refresh on dashboard        # in-app dashboard update interval

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_LEVEL          = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE           = "logs/engine.log"
