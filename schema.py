"""
db/schema.py — Database layer.

Two stores:
  • PostgreSQL  — trade logs, arb records, sentiment events (relational, durable)
  • InfluxDB    — tick data, price velocity, latency metrics (time-series, fast reads)

Run  `python -m db.schema --init`  to create all tables.
"""

import argparse
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# PostgreSQL DDL
# ─────────────────────────────────────────────────────────────────────────────

POSTGRES_DDL = """
-- ── Equity trades ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS equity_trades (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    symbol          VARCHAR(12)  NOT NULL,
    side            VARCHAR(8)   NOT NULL,   -- 'long' | 'short'
    qty             INTEGER      NOT NULL,
    entry_price     NUMERIC(12,4) NOT NULL,
    exit_price      NUMERIC(12,4),
    exit_reason     VARCHAR(32),             -- 'TP' | 'SL' | 'TRAIL' | 'TIME' | 'CLOSE'
    realized_pnl    NUMERIC(12,4),
    entry_ema_fast  NUMERIC(12,4),
    entry_ema_slow  NUMERIC(12,4),
    entry_rsi       NUMERIC(6,2),
    entry_atr       NUMERIC(10,4),
    entry_adx       NUMERIC(6,2),
    entry_vix       NUMERIC(6,2),
    entry_vol_ratio NUMERIC(8,4),
    htf_trend       VARCHAR(8),
    strategy_ver    VARCHAR(16) DEFAULT '3.0',
    paper           BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE INDEX IF NOT EXISTS idx_eq_ts     ON equity_trades (ts DESC);
CREATE INDEX IF NOT EXISTS idx_eq_symbol ON equity_trades (symbol);

-- ── Kalshi arb trades ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS kalshi_trades (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    market_id       VARCHAR(64)  NOT NULL,   -- Kalshi market ticker
    sport           VARCHAR(16),
    event_desc      TEXT,
    side            VARCHAR(8)   NOT NULL,   -- 'yes' | 'no'
    contracts       INTEGER      NOT NULL,
    entry_price     NUMERIC(6,4) NOT NULL,   -- 0.01 – 0.99
    exit_price      NUMERIC(6,4),
    exit_reason     VARCHAR(32),
    realized_pnl    NUMERIC(12,4),
    implied_prob    NUMERIC(6,4),
    book_prob       NUMERIC(6,4),            -- from theOddsAPI
    edge_pct        NUMERIC(6,4),
    spread_at_entry NUMERIC(6,4),
    is_arb          BOOLEAN DEFAULT FALSE,
    early_exit      BOOLEAN DEFAULT FALSE,
    env             VARCHAR(8) DEFAULT 'demo'
);

CREATE INDEX IF NOT EXISTS idx_kal_ts       ON kalshi_trades (ts DESC);
CREATE INDEX IF NOT EXISTS idx_kal_market   ON kalshi_trades (market_id);

-- ── Sentiment / news events ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sentiment_events (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source      VARCHAR(32),   -- 'benzinga' | 'twitter' | 'manual'
    headline    TEXT NOT NULL,
    symbols     TEXT[],        -- array of affected tickers
    sentiment   VARCHAR(16),   -- 'bullish' | 'bearish' | 'halt'
    triggered_halt BOOLEAN DEFAULT FALSE
);

-- ── System health / latency log ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS system_health (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    feed            VARCHAR(32),   -- 'alpaca_ws' | 'kalshi_ws' | 'odds_api'
    latency_ms      INTEGER,
    status          VARCHAR(16),   -- 'ok' | 'stale' | 'error'
    detail          TEXT
);

-- ── Daily P&L summary (materialised view refreshed EOD) ──────────────────────
CREATE TABLE IF NOT EXISTS daily_pnl (
    date            DATE PRIMARY KEY,
    equity_pnl      NUMERIC(14,4) DEFAULT 0,
    kalshi_pnl      NUMERIC(14,4) DEFAULT 0,
    total_pnl       NUMERIC(14,4) DEFAULT 0,
    equity_trades   INTEGER DEFAULT 0,
    kalshi_trades   INTEGER DEFAULT 0,
    win_rate        NUMERIC(5,2),
    sharpe_daily    NUMERIC(8,4),
    max_drawdown    NUMERIC(8,4),
    kill_switch_hit BOOLEAN DEFAULT FALSE
);

-- ── Capital allocation snapshot ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS capital_snapshot (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    total_equity    NUMERIC(14,4),
    stock_allocated NUMERIC(14,4),
    kalshi_allocated NUMERIC(14,4),
    cash_available  NUMERIC(14,4),
    open_stock_pos  INTEGER,
    open_kalshi_pos INTEGER
);
"""

# ─────────────────────────────────────────────────────────────────────────────
# InfluxDB measurements (schema documentation — Influx is schema-on-write)
# ─────────────────────────────────────────────────────────────────────────────
#
# Measurement: equity_ticks
#   tags:    symbol, feed
#   fields:  price (float), volume (int), bid (float), ask (float),
#            spread (float), ema_fast (float), ema_slow (float),
#            rsi (float), atr (float), adx (float)
#   time:    nanosecond precision
#
# Measurement: kalshi_ticks
#   tags:    market_id, sport, side
#   fields:  yes_bid (float), yes_ask (float), no_bid (float), no_ask (float),
#            last_price (float), volume (int), implied_prob (float),
#            book_prob (float), edge (float), velocity (float)
#   time:    nanosecond precision
#
# Measurement: latency
#   tags:    feed
#   fields:  latency_ms (int), ok (bool)
#   time:    nanosecond precision
#
# Measurement: vix_adx
#   tags:    symbol
#   fields:  vix (float), adx (float), regime (string)
#   time:    nanosecond precision


# ─────────────────────────────────────────────────────────────────────────────
# Connection helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_postgres_conn():
    """Return a psycopg2 connection. Caller must close."""
    import psycopg2
    from config import POSTGRES_DSN
    return psycopg2.connect(POSTGRES_DSN)


def get_influx_client():
    """Return an InfluxDB WriteAPI client."""
    from influxdb_client import InfluxDBClient
    from config import INFLUX_URL, INFLUX_TOKEN, INFLUX_ORG
    client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
    return client


def init_postgres():
    conn = get_postgres_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(POSTGRES_DDL)
        conn.commit()
        log.info("PostgreSQL schema initialised.")
    finally:
        conn.close()


def log_equity_trade(trade: dict):
    conn = get_postgres_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO equity_trades
                  (symbol, side, qty, entry_price, exit_price, exit_reason,
                   realized_pnl, entry_ema_fast, entry_ema_slow, entry_rsi,
                   entry_atr, entry_adx, entry_vix, entry_vol_ratio, htf_trend, paper)
                VALUES
                  (%(symbol)s, %(side)s, %(qty)s, %(entry_price)s, %(exit_price)s,
                   %(exit_reason)s, %(realized_pnl)s, %(entry_ema_fast)s,
                   %(entry_ema_slow)s, %(entry_rsi)s, %(entry_atr)s, %(entry_adx)s,
                   %(entry_vix)s, %(entry_vol_ratio)s, %(htf_trend)s, %(paper)s)
            """, trade)
        conn.commit()
    except Exception as e:
        log.error(f"DB equity_trade insert failed: {e}")
    finally:
        conn.close()


def log_kalshi_trade(trade: dict):
    conn = get_postgres_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO kalshi_trades
                  (market_id, sport, event_desc, side, contracts, entry_price,
                   exit_price, exit_reason, realized_pnl, implied_prob,
                   book_prob, edge_pct, spread_at_entry, is_arb, early_exit, env)
                VALUES
                  (%(market_id)s, %(sport)s, %(event_desc)s, %(side)s,
                   %(contracts)s, %(entry_price)s, %(exit_price)s,
                   %(exit_reason)s, %(realized_pnl)s, %(implied_prob)s,
                   %(book_prob)s, %(edge_pct)s, %(spread_at_entry)s,
                   %(is_arb)s, %(early_exit)s, %(env)s)
            """, trade)
        conn.commit()
    except Exception as e:
        log.error(f"DB kalshi_trade insert failed: {e}")
    finally:
        conn.close()


def write_tick(measurement: str, tags: dict, fields: dict):
    """Fire-and-forget tick write to InfluxDB."""
    try:
        from influxdb_client.client.write_api import SYNCHRONOUS
        from influxdb_client import Point
        from config import INFLUX_BUCKET
        client = get_influx_client()
        write_api = client.write_api(write_options=SYNCHRONOUS)
        p = Point(measurement)
        for k, v in tags.items():
            p = p.tag(k, v)
        for k, v in fields.items():
            p = p.field(k, v)
        write_api.write(bucket=INFLUX_BUCKET, record=p)
        client.close()
    except Exception as e:
        log.debug(f"InfluxDB write skipped ({measurement}): {e}")


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--init", action="store_true", help="Initialise PostgreSQL schema")
    args = parser.parse_args()
    if args.init:
        logging.basicConfig(level="INFO")
        init_postgres()
