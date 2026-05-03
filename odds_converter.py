"""
core/odds_converter.py — Probability & Arbitrage Math Engine.

Converts American / Decimal / Fractional odds into Kalshi-style
implied probabilities (range $0.01 – $0.99).

Also computes:
  • No-vig fair probability (removes bookmaker margin)
  • Arbitrage edge vs Kalshi market price
  • Kelly Criterion position sizing
  • In-play velocity tracker (detects game-changing events)
"""

import logging
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

log = logging.getLogger(__name__)


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class OddsSnapshot:
    market_id:     str
    event_desc:    str
    sport:         str
    book_name:     str
    outcome:       str       # e.g. "Kansas City Chiefs"
    american_odds: int | None = None
    decimal_odds:  float | None = None
    fractional:    str | None = None  # e.g. "5/2"
    timestamp:     datetime = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now(timezone.utc)


@dataclass
class ArbOpportunity:
    market_id:      str
    event_desc:     str
    sport:          str
    kalshi_side:    Literal["yes", "no"]
    kalshi_price:   float    # 0.01 – 0.99
    book_implied:   float    # no-vig implied prob from traditional book
    edge_pct:       float    # kalshi_price - book_implied  (positive = buy YES cheap)
    kelly_fraction: float    # recommended fraction of bankroll
    max_contracts:  int
    is_tradeable:   bool
    reasoning:      str


# ── Odds conversion ───────────────────────────────────────────────────────────

class OddsConverter:
    """
    All probability math. Stateless — methods are pure functions.
    """

    @staticmethod
    def american_to_decimal(american: int) -> float:
        """Convert American moneyline to decimal odds."""
        if american >= 100:
            return american / 100 + 1
        else:
            return 100 / abs(american) + 1

    @staticmethod
    def decimal_to_implied_prob(decimal: float) -> float:
        """Raw (vig-included) implied probability from decimal odds."""
        if decimal <= 1.0:
            raise ValueError(f"Decimal odds must be > 1.0, got {decimal}")
        return 1.0 / decimal

    @staticmethod
    def fractional_to_decimal(fractional: str) -> float:
        """e.g. '5/2' → 3.5"""
        num, den = map(float, fractional.split("/"))
        return num / den + 1

    @staticmethod
    def remove_vig(prob_a: float, prob_b: float) -> tuple[float, float]:
        """
        Given two raw implied probabilities that sum > 1.0 (the overround),
        return the no-vig fair probabilities that sum to exactly 1.0.

        Example:
          Home -150 (prob=0.600), Away +120 (prob=0.455) → overround = 1.055
          Fair: home = 0.600/1.055 = 0.569, away = 0.431
        """
        total = prob_a + prob_b
        return prob_a / total, prob_b / total

    @staticmethod
    def american_to_kalshi(american: int) -> float:
        """
        Convert American odds to Kalshi YES price (0.01–0.99).
        Applies no-vig normalisation against the implied complement.
        """
        decimal  = OddsConverter.american_to_decimal(american)
        raw_prob = OddsConverter.decimal_to_implied_prob(decimal)
        # Clamp to valid Kalshi range
        return max(0.01, min(0.99, round(raw_prob, 4)))

    @staticmethod
    def no_vig_prob_from_american(american_a: int, american_b: int) -> tuple[float, float]:
        """
        Given both sides of a market (e.g. home/away), return no-vig probs.
        """
        dec_a  = OddsConverter.american_to_decimal(american_a)
        dec_b  = OddsConverter.american_to_decimal(american_b)
        prob_a = OddsConverter.decimal_to_implied_prob(dec_a)
        prob_b = OddsConverter.decimal_to_implied_prob(dec_b)
        return OddsConverter.remove_vig(prob_a, prob_b)

    @staticmethod
    def kelly_criterion(edge: float, win_prob: float, odds_decimal: float) -> float:
        """
        Full Kelly fraction.
          f* = (b·p − q) / b
          b  = decimal_odds − 1  (net profit per $1 risked)
          p  = win probability
          q  = 1 − p
        Returns a fraction of bankroll (0.0–1.0). Cap at 0.25 for safety.
        """
        b = odds_decimal - 1
        if b <= 0:
            return 0.0
        q = 1 - win_prob
        kelly = (b * win_prob - q) / b
        return max(0.0, min(0.25, kelly))   # half-Kelly cap at 0.25


# ── Arbitrage finder ─────────────────────────────────────────────────────────

class ArbFinder:
    """
    Compares Kalshi market prices against theOddsAPI prices to find edges.
    """

    def __init__(self, min_edge: float = 0.05, max_spread: float = 0.08):
        self.min_edge   = min_edge
        self.max_spread = max_spread
        self.converter  = OddsConverter()

    def evaluate(
        self,
        market_id:       str,
        event_desc:      str,
        sport:           str,
        kalshi_yes_bid:  float,   # best bid on YES
        kalshi_yes_ask:  float,   # best ask on YES
        book_american_a: int,     # traditional book odds for outcome A
        book_american_b: int,     # traditional book odds for outcome B (complement)
        max_contracts:   int = 100,
        bankroll:        float = 10_000,
    ) -> ArbOpportunity | None:
        """
        Returns ArbOpportunity if an edge exists, else None.
        """
        try:
            spread = kalshi_yes_ask - kalshi_yes_bid
            if spread > self.max_spread:
                log.debug(f"{market_id}: spread {spread:.3f} > max {self.max_spread}")
                return None

            fair_a, fair_b = self.converter.no_vig_prob_from_american(
                book_american_a, book_american_b
            )

            # Mid-point of Kalshi market
            kalshi_mid = (kalshi_yes_bid + kalshi_yes_ask) / 2

            # Edge: how much cheaper is Kalshi YES vs the no-vig fair probability?
            # Positive edge = Kalshi YES is underpriced → buy YES
            # Negative edge → potentially buy NO instead
            yes_edge = fair_a - kalshi_mid
            no_edge  = fair_b - (1 - kalshi_mid)

            best_side = "yes" if yes_edge >= no_edge else "no"
            best_edge = yes_edge if best_side == "yes" else no_edge

            if best_edge >= self.min_edge:
                side       = best_side
                edge       = best_edge
                entry_price = kalshi_yes_ask if side == "yes" else (1 - kalshi_yes_bid)
                book_implied = fair_a if side == "yes" else fair_b
                dec_odds   = 1 / entry_price if entry_price > 0 else 1
                kelly      = self.converter.kelly_criterion(edge, book_implied, dec_odds)
                contracts  = min(max_contracts, int(kelly * bankroll / 100))

                return ArbOpportunity(
                    market_id      = market_id,
                    event_desc     = event_desc,
                    sport          = sport,
                    kalshi_side    = side,
                    kalshi_price   = round(entry_price, 4),
                    book_implied   = round(book_implied, 4),
                    edge_pct       = round(abs(edge), 4),
                    kelly_fraction = round(kelly, 4),
                    max_contracts  = contracts,
                    is_tradeable   = contracts > 0,
                    reasoning      = (
                        f"Book no-vig: {book_implied:.3f} | "
                        f"Kalshi mid: {kalshi_mid:.3f} | "
                        f"Edge: {edge*100:.2f}% | "
                        f"Kelly: {kelly*100:.1f}% | "
                        f"Spread: {spread:.3f}"
                    ),
                )
        except Exception as e:
            log.error(f"ArbFinder.evaluate failed for {market_id}: {e}")
        return None


# ── In-Play Velocity Tracker ──────────────────────────────────────────────────

class VelocityTracker:
    """
    Detects rapid Kalshi price movements that indicate a game-changing
    event (touchdown, injury, goal) before the traditional books adjust.

    Uses a sliding window of price ticks.
    """

    def __init__(
        self,
        window_seconds:   int   = 60,
        threshold_pct:    float = 0.03,   # 3% move = major event
        min_ticks:        int   = 3,
    ):
        self.window_s    = window_seconds
        self.threshold   = threshold_pct
        self.min_ticks   = min_ticks
        # market_id → deque of (timestamp, price)
        self._history: dict[str, deque] = {}

    def update(self, market_id: str, price: float) -> dict:
        """
        Record new price tick. Returns velocity analysis:
        {
          'velocity': float,        # price change over window (fraction)
          'direction': 'up'|'down'|'flat',
          'alert': bool,            # True if exceeds threshold
          'ticks': int
        }
        """
        now = time.monotonic()
        if market_id not in self._history:
            self._history[market_id] = deque()

        dq = self._history[market_id]
        dq.append((now, price))

        # Purge old ticks outside window
        cutoff = now - self.window_s
        while dq and dq[0][0] < cutoff:
            dq.popleft()

        if len(dq) < self.min_ticks:
            return {"velocity": 0.0, "direction": "flat", "alert": False, "ticks": len(dq)}

        oldest_price = dq[0][1]
        if oldest_price == 0:
            return {"velocity": 0.0, "direction": "flat", "alert": False, "ticks": len(dq)}

        velocity = (price - oldest_price) / oldest_price
        direction = "up" if velocity > 0.001 else ("down" if velocity < -0.001 else "flat")
        alert = abs(velocity) >= self.threshold

        if alert:
            log.warning(
                f"⚡ VELOCITY ALERT {market_id}: "
                f"{velocity*100:+.2f}% in {self.window_s}s — possible in-play event"
            )

        return {
            "velocity":  round(velocity, 5),
            "direction": direction,
            "alert":     alert,
            "ticks":     len(dq),
        }

    def get_history(self, market_id: str) -> list[tuple[float, float]]:
        """Return list of (timestamp, price) for charting."""
        return list(self._history.get(market_id, []))
