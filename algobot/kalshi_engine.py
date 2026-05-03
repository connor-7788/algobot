"""
strategies/kalshi_engine.py — Sports Prediction Market Arbitrage Leg.

Strategy:
  1. Poll Kalshi REST API for active sports markets
  2. Fetch theOddsAPI for same event from FanDuel / DraftKings
  3. Convert book odds → no-vig probability
  4. Compare to Kalshi YES/NO mid-price
  5. If edge > MIN_EDGE_PCT and spread < MAX_SPREAD_PCT → enter
  6. In-play velocity tracker monitors for game events (touchdowns etc)
     → triggers early exit at EARLY_EXIT_THRESH if position moves strongly in favour

Early Exit Logic:
  • If YES contract bought at 0.40, and price rises to 0.85 → sell before resolution
  • Locks in ~$45 per contract instead of waiting for $100 at settlement
  • Uses Kalshi "sell" order (maker preferred for lower fees)

Kalshi Order Types:
  • Limit order at ask (maker) when spread allows
  • Market order only when velocity alert + time pressure
"""

import logging
import threading
import time
import base64
from datetime import datetime, timezone
from collections import deque
from urllib.parse import urlparse
from statistics import mean

import requests
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

import config

try:
    from core.risk_gatekeeper import RiskGatekeeper
    from core.odds_converter import ArbFinder, VelocityTracker, OddsConverter
except ImportError:
    from risk_gatekeeper import RiskGatekeeper
    from odds_converter import ArbFinder, VelocityTracker, OddsConverter

log = logging.getLogger(__name__)

KALSHI_BASE = {
    "demo": "https://demo-api.kalshi.co/trade-api/v2",
    "prod": "https://api.elections.kalshi.com/trade-api/v2",
}


# ── Kalshi REST client (lightweight) ─────────────────────────────────────────

class KalshiClient:
    """
    Minimal Kalshi REST client.
    Authentication via Kalshi API key ID + RSA private-key request signing.
    """

    def __init__(self, api_key_id: str, private_key_path: str, env: str = "demo"):
        self.base             = KALSHI_BASE.get(env, KALSHI_BASE["demo"])
        self.env              = env
        self.api_key_id       = api_key_id
        self.private_key_path = private_key_path
        self._lock            = threading.Lock()
        self._private_key     = self._load_private_key(private_key_path)
        self.auth_error       = ""

        if not self.api_key_id:
            self.auth_error = "missing Kalshi API key ID"
        elif self._private_key is None:
            self.auth_error = f"could not read private key at {private_key_path}"
        else:
            try:
                self.get_balance()
                log.info(f"Kalshi signed API auth OK ({self.env})")
            except Exception as e:
                self.auth_error = self._friendly_auth_error(e)
                log.error(f"Kalshi signed API auth failed: {e}")

    def _friendly_auth_error(self, err: Exception) -> str:
        text = str(err)
        if "401" in text or "Unauthorized" in text:
            return (
                "Kalshi rejected the signed request. Check that the API Key ID "
                "matches this exact private key and that you selected the same "
                "environment where the key was created. Demo keys must come from "
                "demo.kalshi.co; production keys must use prod."
            )
        return text

    @staticmethod
    def _load_private_key(path: str):
        # Support Railway/cloud: paste PEM content into KALSHI_PRIVATE_KEY_CONTENT env var
        pem_content = os.environ.get("KALSHI_PRIVATE_KEY_CONTENT", "").strip()
        if pem_content:
            try:
                # env var may use literal \n — normalize to real newlines
                pem_bytes = pem_content.replace("\\n", "\n").encode("utf-8")
                return serialization.load_pem_private_key(
                    pem_bytes, password=None, backend=default_backend()
                )
            except Exception as e:
                log.error(f"Kalshi private key load from env failed: {e}")
                return None
        # Fallback: load from file path
        try:
            with open(path, "rb") as f:
                return serialization.load_pem_private_key(
                    f.read(), password=None, backend=default_backend()
                )
        except Exception as e:
            log.error(f"Kalshi private key load failed: {e}")
            return None

    def is_ready(self) -> bool:
        return bool(self.api_key_id and self._private_key and not self.auth_error)

    def _signature(self, timestamp: str, method: str, path: str) -> str:
        path_without_query = path.split("?")[0]
        message = f"{timestamp}{method.upper()}{path_without_query}".encode("utf-8")
        signature = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def _headers(self, method: str, url: str) -> dict:
        timestamp = str(int(time.time() * 1000))
        path = urlparse(url).path
        return {
            "KALSHI-ACCESS-KEY":       self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": self._signature(timestamp, method, path),
            "Content-Type":            "application/json",
        }

    def get_markets(self, status: str = "open", limit: int = 100) -> list[dict]:
        try:
            url = f"{self.base}/markets"
            r = requests.get(
                url,
                params={"status": status, "limit": limit},
                headers=self._headers("GET", url), timeout=10
            )
            r.raise_for_status()
            return r.json().get("markets", [])
        except Exception as e:
            log.debug(f"Kalshi get_markets: {e}")
            return []

    def get_orderbook(self, ticker: str) -> dict:
        try:
            url = f"{self.base}/markets/{ticker}/orderbook"
            r = requests.get(
                url,
                headers=self._headers("GET", url), timeout=5
            )
            r.raise_for_status()
            return r.json().get("orderbook", {})
        except Exception as e:
            log.debug(f"Kalshi orderbook {ticker}: {e}")
            return {}

    def submit_order(self, ticker: str, side: str, action: str,
                     count: int, price: float, order_type: str = "limit") -> dict:
        """
        side:   'yes' | 'no'
        action: 'buy' | 'sell'
        price:  in cents (1–99)
        """
        payload = {
            "ticker":     ticker,
            "side":       side,
            "action":     action,
            "count":      count,
            "type":       order_type,
        }
        if order_type == "limit":
            payload["yes_price" if side == "yes" else "no_price"] = int(price * 100)

        try:
            url = f"{self.base}/portfolio/orders"
            r = requests.post(
                url,
                json=payload, headers=self._headers("POST", url), timeout=10
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.error(f"Kalshi order failed {ticker}: {e}")
            return {}

    def get_positions(self) -> list[dict]:
        try:
            url = f"{self.base}/portfolio/positions"
            r = requests.get(
                url,
                headers=self._headers("GET", url), timeout=10
            )
            r.raise_for_status()
            return r.json().get("market_positions", [])
        except Exception as e:
            log.debug(f"Kalshi positions: {e}")
            return []

    def get_balance(self) -> float:
        try:
            url = f"{self.base}/portfolio/balance"
            r = requests.get(
                url,
                headers=self._headers("GET", url), timeout=5
            )
            r.raise_for_status()
            return float(r.json().get("balance", 0)) / 100  # cents → dollars
        except Exception as e:
            if self.auth_error:
                raise
            raise RuntimeError(e) from e


# ── Odds API bridge ───────────────────────────────────────────────────────────

class OddsAPIBridge:
    """
    Fetches live odds from theOddsAPI.com for comparison against Kalshi.
    Supports: NFL, NBA, MLB, EPL
    """

    SPORT_KEYS = {
        "NFL":   "americanfootball_nfl",
        "NBA":   "basketball_nba",
        "MLB":   "baseball_mlb",
        "EPL":   "soccer_epl",
        "NHL":   "icehockey_nhl",
        "MMA":   "mma_mixed_martial_arts",
        "NCAAF": "americanfootball_ncaaf",
        "NCAAB": "basketball_ncaab",
    }

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base    = "https://api.the-odds-api.com/v4"
        self._cache  = {}   # event_key → {h2h data}
        self._cache_ts: dict[str, float] = {}

    def get_odds(self, sport: str) -> list[dict]:
        """Return list of events with H2H odds from FanDuel/DraftKings."""
        sport_key = self.SPORT_KEYS.get(sport.upper())
        if not sport_key or not self.api_key:
            return []

        cache_key = sport_key
        if cache_key in self._cache_ts and time.time() - self._cache_ts[cache_key] < 30:
            return self._cache.get(cache_key, [])

        try:
            r = requests.get(
                f"{self.base}/sports/{sport_key}/odds",
                params={
                    "apiKey":     self.api_key,
                    "regions":    "us",
                    "markets":    "h2h",
                    "bookmakers": "fanduel,draftkings",
                    "oddsFormat": "american",
                },
                timeout=10
            )
            r.raise_for_status()
            data = r.json()
            self._cache[cache_key]    = data
            self._cache_ts[cache_key] = time.time()
            return data
        except Exception as e:
            log.debug(f"OddsAPI {sport}: {e}")
            return []

    def find_best_odds(self, event_id: str, sport: str) -> dict | None:
        """Find best available American odds for a specific event."""
        events = self.get_odds(sport)
        for ev in events:
            if ev.get("id") == event_id or ev.get("home_team", "") in event_id:
                best = {"home": None, "away": None}
                for book in ev.get("bookmakers", []):
                    for market in book.get("markets", []):
                        if market.get("key") == "h2h":
                            outcomes = market.get("outcomes", [])
                            if len(outcomes) >= 2:
                                home_odds = outcomes[0].get("price")
                                away_odds = outcomes[1].get("price")
                                if best["home"] is None or abs(home_odds) < abs(best.get("home_raw", 9999)):
                                    best["home"] = home_odds
                                    best["home_raw"] = home_odds
                                    best["away"] = away_odds
                return best
        return None


# ── Kalshi Arbitrage Engine ───────────────────────────────────────────────────

class KalshiEngine:
    """
    Main Kalshi arbitrage engine. Runs in its own thread.
    """

    def __init__(self, kalshi: KalshiClient, odds_bridge: OddsAPIBridge,
                 gatekeeper: RiskGatekeeper):
        self.kalshi      = kalshi
        self.odds        = odds_bridge
        self.gk          = gatekeeper
        self.arb_finder  = ArbFinder(
            min_edge   = config.MIN_EDGE_PCT,
            max_spread = config.MAX_SPREAD_PCT
        )
        self.velocity    = VelocityTracker(
            window_seconds = config.VELOCITY_WINDOW_S,
            threshold_pct  = config.VELOCITY_THRESHOLD,
        )
        self._running    = False
        self._thread     = None
        self._positions: dict[str, dict] = {}   # market_id → position info
        self.trade_log   = deque(maxlen=100)
        self.arb_table   = []    # live arbitrage opportunities for dashboard
        self.confidence_table = []
        self.orderbook_cache: dict[str, dict] = {}
        self._confidence_history: dict[str, deque] = {}
        self._lock       = threading.Lock()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self):
        self._running = True
        self._thread  = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        log.info("KalshiEngine started.")

    def stop(self):
        self._running = False

    def _run_loop(self):
        while self._running:
            try:
                self.gk.update_feed_timestamp("kalshi_ws")  # REST polling counts as alive
                self._manage_positions()
                self._scan_for_arb()
            except Exception as e:
                log.error(f"KalshiEngine error: {e}", exc_info=True)
            time.sleep(config.KALSHI_POLL_SEC)

    # ── Position management ───────────────────────────────────────────────────

    def _manage_positions(self):
        """Check existing positions for early exit conditions."""
        if not self._positions:
            return

        live_positions = {p["ticker"]: p for p in self.kalshi.get_positions()}

        for market_id in list(self._positions.keys()):
            pos = self._positions[market_id]
            live = live_positions.get(market_id)
            if not live:
                with self._lock:
                    removed = self._positions.pop(market_id, None)
                if removed:
                    self.gk.position_closed(
                        "kalshi",
                        removed.get("notional", removed["contracts"] * removed["entry_price"]),
                    )
                continue

            # Get current price
            ob = self.kalshi.get_orderbook(market_id)
            if not ob:
                continue

            cur_price = self._mid_price(ob, pos["side"])
            if cur_price is None:
                continue

            # Update velocity
            vel = self.velocity.update(market_id, cur_price)

            # Early exit logic
            pnl_pct = (cur_price - pos["entry_price"]) / pos["entry_price"]
            should_exit = False
            exit_reason = ""

            if cur_price >= config.EARLY_EXIT_THRESH:
                should_exit = True
                exit_reason = f"EARLY_EXIT ({cur_price:.2f} ≥ {config.EARLY_EXIT_THRESH})"
            elif vel["alert"] and vel["direction"] == "down" and pos["side"] == "yes":
                should_exit = True
                exit_reason = f"VELOCITY_EXIT ({vel['velocity']*100:+.1f}% in {config.VELOCITY_WINDOW_S}s)"
            elif vel["alert"] and vel["direction"] == "up" and pos["side"] == "no":
                should_exit = True
                exit_reason = f"VELOCITY_EXIT ({vel['velocity']*100:+.1f}% in {config.VELOCITY_WINDOW_S}s)"

            if should_exit:
                self._close_position(market_id, pos, cur_price, exit_reason)

    def _scan_for_arb(self):
        """Scan active Kalshi markets for arbitrage opportunities."""
        opportunities = []
        confidence_rows = []

        for sport in config.KALSHI_SPORTS:
            markets = self.kalshi.get_markets()
            sport_markets = [
                m for m in markets
                if (
                    sport.upper() in m.get("title", "").upper()
                    or sport.upper() in m.get("series_ticker", "").upper()
                    or sport.upper() in m.get("event_ticker", "").upper()
                    or sport.upper() in (m.get("category") or "").upper()
                    or any(
                        sport.upper()[:3] in (tag or "").upper()
                        for tag in (m.get("tags") or [])
                    )
                )
            ]

            book_events = self.odds.get_odds(sport)

            for market in sport_markets[:20]:   # cap per sport to avoid rate limits
                ticker = market.get("ticker", "")
                if not ticker or ticker in self._positions:
                    continue

                ob = self.kalshi.get_orderbook(ticker)
                if not ob:
                    continue
                self.orderbook_cache[ticker] = ob

                yes_bid = self._best_price(ob, "yes", "bid")
                yes_ask = self._best_price(ob, "yes", "ask")
                if yes_bid is None or yes_ask is None:
                    continue

                # Try to match this market to a traditional book event
                book_match = self._match_to_book(market, book_events)
                if not book_match:
                    continue

                arb = self.arb_finder.evaluate(
                    market_id       = ticker,
                    event_desc      = market.get("title", ticker),
                    sport           = sport,
                    kalshi_yes_bid  = yes_bid,
                    kalshi_yes_ask  = yes_ask,
                    book_american_a = book_match["home"],
                    book_american_b = book_match["away"],
                    max_contracts   = 1_000_000,
                    bankroll        = self.gk.total_equity or 10_000,
                )

                if arb:
                    yes_objective = self._yes_objective(market)
                    win_condition = self._win_condition(arb.kalshi_side, yes_objective)
                    confidence = self._confidence_check(
                        arb=arb,
                        spread=yes_ask - yes_bid,
                        book_match=book_match,
                    )
                    contracts, stake = self._position_size(arb.kalshi_price)
                    arb.max_contracts = contracts
                    arb.is_tradeable = contracts > 0 and confidence["approved"]
                    arb.yes_objective = yes_objective
                    arb.win_condition = win_condition
                    arb.book_home_odds = book_match.get("home")
                    arb.book_away_odds = book_match.get("away")
                    arb.kalshi_american_odds = self._price_to_american(arb.kalshi_price)
                    arb.kalshi_decimal_odds = round(1 / arb.kalshi_price, 3) if arb.kalshi_price else None

                    confidence_rows.append({
                        "market_id": arb.market_id,
                        "event": arb.event_desc,
                        "yes_objective": yes_objective,
                        "win_condition": win_condition,
                        "sport": arb.sport,
                        "side": arb.kalshi_side,
                        "price": arb.kalshi_price,
                        "kalshi_american_odds": arb.kalshi_american_odds,
                        "kalshi_decimal_odds": arb.kalshi_decimal_odds,
                        "book_home_odds": book_match.get("home"),
                        "book_away_odds": book_match.get("away"),
                        "edge_pct": arb.edge_pct,
                        "spread": round(yes_ask - yes_bid, 4),
                        "book_count": book_match.get("book_count", 0),
                        "confirmations": confidence["confirmations"],
                        "score": confidence["score"],
                        "approved": confidence["approved"],
                        "reason": confidence["reason"],
                        "stake_usd": round(stake, 2),
                        "contracts": contracts,
                    })
                    opportunities.append({
                        "market_id":   arb.market_id,
                        "event":       arb.event_desc,
                        "yes_objective": yes_objective,
                        "win_condition": win_condition,
                        "sport":       arb.sport,
                        "side":        arb.kalshi_side,
                        "price":       arb.kalshi_price,
                        "kalshi_american_odds": arb.kalshi_american_odds,
                        "kalshi_decimal_odds": arb.kalshi_decimal_odds,
                        "book_home_odds": book_match.get("home"),
                        "book_away_odds": book_match.get("away"),
                        "book_prob":   arb.book_implied,
                        "edge_pct":    arb.edge_pct,
                        "contracts":   arb.max_contracts,
                        "kelly":       arb.kelly_fraction,
                        "tradeable":   arb.is_tradeable,
                        "confidence_score": confidence["score"],
                        "confidence_reason": confidence["reason"],
                        "confirmations": confidence["confirmations"],
                        "stake_usd": round(stake, 2),
                        "book_count": book_match.get("book_count", 0),
                        "reasoning":   arb.reasoning,
                        "spread":      round(yes_ask - yes_bid, 4),
                    })

                    if arb.is_tradeable and len(self._positions) < config.MAX_CONCURRENT_KALSHI:
                        self._enter_position(arb, yes_bid, yes_ask)

        with self._lock:
            self.arb_table = sorted(opportunities, key=lambda x: x["edge_pct"], reverse=True)
            self.confidence_table = sorted(confidence_rows, key=lambda x: x["score"], reverse=True)

    # ── Order execution ───────────────────────────────────────────────────────

    def _enter_position(self, arb, yes_bid, yes_ask):
        if arb.market_id in self._positions:
            return
        if arb.max_contracts <= 0:
            return
        if len(self._positions) >= config.MAX_CONCURRENT_KALSHI:
            log.info("Kalshi max high-confidence bet cap reached.")
            return

        notional = arb.max_contracts * arb.kalshi_price  # contract price is already dollars
        ok, reason = self.gk.check("kalshi", arb.market_id, notional, arb.kalshi_side)
        if not ok:
            log.info(f"Kalshi gatekeeper blocked {arb.market_id}: {reason}")
            return

        # Use maker (limit) order at ask for YES, bid for NO
        action = "buy"
        price  = yes_ask if arb.kalshi_side == "yes" else (1 - yes_bid)

        result = self.kalshi.submit_order(
            ticker     = arb.market_id,
            side       = arb.kalshi_side,
            action     = action,
            count      = arb.max_contracts,
            price      = price,
            order_type = "limit",
        )

        if result:
            pos = {
                "market_id":   arb.market_id,
                "side":        arb.kalshi_side,
                "contracts":   arb.max_contracts,
                "entry_price": price,
                "book_prob":   arb.book_implied,
                "edge_pct":    arb.edge_pct,
                "event":       arb.event_desc,
                "yes_objective": getattr(arb, "yes_objective", arb.event_desc),
                "win_condition": getattr(arb, "win_condition", arb.event_desc),
                "sport":       arb.sport,
                "notional":    notional,
                "confidence_score": getattr(arb, "confidence_score", None),
                "kalshi_american_odds": getattr(arb, "kalshi_american_odds", None),
                "kalshi_decimal_odds": getattr(arb, "kalshi_decimal_odds", None),
                "book_home_odds": getattr(arb, "book_home_odds", None),
                "book_away_odds": getattr(arb, "book_away_odds", None),
                "entry_time":  datetime.now(),
            }
            with self._lock:
                self._positions[arb.market_id] = pos
            self.gk.position_opened("kalshi", notional)
            self._log_trade(pos, price, 0, "ENTER")
            log.info(f"KALSHI ENTER {arb.kalshi_side.upper()} "
                     f"{arb.max_contracts}× {arb.market_id} @ {price:.2f} "
                     f"[edge {arb.edge_pct*100:.1f}%]")

    def _close_position(self, market_id: str, pos: dict, cur_price: float, reason: str):
        close_action = "sell"
        result = self.kalshi.submit_order(
            ticker     = market_id,
            side       = pos["side"],
            action     = close_action,
            count      = pos["contracts"],
            price      = cur_price,
            order_type = "limit",
        )
        if result or True:   # proceed even if API uncertain
            pnl = (cur_price - pos["entry_price"]) * pos["contracts"]
            with self._lock:
                self._positions.pop(market_id, None)
            self.gk.position_closed(
                "kalshi",
                pos.get("notional", pos["contracts"] * pos["entry_price"]),
            )
            self._log_trade(pos, pos["entry_price"], cur_price, reason, pnl)
            log.info(f"KALSHI CLOSE {market_id} [{reason}] P&L ${pnl:+.2f}")

    def _log_trade(self, pos: dict, entry: float, exit_p: float, action: str, pnl: float = 0):
        self.trade_log.appendleft({
            "ts":       datetime.now().strftime("%H:%M:%S"),
            "market_id":pos["market_id"],
            "event":    pos.get("event", ""),
            "win_condition": pos.get("win_condition", ""),
            "sport":    pos.get("sport", ""),
            "side":     pos["side"],
            "contracts":pos["contracts"],
            "entry":    round(entry, 3),
            "exit":     round(exit_p, 3) if exit_p else None,
            "pnl":      round(pnl, 2),
            "action":   action,
            "asset":    "kalshi",
        })

        threading.Thread(
            target=self._write_db, args=(pos, entry, exit_p, pnl, action),
            daemon=True
        ).start()

    def _write_db(self, pos, entry, exit_p, pnl, reason):
        try:
            try:
                from db.schema import log_kalshi_trade
            except ImportError:
                from schema import log_kalshi_trade
            log_kalshi_trade({
                "market_id":     pos["market_id"],
                "sport":         pos.get("sport"),
                "event_desc":    pos.get("event"),
                "side":          pos["side"],
                "contracts":     pos["contracts"],
                "entry_price":   entry,
                "exit_price":    exit_p or None,
                "exit_reason":   reason,
                "realized_pnl":  pnl or None,
                "implied_prob":  entry,
                "book_prob":     pos.get("book_prob"),
                "edge_pct":      pos.get("edge_pct"),
                "spread_at_entry": None,
                "is_arb":        True,
                "early_exit":    "EARLY_EXIT" in reason or "VELOCITY" in reason,
                "env":           config.KALSHI_ENV,
            })
        except:
            pass

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _best_price(ob: dict, side: str, bid_ask: str) -> float | None:
        levels = ob.get(side, {}).get(bid_ask, [])
        if not levels:
            return None
        # levels is list of [price_cents, quantity]
        try:
            if bid_ask == "bid":
                return max(l[0] for l in levels) / 100
            else:
                return min(l[0] for l in levels) / 100
        except:
            return None

    @staticmethod
    def _mid_price(ob: dict, side: str) -> float | None:
        bid = KalshiEngine._best_price(ob, side, "bid")
        ask = KalshiEngine._best_price(ob, side, "ask")
        if bid and ask:
            return (bid + ask) / 2
        return bid or ask

    def _confidence_check(self, arb, spread: float, book_match: dict) -> dict:
        now = time.monotonic()
        key = f"{arb.market_id}:{arb.kalshi_side}"
        history = self._confidence_history.setdefault(key, deque(maxlen=20))
        history.append({
            "ts": now,
            "edge": arb.edge_pct,
            "price": arb.kalshi_price,
            "spread": spread,
            "book_count": book_match.get("book_count", 0),
        })

        cutoff = now - config.HIGH_CONF_LOOKBACK_SEC
        while history and history[0]["ts"] < cutoff:
            history.popleft()

        confirmations = len(history)
        edge_values = [h["edge"] for h in history]
        edge_floor = config.HIGH_CONF_MIN_EDGE_PCT
        reasons = []

        if arb.edge_pct < edge_floor:
            reasons.append(f"edge {arb.edge_pct*100:.1f}% < {edge_floor*100:.1f}%")
        if spread > config.HIGH_CONF_MAX_SPREAD_PCT:
            reasons.append(f"spread {spread*100:.1f}% > {config.HIGH_CONF_MAX_SPREAD_PCT*100:.1f}%")
        if book_match.get("book_count", 0) < config.HIGH_CONF_MIN_BOOKS:
            reasons.append(f"only {book_match.get('book_count', 0)} sportsbook match")
        if confirmations < config.HIGH_CONF_CONFIRMATIONS:
            reasons.append(f"{confirmations}/{config.HIGH_CONF_CONFIRMATIONS} confirmations")
        if edge_values and arb.edge_pct < max(edge_values) - config.HIGH_CONF_EDGE_DECAY_PCT:
            reasons.append("edge faded during backtrack")

        score = 0
        score += min(35, int((arb.edge_pct / max(edge_floor, 0.0001)) * 25))
        score += 20 if spread <= config.HIGH_CONF_MAX_SPREAD_PCT else max(0, int(20 * (1 - spread / config.MAX_SPREAD_PCT)))
        score += min(20, book_match.get("book_count", 0) * 10)
        score += min(15, confirmations * 5)
        if len(edge_values) >= 2 and arb.edge_pct >= edge_values[0]:
            score += 10

        approved = not reasons and score >= config.HIGH_CONF_MIN_SCORE
        if not approved and score < config.HIGH_CONF_MIN_SCORE:
            reasons.append(f"score {score} < {config.HIGH_CONF_MIN_SCORE}")

        arb.confidence_score = min(score, 100)
        return {
            "approved": approved,
            "score": min(score, 100),
            "confirmations": confirmations,
            "reason": "HIGH_CONFIDENCE" if approved else "; ".join(reasons),
        }

    def _position_size(self, price: float) -> tuple[int, float]:
        bankroll = self.kalshi.get_balance() or self.gk.total_equity or 10_000
        target_stake = bankroll * config.KALSHI_BET_FRACTION
        capped_stake = (
            min(target_stake, config.MAX_KALSHI_POSITION_USD)
            if config.MAX_KALSHI_POSITION_USD > 0
            else target_stake
        )
        if price <= 0:
            return 0, 0.0
        contracts = int(capped_stake / price)
        return max(0, contracts), contracts * price

    @staticmethod
    def _price_to_american(price: float) -> int | None:
        if price <= 0 or price >= 1:
            return None
        if price >= 0.5:
            return int(round(-100 * price / (1 - price)))
        return int(round(100 * (1 - price) / price))

    @staticmethod
    def _yes_objective(market: dict) -> str:
        for key in ("yes_sub_title", "yes_title", "subtitle", "title"):
            value = market.get(key)
            if value:
                return str(value)
        return market.get("ticker", "Market resolves YES")

    @staticmethod
    def _win_condition(side: str, yes_objective: str) -> str:
        if side == "yes":
            return f"YES wins if: {yes_objective}"
        return f"NO wins if this YES objective does not happen: {yes_objective}"

    @staticmethod
    def _match_to_book(market: dict, book_events: list) -> dict | None:
        """Fuzzy-match a Kalshi market title to a traditional book event."""
        title = market.get("title", "").upper()
        for ev in book_events:
            home = ev.get("home_team", "").upper()
            away = ev.get("away_team", "").upper()
            if home[:4] in title or away[:4] in title:
                home_prices = []
                away_prices = []
                for book in ev.get("bookmakers", []):
                    for mkt in book.get("markets", []):
                        if mkt.get("key") == "h2h":
                            outcomes = mkt.get("outcomes", [])
                            if len(outcomes) >= 2:
                                home_prices.append(outcomes[0].get("price", -110))
                                away_prices.append(outcomes[1].get("price", -110))
                if home_prices and away_prices:
                    return {
                        "home": int(mean(home_prices)),
                        "away": int(mean(away_prices)),
                        "book_count": min(len(home_prices), len(away_prices)),
                        "home_prices": home_prices,
                        "away_prices": away_prices,
                    }
        return None

    # ── Dashboard data ────────────────────────────────────────────────────────

    def get_open_positions(self) -> list[dict]:
        with self._lock:
            return list(self._positions.values())

    def get_velocity_data(self, market_id: str) -> list:
        return self.velocity.get_history(market_id)
