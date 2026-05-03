"""
AlgoTrader v2.1 — Optimized EMA Crossover + RSI Strategy
Connects to Alpaca Markets (paper or live)

Key upgrades over v2.0:
  • Volume confirmation on entry (avoids false breakouts)
  • 5-min higher-timeframe trend filter (avoids counter-trend scalps)
  • EMA separation threshold (avoids weak crossovers)
  • Trailing stop (locks in gains as price rises)
  • Early stop tightening (fast exit if entry goes wrong immediately)
  • Force-close 15 min before market close (no overnight exposure)
  • Max concurrent positions cap (controls total exposure)
  • Daily drawdown circuit breaker (halts trading on bad days)
  • Per-trade max hold time (prevents bag-holding)
"""

import os
import sys
import time
import json
import signal
from datetime import datetime, timedelta, timezone
from collections import deque

try:
    import alpaca_trade_api as tradeapi
    import pandas as pd
    import numpy as np
    from rich.console import Console
    from rich.table import Table
    from rich.live import Live
    from rich.layout import Layout
    from rich.panel import Panel
    from rich.text import Text
    from rich import box
    import questionary
except ImportError:
    print("Installing required packages...")
    os.system(f"{sys.executable} -m pip install alpaca-trade-api pandas numpy rich questionary")
    import alpaca_trade_api as tradeapi
    import pandas as pd
    import numpy as np
    from rich.console import Console
    from rich.table import Table
    from rich.live import Live
    from rich.layout import Layout
    from rich.panel import Panel
    from rich.text import Text
    from rich import box
    import questionary

console = Console()

# ── Strategy Parameters ──────────────────────────────────────────────────────
SYMBOLS          = ["AAPL", "TSLA", "NVDA", "AMZN", "MSFT"]
EMA_FAST         = 8
EMA_SLOW         = 21
RSI_PERIOD       = 14
RSI_MIN          = 40          # tighter: avoids deeply oversold / choppy entries
RSI_MAX          = 55          # tighter: avoids overbought entries
TAKE_PROFIT      = 0.020       # 2.0% take profit — realistic intraday target
STOP_LOSS        = 0.010       # 1.0% stop loss — tight, enforced by bracket order
TRAILING_STOP    = 0.008       # 0.8% trailing stop distance
TRAILING_TRIGGER = 0.010       # trailing stop arms once position is up 1.0%
EARLY_STOP       = 0.005       # 0.5% stop in first 5 min — protects against bad fills
EARLY_STOP_MINS  = 5           # minutes after entry where EARLY_STOP applies
MAX_HOLD_MINS    = 75          # force-exit after 75 min — avoids lunch chop
CLOSE_EARLY_MINS = 20          # exit all positions 20 min before close
POSITION_PCT     = 0.10        # 10% of buying power per trade — conservative sizing
MAX_POSITIONS    = 3           # never hold more than 3 stocks at once
MAX_DAILY_LOSS   = 0.02        # halt at 2% daily loss — tighter circuit breaker
EMA_SEP_MIN_PCT  = 0.0015      # EMAs must be 0.15% apart — filters weak crossovers
VOLUME_MULT_MIN  = 1.3         # entry volume must be 1.3x avg — stronger conviction
POLL_SECONDS     = 30          # scan every 30s for tighter exit monitoring
DUMP_THRESHOLD_PCT = 0.012     # 1.2% drop in DUMP_LOOKBACK_BARS triggers emergency exit
DUMP_LOOKBACK_BARS = 2         # number of 1-min bars to watch for dump

# ── Bracket Order Settings ────────────────────────────────────────────────────
# Bracket orders enforce TP and SL at the EXCHANGE level.
# They fire instantly even if the server crashes between polls.
# Software trailing/early stops are additional safety layers on top.
USE_BRACKET_ORDERS = True      # set False to revert to pure software exits

# ── Indicator Math ────────────────────────────────────────────────────────────
def calc_ema(series: pd.Series, period: int) -> float:
    if len(series) < period:
        return None
    return float(series.ewm(span=period, adjust=False).mean().iloc[-1])

def calc_rsi(series: pd.Series, period: int = 14) -> float:
    if len(series) < period + 1:
        return None
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, 1e-10)
    rsi   = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1])

def calc_volume_ratio(volumes: pd.Series, lookback: int = 20) -> float:
    """Current bar volume vs N-bar average. >1.2 = above-average interest."""
    if len(volumes) < lookback + 1:
        return None
    avg = volumes.iloc[-lookback - 1:-1].mean()
    if avg == 0:
        return None
    return float(volumes.iloc[-1] / avg)

# ── AlgoTrader ────────────────────────────────────────────────────────────────
class AlgoTrader:
    def __init__(self, api_key: str, secret_key: str, paper: bool):
        base = "https://paper-api.alpaca.markets" if paper else "https://api.alpaca.markets"
        self.api      = tradeapi.REST(api_key, secret_key, base, api_version="v2")
        self.paper    = paper
        self.mode     = "📄 PAPER" if paper else "💰 LIVE"
        self.trades   = []
        self.log      = deque(maxlen=50)
        self.running  = True
        self.prices   = {}
        self.signals  = {}
        self.positions_cache  = {}
        self.entry_times      = {}   # symbol → datetime of entry
        self.peak_prices      = {}   # symbol → highest price seen since entry
        self.bracket_order_ids = {}  # symbol → bracket order id (for cancel on force-exit)
        self.circuit_broken   = False
        self.starting_equity  = None

        try:
            self.account = self.api.get_account()
            self.starting_equity = float(self.account.last_equity)
            self._log(f"[green]Connected! Account: {self.account.id[:8]}…[/green]")
        except Exception as e:
            self._log(f"[red]Connection failed: {e}[/red]")
            raise

    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log.appendleft(f"[dim]{ts}[/dim]  {msg}")

    def get_buying_power(self) -> float:
        self.account = self.api.get_account()
        return float(self.account.buying_power)

    def get_portfolio_value(self) -> float:
        self.account = self.api.get_account()
        return float(self.account.portfolio_value)

    def get_positions(self) -> dict:
        try:
            return {p.symbol: p for p in self.api.list_positions()}
        except:
            return {}

    def get_bars(self, symbol: str, timeframe: str = "1Min", limit: int = 60) -> pd.DataFrame:
        """Returns a DataFrame with 'close' and 'volume' columns."""
        try:
            bars = self.api.get_bars(symbol, timeframe, limit=limit, feed="iex").df
            if bars.empty:
                return pd.DataFrame()
            return bars[["close", "volume"]]
        except Exception as e:
            self._log(f"[yellow]Bar fetch failed {symbol}/{timeframe}: {e}[/yellow]")
            return pd.DataFrame()

    def get_htf_trend(self, symbol: str) -> str:
        """5-minute higher-timeframe trend: 'BULL', 'BEAR', or 'FLAT'."""
        bars = self.get_bars(symbol, "5Min", limit=30)
        if bars.empty or len(bars) < EMA_SLOW:
            return "FLAT"
        closes = bars["close"]
        ema_f = calc_ema(closes, EMA_FAST)
        ema_s = calc_ema(closes, EMA_SLOW)
        if ema_f is None or ema_s is None:
            return "FLAT"
        sep = abs(ema_f - ema_s) / ema_s
        if sep < 0.0005:
            return "FLAT"
        return "BULL" if ema_f > ema_s else "BEAR"

    def market_is_open(self) -> bool:
        try:
            return self.api.get_clock().is_open
        except:
            return False

    def minutes_to_close(self) -> float:
        """Returns minutes until market close. Negative = market is closed."""
        try:
            clock = self.api.get_clock()
            if not clock.is_open:
                return -1
            now  = datetime.now(timezone.utc)
            close = clock.next_close.replace(tzinfo=timezone.utc)
            return (close - now).total_seconds() / 60
        except:
            return 999

    def check_circuit_breaker(self) -> bool:
        """Returns True if daily loss limit is hit and trading should halt."""
        if self.starting_equity is None:
            return False
        try:
            pv = self.get_portfolio_value()
            loss_pct = (self.starting_equity - pv) / self.starting_equity
            if loss_pct >= MAX_DAILY_LOSS:
                if not self.circuit_broken:
                    self._log(
                        f"[bold red]⛔ CIRCUIT BREAKER: daily loss {loss_pct*100:.1f}% "
                        f"≥ {MAX_DAILY_LOSS*100:.0f}%. Trading halted.[/bold red]"
                    )
                self.circuit_broken = True
                return True
        except:
            pass
        return False

    def place_buy(self, symbol: str, qty: int, entry_price: float):
        """
        Submit a bracket order: market entry + exchange-enforced TP + SL.
        The exchange fires the TP/SL legs instantly — no polling lag.
        Software trailing/early/time exits act as additional safety nets.
        """
        tp_price = round(entry_price * (1 + TAKE_PROFIT), 2)
        sl_price = round(entry_price * (1 - STOP_LOSS), 2)
        # sl_limit slightly below stop to ensure fill in fast markets
        sl_limit = round(sl_price * 0.998, 2)

        try:
            if USE_BRACKET_ORDERS:
                order = self.api.submit_order(
                    symbol=symbol,
                    qty=qty,
                    side="buy",
                    type="market",
                    time_in_force="day",
                    order_class="bracket",
                    take_profit={"limit_price": str(tp_price)},
                    stop_loss={"stop_price": str(sl_price), "limit_price": str(sl_limit)},
                )
                self._log(
                    f"[green]✅ BRACKET BUY {qty}x {symbol} "
                    f"| TP=${tp_price} SL=${sl_price} "
                    f"— order {order.id[:8]}[/green]"
                )
            else:
                order = self.api.submit_order(
                    symbol=symbol, qty=qty, side="buy",
                    type="market", time_in_force="day"
                )
                self._log(f"[green]✅ BUY {qty}x {symbol} — order {order.id[:8]}[/green]")

            self.entry_times[symbol] = datetime.now()
            self.peak_prices[symbol] = entry_price
            self.bracket_order_ids[symbol] = order.id
            return order
        except Exception as e:
            self._log(f"[red]BUY failed {symbol}: {e}[/red]")
            return None

    def place_sell(self, symbol: str, qty: int, reason: str):
        """
        Cancel any open bracket legs (TP/SL) then submit a market sell.
        This prevents the exchange from re-opening a position after we exit.
        """
        # Cancel the bracket parent order so TP/SL legs don't re-trigger
        bracket_id = self.bracket_order_ids.pop(symbol, None)
        if bracket_id and USE_BRACKET_ORDERS:
            try:
                self.api.cancel_order(bracket_id)
            except Exception:
                pass  # already filled or expired — safe to ignore

        # Cancel any other open orders for this symbol (stale legs)
        try:
            open_orders = self.api.list_orders(status="open", symbols=[symbol])
            for o in open_orders:
                try:
                    self.api.cancel_order(o.id)
                except Exception:
                    pass
        except Exception:
            pass

        try:
            order = self.api.submit_order(
                symbol=symbol, qty=qty, side="sell",
                type="market", time_in_force="day"
            )
            self._log(f"[cyan]📤 SELL {qty}x {symbol} [{reason}] — order {order.id[:8]}[/cyan]")
            self.entry_times.pop(symbol, None)
            self.peak_prices.pop(symbol, None)
            return order
        except Exception as e:
            self._log(f"[red]SELL failed {symbol}: {e}[/red]")
            return None

    def record_trade(self, symbol: str, entry: float, price: float, qty: int, exit_label: str):
        profit = round((price - entry) * qty, 2)
        self.trades.insert(0, {
            "time":   datetime.now().strftime("%H:%M:%S"),
            "symbol": symbol,
            "buy":    round(entry, 2),
            "sell":   round(price, 2),
            "qty":    qty,
            "profit": profit,
            "exit":   exit_label,
        })
        self.trades = self.trades[:40]


    def check_for_dump(self, symbol: str, df: "pd.DataFrame") -> bool:
        """
        Impending Dump Protection.

        Examines the last DUMP_LOOKBACK_BARS 1-minute candles and fires an
        immediate market sell if the price has fallen by more than
        DUMP_THRESHOLD_PCT from the high of that window.

        Returns True if an emergency sell was triggered.
        """
        if df.empty or len(df) < DUMP_LOOKBACK_BARS + 1:
            return False

        # Only relevant if we hold the position
        positions = self.positions_cache
        held = positions.get(symbol)
        if not held:
            return False

        window = df["close"].iloc[-(DUMP_LOOKBACK_BARS + 1):]
        window_high = float(window.max())
        current_price = float(window.iloc[-1])

        if window_high == 0:
            return False

        drop_pct = (window_high - current_price) / window_high

        if drop_pct >= DUMP_THRESHOLD_PCT:
            qty = int(held.qty)
            self._log(
                f"[bold red]🚨 DUMP DETECTED {symbol}: "
                f"dropped {drop_pct*100:.2f}% in {DUMP_LOOKBACK_BARS} bars "
                f"(high=${window_high:.2f} → now=${current_price:.2f}). "
                f"Emergency sell {qty} shares.[/bold red]"
            )
            self.place_sell(symbol, qty, "DUMP PROTECTION")
            entry = float(held.avg_entry_price)
            self.record_trade(symbol, entry, current_price, qty, "🚨 DUMP")
            return True

        return False

    def run_strategy(self):
        # Safety checks
        if self.check_circuit_breaker():
            return

        mtc = self.minutes_to_close()
        force_close = 0 < mtc < CLOSE_EARLY_MINS

        positions = self.get_positions()
        self.positions_cache = positions
        bp = self.get_buying_power()
        num_positions = len(positions)

        for symbol in SYMBOLS:
            # ── Fetch 1-min bars ──
            df = self.get_bars(symbol, "1Min", limit=80)
            if df.empty or len(df) < EMA_SLOW + 5:
                self.signals[symbol] = {"status": "waiting for data"}
                continue

            closes  = df["close"]
            volumes = df["volume"]
            price   = float(closes.iloc[-1])

            ema_f  = calc_ema(closes, EMA_FAST)
            ema_s  = calc_ema(closes, EMA_SLOW)
            rsi    = calc_rsi(closes, RSI_PERIOD)
            prev_f = calc_ema(closes.iloc[:-1], EMA_FAST)
            prev_s = calc_ema(closes.iloc[:-1], EMA_SLOW)
            vol_r  = calc_volume_ratio(volumes)

            # Update peak price for trailing stop
            if symbol in self.peak_prices:
                if self.peak_prices[symbol] is None:
                    self.peak_prices[symbol] = price
                else:
                    self.peak_prices[symbol] = max(self.peak_prices[symbol], price)

            self.prices[symbol]  = price
            self.signals[symbol] = {
                "price":    price,
                "ema_fast": round(ema_f, 2) if ema_f else None,
                "ema_slow": round(ema_s, 2) if ema_s else None,
                "rsi":      round(rsi, 1) if rsi else None,
                "vol_r":    round(vol_r, 2) if vol_r else None,
                "trend":    "BULL" if (ema_f and ema_s and ema_f > ema_s) else "BEAR",
            }

            # ── Dump protection (runs before normal exit logic) ───────────
            if self.check_for_dump(symbol, df):
                # Position already sold — refresh positions and skip rest
                positions = self.get_positions()
                self.positions_cache = positions
                continue

            held = positions.get(symbol)

            # ────────────────────────────────────────────────────────────────
            # EXIT LOGIC
            # ────────────────────────────────────────────────────────────────
            if held:
                entry      = float(held.avg_entry_price)
                qty        = int(held.qty)
                pct        = (price - entry) / entry
                peak       = self.peak_prices.get(symbol) or price
                entry_time = self.entry_times.get(symbol)
                mins_held  = (datetime.now() - entry_time).total_seconds() / 60 if entry_time else 999

                sell_reason = None
                exit_label  = None

                # 1. Force-close near market close
                if force_close:
                    sell_reason = "CLOSE"
                    exit_label  = "🔔 CLOSE"

                # 2. Hard take profit
                elif pct >= TAKE_PROFIT:
                    sell_reason = "TAKE PROFIT"
                    exit_label  = "✅ TP"

                # 3. Trailing stop (activates after hitting TRAILING_TRIGGER gain)
                elif peak is not None and (peak - entry) / entry >= TRAILING_TRIGGER:
                    trail_stop_price = peak * (1 - TRAILING_STOP)
                    if price <= trail_stop_price:
                        sell_reason = "TRAIL STOP"
                        exit_label  = "📉 TRAIL"

                # 4. Tighter early stop in first N minutes
                elif mins_held <= EARLY_STOP_MINS and pct <= -EARLY_STOP:
                    sell_reason = "EARLY STOP"
                    exit_label  = "⚡ EARLY SL"

                # 5. Standard stop loss
                elif pct <= -STOP_LOSS:
                    sell_reason = "STOP LOSS"
                    exit_label  = "🛑 SL"

                # 6. Max hold time
                elif mins_held >= MAX_HOLD_MINS:
                    sell_reason = "TIME EXIT"
                    exit_label  = "⏱ TIME"

                # 7. Trend reversal exit (EMA cross back + RSI elevated)
                elif ema_f and ema_s and ema_f < ema_s and rsi and rsi > 55:
                    sell_reason = "TREND EXIT"
                    exit_label  = "📉 TREND"

                if sell_reason:
                    self.place_sell(symbol, qty, sell_reason)
                    self.record_trade(symbol, entry, price, qty, exit_label)
                continue  # skip entry logic if we held (or just sold)

            # ────────────────────────────────────────────────────────────────
            # ENTRY LOGIC
            # ────────────────────────────────────────────────────────────────
            # Don't enter new positions near close
            if force_close:
                continue

            # Cap total open positions
            if num_positions >= MAX_POSITIONS:
                continue

            # Circuit breaker
            if self.circuit_broken:
                continue

            if not (ema_f and ema_s and prev_f and prev_s and rsi):
                continue

            # Condition 1: EMA crossover on 1-min
            crossover = prev_f <= prev_s and ema_f > ema_s

            # Condition 2: EMA separation (avoid weak/noisy crossovers)
            ema_sep = (ema_f - ema_s) / ema_s
            strong_cross = ema_sep >= EMA_SEP_MIN_PCT

            # Condition 3: RSI in sweet spot (momentum but not overbought)
            rsi_ok = RSI_MIN <= rsi <= RSI_MAX

            # Condition 4: Volume above average (real buying interest)
            volume_ok = vol_r is None or vol_r >= VOLUME_MULT_MIN

            # Condition 5: Higher-timeframe trend alignment (5-min must also be BULL)
            htf = self.get_htf_trend(symbol)
            htf_ok = htf == "BULL"

            if crossover and strong_cross and rsi_ok and volume_ok and htf_ok:
                alloc = bp * POSITION_PCT
                qty   = int(alloc // price)
                if qty > 0:
                    self.place_buy(symbol, qty, price)
                    self._log(
                        f"[green]📊 ENTRY {symbol} | RSI={rsi:.1f} "
                        f"EMA8={ema_f:.2f} > EMA21={ema_s:.2f} | "
                        f"Vol×{vol_r:.2f} | HTF={htf} | "
                        f"TP=${round(price*(1+TAKE_PROFIT),2)} "
                        f"SL=${round(price*(1-STOP_LOSS),2)} "
                        f"[bracket={'ON' if USE_BRACKET_ORDERS else 'OFF'}][/green]"
                    )
                    num_positions += 1  # track locally so we cap correctly this scan
            else:
                # Log why we skipped if crossover happened but other filters blocked it
                if crossover:
                    reasons = []
                    if not strong_cross:
                        reasons.append(f"sep={ema_sep*100:.2f}%<min")
                    if not rsi_ok:
                        reasons.append(f"RSI={rsi:.1f}")
                    if not volume_ok:
                        reasons.append(f"vol×{vol_r:.2f}<{VOLUME_MULT_MIN}")
                    if not htf_ok:
                        reasons.append(f"HTF={htf}")
                    if reasons:
                        self._log(f"[yellow]⚠ Skipped {symbol} crossover: {', '.join(reasons)}[/yellow]")

    def build_display(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="stats",  size=5),
            Layout(name="middle"),
            Layout(name="log",    size=14),
        )
        layout["middle"].split_row(
            Layout(name="tickers"),
            Layout(name="trades"),
        )

        # Header
        mode_color = "yellow" if self.paper else "red"
        cb_indicator = "  [bold red]⛔ CIRCUIT BREAK[/bold red]" if self.circuit_broken else ""
        header_text = Text(justify="center")
        header_text.append("  📈 ALGO TRADER v2.1  ", style="bold green on black")
        header_text.append(f"  {self.mode}  ",         style=f"bold {mode_color} on black")
        header_text.append(f"  {datetime.now().strftime('%H:%M:%S')}  ", style="dim")
        layout["header"].update(Panel(header_text, style="green", subtitle=cb_indicator or None))

        # Account stats
        try:
            pv   = self.get_portfolio_value()
            bp   = self.get_buying_power()
            pnl  = pv - float(self.account.last_equity) if hasattr(self.account, "last_equity") else 0
            wins = [t for t in self.trades if t["profit"] > 0]
            wr   = int(len(wins) / len(self.trades) * 100) if self.trades else 0
            mtc  = self.minutes_to_close()
            mtc_str = f"{int(mtc)}m to close" if 0 < mtc < 120 else ("CLOSED" if mtc < 0 else "OPEN")

            stats_table = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
            for _ in range(10):
                stats_table.add_column()

            pnl_style = "green" if pnl >= 0 else "red"
            wr_style  = "green" if wr >= 60 else ("yellow" if wr >= 45 else "red")

            stats_table.add_row(
                "PORTFOLIO", f"[cyan]${pv:,.2f}[/cyan]",
                "CASH",      f"[blue]${bp:,.2f}[/blue]",
                "DAY P&L",   f"[{pnl_style}]{'+' if pnl>=0 else ''}${pnl:,.2f}[/{pnl_style}]",
                "WIN RATE",  f"[{wr_style}]{wr}%  ({len(self.trades)} trades)[/{wr_style}]",
                "MARKET",    f"[dim]{mtc_str}[/dim]",
            )
            layout["stats"].update(Panel(stats_table, title="Account", style="blue"))
        except Exception as e:
            layout["stats"].update(Panel(f"Loading… ({e})", style="blue"))

        # Tickers
        COLORS = {"AAPL": "green", "TSLA": "red", "NVDA": "magenta", "AMZN": "yellow", "MSFT": "cyan"}
        ticker_table = Table(box=box.SIMPLE_HEAVY, show_header=True, header_style="bold dim")
        ticker_table.add_column("SYMBOL",  style="bold", width=7)
        ticker_table.add_column("PRICE",   justify="right", width=9)
        ticker_table.add_column("RSI",     justify="right", width=6)
        ticker_table.add_column("EMA8",    justify="right", width=8)
        ticker_table.add_column("EMA21",   justify="right", width=8)
        ticker_table.add_column("VOL×",    justify="right", width=6)
        ticker_table.add_column("TREND",   width=7)
        ticker_table.add_column("HELD",    width=5)

        for sym in SYMBOLS:
            sig  = self.signals.get(sym, {})
            held = self.positions_cache.get(sym)
            rsi  = sig.get("rsi")
            volr = sig.get("vol_r")
            rsi_color = "green" if rsi and RSI_MIN <= rsi <= RSI_MAX else ("red" if rsi and rsi > 70 else "yellow")
            vol_color = "green" if volr and volr >= VOLUME_MULT_MIN else "dim"
            trend_color = "green" if sig.get("trend") == "BULL" else "red"
            c = COLORS.get(sym, "white")

            # Trailing stop info if held
            held_str = "○"
            if held:
                peak = self.peak_prices.get(sym)
                entry_p = float(held.avg_entry_price)
                if peak and (peak - entry_p) / entry_p >= TRAILING_TRIGGER:
                    held_str = "[yellow]▲[/yellow]"  # trailing active
                else:
                    held_str = "[green]●[/green]"

            ticker_table.add_row(
                f"[{c}]{sym}[/{c}]",
                f"${sig.get('price', '—')}" if sig.get("price") else "—",
                f"[{rsi_color}]{rsi}[/{rsi_color}]" if rsi else "—",
                str(sig.get("ema_fast") or "—"),
                str(sig.get("ema_slow") or "—"),
                f"[{vol_color}]{volr}[/{vol_color}]" if volr else "—",
                f"[{trend_color}]{sig.get('trend','—')}[/{trend_color}]",
                held_str,
            )

        layout["tickers"].update(Panel(ticker_table, title="Live Signals", style="green"))

        # Trades
        trade_table = Table(box=box.SIMPLE_HEAVY, show_header=True, header_style="bold dim")
        trade_table.add_column("TIME",   width=9)
        trade_table.add_column("SYM",   width=6)
        trade_table.add_column("BUY",   justify="right", width=8)
        trade_table.add_column("SELL",  justify="right", width=8)
        trade_table.add_column("QTY",   justify="right", width=5)
        trade_table.add_column("EXIT",  width=12)
        trade_table.add_column("P&L",   justify="right", width=9)

        for t in self.trades[:12]:
            pnl_str   = f"{'+'if t['profit']>=0 else ''}${t['profit']:.2f}"
            pnl_color = "green" if t["profit"] >= 0 else "red"
            c = COLORS.get(t["symbol"], "white")
            trade_table.add_row(
                t["time"],
                f"[{c}]{t['symbol']}[/{c}]",
                f"${t['buy']:.2f}",
                f"${t['sell']:.2f}",
                str(t["qty"]),
                t["exit"],
                f"[{pnl_color}]{pnl_str}[/{pnl_color}]",
            )

        if not self.trades:
            trade_table.add_row("—", "—", "—", "—", "—", "waiting…", "—")

        layout["trades"].update(Panel(trade_table, title="Completed Trades", style="cyan"))

        # Log
        log_text = "\n".join(list(self.log)[:10])
        layout["log"].update(Panel(log_text or "Waiting for market activity…", title="Activity Log", style="dim"))

        return layout

    def run(self):
        self._log("[cyan]Strategy started. Monitoring market…[/cyan]")
        with Live(self.build_display(), refresh_per_second=1, screen=True) as live:
            while self.running:
                try:
                    if self.market_is_open():
                        self._log("[dim]Running strategy scan…[/dim]")
                        self.run_strategy()
                    else:
                        next_open = self.api.get_clock().next_open
                        self._log(f"[yellow]Market closed. Next open: {next_open}[/yellow]")

                    for _ in range(POLL_SECONDS):
                        if not self.running:
                            break
                        live.update(self.build_display())
                        time.sleep(1)

                except KeyboardInterrupt:
                    self.running = False
                except Exception as e:
                    self._log(f"[red]Error: {e}[/red]")
                    time.sleep(10)

        console.print("\n[yellow]Bot stopped.[/yellow]")


# ── Entry Point ───────────────────────────────────────────────────────────────
def main():
    console.print("""
[bold green]
  ╔═══════════════════════════════════╗
  ║      📈  ALGO TRADER v2.1         ║
  ║  Optimized EMA + RSI Strategy     ║
  ╚═══════════════════════════════════╝
[/bold green]
[dim]Connect to Alpaca Markets (paper or live)[/dim]
""")

    config_path = os.path.expanduser("~/.algotrader_config.json")
    saved = {}
    if os.path.exists(config_path):
        try:
            with open(config_path) as f:
                saved = json.load(f)
            console.print("[dim]Found saved API keys.[/dim]")
        except:
            pass

    # Read from env vars first (server / CI mode), then saved config, then prompt.
    api_key    = os.getenv("ALPACA_API_KEY")    or saved.get("api_key", "")
    secret_key = os.getenv("ALPACA_SECRET_KEY") or saved.get("secret_key", "")
    paper_env  = os.getenv("ALPACA_PAPER", "").lower()
    paper      = True  # default safe

    if api_key and secret_key:
        # Non-interactive path: keys from env or saved config
        if paper_env in {"false", "0", "live"}:
            console.print("[bold red]⚠  LIVE trading mode (ALPACA_PAPER=false)[/bold red]")
            paper = False
        else:
            console.print("[yellow]Paper trading mode[/yellow]")
    else:
        # Interactive path: prompt only when actually running in a terminal
        try:
            api_key = questionary.text(
                "Alpaca API Key:",
                default=saved.get("api_key", "")
            ).ask() or ""

            secret_key = questionary.password(
                "Alpaca Secret Key:",
                default=saved.get("secret_key", "") if saved.get("secret_key") else ""
            ).ask() or ""

            mode = questionary.select(
                "Trading mode:",
                choices=[
                    "Paper Trading (safe, simulated)",
                    "Live Trading (real money ⚠️)",
                ]
            ).ask()

            paper = "Paper" in mode

            if not paper:
                confirm = questionary.confirm(
                    "⚠️  LIVE trading uses REAL MONEY. Are you sure?",
                    default=False
                ).ask()
                if not confirm:
                    console.print("[yellow]Switched to paper trading.[/yellow]")
                    paper = True
        except Exception:
            console.print("[red]Interactive prompt failed — set ALPACA_API_KEY / ALPACA_SECRET_KEY env vars.[/red]")
            return

    try:
        with open(config_path, "w") as f:
            json.dump({"api_key": api_key, "secret_key": secret_key}, f)
        os.chmod(config_path, 0o600)
    except:
        pass

    try:
        bot = AlgoTrader(api_key, secret_key, paper)
    except Exception as e:
        console.print(f"[red]Failed to connect: {e}[/red]")
        console.print("[yellow]Check your API keys at alpaca.markets[/yellow]")
        return

    def handle_exit(sig, frame):
        bot.running = False

    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    bot.run()


if __name__ == "__main__":
    main()
