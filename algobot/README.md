# 📈 AlgoTrader v2.2 — Setup & Launch Guide

## What This Bot Does
- **Equity Engine** — EMA 8/21 crossover + RSI + volume confirmation strategy on US stocks via Alpaca
- **Sports Arbitrage Engine** — Kalshi prediction market arb against sportsbook odds
- **Watchdog** — monitors both engines 24/7 and auto-restarts either if they crash
- **Live Dashboards** — Streamlit dashboards on ports 8500 and 8502

---

## Step 1 — Get Python 3

If you don't have Python 3 installed:
1. Go to https://www.python.org/downloads/
2. Download the latest Python 3.x installer
3. Run it, follow the steps
4. Open Terminal and verify: `python3 --version`

---

## Step 2 — Get Your Alpaca API Keys (Required)

1. Go to https://alpaca.markets and sign up (free)
2. From your dashboard → **Paper Trading** (left sidebar)
3. Click **API Keys** → **Generate New Key**
4. Copy your **API Key ID** and **Secret Key** (you only see the secret once!)
5. For real money later: repeat in the **Live Trading** section

---

## Step 3 — Run the Bot

### macOS — easiest way
1. Right-click `AlgoTrader.command` → **Open**
2. macOS will ask permission — click **Open**
3. It installs everything automatically, then asks for your API keys

### Terminal (Mac/Linux)
```bash
cd /path/to/algotrader
chmod +x run_algotrader.sh
./run_algotrader.sh
```

### Direct Python
```bash
cd /path/to/algotrader
pip install alpaca-trade-api pandas numpy requests cryptography rich streamlit plotly questionary
python3 launch.py
```

---

## Step 4 — Enter Your Credentials

When prompted:
- **Alpaca API Key** + **Secret Key** → from Step 2
- **Trading mode** → start with Paper Trading (safe, no real money)
- **Sports engine** → enter Kalshi + theOddsAPI keys, or skip with Enter

Your keys save to `~/.algotrader_config.json` — you only enter them once.

---

## Optional Data Sources

When prompted, you can paste API keys for extra live data:

| Source | What it adds | Get key at |
|--------|-------------|------------|
| **Polygon.io** | Real-time US equities, options chain | https://polygon.io |
| **Finnhub** | Global stocks, forex, crypto, news sentiment | https://finnhub.io |
| **CoinGecko** | Crypto prices (FREE — no key needed) | automatic |
| **ActionNetwork** | Sharp money %, line movement | https://actionnetwork.com (public, no key needed) |
| **Sportradar** | Pro sports: lineups, injuries, live scores | https://developer.sportradar.com |

All optional — skip any you don't need.

---

## Dashboards

After launch, open these in your browser:
- **http://localhost:8500** — Combined command center (equity + sports)
- **http://localhost:8502** — Kalshi high-confidence bets

---

## Strategy Details

**Entry:**
- EMA 8 crosses above EMA 21
- RSI between 38–58
- Volume 1.2× the 20-bar average
- 5-min higher-timeframe trend aligned

**Exits:**
- Take profit: +2.5%
- Stop loss: −1.3% (ATR-based)
- Trailing stop: activates after +0.8% gain
- Max hold: 90 minutes
- Force-exit: 15 minutes before market close

**Risk Controls:**
- Max 3 concurrent positions
- 2% daily loss circuit breaker (halts all trading)
- VIX > 30 reduces position size by 50%
- Sentiment halt on FOMC / news keywords

---

## Stopping the Bot

Press `Ctrl + C` in the Terminal window. Both engines and dashboards shut down cleanly.

---

## Files

| File | Purpose |
|------|---------|
| `launch.py` | Main launcher — starts engines + dashboards |
| `cross_asset_trader.py` | Core engine runner + watchdog + data hub |
| `equity_engine.py` | Wall Street leg (Alpaca stocks) |
| `kalshi_engine.py` | Sports arb leg (Kalshi + OddsAPI) |
| `risk_gatekeeper.py` | Daily loss limits, position caps, VIX filter |
| `sentiment_filter.py` | News/FOMC halt logic |
| `config.py` | All tunable parameters in one place |
| `dashboard_combined.py` | Streamlit dashboard port 8500 |
| `dashboard_kalshi_bets.py` | Streamlit dashboard port 8502 |

---

## ⚠️ Disclaimer

This is an algorithmic trading tool for educational purposes.
Past performance does not guarantee future results.
Only risk money you can afford to lose.
You are solely responsible for any trades placed.
