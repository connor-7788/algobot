"""
core/risk_gatekeeper.py — The Firewall.

Every order from every strategy MUST pass through RiskGatekeeper.check()
before being submitted. Returns (approved: bool, reason: str).

Checks (in order):
  1. Kill-switch  — 2% max daily loss across entire account
  2. Latency      — data feed age > 500ms halts new orders
  3. Sentiment    — active news halt flag
  4. Capital      — per-asset-class allocation limits
  5. Position cap — max concurrent open positions
  6. VIX regime   — reduce size in high-volatility environments
  7. Stale-price  — last known price age check
"""

import logging
import threading
from datetime import datetime, timezone
from typing import Literal

import config

log = logging.getLogger(__name__)

AssetClass = Literal["equity", "kalshi"]


class RiskGatekeeper:
    """
    Singleton-friendly stateful firewall.
    Holds shared state across all strategy threads.
    """

    def __init__(self):
        self._lock            = threading.Lock()

        # Kill-switch state
        self.kill_switch_active = False
        self.starting_equity    = None
        self.current_equity     = None

        # Latency state (feed_name → last update datetime)
        self.feed_timestamps: dict[str, datetime] = {}

        # News/sentiment halt
        self.sentiment_halt    = False
        self.sentiment_reason  = ""

        # Open position counts
        self.open_equity_count = 0
        self.open_kalshi_count = 0

        # Capital allocated
        self.equity_allocated  = 0.0
        self.kalshi_allocated  = 0.0
        self.total_equity      = 0.0

        # VIX
        self.current_vix       = 20.0

        # ADX (per symbol)
        self.adx_cache: dict[str, float] = {}

        # Last seen price per symbol + timestamp
        self.last_price_ts: dict[str, datetime] = {}

        log.info("RiskGatekeeper initialised.")

    # ── Public API ────────────────────────────────────────────────────────────

    def check(
        self,
        asset_class: AssetClass,
        symbol: str,
        notional_usd: float,
        direction: str,          # 'long' | 'short' | 'yes' | 'no'
    ) -> tuple[bool, str]:
        """
        Master pre-flight check. Returns (True, 'ok') or (False, reason).
        """
        with self._lock:
            # 1. Kill-switch
            if self.kill_switch_active:
                return False, "KILL_SWITCH: daily loss limit hit"

            # 2. Latency
            latency_fail = self._latency_check(asset_class)
            if latency_fail:
                return False, latency_fail

            # 3. Sentiment halt
            if self.sentiment_halt:
                return False, f"SENTIMENT_HALT: {self.sentiment_reason}"

            # 4. Capital allocation
            cap_fail = self._capital_check(asset_class, notional_usd)
            if cap_fail:
                return False, cap_fail

            # 5. Position cap
            pos_fail = self._position_cap_check(asset_class)
            if pos_fail:
                return False, pos_fail

            # 6. Stale price
            stale_fail = self._stale_price_check(symbol)
            if stale_fail:
                return False, stale_fail

            return True, "ok"

    def update_equity(self, current: float):
        """Call after every account refresh."""
        with self._lock:
            self.current_equity = current
            if self.starting_equity is None:
                self.starting_equity = current
                log.info(f"Starting equity set: ${current:,.2f}")
                return

            loss_pct = (self.starting_equity - current) / self.starting_equity
            if loss_pct >= config.MAX_DAILY_LOSS_PCT:
                if not self.kill_switch_active:
                    self.kill_switch_active = True
                    log.critical(
                        f"⛔ KILL SWITCH ACTIVATED — "
                        f"daily loss {loss_pct*100:.2f}% ≥ "
                        f"{config.MAX_DAILY_LOSS_PCT*100:.0f}%"
                    )

    def reset_daily(self):
        """Call at market open each day to reset daily counters."""
        with self._lock:
            self.starting_equity    = self.current_equity
            self.kill_switch_active = False
            log.info("Daily risk counters reset.")

    def update_feed_timestamp(self, feed: str):
        """Mark a feed as alive right now."""
        self.feed_timestamps[feed] = datetime.now(timezone.utc)

    def set_sentiment_halt(self, active: bool, reason: str = ""):
        with self._lock:
            self.sentiment_halt   = active
            self.sentiment_reason = reason
            if active:
                log.warning(f"Sentiment halt ACTIVE: {reason}")
            else:
                log.info("Sentiment halt cleared.")

    def update_vix(self, vix: float):
        with self._lock:
            self.current_vix = vix

    def update_adx(self, symbol: str, adx: float):
        self.adx_cache[symbol] = adx

    def register_price(self, symbol: str):
        self.last_price_ts[symbol] = datetime.now(timezone.utc)

    def position_opened(self, asset_class: AssetClass, notional: float):
        with self._lock:
            if asset_class == "equity":
                self.open_equity_count += 1
                self.equity_allocated  += notional
            else:
                self.open_kalshi_count += 1
                self.kalshi_allocated  += notional

    def position_closed(self, asset_class: AssetClass, notional: float):
        with self._lock:
            if asset_class == "equity":
                self.open_equity_count  = max(0, self.open_equity_count - 1)
                self.equity_allocated   = max(0, self.equity_allocated  - notional)
            else:
                self.open_kalshi_count  = max(0, self.open_kalshi_count - 1)
                self.kalshi_allocated   = max(0, self.kalshi_allocated  - notional)

    def adjusted_size_multiplier(self) -> float:
        """
        Returns 0.5 if VIX is elevated (reduce position size by 50%),
        1.0 otherwise.
        """
        if self.current_vix >= config.VIX_HIGH_THRESHOLD:
            return 0.5
        return 1.0

    def market_regime(self, symbol: str) -> str:
        """
        Returns 'trending' if ADX > threshold, else 'ranging'.
        Strategies can use this to switch between momentum/mean-reversion.
        """
        adx = self.adx_cache.get(symbol, 0)
        return "trending" if adx >= config.ADX_TREND_MIN else "ranging"

    def status_dict(self) -> dict:
        """Snapshot for dashboard display."""
        return {
            "kill_switch":       self.kill_switch_active,
            "sentiment_halt":    self.sentiment_halt,
            "sentiment_reason":  self.sentiment_reason,
            "starting_equity":   self.starting_equity,
            "current_equity":    self.current_equity,
            "daily_pnl_pct":     (
                (self.current_equity - self.starting_equity) / self.starting_equity * 100
                if self.starting_equity and self.current_equity else 0
            ),
            "open_equity":       self.open_equity_count,
            "open_kalshi":       self.open_kalshi_count,
            "equity_allocated":  self.equity_allocated,
            "kalshi_allocated":  self.kalshi_allocated,
            "current_vix":       self.current_vix,
            "size_multiplier":   self.adjusted_size_multiplier(),
        }

    # ── Internal checks ───────────────────────────────────────────────────────

    def _latency_check(self, asset_class: AssetClass) -> str | None:
        feed = "alpaca_ws" if asset_class == "equity" else "kalshi_ws"
        ts   = self.feed_timestamps.get(feed)
        if ts is None:
            return f"LATENCY: no timestamp for feed '{feed}'"
        age_ms = (datetime.now(timezone.utc) - ts).total_seconds() * 1000
        if age_ms > config.DATA_LATENCY_LIMIT_MS:
            return f"LATENCY: {feed} feed is {age_ms:.0f}ms stale (limit {config.DATA_LATENCY_LIMIT_MS}ms)"
        return None

    def _capital_check(self, asset_class: AssetClass, notional: float) -> str | None:
        if not self.total_equity:
            return None  # can't check without equity info, pass through
        if asset_class == "equity":
            max_alloc = self.total_equity * config.CAPITAL_SPLIT_STOCK_PCT
            if self.equity_allocated + notional > max_alloc:
                return (f"CAPITAL: equity allocation ${self.equity_allocated+notional:,.0f} "
                        f"would exceed limit ${max_alloc:,.0f}")
        else:
            max_alloc = self.total_equity * config.CAPITAL_SPLIT_KALSHI_PCT
            if self.kalshi_allocated + notional > max_alloc:
                return (f"CAPITAL: kalshi allocation ${self.kalshi_allocated+notional:,.0f} "
                        f"would exceed limit ${max_alloc:,.0f}")
        return None

    def _position_cap_check(self, asset_class: AssetClass) -> str | None:
        if asset_class == "equity" and self.open_equity_count >= config.MAX_CONCURRENT_STOCKS:
            return f"POS_CAP: {self.open_equity_count} equity positions open (max {config.MAX_CONCURRENT_STOCKS})"
        if asset_class == "kalshi" and self.open_kalshi_count >= config.MAX_CONCURRENT_KALSHI:
            return f"POS_CAP: {self.open_kalshi_count} Kalshi positions open (max {config.MAX_CONCURRENT_KALSHI})"
        return None

    def _stale_price_check(self, symbol: str) -> str | None:
        ts = self.last_price_ts.get(symbol)
        if ts is None:
            return None  # new symbol, no prior price — let strategy decide
        age_ms = (datetime.now(timezone.utc) - ts).total_seconds() * 1000
        if age_ms > config.DATA_LATENCY_LIMIT_MS * 2:
            return f"STALE_PRICE: {symbol} last price is {age_ms:.0f}ms old"
        return None


# ── Capital Conflict Resolver ──────────────────────────────────────────────────

class CapitalConflictResolver:
    """
    Resolves simultaneous high-alpha stock breakout vs high-edge Kalshi arb.

    Priority logic:
      1. Kill-switch / sentiment halt → neither executes
      2. Edge score comparison:
           stock_score  = alpha_pct × ADX_normalised × (1 / VIX_normalised)
           kalshi_score = edge_pct  × liquidity_score × (1 − spread_pct)
      3. If scores within 10% of each other → execute BOTH at 60/40 split
      4. If stock_score > kalshi_score by >10% → full stock, defer Kalshi
      5. If kalshi_score > stock_score by >10% → full Kalshi, defer stock
      6. Both always subject to per-class capital limits
    """

    def __init__(self, gatekeeper: RiskGatekeeper):
        self.gk = gatekeeper

    def resolve(
        self,
        stock_alpha_pct:   float,  # expected return % from stock signal
        stock_symbol:      str,
        kalshi_edge_pct:   float,  # arbitrage edge % from Kalshi
        kalshi_market_id:  str,
        kalshi_spread_pct: float,
        available_capital: float,
    ) -> dict:
        """
        Returns allocation decision:
        {
          'stock_notional': float,
          'kalshi_notional': float,
          'decision': str,
          'reasoning': str
        }
        """
        if self.gk.kill_switch_active or self.gk.sentiment_halt:
            return {
                "stock_notional":  0, "kalshi_notional": 0,
                "decision": "BLOCKED",
                "reasoning": "Kill-switch or sentiment halt active"
            }

        adx     = self.gk.adx_cache.get(stock_symbol, 20)
        vix     = self.gk.current_vix
        vix_adj = max(vix / 20, 1.0)   # normalised: 1.0 = calm, 2.0 = VIX@40

        stock_score  = stock_alpha_pct  * (adx / 25) * (1 / vix_adj)
        kalshi_score = kalshi_edge_pct  * (1 - kalshi_spread_pct)

        stock_cap  = available_capital * config.CAPITAL_SPLIT_STOCK_PCT
        kalshi_cap = available_capital * config.CAPITAL_SPLIT_KALSHI_PCT

        diff = abs(stock_score - kalshi_score)
        within_band = diff / max(stock_score, kalshi_score, 0.0001) <= 0.10

        if within_band:
            return {
                "stock_notional":  stock_cap  * 0.60,
                "kalshi_notional": kalshi_cap * 0.60,
                "decision": "BOTH",
                "reasoning": (
                    f"Scores within 10% band "
                    f"(stock={stock_score:.3f} kalshi={kalshi_score:.3f}). "
                    f"Running both at 60% size."
                )
            }
        elif stock_score > kalshi_score:
            return {
                "stock_notional":  stock_cap,
                "kalshi_notional": 0,
                "decision": "STOCK_PRIORITY",
                "reasoning": (
                    f"Stock score {stock_score:.3f} > Kalshi {kalshi_score:.3f} "
                    f"(ADX={adx:.1f}, VIX={vix:.1f}). Deferring Kalshi."
                )
            }
        else:
            return {
                "stock_notional":  0,
                "kalshi_notional": kalshi_cap,
                "decision": "KALSHI_PRIORITY",
                "reasoning": (
                    f"Kalshi edge {kalshi_score:.3f} > Stock {stock_score:.3f}. "
                    f"Deferring stock."
                )
            }
