"""
core/sentiment_filter.py — News & Sentiment Intelligence Layer.

Sources:
  • Benzinga Pro REST API  — financial news, earnings, FDA events, FOMC
  • The Odds API headlines — injury reports, game status changes
  • Manual override        — operator can inject halt/resume via CLI

Flow:
  1. NewsPoller runs in background thread every 60s
  2. Each headline is scored against HALT_KEYWORDS
  3. If match found → RiskGatekeeper.set_sentiment_halt(True)
  4. Halt auto-expires after HALT_DURATION_MINS unless renewed
"""

import logging
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Callable

import config

log = logging.getLogger(__name__)


class SentimentFilter:
    """
    Polls Benzinga and scores headlines. Notifies gatekeeper of halts.
    """

    def __init__(
        self,
        halt_callback:   Callable[[bool, str], None],   # gatekeeper.set_sentiment_halt
        halt_duration_mins: int = 15,
    ):
        self.halt_callback    = halt_callback
        self.halt_duration    = timedelta(minutes=halt_duration_mins)
        self._halt_expires_at: datetime | None = None
        self._thread          = None
        self._running         = False
        self._recent_events   = []   # for dashboard display
        self._lock            = threading.Lock()

    # ── Public ────────────────────────────────────────────────────────────────

    def start(self):
        self._running = True
        self._thread  = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        log.info("SentimentFilter started.")

    def stop(self):
        self._running = False

    def manual_halt(self, reason: str = "Manual operator halt"):
        """Operator-triggered halt from CLI or dashboard."""
        self._activate_halt(reason)
        log.warning(f"Manual halt triggered: {reason}")

    def manual_resume(self):
        """Operator-triggered resume."""
        with self._lock:
            self._halt_expires_at = None
        self.halt_callback(False, "")
        log.info("Manual resume — sentiment halt cleared.")

    def recent_events(self, n: int = 10) -> list[dict]:
        """Return last N events for dashboard log."""
        return self._recent_events[-n:]

    # ── Internal ──────────────────────────────────────────────────────────────

    def _poll_loop(self):
        while self._running:
            # Check if current halt has expired
            with self._lock:
                if self._halt_expires_at and datetime.now(timezone.utc) > self._halt_expires_at:
                    self._halt_expires_at = None
                    self.halt_callback(False, "")
                    log.info("Sentiment halt expired — trading resumed.")

            # Poll Benzinga
            try:
                headlines = self._fetch_benzinga()
                for h in headlines:
                    self._evaluate_headline(h, source="benzinga")
            except Exception as e:
                log.debug(f"Benzinga poll error: {e}")

            time.sleep(60)

    def _fetch_benzinga(self) -> list[dict]:
        """
        Fetch recent headlines from Benzinga API.
        Returns list of {headline, symbols, published_at}.
        """
        if not config.BENZINGA_API_KEY:
            return []

        import requests
        params = {
            "token":       config.BENZINGA_API_KEY,
            "pageSize":    20,
            "displayOutput": "headline",
        }
        try:
            r = requests.get(
                "https://api.benzinga.com/api/v2/news",
                params=params, timeout=5
            )
            r.raise_for_status()
            items = r.json() if isinstance(r.json(), list) else r.json().get("stories", [])
            return [
                {
                    "headline":     item.get("title", ""),
                    "symbols":      [s.get("name") for s in item.get("stocks", [])],
                    "published_at": item.get("created", ""),
                }
                for item in items
            ]
        except Exception as e:
            log.debug(f"Benzinga request failed: {e}")
            return []

    def _evaluate_headline(self, item: dict, source: str):
        headline = item.get("headline", "").upper()
        symbols  = item.get("symbols", [])

        for kw in config.SENTIMENT_HALT_KEYWORDS:
            if kw.upper() in headline:
                reason = f"[{source}] Keyword '{kw}' detected: {item['headline'][:80]}"
                self._activate_halt(reason)
                self._log_event(item["headline"], symbols, "halt", source, triggered=True)
                return

        # Positive / negative classification (lightweight, no ML required)
        positive_words = ["BEAT", "EXCEEDS", "RAISES GUIDANCE", "BUYOUT", "UPGRADE"]
        negative_words = ["MISS", "CUTS GUIDANCE", "DOWNGRADE", "BANKRUPTCY", "HALT"]
        headline_upper = headline

        sentiment = "neutral"
        for w in positive_words:
            if w in headline_upper:
                sentiment = "bullish"
                break
        for w in negative_words:
            if w in headline_upper:
                sentiment = "bearish"
                break

        self._log_event(item["headline"], symbols, sentiment, source, triggered=False)

    def _activate_halt(self, reason: str):
        with self._lock:
            self._halt_expires_at = datetime.now(timezone.utc) + self.halt_duration
        self.halt_callback(True, reason)

    def _log_event(self, headline, symbols, sentiment, source, triggered):
        event = {
            "ts":        datetime.now(timezone.utc).isoformat(),
            "source":    source,
            "headline":  headline[:120],
            "symbols":   symbols,
            "sentiment": sentiment,
            "halted":    triggered,
        }
        self._recent_events.append(event)
        if len(self._recent_events) > 100:
            self._recent_events.pop(0)

        if triggered:
            log.warning(f"HALT triggered: {headline[:80]}")
        else:
            log.debug(f"News [{sentiment}]: {headline[:60]}")
