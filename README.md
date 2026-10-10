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
| `KALSHI_ENV` | `demo` | `demo` or `prod` |
| `KALSHI_API_KEY` | | API key ID |
| `KALSHI_PRIVATE_KEY_CONTENT` | | PEM text; literal `\n` is fine |
| `KALSHI_PRIVATE_KEY_PATH` | | Alternative to the above (Render secret file) |
| `ODDS_API_KEY` | | the-odds-api.com; without it Edge Finder finds nothing |
| `JOURNAL_DB_PATH` | `data/journal.db` | Put it on a persistent disk in production |
| `DASHBOARD_PASSWORD` | | If set, required to flip the kill switch |

A local `.env` file is read automatically and is git-ignored.

## Run locally

```bash
pip install -r requirements.txt
python launch.py                 # dashboard on http://localhost:10000
python -m unittest discover -s tests -t .
```

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
4. Check the Scout log at startup for "returned 0 open markets". That means a series ticker in `config.sport_series` is wrong.
5. Watch Odds API credits. Each `/odds` call costs credits; `odds_cache_s` controls how often each sport is refreshed.

## Render notes

`render.yaml` sets the Starter plan with a 1 GB disk. The free plan spins idle services down (the engine thread stops with it) and has no disk, so the journal would reset on every deploy. That reset would wipe realized P&L (sizing drops back to the floor) and today's loss total (the daily limit forgets losses). Don't run it live on the free plan.
