# AlgoTrader — Deploy to Railway (run from your phone)

## Step 1 — Create a free Railway account
Go to https://railway.app and sign up with GitHub.

## Step 2 — Install Railway CLI (on your Mac, one time)
```bash
brew install railway
```

## Step 3 — Unzip the bot and deploy
```bash
unzip AlgoTrader_v2.2_FULL.zip
cd algotrader
railway login
railway init       # creates a new project — call it "algotrader"
railway up         # deploys the code
```

## Step 4 — Set your environment variables
In the Railway dashboard → your project → Variables, add:

| Variable | Value |
|---|---|
| ALPACA_API_KEY | your Alpaca key |
| ALPACA_SECRET_KEY | your Alpaca secret |
| ALPACA_PAPER | true (or false for live) |
| KALSHI_API_KEY | your Kalshi key ID |
| KALSHI_ENV | demo (or prod) |
| ODDS_API_KEY | your OddsAPI key |

> ⚠️ KALSHI private key: Railway can't store files, so paste the contents
> of your .pem file into a variable called KALSHI_PRIVATE_KEY_CONTENT
> (the bot will need a small code tweak for this — see note below).

## Step 5 — Get your public URL
Railway gives you a URL like:
`https://algotrader-production.up.railway.app`

Open that on your phone — it's your live dashboard. Bookmark it.

## Step 6 — Check logs
```bash
railway logs
```
Or view them in the Railway dashboard.

## Free tier limits
- 500 hours/month free (enough for ~20 days 24/7)
- $5/month for unlimited (recommended for always-on trading)

## Notes
- The engine auto-restarts if it crashes (built into launch.py)
- Dashboard updates every 1 second
- Both dashboards run on the same public URL (combined view)
