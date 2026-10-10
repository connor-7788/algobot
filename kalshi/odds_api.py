"""
Sportsbook consensus from the-odds-api.com (v4) and the devig math.

Each bookmaker's two-way moneyline is converted to implied probabilities and
normalized so they sum to 1 (multiplicative devig). Every book becomes its own
Source, so Skeptic can count independent sources and check their ages.

Quota: every /odds call spends credits (markets x regions). Results are cached
per sport for settings.odds_cache_s, and the remaining-credit header is logged.
"""

from __future__ import annotations

import logging
import re
import statistics
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests

from kalshi.client import parse_ts
from kalshi.proposal import Source

log = logging.getLogger("kalshi.odds")


def american_to_prob(odds: float) -> float:
    odds = float(odds)
    if odds == 0:
        raise ValueError("american odds cannot be 0")
    if odds > 0:
        return 100.0 / (odds + 100.0)
    return -odds / (-odds + 100.0)


def devig_multiplicative(implied: list[float]) -> list[float]:
    total = sum(implied)
    if total <= 0:
        raise ValueError("implied probabilities must be positive")
    return [p / total for p in implied]


@dataclass
class BookLine:
    book: str
    probs: dict[str, float]          # team name -> devigged probability
    last_update: datetime


@dataclass
class OddsEvent:
    event_id: str
    sport_key: str
    home_team: str
    away_team: str
    commence_time: datetime | None
    lines: list[BookLine] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def sources_for(self, team: str) -> list[Source]:
        out = []
        for ln in self.lines:
            if team in ln.probs:
                out.append(Source(name=ln.book, probability=ln.probs[team],
                                  fetched_at=self.fetched_at, observed_at=ln.last_update,
                                  detail={"event_id": self.event_id, "team": team}))
        return out


def parse_odds_events(payload: list, sport_key: str, fetched_at: datetime | None = None) -> list[OddsEvent]:
    fetched_at = fetched_at or datetime.now(timezone.utc)
    events = []
    for ev in payload or []:
        home, away = ev.get("home_team", ""), ev.get("away_team", "")
        oe = OddsEvent(ev.get("id", ""), sport_key, home, away,
                       parse_ts(ev.get("commence_time")), fetched_at=fetched_at)
        for bk in ev.get("bookmakers", []):
            for mkt in bk.get("markets", []):
                if mkt.get("key") != "h2h":
                    continue
                outcomes = {o.get("name"): o.get("price") for o in mkt.get("outcomes", [])}
                # Two-way only. A third outcome (draw) means this isn't the market we model.
                if set(outcomes) != {home, away}:
                    continue
                try:
                    implied = [american_to_prob(outcomes[home]), american_to_prob(outcomes[away])]
                    fair = devig_multiplicative(implied)
                except (TypeError, ValueError):
                    continue
                updated = parse_ts(mkt.get("last_update") or bk.get("last_update")) or fetched_at
                oe.lines.append(BookLine(bk.get("key", ""), {home: fair[0], away: fair[1]}, updated))
        if oe.lines:
            events.append(oe)
    return events


class OddsAPIClient:
    def __init__(self, settings, session: requests.Session | None = None, clock=time.time):
        self.settings = settings
        self.session = session or requests.Session()
        self.clock = clock
        self._cache: dict[str, tuple[float, list[OddsEvent]]] = {}
        self._lock = threading.Lock()

    def events(self, sport_key: str) -> list[OddsEvent]:
        if not self.settings.odds_api_key:
            return []
        now = self.clock()
        with self._lock:
            hit = self._cache.get(sport_key)
            if hit and now - hit[0] < self.settings.odds_cache_s:
                return hit[1]
        resp = self.session.get(
            f"{self.settings.odds_api_base}/sports/{sport_key}/odds",
            params={
                "apiKey": self.settings.odds_api_key,
                "markets": "h2h",
                "oddsFormat": "american",
                "bookmakers": self.settings.odds_bookmakers,
            },
            timeout=self.settings.http_timeout_s,
        )
        resp.raise_for_status()
        remaining = resp.headers.get("x-requests-remaining")
        if remaining is not None:
            log.info("Odds API %s: %s credits remaining", sport_key, remaining)
        events = parse_odds_events(resp.json(), sport_key)
        with self._lock:
            self._cache[sport_key] = (now, events)
        return events


# ── Matching a Kalshi game market to a sportsbook event ─────────────────────

def _tokens(s: str) -> list[str]:
    return re.sub(r"[^a-z0-9 ]", " ", (s or "").lower()).split()


def team_match_score(kalshi_name: str, book_team: str) -> float:
    """1.0 when every Kalshi token appears in the book's team name in order at
    the start (e.g. "Dallas" ~ "Dallas Cowboys", "New York G" ~ "New York Giants").
    0 when they don't line up."""
    k, b = _tokens(kalshi_name), _tokens(book_team)
    if not k or not b or len(k) > len(b):
        return 0.0
    for i, tok in enumerate(k):
        if b[i] == tok:
            continue
        if i == len(k) - 1 and b[i].startswith(tok):   # trailing abbreviation like "G"
            continue
        # Kalshi sometimes uses the nickname only ("Cowboys").
        return 1.0 if " ".join(k) == " ".join(b[-len(k):]) else 0.0
    return 1.0


def match_market(yes_name: str, title: str, start: datetime | None,
                 events: list[OddsEvent], max_start_diff_h: float) -> tuple[OddsEvent, str] | None:
    """Return (event, team that YES pays on), or None when there's no single
    unambiguous match. Ambiguity means no trade."""
    hits = []
    title_toks = set(_tokens(title))
    for ev in events:
        if start and ev.commence_time:
            if abs((ev.commence_time - start).total_seconds()) > max_start_diff_h * 3600:
                continue
        for team, other in ((ev.home_team, ev.away_team), (ev.away_team, ev.home_team)):
            if team_match_score(yes_name, team) < 1.0:
                continue
            # Confirm the opponent shows up in the Kalshi title (any token of its name).
            if title_toks and not (set(_tokens(other)) & title_toks):
                continue
            hits.append((ev, team))
    if len(hits) != 1:
        if len(hits) > 1:
            log.info("ambiguous match for %r: %d events", yes_name, len(hits))
        return None
    return hits[0]


def consensus(sources: list[Source]) -> tuple[float, float]:
    """(mean probability, stdev across books). stdev is 0 for a single book."""
    probs = [s.probability for s in sources]
    if not probs:
        raise ValueError("no sources")
    mean = statistics.fmean(probs)
    sd = statistics.pstdev(probs) if len(probs) > 1 else 0.0
    return mean, sd
