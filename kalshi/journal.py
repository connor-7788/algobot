"""
SQLite journal. Every proposal is written here, rejected ones included, along
with each agent's verdict. The dashboard reads it; Risk and Skeptic query it
for realized P&L and per-market-type records; the kill switch flag lives here.

The engine and the Streamlit process open separate connections. WAL mode lets
the dashboard read while the engine writes.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from kalshi.proposal import OPEN_STATUSES, REALIZED_STATUSES, Status, TradeProposal

SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    proposal_id     TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    market_ticker   TEXT NOT NULL,
    event_ticker    TEXT NOT NULL,
    series_ticker   TEXT,
    title           TEXT,
    side            TEXT NOT NULL,
    price_cents     INTEGER NOT NULL,
    contract_count  INTEGER NOT NULL,
    original_count  INTEGER NOT NULL,
    fair_probability REAL NOT NULL,
    edge_cents      REAL,
    confidence_score INTEGER,
    status          TEXT NOT NULL,
    rejected_by     TEXT,
    reject_reason   TEXT,
    paper           INTEGER NOT NULL,
    order_id        TEXT,
    filled_count    INTEGER DEFAULT 0,
    avg_fill_price_cents REAL DEFAULT 0,
    fees_paid_usd   REAL DEFAULT 0,
    result          TEXT,
    realized_pnl_usd REAL,
    settled_at      TEXT,
    payload         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_prop_status ON proposals(status);
CREATE INDEX IF NOT EXISTS ix_prop_series ON proposals(series_ticker, settled_at);
CREATE INDEX IF NOT EXISTS ix_prop_created ON proposals(created_at);

CREATE TABLE IF NOT EXISTS flags (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT
);

CREATE TABLE IF NOT EXISTS agent_status (
    agent         TEXT PRIMARY KEY,
    state         TEXT NOT NULL,          -- working | idle | halted | error
    activity      TEXT NOT NULL,          -- what it's doing right now
    last_message  TEXT,                   -- what it last said
    last_decision TEXT,                   -- APPROVE / REJECT / MODIFY / INFO
    ticker        TEXT,
    detail        TEXT,                   -- JSON
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_feed (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    agent    TEXT NOT NULL,
    decision TEXT,
    ticker   TEXT,
    message  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_feed_agent ON agent_feed(agent, id);

CREATE TABLE IF NOT EXISTS marks (
    ticker     TEXT PRIMARY KEY,
    yes_bid    REAL,
    yes_ask    REAL,
    last_price REAL,
    status     TEXT,
    result     TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    level   TEXT NOT NULL,
    kind    TEXT NOT NULL,
    message TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS observations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker      TEXT NOT NULL,
    series      TEXT,
    close_ts    INTEGER NOT NULL,
    secs_left   REAL NOT NULL,
    spot        REAL NOT NULL,
    strike      REAL NOT NULL,
    strike_type TEXT,
    sigma       REAL NOT NULL,
    bid         INTEGER,
    ask         INTEGER,
    observed_at TEXT NOT NULL,
    y           INTEGER
);
CREATE INDEX IF NOT EXISTS obs_ticker ON observations(ticker);
CREATE INDEX IF NOT EXISTS obs_close ON observations(close_ts);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


class Journal:
    _realized = tuple(sorted(st.value for st in REALIZED_STATUSES))

    def __init__(self, path: str, paper: bool = True):
        self.path = path
        self.paper = paper
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            if path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── Proposals ────────────────────────────────────────────────────────────

    def save(self, p: TradeProposal) -> None:
        rejecting = next((v for v in p.verdicts if v.decision.value == "REJECT"), None)
        row = {
            "proposal_id": p.proposal_id,
            "created_at": _iso(p.created_at),
            "updated_at": _now(),
            "market_ticker": p.market_ticker,
            "event_ticker": p.event_ticker,
            "series_ticker": p.series_ticker,
            "title": p.title,
            "side": p.side,
            "price_cents": int(p.price_cents),
            "contract_count": int(p.contract_count),
            "original_count": int(p.original_count),
            "fair_probability": float(p.fair_probability),
            "edge_cents": float(p.edge_cents),
            "confidence_score": int(p.confidence_score),
            "status": p.status.value,
            "rejected_by": rejecting.agent if rejecting else None,
            "reject_reason": rejecting.reason if rejecting else None,
            "paper": 1 if self.paper else 0,
            "order_id": p.order_id,
            "filled_count": int(p.filled_count),
            "avg_fill_price_cents": float(p.avg_fill_price_cents),
            "fees_paid_usd": float(p.fees_paid_usd),
            "result": p.result,
            "realized_pnl_usd": p.realized_pnl_usd,
            "settled_at": _iso(p.settled_at),
            "payload": json.dumps(p.to_dict(), default=str),
        }
        cols = ", ".join(row)
        marks = ", ".join(f":{k}" for k in row)
        updates = ", ".join(f"{k}=excluded.{k}" for k in row if k not in ("proposal_id", "created_at"))
        with self._lock:
            self._conn.execute(
                f"INSERT INTO proposals ({cols}) VALUES ({marks}) "
                f"ON CONFLICT(proposal_id) DO UPDATE SET {updates}", row)
            self._conn.commit()

    def get(self, proposal_id: str) -> TradeProposal | None:
        with self._lock:
            r = self._conn.execute("SELECT payload FROM proposals WHERE proposal_id=?",
                                   (proposal_id,)).fetchone()
        return TradeProposal.from_dict(json.loads(r["payload"])) if r else None

    def by_status(self, statuses) -> list[TradeProposal]:
        vals = [s.value if isinstance(s, Status) else s for s in statuses]
        marks = ",".join("?" for _ in vals)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT payload FROM proposals WHERE status IN ({marks}) ORDER BY created_at",
                vals).fetchall()
        return [TradeProposal.from_dict(json.loads(r["payload"])) for r in rows]

    def traded_tickers(self) -> set[str]:
        """Markets where we already got filled (open, closed early, or settled)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT market_ticker FROM proposals WHERE filled_count > 0 AND paper=?",
                (1 if self.paper else 0,)).fetchall()
        return {r["market_ticker"] for r in rows}

    def open_positions(self) -> list[TradeProposal]:
        """Proposals holding a slot: approved, filled, or in an unknown state."""
        return self.by_status(OPEN_STATUSES)

    # ── P&L queries used by Risk and Skeptic ─────────────────────────────────

    def realized_pnl_total(self) -> float:
        with self._lock:
            r = self._conn.execute(
                "SELECT COALESCE(SUM(realized_pnl_usd), 0) AS s FROM proposals "
                "WHERE status IN (?, ?) AND paper=?", (*self._realized, 1 if self.paper else 0)).fetchone()
        return float(r["s"])

    def realized_pnl_since(self, since: datetime) -> float:
        with self._lock:
            r = self._conn.execute(
                "SELECT COALESCE(SUM(realized_pnl_usd), 0) AS s FROM proposals "
                "WHERE status IN (?, ?) AND paper=? AND settled_at >= ?",
                (*self._realized, 1 if self.paper else 0, _iso(since))).fetchone()
        return float(r["s"])

    def market_type_results(self, series_ticker: str, limit: int) -> list[float]:
        """Realized P&L of the most recent settled trades in a series, newest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT realized_pnl_usd FROM proposals WHERE status IN (?, ?) AND series_ticker=? "
                "AND paper=? ORDER BY settled_at DESC LIMIT ?",
                (*self._realized, series_ticker, 1 if self.paper else 0, limit)).fetchall()
        return [float(r["realized_pnl_usd"] or 0.0) for r in rows]

    # ── Dashboard reads ──────────────────────────────────────────────────────

    def win_loss(self) -> tuple[int, int]:
        """(closed or settled trades, winners) for this mode."""
        with self._lock:
            r = self._conn.execute(
                f"SELECT COUNT(*), COALESCE(SUM(realized_pnl_usd > 0), 0) FROM proposals WHERE status IN "
                f"({','.join('?' * len(self._realized))}) AND paper=?",
                (*self._realized, 1 if self.paper else 0)).fetchone()
        return int(r[0] or 0), int(r[1] or 0)

    def realized_rows(self, limit: int = 5000) -> list[dict]:
        """Closed/settled trades only (the P&L history needs nothing else)."""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM proposals WHERE status IN ({','.join('?' * len(self._realized))}) AND paper=? "
                "ORDER BY settled_at DESC LIMIT ?", (*self._realized, 1 if self.paper else 0, int(limit))).fetchall()
        return [{k: r[k] for k in r.keys() if k != "payload"} for r in rows]

    def recent_results(self, limit: int) -> list[float]:
        """Realized P&L of the last `limit` closed or settled trades, newest first."""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT realized_pnl_usd FROM proposals WHERE status IN ({','.join('?' * len(self._realized))}) "
                "AND realized_pnl_usd IS NOT NULL AND filled_count > 0 AND paper=? ORDER BY settled_at DESC LIMIT ?",
                (*self._realized, 1 if self.paper else 0, int(limit))).fetchall()
        return [float(r[0]) for r in rows]

    def rows(self, limit: int = 500) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM proposals ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def recent_events(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ── Flags and events ─────────────────────────────────────────────────────

    def set_flag(self, key: str, value: str, by: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO flags (key, value, updated_at, updated_by) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                (key, value, _now(), by))
            self._conn.commit()

    def get_flag(self, key: str, default: str = "") -> str:
        with self._lock:
            r = self._conn.execute("SELECT value FROM flags WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

    # ── Live agent status (Agents page) ──────────────────────────────────────

    FEED_KEEP = 3000

    def set_agent_status(self, agent: str, state: str, activity: str, *, message: str | None = None,
                         decision: str | None = None, ticker: str | None = None,
                         detail: dict | None = None) -> None:
        """Upsert the agent's current state. A message also lands in the feed.
        Passing message=None keeps the previous last_message/last_decision."""
        now = _now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO agent_status (agent, state, activity, last_message, last_decision, ticker, "
                "detail, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(agent) DO UPDATE SET "
                "state=excluded.state, activity=excluded.activity, updated_at=excluded.updated_at, "
                "last_message=COALESCE(excluded.last_message, agent_status.last_message), "
                "last_decision=COALESCE(excluded.last_decision, agent_status.last_decision), "
                "ticker=COALESCE(excluded.ticker, agent_status.ticker), "
                "detail=COALESCE(excluded.detail, agent_status.detail)",
                (agent, state, activity[:500], message[:1000] if message else None, decision, ticker,
                 json.dumps(detail, default=str) if detail is not None else None, now))
            if message:
                cur = self._conn.execute(
                    "INSERT INTO agent_feed (ts, agent, decision, ticker, message) VALUES (?, ?, ?, ?, ?)",
                    (now, agent, decision, ticker, message[:1000]))
                if cur.lastrowid and cur.lastrowid % 500 == 0:
                    self._conn.execute("DELETE FROM agent_feed WHERE id <= ?",
                                       (cur.lastrowid - self.FEED_KEEP,))
            self._conn.commit()

    def agent_statuses(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM agent_status").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d["detail"]) if d.get("detail") else {}
            out.append(d)
        return out

    def agent_feed(self, limit: int = 200, agent: str | None = None) -> list[dict]:
        with self._lock:
            if agent:
                rows = self._conn.execute("SELECT * FROM agent_feed WHERE agent=? ORDER BY id DESC LIMIT ?",
                                          (agent, limit)).fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM agent_feed ORDER BY id DESC LIMIT ?",
                                          (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ── Live marks (written by MarkFeed every second, read by the P&L page) ──

    def upsert_marks(self, marks: list[dict]) -> None:
        if not marks:
            return
        now = _now()
        with self._lock:
            self._conn.executemany(
                "INSERT INTO marks (ticker, yes_bid, yes_ask, last_price, status, result, updated_at) "
                "VALUES (:ticker, :yes_bid, :yes_ask, :last_price, :status, :result, :updated_at) "
                "ON CONFLICT(ticker) DO UPDATE SET yes_bid=excluded.yes_bid, yes_ask=excluded.yes_ask, "
                "last_price=excluded.last_price, status=excluded.status, result=excluded.result, "
                "updated_at=excluded.updated_at",
                [dict(m, updated_at=now) for m in marks])
            self._conn.commit()

    def get_marks(self) -> dict[str, dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM marks").fetchall()
        return {r["ticker"]: dict(r) for r in rows}

    def log_event(self, kind: str, message: str, level: str = "INFO") -> None:
        with self._lock:
            self._conn.execute("INSERT INTO events (ts, level, kind, message) VALUES (?, ?, ?, ?)",
                               (_now(), level, kind, message[:2000]))
            self._conn.commit()

    # ── Learner observations ─────────────────────────────────────────────────

    def add_observation(self, *, ticker, series, close_ts, secs_left, spot, strike, strike_type,
                        sigma, bid, ask, y=None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO observations (ticker, series, close_ts, secs_left, spot, strike, strike_type,"
                " sigma, bid, ask, observed_at, y) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (ticker, series, int(close_ts), float(secs_left), float(spot), float(strike), strike_type,
                 float(sigma), bid, ask, _now(), y))
            self._conn.commit()

    def observed_tickers(self) -> set[str]:
        with self._lock:
            rows = self._conn.execute("SELECT DISTINCT ticker FROM observations").fetchall()
        return {r[0] for r in rows}

    def unlabeled_tickers(self, before_ts: int) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT ticker FROM observations WHERE y IS NULL AND close_ts < ? LIMIT 2000",
                (int(before_ts),)).fetchall()
        return [r[0] for r in rows]

    def label_observations(self, ticker: str, y: int) -> int:
        with self._lock:
            cur = self._conn.execute("UPDATE observations SET y=? WHERE ticker=? AND y IS NULL", (int(y), ticker))
            self._conn.commit()
            return cur.rowcount

    def labeled_observations(self, since_ts: int = 0) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM observations WHERE y IS NOT NULL AND close_ts >= ? ORDER BY close_ts",
                (int(since_ts),)).fetchall()
        return [dict(r) for r in rows]

    def observation_counts(self) -> dict:
        with self._lock:
            r = self._conn.execute(
                "SELECT COUNT(*), SUM(y IS NOT NULL), COUNT(DISTINCT ticker) FROM observations").fetchone()
        return {"total": r[0] or 0, "labelled": r[1] or 0, "markets": r[2] or 0}

    def prune_observations(self, before_ts: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM observations WHERE close_ts < ?", (int(before_ts),))
            self._conn.commit()
