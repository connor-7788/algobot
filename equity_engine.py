"""
strategies/equity_engine.py — Wall Street Leg of the Cross-Asset Engine.

Strategy: EMA 8/21 crossover + RSI filter + Volume confirmation
          + 5-min higher-timeframe trend alignment + ADX regime check.

Sizing: ATR-based (position size = (account_equity × risk_pct) / ATR_stop_distance)
        Scaled down 50% when VIX ≥ 30.

Exits:  Hard TP, ATR trailing stop, early stop (first 5 min),
        max hold time, trend-reversal exit, forced pre-close exit.
"""

import logging
import threading
import time
from datetime import datetime, timezone
from collections import deque

import pandas as pd
import numpy as np

import config

try:
    from core.risk_gatekeeper import RiskGatekeeper
except ImportError:
    from risk_gatekeeper import RiskGatekeeper

log = logging.getLogger(__name__)


# ── Indicator Library ─────────────────────────────────────────────────────────

def ema(series: pd.Series, period: int) -> float | None:
    if len(series) < period:
        return None
    return float(series.ewm(span=period, adjust=False).mean().iloc[-1])

def rsi(series: pd.Series, period: int = 14) -> float | None:
    if len(series) < period + 1:
        return None
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, 1e-10)
    return float((100 - 100 / (1 + rs)).iloc[-1])

def atr(df: pd.DataFrame, period: int = 14) -> float | None:
    """Average True Range from OHLCV DataFrame."""
    if len(df) < period + 1 or "high" not in df or "low" not in df:
        return None
    high  = df["high"]
    low   = df["low"]
    close = df["close"]
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs(),
    ], axis=1).max(axis=1)
    return float(tr.rolling(period).mean().iloc[-1])

def adx(df: pd.DataFrame, period: int = 14) -> float | None:
    """Average Directional Index."""
    if len(df) < period * 2 or "high" not in df:
        return None
    try:
        high  = df["high"]
        low   = df["low"]
        close = df["close"]
        plus_dm  = high.diff().clip(lower=0)
        minus_dm = (-low.diff()).clip(lower=0)
        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low  - close.shift()).abs(),
        ], axis=1).max(axis=1)
        atr_s   = tr.ewm(span=period).mean()
        plus_di = 100 * plus_dm.ewm(span=period).mean()  / atr_s.replace(0, 1e-10)
        minus_di= 100 * minus_dm.ewm(span=period).mean() / atr_s.replace(0, 1e-10)
        dx      = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1e-10)
        return float(dx.ewm(span=period).mean().iloc[-1])
    except:
        return None

def volume_ratio(volumes: pd.Series, lookback: int = 20) -> float | None:
    if len(volumes) < lookback + 1:
        return None
    avg = volumes.iloc[-lookback - 1:-1].mean()
    return float(volumes.iloc[-1] / avg) if avg > 0 else None


# ── Position State ────────────────────────────────────────────────────────────

class EquityPosition:
    def __init__(self, symbol, side, qty, entry_price, atr_val, ema_f, ema_s,
                 rsi_v, adx_v, vix_v, vol_r, htf_trend):
        self.symbol      = symbol
        self.side        = side          # 'long' | 'short'
        self.qty         = qty
        self.entry_price = entry_price
        self.atr_val     = atr_val
        self.peak_price  = entry_price   # best price achieved
        self.entry_time  = datetime.now()
        self.trail_active = False

        # Indicator snapshot at entry (for DB log)
        self.entry_ema_fast = ema_f
        self.entry_ema_slow = ema_s
        self.entry_rsi      = rsi_v
        self.entry_adx      = adx_v
        self.entry_vix      = vix_v
        self.entry_vol_ratio= vol_r
        self.htf_trend      = htf_trend

    def update_peak(self, price: float):
        if self.side == "long":
            self.peak_price = max(self.peak_price, price)
        else:
            self.peak_price = min(self.peak_price, price)

    def pct_gain(self, price: float) -> float:
        """Positive = in profit regardless of side."""
        if self.side == "long":
            return (price - self.entry_price) / self.entry_price
        else:
            return (self.entry_price - price) / self.entry_price

    def peak_gain(self) -> float:
        return self.pct_gain(self.peak_price)

    def mins_held(self) -> float:
        return (datetime.now() - self.entry_time).total_seconds() / 60

    def atr_stop_price(self) -> float:
        """ATR-based initial stop level."""
        dist = self.atr_val * config.ATR_MULTIPLIER
        if self.side == "long":
            return self.entry_price - dist
        else:
            return self.entry_price + dist


# ── Main Engine ───────────────────────────────────────────────────────────────

class EquityEngine:
    """
    Runs in its own thread. Polls Alpaca, computes signals, manages positions.
    """

    def __init__(self, api, gatekeeper: RiskGatekeeper, paper: bool = True):
        self.api         = api
        self.gk          = gatekeeper
        self.paper       = paper
        self._running    = False
        self._thread     = None
        self._positions: dict[str, EquityPosition] = {}   # local state
        self.trade_log   = deque(maxlen=100)
        self.signal_cache: dict[str, dict] = {}
        self._lock       = threading.Lock()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self):
        self._running = True
        self._thread  = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        log.info("EquityEngine started.")

    def stop(self):
        self._running = False

    def _run_loop(self):
        while self._running:
            try:
                self.gk.update_feed_timestamp("alpaca_ws")
                self._sync_positions()
                self._refresh_equity()
                self._scan()
            except Exception as e:
                log.error(f"EquityEngine error: {e}", exc_info=True)
            time.sleep(config.POLL_SECONDS)

    # ── Synchronise with Alpaca ────────────────────────────────────────────────

    def _sync_positions(self):
        """Pull live positions from Alpaca and reconcile local state."""
        try:
            live = {p.symbol: p for p in self.api.list_positions()}
            # Remove positions we think we have but Alpaca doesn't
            for sym in list(self._positions):
                if sym not in live:
                    log.info(f"Position {sym} no longer in Alpaca — removing.")
                    self._positions.pop(sym, None)
        except Exception as e:
            log.warning(f"Position sync failed: {e}")

    def _refresh_equity(self):
        try:
            acct = self.api.get_account()
            eq   = float(acct.equity)
            self.gk.update_equity(eq)
            self.gk.total_equity = eq
        except:
            pass

    # ── Data ──────────────────────────────────────────────────────────────────

    def _get_bars(self, symbol: str, tf: str, limit: int) -> pd.DataFrame:
        try:
            df = self.api.get_bars(symbol, tf, limit=limit, feed="iex").df
            return df if not df.empty else pd.DataFrame()
        except Exception as e:
            log.debug(f"Bars {symbol}/{tf}: {e}")
            return pd.DataFrame()

    def _htf_trend(self, symbol: str) -> str:
        df = self._get_bars(symbol, "5Min", 30)
        if df.empty or len(df) < config.EMA_SLOW:
            return "FLAT"
        ef = ema(df["close"], config.EMA_FAST)
        es = ema(df["close"], config.EMA_SLOW)
        if ef is None or es is None:
            return "FLAT"
        sep = abs(ef - es) / es
        if sep < 0.0005:
            return "FLAT"
        return "BULL" if ef > es else "BEAR"

    def _market_minutes_to_close(self) -> float:
        try:
            clock = self.api.get_clock()
            if not clock.is_open:
                return -1
            now   = datetime.now(timezone.utc)
            close = clock.next_close.replace(tzinfo=timezone.utc)
            return (close - now).total_seconds() / 60
        except:
            return 999

    def _market_is_open(self) -> bool:
        try:
            return self.api.get_clock().is_open
        except:
            return False

    # ── Position sizing ───────────────────────────────────────────────────────

    def _calc_qty(self, price: float, atr_val: float, equity: float) -> int:
        """
        ATR-based position sizing.
        risk_dollars = equity × 1%  (risk 1% of account per trade)
        stop_distance = ATR × ATR_MULTIPLIER
        qty = risk_dollars / stop_distance
        Then scale by VIX multiplier.
        """
        risk_dollars   = equity * 0.01
        stop_distance  = atr_val * config.ATR_MULTIPLIER
        if stop_distance <= 0 or price <= 0:
            return 0
        qty = int(risk_dollars / stop_distance)
        qty = int(qty * self.gk.adjusted_size_multiplier())
        # Also respect per-position notional cap
        max_qty = int((equity * config.MAX_STOCK_POSITION_PCT) / price)
        return min(qty, max_qty)

    # ── Core scan ─────────────────────────────────────────────────────────────

    def _scan(self):
        if not self._market_is_open():
            return

        mtc         = self._market_minutes_to_close()
        force_close = 0 < mtc < config.CLOSE_EARLY_MINS

        for symbol in config.STOCK_SYMBOLS:
            try:
                self._process_symbol(symbol, force_close)
            except Exception as e:
                log.error(f"Error processing {symbol}: {e}")

    def _process_symbol(self, symbol: str, force_close: bool):
        df = self._get_bars(symbol, "1Min", 90)
        if df.empty or len(df) < config.EMA_SLOW + 5:
            return

        closes  = df["close"]
        price   = float(closes.iloc[-1])
        atr_val = atr(df) or 0
        adx_val = adx(df) or 0
        vix_val = self.gk.current_vix

        # Update gatekeeper caches
        self.gk.register_price(symbol)
        self.gk.update_adx(symbol, adx_val)

        ef      = ema(closes, config.EMA_FAST)
        es      = ema(closes, config.EMA_SLOW)
        rsi_v   = rsi(closes)
        prev_ef = ema(closes.iloc[:-1], config.EMA_FAST)
        prev_es = ema(closes.iloc[:-1], config.EMA_SLOW)
        vol_r   = volume_ratio(df["volume"]) if "volume" in df else None

        # Cache signal for dashboard
        self.signal_cache[symbol] = {
            "price":    round(price, 2),
            "ema_fast": round(ef, 2)   if ef    else None,
            "ema_slow": round(es, 2)   if es    else None,
            "rsi":      round(rsi_v,1) if rsi_v else None,
            "atr":      round(atr_val,4),
            "adx":      round(adx_val,1),
            "vol_r":    round(vol_r,2) if vol_r else None,
            "trend":    "BULL" if (ef and es and ef > es) else "BEAR",
            "regime":   self.gk.market_regime(symbol),
        }

        # ── EXIT ─────────────────────────────────────────────────────────────
        pos = self._positions.get(symbol)
        if pos:
            pos.update_peak(price)
            exit_reason = self._check_exit(pos, price, ef, es, rsi_v, force_close)
            if exit_reason:
                self._close_position(pos, price, exit_reason)
            return

        # ── ENTRY ─────────────────────────────────────────────────────────────
        if force_close or not ef or not es or not prev_ef or not prev_es or not rsi_v:
            return

        ema_sep    = abs(ef - es) / es
        volume_ok  = vol_r is None or vol_r >= config.VOLUME_MULT_MIN
        htf        = self._htf_trend(symbol)

        long_cross  = prev_ef <= prev_es and ef > es
        long_ok     = long_cross and ema_sep >= config.EMA_SEP_MIN_PCT \
                      and config.RSI_LONG_MIN <= rsi_v <= config.RSI_LONG_MAX \
                      and volume_ok and htf == "BULL"

        short_cross = prev_ef >= prev_es and ef < es
        short_ok    = short_cross and ema_sep >= config.EMA_SEP_MIN_PCT \
                      and config.RSI_SHORT_MIN <= rsi_v <= config.RSI_SHORT_MAX \
                      and volume_ok and htf == "BEAR"

        if not long_ok and not short_ok:
            if long_cross or short_cross:
                log.debug(f"Signal filtered {symbol}: "
                          f"sep={ema_sep:.4f} vol={vol_r} htf={htf} rsi={rsi_v:.1f}")
            return

        side      = "long" if long_ok else "short"
        equity    = self.gk.current_equity or 100_000
        qty       = self._calc_qty(price, atr_val, equity)
        notional  = qty * price

        if qty <= 0:
            return

        ok, reason = self.gk.check("equity", symbol, notional, side)
        if not ok:
            log.info(f"Gatekeeper blocked {symbol} {side}: {reason}")
            return

        self._open_position(symbol, side, qty, price, atr_val,
                            ef, es, rsi_v, adx_val, vix_val, vol_r or 0, htf)

    def _check_exit(self, pos: EquityPosition, price: float,
                    ef, es, rsi_v, force_close: bool) -> str | None:
        pct = pos.pct_gain(price)

        if force_close:
            return "CLOSE"
        if pct >= config.TAKE_PROFIT_PCT:
            return "TP"

        # Trailing stop
        peak_gain = pos.peak_gain()
        if peak_gain >= config.TRAILING_TRIGGER:
            pos.trail_active = True
            if pos.side == "long":
                trail_stop = pos.peak_price * (1 - config.TRAILING_STOP_PCT)
                if price <= trail_stop:
                    return "TRAIL"
            else:
                trail_stop = pos.peak_price * (1 + config.TRAILING_STOP_PCT)
                if price >= trail_stop:
                    return "TRAIL"

        # ATR stop
        atr_stop = pos.atr_stop_price()
        if pos.side == "long" and price <= atr_stop:
            return "ATR_SL"
        if pos.side == "short" and price >= atr_stop:
            return "ATR_SL"

        # Early tight stop
        if pos.mins_held() <= config.EARLY_STOP_MINS and pct <= -config.EARLY_STOP_PCT:
            return "EARLY_SL"

        # Max hold
        if pos.mins_held() >= config.MAX_HOLD_MINS:
            return "TIME"

        # Trend reversal
        if ef and es and rsi_v:
            if pos.side == "long"  and ef < es and rsi_v > 55:
                return "TREND_REV"
            if pos.side == "short" and ef > es and rsi_v < 45:
                return "TREND_REV"

        return None

    # ── Order execution ───────────────────────────────────────────────────────

    def _open_position(self, symbol, side, qty, price, atr_val,
                       ef, es, rsi_v, adx_val, vix_val, vol_r, htf):
        order_side = "buy" if side == "long" else "sell"
        try:
            self.api.submit_order(
                symbol=symbol, qty=qty, side=order_side,
                type="market", time_in_force="day"
            )
            pos = EquityPosition(symbol, side, qty, price, atr_val,
                                 ef, es, rsi_v, adx_val, vix_val, vol_r, htf)
            with self._lock:
                self._positions[symbol] = pos
            self.gk.position_opened("equity", qty * price)
            self._log_trade(symbol, side, "OPEN", qty, price, 0, "ENTRY")
            log.info(f"OPEN {side.upper()} {qty}×{symbol} @ ${price:.2f} "
                     f"ATR={atr_val:.3f} ADX={adx_val:.1f} RSI={rsi_v:.1f}")
        except Exception as e:
            log.error(f"Order failed {symbol}: {e}")

    def _close_position(self, pos: EquityPosition, price: float, reason: str):
        close_side = "sell" if pos.side == "long" else "buy"
        pnl = (price - pos.entry_price) * pos.qty if pos.side == "long" \
              else (pos.entry_price - price) * pos.qty
        try:
            self.api.submit_order(
                symbol=pos.symbol, qty=pos.qty, side=close_side,
                type="market", time_in_force="day"
            )
            with self._lock:
                self._positions.pop(pos.symbol, None)
            self.gk.position_closed("equity", pos.qty * pos.entry_price)
            self._log_trade(pos.symbol, pos.side, reason, pos.qty,
                            pos.entry_price, price, pnl)
            log.info(f"CLOSE {pos.side.upper()} {pos.symbol} [{reason}] "
                     f"P&L ${pnl:+.2f}")
        except Exception as e:
            log.error(f"Close failed {pos.symbol}: {e}")

    def _log_trade(self, symbol, side, action, qty, entry, exit_price, pnl):
        self.trade_log.appendleft({
            "ts":         datetime.now().strftime("%H:%M:%S"),
            "symbol":     symbol,
            "side":       side,
            "action":     action,
            "qty":        qty,
            "entry":      round(entry, 2),
            "exit":       round(exit_price, 2) if exit_price else None,
            "pnl":        round(pnl, 2) if pnl else 0,
            "asset":      "equity",
        })

        # Async DB log
        threading.Thread(
            target=self._write_db, args=(symbol, side, qty, entry, exit_price, pnl, action),
            daemon=True
        ).start()

    def _write_db(self, symbol, side, qty, entry, exit_p, pnl, reason):
        try:
            try:
                from db.schema import log_equity_trade
            except ImportError:
                from schema import log_equity_trade
            sig = self.signal_cache.get(symbol, {})
            log_equity_trade({
                "symbol":         symbol, "side": side, "qty": qty,
                "entry_price":    entry,  "exit_price": exit_p,
                "exit_reason":    reason, "realized_pnl": pnl,
                "entry_ema_fast": sig.get("ema_fast"),
                "entry_ema_slow": sig.get("ema_slow"),
                "entry_rsi":      sig.get("rsi"),
                "entry_atr":      sig.get("atr"),
                "entry_adx":      sig.get("adx"),
                "entry_vix":      self.gk.current_vix,
                "entry_vol_ratio":sig.get("vol_r"),
                "htf_trend":      sig.get("trend"),
                "paper":          self.paper,
            })
        except:
            pass

    # ── Dashboard data ────────────────────────────────────────────────────────

    def get_open_positions(self) -> list[dict]:
        """Snapshot of current positions for dashboard."""
        result = []
        try:
            live_positions = {p.symbol: p for p in self.api.list_positions()}
        except:
            live_positions = {}

        with self._lock:
            for sym, pos in self._positions.items():
                live = live_positions.get(sym)
                cur_price = float(live.current_price) if live else pos.entry_price
                pnl = (cur_price - pos.entry_price) * pos.qty if pos.side == "long" \
                      else (pos.entry_price - cur_price) * pos.qty
                result.append({
                    "symbol":      sym,
                    "side":        pos.side,
                    "qty":         pos.qty,
                    "entry":       pos.entry_price,
                    "current":     cur_price,
                    "pnl_usd":     round(pnl, 2),
                    "pnl_pct":     round((cur_price - pos.entry_price) / pos.entry_price * 100
                                         * (1 if pos.side == "long" else -1), 2),
                    "mins_held":   round(pos.mins_held(), 1),
                    "trail_active":pos.trail_active,
                    "atr_stop":    round(pos.atr_stop_price(), 2),
                })
        return result
