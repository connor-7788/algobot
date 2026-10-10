# AlgoTrader: Kalshi agent pipeline

Every Kalshi trade passes through a chain of agents. Each one approves, kills, or (Risk only) shrinks it before anything reaches the exchange.

```
Scout -> Edge Finder -> Skeptic -> Risk Manager -> Execution
                                                      |
Monitor (settlement, realized P&L) <------------------+
```

Only `kalshi/agents/execution.py` imports the order client (`kalshi/orders.py`). A unit test parses every file and fails if anything else does. Execution re-verifies the whole verdict chain before sending and refuses anything missing an approval.

PAPER_MODE defaults to true. In paper mode Execution logs the exact order it would have sent and simulates a fill at the ask. Nothing touches your Kalshi balance until you set `PAPER_MODE=false`.

## Layout

| Path | What it does |
|---|---|
| `launch.py` | Binds `$PORT` with Streamlit first, then starts the engine in a background thread |
| `config.py` | Every threshold, plus env-var loading |
| `kalshi/proposal.py` | `TradeProposal`, verdicts, MODIFY scope rules |
| `kalshi/agents/scout.py` | Filters open markets by volume, spread, depth, time to close |
| `kalshi/agents/edge_finder.py` | Fair value from devigged sportsbook lines, edge after fees, confidence score |
| `kalshi/agents/skeptic.py` | Kills on stale data, line moves, single source, losing record |
| `kalshi/agents/risk_manager.py` | Sizing, 10-position cap, per-event cap, daily loss limit, kill switch |
| `kalshi/agents/execution.py` | Limit orders only, slippage cap, partial fills, cancel after timeout |
| `kalshi/agents/monitor.py` | Settles positions and books realized P&L |
| `kalshi/orchestrator.py` | Runs the loop, enforces per-agent timeouts, writes the journal |
| `kalshi/journal.py` | SQLite: proposals, verdicts, live agent status, kill switch flag |
| `kalshi/fees.py` | Kalshi fee formula and 2026 rounding rules |
| `dashboard/` | Overview, Agents Live, Journal pages |
| `tests/` | stdlib `unittest`, no network |

## Environment variables

Same names your old Render service used, plus a few new ones.

| Var | Default | Notes |
|---|---|---|
| `PAPER_MODE` | `true` | `false` sends real orders |
| `KILL_SWITCH` | `false` | `true` stops all new trading; the dashboard toggle can't override it |
| `KALSHI_ENV` | `demo` | `prod` recommended for paper mode (real prices, nothing sent); live trading on prod needs a prod key |
| `KALSHI_API_KEY` | | API key ID |
| `KALSHI_PRIVATE_KEY_CONTENT` | | PEM text; literal `\n` is fine |
| `KALSHI_PRIVATE_KEY_PATH` | | Alternative to the above (Render secret file) |
| `MARKET_FOCUS` | `crypto_15m` | `crypto_15m` or `sports_live` |
| `ODDS_API_KEY` | | the-odds-api.com; only needed for `sports_live` |
| `JOURNAL_DB_PATH` | `data/journal.db` | Put it on a persistent disk in production |
| `DASHBOARD_PASSWORD` | | If set, required to flip the kill switch |

A local `.env` file is read automatically and is git-ignored.

## Run locally

```bash
pip install -r requirements.txt
python launch.py                 # dashboard on http://localhost:10000
python -m unittest discover -s tests -t .
```

## What it trades: 15-minute crypto markets (default)

`MARKET_FOCUS` (env var, default `crypto_15m`) picks the market set. The default trades Kalshi's 15-minute up/down markets for BTC, ETH and SOL (`KXBTC15M`, `KXETH15M`, `KXSOL15M`). Each one asks whether the coin's CF Benchmarks index, averaged over the final 60 seconds, ends at or above the average from the 60 seconds before it opened. A new market opens every 15 minutes, around the clock.

Fair value comes from live spot prices on Coinbase, Kraken and Bitstamp (three of the exchanges the index is built from) and the last hour of 1-minute volatility: the probability that the price clears the strike in the time left, treating the final 60-second average correctly. Each exchange counts as a separate source, so Skeptic still requires two that agree. The bot scans every 5 seconds and stops opening trades 75 seconds before the close.

Expect most scans to end in a PASS. These markets are traded hard by firms with faster feeds than this bot, so a 3c edge after fees doesn't show up often.

Set `MARKET_FOCUS=sports_live` to go back to live sports (that mode needs `ODDS_API_KEY`).

## Backtest findings (Oct 2-10, 2026)

2,400 settled BTC/ETH/SOL 15-minute markets with minute-by-minute Kalshi quotes and Coinbase prices, split into 5 days to fit and 3 days to test:

* Kalshi's own price was better calibrated than the spot-price model (Brier 0.144 vs 0.150 on the test days). The raw model was underconfident, so it kept "finding" edge in long shots that the market priced correctly.
* The original strategy lost 3.3c per contract over 2,132 trades (t = -3.4). Take-profit/stop-loss made it worse (-4.8c).
* Contracts priced 10-35c lost money in every time window, in both periods.
* Heavy favorites (90-97c) won 90-96% of the time but lost money over the first 5 days and made a little over the last 3: no consistent edge.

So the bot now scales model volatility by 0.8, blends its probability with the market's (model weight 0.25), never buys below 30c, holds to settlement, and enters each market at most once. Expect it to trade rarely. That's the point: most of what it used to trade had negative expected value.

## Learner: the agents improve from results

`kalshi/learner.py`. Every minute Edge Finder looks at a market, it records spot, strike, time left, volatility and Kalshi's bid/ask. After the market settles, Learner attaches the outcome. That's ~13 labelled examples per market whether or not the bot traded, so it learns from every market, not only from its own handful of trades.

Every 30 minutes, once there are 1,500+ labelled examples (about 10 hours of markets), it refits on the last 72 hours:

* Calibration: it searches volatility scale 0.5-1.3 and model weight 0-0.75 on the older 70% of markets and only switches if the new values beat the current ones on the newer 30% (held-out log loss, by at least 0.002).
* Entry threshold: it tries 3, 5, 8 and 12c minimum edge. A threshold is used only if it made money on the older 70% (30+ simulated trades) and also on the newer 30% (t >= 1). If none does, Edge Finder is paused until the evidence changes.

Every change is versioned, logged to the journal and announced on the Agents Live page. Learner never touches Risk Manager's limits. Risk Manager learns one thing on its own: if the last 10 settled trades lost money in total, it halves the stake until the record recovers. Skeptic already refuses a series whose recent record is negative.

## Why it doesn't bet often

The Agents Live page shows a tally of Edge Finder's reasons for the last hour. In practice almost all of it is "edge too small after spread + fees": fair value leans 75% on Kalshi's own price (because the backtest showed Kalshi's price was the better forecast), so to find 3c of edge the model has to disagree with the market by roughly 20+ points after paying the half-spread, a 1c slippage cap and a ~1.7c fee. That rarely happens. Only three markets (BTC, ETH, SOL) are open at a time and each is traded at most once, so even with edge everywhere the ceiling is ~12 trades an hour.

The free Render plan was the other cause: it stops the service after 15 minutes without a visitor, and the engine stops with it. The engine now pings its own public URL (`RENDER_EXTERNAL_URL`, set by Render) every 10 minutes so it stays awake. That uses ~744 of the 750 free instance hours per month, so it only works for one always-on free service per account.

## Take-profit and stop-loss (off by default)

Every second, the mark feed reprices open positions and Risk Manager checks two limits on net P&L (selling at the bid, minus the exit fee, minus what you paid): `risk_take_profit_pct` (default +25%) and `risk_stop_loss_pct` (default 20% loss). When one hits, Execution sells the whole position. Live, that's an immediate-or-cancel, reduce-only limit order at the bid; in paper mode it simulates the sale at the bid. Exits count as realized P&L and show as CLOSED in the journal. Positions that hit neither limit are held to settlement. Turn exits off with `risk_exits_enabled = False`.

## Live games only (sports mode)

`edge_game_phase` in `config.py` is `live_only`. Scout only fetches books for games near or in progress, and Edge Finder passes on anything that hasn't kicked off or started more than 4.5 hours ago. Set it to `pregame_only` or `both` to change that.

Live pricing is where stale data costs the most, so the freshness limits are tight: the bot scans every 30 s, refreshes sportsbook odds every 30 s, and Skeptic kills any trade whose inputs are over 60 s old, whose sportsbook line hasn't updated in 2 minutes (usually a suspended line), or whose Kalshi price moved 5c or more in the last 2 minutes. Expect Skeptic to kill most live proposals. Orders that don't fill within 15 s are cancelled.

Odds API cost: about 2 credits a minute for each sport with a game in progress, and nothing for sports with no live games.

## Paper mode on real prices

Market data (markets, order books) is public, so the bot reads it without signing. That lets paper mode run on `KALSHI_ENV=prod` with real prices even though your key is a demo key. Demo prices are thin and often stale, which makes paper results there meaningless. Signed calls (balance, positions, orders) only happen in live mode, and a demo key on prod fails at startup instead of trading.

## How each rule works

Sizing is 2% of realized profit, clamped between `risk_stake_floor_usd` and `risk_stake_ceiling_usd`. With no realized profit yet, you trade the floor. Contracts are `floor(stake / (ask + slippage cap + fee))`.

Edge Finder doesn't size. It sets an upper bound on contracts (the lesser of `edge_max_contracts` and half the depth at the ask), and Risk sizes down from there. Risk can never raise the count or touch side or price; `TradeProposal.add_verdict` raises `ScopeViolation` if any agent tries, and the orchestrator records that as a REJECT.

Edge is `100 * fair - (ask + slippage cap) - fee per contract`, in cents. The slippage cap is in there because Execution's limit price is ask + cap, so the edge has to survive paying it. The fee is the worse of taker and maker for a 1-contract fill, which bounds the per-contract fee from above.

The fee formula was checked against Kalshi's schedule effective July 7, 2026: taker `0.07 * C * P * (1-P)`, maker `0.0175 * C * P * (1-P)` on series with maker fees, rounded up to your account's precision. `fee_balance_precision` defaults to $0.01, which overstates fees slightly if you're a direct member. That's the safe direction.

Daily loss limit counts realized P&L since midnight New York time. When it's hit, Risk rejects everything and the orchestrator stops scanning until the next day.

The 10-position cap counts approved, filled, and uncertain proposals. An UNCERTAIN proposal is one where Execution couldn't confirm the order state (a timeout mid-send, say). It keeps its slot until Execution reconciles it by `client_order_id`, which is the proposal ID, so a retry can never place a second order.

Per-agent timeouts count as REJECT. The exception is Execution: if it times out, an order might be live, so the proposal goes to UNCERTAIN instead.

## Dashboard

Overview shows open positions, realized P&L, today's P&L against the limit, the next stake size, and a one-line status per agent.

Agents Live shows a card per agent with what it's doing right now, the last thing it said (APPROVE, REJECT, MODIFY, PASS), and the data behind that, plus a live feed you can filter by agent. It refreshes on its own every 2 to 30 seconds.

Journal lists every proposal with each agent's verdict and reason. The "Rejected by" filter and the bar chart show which agent kills the most trades. Pick a proposal to see its full verdict trail and data snapshots.

The kill switch toggle is in the sidebar on every page.

## Before you set PAPER_MODE=false

1. Let paper mode run long enough to see settled results in the journal. Paper fills at the ask are optimistic: real limit orders don't always fill.
2. Check `KALSHI_ENV`. Demo keys only work on demo, prod keys only on prod.
3. Verify the NO-side mapping with one contract in demo. Kalshi's V2 order API uses a single YES book, so buying NO at 40c is sent as selling YES at 60c. The docs support this, but confirm the resulting position shows as NO before trusting it with size.
4. Make a prod API key at kalshi.com (Account, API keys) and put its ID and PEM in Render. Your current key is a demo key.
5. Watch Odds API credits. Each `/odds` call costs credits; `odds_cache_s` controls how often each sport is refreshed.

## Render notes

`render.yaml` sets the Starter plan with a 1 GB disk. The free plan spins idle services down (the engine thread stops with it) and has no disk, so the journal would reset on every deploy. That reset would wipe realized P&L (sizing drops back to the floor) and today's loss total (the daily limit forgets losses). Don't run it live on the free plan.
