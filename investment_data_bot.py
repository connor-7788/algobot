"""
Investment Server Auto-Data Discord Bot
========================================
Every channel gets its own live data embed that auto-updates every 30 minutes.

Channel -> What gets posted:
  #market-data       -> Full snapshot: indices, VIX, crypto, gold, oil
  #stock-discussion  -> Top movers and most active stocks
  #hot-picks         -> Biggest gainers of the day
  #charts            -> Key technical levels for SPY/QQQ/top stocks
  #etfs-index-funds  -> Major ETF performance dashboard
  #market-news       -> Latest financial headlines
  #global-economy    -> DXY, gold, oil, 10Y yield, VIX, international markets
  #crypto-general    -> Top 8 crypto prices + fear/greed index
  #bitcoin           -> BTC price, fear/greed, dominance, key levels
  #ethereum          -> ETH price, gas, L2 ecosystem, ETH/BTC ratio
  #altcoins          -> Top altcoin gainers and losers
  #dividends         -> Dividend stock performance dashboard

HOW TO USE:
  1. pip3 install "discord.py" aiohttp
  2. Go to https://discord.com/developers/applications
     -> New Application -> Bot -> Reset Token -> copy it
     -> Enable: Server Members Intent + Message Content Intent
     -> OAuth2 -> URL Generator -> bot + applications.commands -> Administrator
     -> Copy invite URL, paste in browser, add to your server
  3. Run: DISCORD_BOT_TOKEN=your_token python3 investment_data_bot.py
  4. In Discord type /setup  -- builds all channels and starts live data
  5. Bot auto-updates every 30 min + refreshes on market open/close
"""

import os
import json
import asyncio
import logging
from datetime import datetime, time, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks
import aiohttp

BOT_TOKEN   = os.getenv("DISCORD_BOT_TOKEN", "PASTE_YOUR_TOKEN_HERE")
DATA_FILE   = Path("bot_data.json")
UPDATE_MINS = 30

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Persistent data ───────────────────────────────────────────────────────────
def load_data():
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text())
        except Exception:
            pass
    return {"last_msg": {}, "guilds": [], "watchlists": {}, "alerts": {}, "suggestions": {}}

def save_data(d):
    DATA_FILE.write_text(json.dumps(d, indent=2))

data = load_data()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ── Server layout ─────────────────────────────────────────────────────────────
CATEGORIES = [
    {"name": "🔐 VERIFICATION",  "channels": ["verify"]},
    {"name": "📌 INFORMATION",   "channels": ["rules", "announcements", "getting-started", "bot-commands"]},
    {"name": "📈 STOCKS",        "channels": ["stock-discussion", "hot-picks", "charts", "dividends", "etfs-index-funds"]},
    {"name": "📰 NEWS & MACRO",  "channels": ["market-news", "global-economy"]},
    {"name": "🪙 CRYPTO",        "channels": ["crypto-general", "bitcoin", "ethereum", "altcoins"]},
    {"name": "🤖 LIVE DATA",     "channels": ["market-data", "price-alerts"]},
    {"name": "💬 COMMUNITY",     "channels": ["general-chat", "strategies", "questions", "memes"]},
    {"name": "💡 SUGGESTIONS",   "channels": ["suggestions", "approved-ideas"]},
    {"name": "💎 PREMIUM",       "channels": ["premium-general", "premium-picks", "premium-analysis",
                                               "premium-crypto", "premium-alerts", "premium-portfolio"]},
    {"name": "🛒 UPGRADE TO PREMIUM", "channels": ["get-premium"]},
]

TOPICS = {
    "rules":             "Server rules — read before participating",
    "announcements":     "Important server announcements",
    "getting-started":   "New here? Start here",
    "bot-commands":      "Use /market /watchlist /alert here",
    "stock-discussion":  "📊 Auto-updates every 30min: top movers",
    "hot-picks":         "🔥 Auto-updates every 30min: top gainers",
    "charts":            "📈 Auto-updates every 30min: key technical levels",
    "dividends":         "💰 Auto-updates daily: dividend stocks dashboard",
    "etfs-index-funds":  "🏦 Auto-updates every 30min: major ETF performance",
    "market-news":       "📰 Auto-updates every 30min: latest headlines",
    "global-economy":    "🌍 Auto-updates every 30min: DXY, oil, gold, yields",
    "crypto-general":    "🪙 Auto-updates every 30min: top crypto prices",
    "bitcoin":           "₿ Auto-updates every 30min: BTC dashboard",
    "ethereum":          "⟠ Auto-updates every 30min: ETH dashboard",
    "altcoins":          "🎲 Auto-updates every 30min: altcoin movers",
    "market-data":       "📊 Full market snapshot — updates every 30min",
    "price-alerts":      "🔔 Your price alerts fire here",
    "general-chat":      "Community discussion",
    "strategies":        "Trading and investing strategies",
    "questions":         "Ask anything — no question is too basic",
    "memes":             "Investment memes",
    "suggestions":       "Submit ideas with /suggest",
    "approved-ideas":    "Approved server changes",
    "get-premium":       "💎 Upgrade to Premium — $50/month or $300/lifetime",
    # Verification
    "verify":               "✅ Click the button below to verify and access the server",
    # Premium
    "premium-general":      "💎 PREMIUM — General discussion for members",
    "premium-picks":        "💎 PREMIUM — High-conviction trade setups with full analysis",
    "premium-analysis":     "💎 PREMIUM — Deep dive DD and sector analysis",
    "premium-crypto":       "💎 PREMIUM — Premium crypto plays and on-chain alpha",
    "premium-alerts":       "💎 PREMIUM — Real-time entry/exit alerts",
    "premium-portfolio":    "💎 PREMIUM — Model portfolio tracking",
}

# ── Role names ───────────────────────────────────────────────────────────────
ROLE_MEMBER  = "✅ Member"       # given after verification
ROLE_PREMIUM = "💎 Premium"      # given by admin after payment
ROLE_BOT_BYPASS = "🤖 Bot"       # optional role for trusted bots

# ── Data fetching ─────────────────────────────────────────────────────────────
async def yf(session, ticker):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1d"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8),
                               headers={"User-Agent": "Mozilla/5.0"}) as r:
            if r.status == 200:
                d = await r.json()
                m = d["chart"]["result"][0]["meta"]
                price = m.get("regularMarketPrice", 0)
                prev  = m.get("chartPreviousClose", price) or price
                chg   = price - prev
                pct   = (chg / prev * 100) if prev else 0
                return {"t": ticker, "price": price, "chg": chg, "pct": pct,
                        "high": m.get("regularMarketDayHigh", price),
                        "low":  m.get("regularMarketDayLow", price),
                        "vol":  m.get("regularMarketVolume", 0), "ok": True}
    except Exception as e:
        log.debug(f"yf {ticker}: {e}")
    return {"t": ticker, "price": None, "chg": 0, "pct": 0, "ok": False}

async def many(tickers):
    async with aiohttp.ClientSession() as s:
        return await asyncio.gather(*[yf(s, t) for t in tickers])

async def fear_greed():
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get("https://api.alternative.me/fng/?limit=1",
                             timeout=aiohttp.ClientTimeout(total=5)) as r:
                if r.status == 200:
                    d = await r.json()
                    v = d["data"][0]
                    return int(v["value"]), v["value_classification"]
    except Exception:
        pass
    return 50, "Neutral"

async def headlines():
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                "https://feeds.finance.yahoo.com/rss/2.0/headline?s=^GSPC&region=US&lang=en-US",
                timeout=aiohttp.ClientTimeout(total=8),
                headers={"User-Agent": "Mozilla/5.0"}
            ) as r:
                if r.status == 200:
                    import re
                    txt = await r.text()
                    return re.findall(r"<title><!\[CDATA\[(.*?)\]\]></title>", txt)[1:8]
    except Exception as e:
        log.debug(f"headlines: {e}")
    return ["Unable to load headlines — check back soon."]

# ── Helpers ───────────────────────────────────────────────────────────────────
def a(pct):   return "🟢" if pct >= 0 else "🔴"
def s(pct):   return "+" if pct >= 0 else ""
def ts():     return datetime.now(timezone.utc).strftime("%b %d %H:%M UTC")
def bar(v, mx=10): return "█" * min(int(abs(v) * 2), mx) + "░" * max(0, mx - int(abs(v) * 2))

# ── Embed builders ────────────────────────────────────────────────────────────

async def embed_market_data():
    res = await many(["SPY","QQQ","DIA","IWM","^VIX","GC=F","CL=F","DX-Y.NYB","^TNX","BTC-USD","ETH-USD"])
    r = {x["t"]: x for x in res}
    fg_val, fg_label = await fear_greed()

    e = discord.Embed(title="📊 Full Market Snapshot", color=0x00FF88,
                      timestamp=datetime.now(timezone.utc))
    e.set_footer(text=f"Yahoo Finance • {ts()} • Not financial advice")

    idx = "\n".join(
        f"{a(r[t]['pct'])} **{lbl}** `${r[t]['price']:,.2f}` `{s(r[t]['pct'])}{r[t]['pct']:.2f}%`"
        for t, lbl in [("SPY","S&P 500"),("QQQ","Nasdaq 100"),("DIA","Dow Jones"),("IWM","Russell 2000")]
        if r.get(t, {}).get("ok")
    )
    e.add_field(name="🇺🇸 US Indices", value=idx or "N/A", inline=False)

    vix = r.get("^VIX", {})
    vix_str = f"`{vix['price']:.2f}`" if vix.get("ok") else "N/A"
    fg_em = "😱" if fg_val < 25 else "😨" if fg_val < 45 else "😐" if fg_val < 55 else "😀" if fg_val < 75 else "🤑"
    e.add_field(name="😰 Fear Gauges",
                value=f"**VIX** {vix_str}\n**Crypto F&G** {fg_em} `{fg_val} — {fg_label}`", inline=True)

    macro = "\n".join(
        f"{a(r[t]['pct'])} **{lbl}** `{r[t]['price']:,.2f}` `{s(r[t]['pct'])}{r[t]['pct']:.2f}%`"
        for t, lbl in [("GC=F","Gold"),("CL=F","Oil"),("DX-Y.NYB","DXY"),("^TNX","10Y Yield")]
        if r.get(t, {}).get("ok")
    )
    e.add_field(name="🌍 Macro", value=macro or "N/A", inline=True)

    crypto = "\n".join(
        f"{a(r[t]['pct'])} **{lbl}** `${r[t]['price']:,.0f}` `{s(r[t]['pct'])}{r[t]['pct']:.2f}%`"
        for t, lbl in [("BTC-USD","Bitcoin"),("ETH-USD","Ethereum")]
        if r.get(t, {}).get("ok")
    )
    e.add_field(name="🪙 Crypto", value=crypto or "N/A", inline=False)
    return e


async def embed_stock_discussion():
    tickers = ["AAPL","MSFT","NVDA","TSLA","AMZN","META","GOOGL","AMD","NFLX","JPM"]
    res = await many(tickers)
    ok = [x for x in res if x.get("ok")]
    ok.sort(key=lambda x: abs(x["pct"]), reverse=True)

    e = discord.Embed(title="📈 Stock Market — Today's Top Movers",
                      color=0x27AE60, timestamp=datetime.now(timezone.utc))
    e.set_footer(text=f"Yahoo Finance • Updates every {UPDATE_MINS}min • Not financial advice")

    gainers = [x for x in ok if x["pct"] > 0][:5]
    losers  = [x for x in ok if x["pct"] < 0][:5]

    def fmt_row(x):
        vol = f"{x['vol']/1e6:.1f}M" if x['vol'] >= 1e6 else f"{x['vol']/1e3:.0f}K"
        return f"{a(x['pct'])} **${x['t']}** `${x['price']:,.2f}` `{s(x['pct'])}{x['pct']:.2f}%` Vol:{vol}"

    e.add_field(name="🟢 Top Gainers", value="\n".join(fmt_row(x) for x in gainers) or "N/A", inline=False)
    e.add_field(name="🔴 Top Losers",  value="\n".join(fmt_row(x) for x in losers)  or "N/A", inline=False)
    return e


async def embed_hot_picks():
    tickers = ["NVDA","TSLA","PLTR","SOFI","MSTR","COIN","RBLX","HOOD","IONQ","RIVN"]
    res = await many(tickers)
    ok = sorted([x for x in res if x.get("ok")], key=lambda x: x["pct"], reverse=True)

    e = discord.Embed(title="🔥 Hot Picks — Biggest Moves Today",
                      description="*High momentum plays. Do your own research before trading.*",
                      color=0xFF4500, timestamp=datetime.now(timezone.utc))
    e.set_footer(text=f"Yahoo Finance • Updates every {UPDATE_MINS}min • Not financial advice")

    lines = [
        f"{a(x['pct'])} **${x['t']}** `${x['price']:,.2f}`  `{s(x['pct'])}{x['pct']:.2f}%`  `{bar(x['pct'])}`"
        for x in ok[:8]
    ]
    e.add_field(name="📊 Today's Momentum", value="\n".join(lines) or "Loading…", inline=False)
    return e


async def embed_charts():
    tickers = ["SPY","QQQ","AAPL","NVDA","TSLA"]
    res = await many(tickers)
    r = {x["t"]: x for x in res}

    e = discord.Embed(title="📊 Key Technical Levels Today",
                      description="*Today's High / Low / Close — use as support & resistance reference.*",
                      color=0x2980B9, timestamp=datetime.now(timezone.utc))
    e.set_footer(text=f"Yahoo Finance • Updates every {UPDATE_MINS}min • Not financial advice")

    for t in tickers:
        d = r.get(t, {})
        if d.get("ok"):
            e.add_field(
                name=f"${t}",
                value=(f"`Price` **${d['price']:,.2f}** {a(d['pct'])} `{s(d['pct'])}{d['pct']:.2f}%`\n"
                       f"`High ` ${d['high']:,.2f}  `Low` ${d['low']:,.2f}"),
                inline=True
            )
    return e


async def embed_etf():
    tickers = ["SPY","QQQ","VTI","IWM","XLK","XLF","XLE","XLV","GLD","TLT"]
    res = await many(tickers)
    labels = {"SPY":"S&P 500","QQQ":"Nasdaq 100","VTI":"Total Market","IWM":"Russell 2000",
              "XLK":"Technology","XLF":"Financials","XLE":"Energy","XLV":"Healthcare",
              "GLD":"Gold ETF","TLT":"20Y Bonds"}

    e = discord.Embed(title="🏦 ETF Performance Dashboard",
                      color=0x16A085, timestamp=datetime.now(timezone.utc))
    e.set_footer(text=f"Yahoo Finance • Updates every {UPDATE_MINS}min • Not financial advice")

    lines = [
        f"{a(x['pct'])} **{x['t']}** ({labels.get(x['t'],'')}) `${x['price']:,.2f}` `{s(x['pct'])}{x['pct']:.2f}%`"
        for x in res if x.get("ok")
    ]
    e.add_field(name="📊 Today's ETF Returns", value="\n".join(lines) or "Loading…", inline=False)
    return e


async def embed_market_news():
    hl = await headlines()
    e = discord.Embed(title="📰 Latest Market Headlines",
                      color=0xE74C3C, timestamp=datetime.now(timezone.utc))
    e.set_footer(text=f"Yahoo Finance RSS • Updates every {UPDATE_MINS}min")

    text = "\n\n".join(f"**{i}.** {h}" for i, h in enumerate(hl[:8], 1))
    e.add_field(name="🗞️ Top Stories", value=text or "No headlines available.", inline=False)
    e.add_field(name="📌 Sources",
                value="[Bloomberg](https://bloomberg.com) • [Reuters](https://reuters.com) • "
                      "[WSJ](https://wsj.com) • [CNBC](https://cnbc.com)",
                inline=False)
    return e


async def embed_global_economy():
    tickers = ["GC=F","CL=F","SI=F","DX-Y.NYB","^TNX","^TYX","^VIX","EURUSD=X","^FTSE","^N225"]
    res = await many(tickers)
    r = {x["t"]: x for x in res}

    e = discord.Embed(title="🌍 Global Economy & Macro Dashboard",
                      color=0x1ABC9C, timestamp=datetime.now(timezone.utc))
    e.set_footer(text=f"Yahoo Finance • Updates every {UPDATE_MINS}min • Not financial advice")

    comm = "\n".join(
        f"{a(r[t]['pct'])} **{lbl}** `${r[t]['price']:,.2f}` `{s(r[t]['pct'])}{r[t]['pct']:.2f}%`"
        for t, lbl in [("GC=F","🥇 Gold"),("CL=F","🛢️ Oil WTI"),("SI=F","🥈 Silver")]
        if r.get(t, {}).get("ok")
    )
    e.add_field(name="🏭 Commodities", value=comm or "N/A", inline=True)

    rates = "\n".join(
        f"{a(r[t]['pct'])} **{lbl}** `{r[t]['price']:.3f}` `{s(r[t]['pct'])}{r[t]['pct']:.2f}%`"
        for t, lbl in [("^TNX","📊 10Y Yield"),("^TYX","📊 30Y Yield"),
                       ("DX-Y.NYB","💵 DXY"),("EURUSD=X","💶 EUR/USD")]
        if r.get(t, {}).get("ok")
    )
    e.add_field(name="💱 Rates & FX", value=rates or "N/A", inline=True)

    vix = r.get("^VIX", {})
    intl_lines = []
    if vix.get("ok"):
        vl = "😱 EXTREME" if vix["price"] > 30 else "😨 HIGH" if vix["price"] > 20 else "😐 MODERATE" if vix["price"] > 15 else "😎 LOW"
        intl_lines.append(f"**VIX** `{vix['price']:.2f}` — {vl}")
    for t, lbl in [("^FTSE","🇬🇧 FTSE 100"),("^N225","🇯🇵 Nikkei 225")]:
        d = r.get(t, {})
        if d.get("ok"):
            intl_lines.append(f"{a(d['pct'])} **{lbl}** `{d['price']:,.0f}` `{s(d['pct'])}{d['pct']:.2f}%`")
    e.add_field(name="🌐 Volatility & International", value="\n".join(intl_lines) or "N/A", inline=False)
    return e


async def embed_crypto():
    tickers = ["BTC-USD","ETH-USD","SOL-USD","BNB-USD","XRP-USD","ADA-USD","AVAX-USD","DOGE-USD"]
    res = await many(tickers)
    fg_val, fg_label = await fear_greed()

    e = discord.Embed(title="🪙 Crypto Market Overview",
                      color=0xF39C12, timestamp=datetime.now(timezone.utc))
    e.set_footer(text=f"Yahoo Finance • Updates every {UPDATE_MINS}min • High risk asset class")

    fg_em = "😱" if fg_val < 25 else "😨" if fg_val < 45 else "😐" if fg_val < 55 else "😀" if fg_val < 75 else "🤑"
    fg_bar = "█" * (fg_val // 10) + "░" * (10 - fg_val // 10)
    e.add_field(name="😨 Fear & Greed Index",
                value=f"{fg_em} **{fg_val}/100** — {fg_label}\n`{fg_bar}`", inline=False)

    labels = {"BTC-USD":"₿ Bitcoin","ETH-USD":"⟠ Ethereum","SOL-USD":"◎ Solana",
              "BNB-USD":"🔶 BNB","XRP-USD":"💧 XRP","ADA-USD":"🔵 Cardano",
              "AVAX-USD":"🔺 Avalanche","DOGE-USD":"🐕 Dogecoin"}
    lines = [
        f"{a(x['pct'])} **{labels.get(x['t'], x['t'])}** `${x['price']:,.4g}` `{s(x['pct'])}{x['pct']:.2f}%`"
        for x in res if x.get("ok")
    ]
    e.add_field(name="📊 Top Cryptocurrencies", value="\n".join(lines) or "Loading…", inline=False)
    return e


async def embed_bitcoin():
    res = await many(["BTC-USD","ETH-USD"])
    r = {x["t"]: x for x in res}
    btc = r.get("BTC-USD", {})
    eth = r.get("ETH-USD", {})
    fg_val, fg_label = await fear_greed()

    e = discord.Embed(title="₿ Bitcoin Dashboard", color=0xF7931A,
                      timestamp=datetime.now(timezone.utc))
    e.set_footer(text=f"Yahoo Finance • Updates every {UPDATE_MINS}min • Not financial advice")

    if btc.get("ok"):
        e.add_field(name="💰 BTC Price",
                    value=(f"**${btc['price']:,.2f}**\n"
                           f"{a(btc['pct'])} `{s(btc['pct'])}{btc['pct']:.2f}%` today\n"
                           f"H:`${btc['high']:,.0f}` L:`${btc['low']:,.0f}`"), inline=True)

    fg_em = "😱" if fg_val < 25 else "😨" if fg_val < 45 else "😐" if fg_val < 55 else "😀" if fg_val < 75 else "🤑"
    e.add_field(name=f"{fg_em} Fear & Greed",
                value=f"**{fg_val}/100**\n{fg_label}\n`{'█'*(fg_val//10)}{'░'*(10-fg_val//10)}`", inline=True)

    if btc.get("ok") and eth.get("ok") and eth["price"]:
        e.add_field(name="⚖️ BTC/ETH Ratio",
                    value=f"`{btc['price']/eth['price']:.2f}` ETH per BTC", inline=True)

    e.add_field(name="🔗 Useful Links",
                value=("[📊 BTC Chart](https://www.tradingview.com/chart/?symbol=BTCUSD) • "
                       "[⛓️ Mempool](https://mempool.space) • "
                       "[📈 On-Chain](https://glassnode.com)"), inline=False)
    return e


async def embed_ethereum():
    res = await many(["ETH-USD","BTC-USD"])
    r = {x["t"]: x for x in res}
    eth = r.get("ETH-USD", {})
    btc = r.get("BTC-USD", {})

    e = discord.Embed(title="⟠ Ethereum Dashboard", color=0x627EEA,
                      timestamp=datetime.now(timezone.utc))
    e.set_footer(text=f"Yahoo Finance • Updates every {UPDATE_MINS}min • Not financial advice")

    if eth.get("ok"):
        e.add_field(name="💰 ETH Price",
                    value=(f"**${eth['price']:,.2f}**\n"
                           f"{a(eth['pct'])} `{s(eth['pct'])}{eth['pct']:.2f}%` today\n"
                           f"H:`${eth['high']:,.0f}` L:`${eth['low']:,.0f}`"), inline=True)

    if btc.get("ok") and eth.get("ok") and btc["price"]:
        e.add_field(name="⚖️ ETH/BTC Ratio",
                    value=f"`{eth['price']/btc['price']:.5f}` BTC\n*(higher = ETH outperforming)*",
                    inline=True)

    e.add_field(name="🔗 Ecosystem Links",
                value=("[📊 Chart](https://www.tradingview.com/chart/?symbol=ETHUSD) • "
                       "[⛽ Gas](https://etherscan.io/gastracker) • "
                       "[📈 DeFi](https://defillama.com) • "
                       "[🔍 Explorer](https://etherscan.io)"), inline=False)
    e.add_field(name="🏗️ Layer 2s",
                value="**Arbitrum** • **Optimism** • **Base** • **zkSync** • **Polygon**\n"
                      "[L2 Activity →](https://l2beat.com)", inline=False)
    return e


async def embed_altcoins():
    tickers = ["SOL-USD","AVAX-USD","DOT-USD","LINK-USD","MATIC-USD",
               "UNI-USD","ATOM-USD","LTC-USD","BCH-USD","NEAR-USD"]
    res = await many(tickers)
    ok = sorted([x for x in res if x.get("ok")], key=lambda x: x["pct"], reverse=True)

    e = discord.Embed(title="🎲 Altcoin Movers", color=0x9B59B6,
                      timestamp=datetime.now(timezone.utc))
    e.set_footer(text=f"Yahoo Finance • Updates every {UPDATE_MINS}min • High risk — DYOR")

    gainers = ok[:5]
    losers  = list(reversed(ok))[:5]

    e.add_field(
        name="🟢 Top Gainers",
        value="\n".join(
            f"🟢 **{x['t'].replace('-USD','')}** `${x['price']:,.4g}` `+{x['pct']:.2f}%`"
            for x in gainers
        ) or "N/A", inline=True
    )
    e.add_field(
        name="🔴 Top Losers",
        value="\n".join(
            f"🔴 **{x['t'].replace('-USD','')}** `${x['price']:,.4g}` `{x['pct']:.2f}%`"
            for x in losers
        ) or "N/A", inline=True
    )
    return e


async def embed_dividends():
    tickers = ["JNJ","KO","PG","T","VZ","MO","O","MAIN","SCHD","VYM"]
    res = await many(tickers)
    labels = {"JNJ":"J&J","KO":"Coca-Cola","PG":"P&G","T":"AT&T","VZ":"Verizon",
              "MO":"Altria","O":"Realty Income","MAIN":"Main St Capital",
              "SCHD":"Schwab Div ETF","VYM":"Vanguard High Div"}

    e = discord.Embed(title="💰 Dividend Stocks Dashboard",
                      description="*Tracking high-quality dividend payers.*",
                      color=0xF1C40F, timestamp=datetime.now(timezone.utc))
    e.set_footer(text=f"Yahoo Finance • Updates every {UPDATE_MINS}min • Not financial advice")

    lines = [
        f"{a(x['pct'])} **{x['t']}** ({labels.get(x['t'],'')}) `${x['price']:,.2f}` `{s(x['pct'])}{x['pct']:.2f}%`"
        for x in res if x.get("ok")
    ]
    e.add_field(name="📊 Dividend Stock Performance", value="\n".join(lines) or "Loading…", inline=False)
    e.add_field(name="🔧 Research Tools",
                value="[Dividend.com](https://dividend.com) • [DRIP Calculator](https://dripreturn.com)",
                inline=False)
    return e


# ── Verification UI ──────────────────────────────────────────────────────────

class VerifyButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            style=discord.ButtonStyle.success,
            label="✅  Verify — I am not a bot",
            custom_id="verify_button",
            emoji="🛡️",
        )

    async def callback(self, interaction: discord.Interaction):
        guild  = interaction.guild
        member = interaction.user

        # Give Member role
        role = discord.utils.get(guild.roles, name=ROLE_MEMBER)
        if role is None:
            role = await guild.create_role(name=ROLE_MEMBER, color=discord.Color(0x00FF88),
                                           reason="Auto-created by verification system")
        if role not in member.roles:
            await member.add_roles(role, reason="Passed verification")

        await interaction.response.send_message(
            "✅ **Verified!** You now have access to the server. Welcome! 🚀",
            ephemeral=True
        )


class VerifyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)   # persistent across bot restarts
        self.add_item(VerifyButton())


async def post_verify_message(channel: discord.TextChannel):
    """Post the verification embed in #verify."""
    e = discord.Embed(
        title="Shield Server Verification",
        description=(
            "**Welcome to the server!**\n\n"
            "To keep the community safe and bot-free, you need to verify "
            "before you can access any channels.\n\n"
            "------------------------------\n"
            "**Click the button below to verify.**\n"
            "------------------------------\n\n"
            "This takes one second and proves you are a real person. \u2705\n\n"
            "*If the button does not work, type /verify in this channel.*"
        ),
        color=0x00FF88,
    )
    e.set_footer(text="Server Security - Automated verification")
    async for msg in channel.history(limit=10):
        if msg.author == channel.guild.me:
            try:
                await msg.delete()
            except Exception:
                pass
    await channel.send(embed=e, view=VerifyView())


async def post_premium_welcome(channel: discord.TextChannel):
    """Post the premium welcome card in #premium-general."""
    e = discord.Embed(
        title="Premium Membership",
        description=(
            "You have unlocked **Premium membership**. Here is what you get:\n\n"
            "------------------------------\n"
            "**#premium-picks** - High-conviction setups with full entry/exit plans\n"
            "**#premium-analysis** - In-depth DD and sector breakdowns\n"
            "**#premium-crypto** - On-chain alpha and premium crypto plays\n"
            "**#premium-alerts** - Real-time entry/exit alerts as they happen\n"
            "**#premium-portfolio** - Model portfolio with live tracking\n"
            "------------------------------\n\n"
            "**Premium Rules:**\n"
            "- Keep all premium content inside premium channels\n"
            "- Do not screenshot or share premium picks publicly\n"
            "- These are ideas, not financial advice - always DYOR\n\n"
            "*Thank you for supporting the server!*"
        ),
        color=0xFFD700,
    )
    e.set_footer(text="PortfolioPlus Premium - Not financial advice")
    async for msg in channel.history(limit=5):
        if msg.author == channel.guild.me:
            try:
                await msg.delete()
            except Exception:
                pass
    await channel.send(embed=e)


# ── Channel -> builder map ─────────────────────────────────────────────────────

async def embed_get_premium():
    """Sales embed for #get-premium channel."""
    e = discord.Embed(
        title="💎 Upgrade to Premium",
        description=(
            "Get access to our **exclusive premium investment community** "
            "with high-conviction trade setups, real-time alerts, and "
            "in-depth analysis you won't find anywhere else."
        ),
        color=0xFFD700,
    )

    e.add_field(
        name="💎 What You Get",
        value=(
            "**#premium-picks** — High-conviction stock & crypto setups\n"
            "**#premium-analysis** — Deep-dive DD and sector breakdowns\n"
            "**#premium-crypto** — On-chain alpha and premium crypto plays\n"
            "**#premium-alerts** — Real-time entry/exit alerts\n"
            "**#premium-portfolio** — Full model portfolio tracking\n"
            "**Early access** to new features and events"
        ),
        inline=False,
    )

    e.add_field(
        name="💵 Pricing",
        value=(
            "```\n"
            "Monthly Plan    $50 / month\n"
            "Lifetime Plan   $300 one-time payment\n"
            "```\n"
            "**Lifetime saves you $300+ after 6 months!**"
        ),
        inline=False,
    )

    e.add_field(
        name="🛒 How to Purchase",
        value=(
            "1. Choose your plan below\n"
            "2. Complete your payment\n"
            "3. DM an admin with your receipt\n"
            "4. Get your `💎 Premium` role instantly\n\n"
            "**Payment Links:**\n"
            "Monthly — [Click here to pay $50/month](https://buy.stripe.com/8x2fZg1TYcDo6rcg8b9IQ01)\n"
            "Lifetime — [Click here to pay $300 lifetime](https://buy.stripe.com/7sY28qdCGgTE3f09JN9IQ00)"
        ),
        inline=False,
    )

    e.add_field(
        name="❓ Questions?",
        value="DM an admin or ping a moderator. We typically respond within a few hours.",
        inline=False,
    )

    e.set_footer(text="PortfolioPlus Premium • Not financial advice • All sales final")
    e.set_thumbnail(url="https://cdn.discordapp.com/emojis/💎")
    return e


BUILDERS = {
    "market-data":      embed_market_data,
    "stock-discussion": embed_stock_discussion,
    "hot-picks":        embed_hot_picks,
    "charts":           embed_charts,
    "etfs-index-funds": embed_etf,
    "market-news":      embed_market_news,
    "global-economy":   embed_global_economy,
    "crypto-general":   embed_crypto,
    "bitcoin":          embed_bitcoin,
    "ethereum":         embed_ethereum,
    "altcoins":         embed_altcoins,
    "dividends":        embed_dividends,
    "get-premium":      embed_get_premium,
}


# ── Post/edit embed to channel ────────────────────────────────────────────────
async def update_channel(channel: discord.TextChannel):
    name = channel.name.split("│")[-1].strip()
    builder = BUILDERS.get(name)
    if not builder:
        return
    try:
        embed = await builder()
        key   = f"{channel.guild.id}_{name}"
        # Safely handle old bot_data.json that might be missing keys
        for k in ("last_msg", "guilds", "watchlists", "alerts", "suggestions"):
            if k not in data:
                data[k] = {} if k != "guilds" else []
        last  = data["last_msg"].get(key)
        if last:
            try:
                msg = await channel.fetch_message(last)
                await msg.edit(embed=embed)
                log.info(f"Edited #{name}")
                return
            except (discord.NotFound, discord.HTTPException):
                pass
        msg = await channel.send(embed=embed)
        data["last_msg"][key] = msg.id
        save_data(data)
        log.info(f"Posted to #{name}")
    except discord.HTTPException as e:
        if e.status == 429:
            retry = getattr(e, "retry_after", 10)
            log.warning(f"Rate limited #{name} — waiting {retry:.1f}s")
            await asyncio.sleep(retry + 1)
        else:
            log.error(f"update_channel #{name}: {e}")
    except Exception as e:
        log.error(f"update_channel #{name}: {e}")


async def update_all():
    """Update all data channels. 3s between each to stay under rate limits."""
    total = 0
    for guild in bot.guilds:
        to_update = [ch for ch in guild.text_channels
                     if ch.name.split("│")[-1].strip() in BUILDERS]
        log.info(f"Updating {len(to_update)} channels in {guild.name}...")
        for ch in to_update:
            await update_channel(ch)
            total += 1
            await asyncio.sleep(3.0)
    log.info(f"update_all complete — {total} channels")


# ── Background tasks ──────────────────────────────────────────────────────────
@tasks.loop(minutes=UPDATE_MINS, reconnect=True)
async def auto_update():
    # Skip the very first run (fires immediately on start) — wait for the full interval
    if auto_update.current_loop == 0:
        log.info(f"Auto-update armed. First run in {UPDATE_MINS} minutes.")
        return
    log.info("Scheduled channel update...")
    await update_all()

@tasks.loop(time=time(14, 31, tzinfo=timezone.utc))   # 9:31 AM ET
async def market_open():
    if datetime.now(timezone.utc).weekday() >= 5:
        return
    await update_all()
    for guild in bot.guilds:
        ch = discord.utils.get(guild.text_channels, name="market-data")
        if ch:
            e = discord.Embed(title="🔔 US Markets Are Open!",
                              description="Markets are live. Stay sharp, manage your risk! 📈\n*Not financial advice.*",
                              color=0x00FF88, timestamp=datetime.now(timezone.utc))
            await ch.send(embed=e)

@tasks.loop(time=time(21, 0, tzinfo=timezone.utc))    # 4:00 PM ET
async def market_close():
    if datetime.now(timezone.utc).weekday() >= 5:
        return
    await update_all()
    for guild in bot.guilds:
        ch = discord.utils.get(guild.text_channels, name="market-data")
        if ch:
            e = discord.Embed(title="🔕 Markets Closed — End of Day",
                              description="That's a wrap. See the snapshot above for closing levels.\n*Not financial advice.*",
                              color=0xFF6600, timestamp=datetime.now(timezone.utc))
            await ch.send(embed=e)

@tasks.loop(minutes=15)
async def check_alerts():
    active = [a for a in data["alerts"].values() if not a.get("triggered")]
    if not active:
        return
    tickers = list({a["ticker"] for a in active})
    res = await many(tickers)
    prices = {x["t"]: x["price"] for x in res if x.get("ok")}

    for al in active:
        cur = prices.get(al["ticker"])
        if not cur:
            continue
        hit = (al["direction"] == "above" and cur >= al["target"]) or \
              (al["direction"] == "below" and cur <= al["target"])
        if hit:
            try:
                user = await bot.fetch_user(int(al["user_id"]))
                channel = bot.get_channel(int(al.get("channel_id", 0)))
                e = discord.Embed(title=f"🔔 Alert: {al['ticker']}",
                                  description=(f"**{al['ticker']}** hit `${cur:,.2f}`\n"
                                               f"Your target: {al['direction']} `${al['target']:,.2f}` ✅"),
                                  color=0xFFD700)
                if channel:
                    await channel.send(f"{user.mention}", embed=e)
                else:
                    await user.send(embed=e)
                data["alerts"][al["id"]]["triggered"] = True
                save_data(data)
            except Exception as ex:
                log.error(f"Alert failed: {ex}")


# ── Slash commands ────────────────────────────────────────────────────────────
async def _ensure_roles(guild):
    """Create Member and Premium roles if they don't exist."""
    member_role = discord.utils.get(guild.roles, name=ROLE_MEMBER)
    if not member_role:
        member_role = await guild.create_role(
            name=ROLE_MEMBER, color=discord.Color(0x00FF88),
            reason="Verification system role"
        )
    premium_role = discord.utils.get(guild.roles, name=ROLE_PREMIUM)
    if not premium_role:
        premium_role = await guild.create_role(
            name=ROLE_PREMIUM, color=discord.Color(0xFFD700),
            hoist=True, reason="Premium membership role"
        )
    return member_role, premium_role


async def _apply_permissions(guild, member_role, premium_role):
    """
    Walk every existing text channel and set permissions.
    Never creates, moves, or deletes channels.
      #verify     -> everyone can read, no one can send (bot only)
      #premium-*  -> premium role only
      everything  -> member role only
    """
    everyone = guild.default_role
    updated  = 0

    verify_ow = {
        everyone:     discord.PermissionOverwrite(read_messages=True,  send_messages=False),
        guild.me:     discord.PermissionOverwrite(read_messages=True,  send_messages=True),
    }
    member_ow = {
        everyone:     discord.PermissionOverwrite(read_messages=False),
        member_role:  discord.PermissionOverwrite(read_messages=True),
        premium_role: discord.PermissionOverwrite(read_messages=True),
        guild.me:     discord.PermissionOverwrite(read_messages=True, send_messages=True),
    }
    premium_ow = {
        everyone:     discord.PermissionOverwrite(read_messages=False),
        member_role:  discord.PermissionOverwrite(read_messages=False),
        premium_role: discord.PermissionOverwrite(read_messages=True),
        guild.me:     discord.PermissionOverwrite(read_messages=True, send_messages=True),
    }

    text_channels = [ch for ch in guild.channels if isinstance(ch, discord.TextChannel)]
    for idx, ch in enumerate(text_channels):
        name = ch.name.lower().split("│")[-1].strip()
        if name == "verify":
            ow = verify_ow
        elif name == "get-premium":
            ow = member_ow
        elif "premium" in name:
            ow = premium_ow
        else:
            ow = member_ow
        try:
            await ch.edit(overwrites=ow)
            updated += 1
        except discord.Forbidden:
            log.warning(f"Cannot edit #{ch.name} - no permission")
        except Exception as e:
            log.error(f"Edit #{ch.name}: {e}")
        # 1.5s per channel edit — Discord allows ~5/sec per route but we stay safe
        await asyncio.sleep(1.5)
        # Extra pause every 10 channels to let the bucket reset
        if (idx + 1) % 10 == 0:
            log.info(f"Permissions: {idx+1}/{len(text_channels)} done, pausing 5s...")
            await asyncio.sleep(5)
    return updated


@tree.command(name="setup", description="📊 Start live data feeds (does NOT touch channel structure)")
@app_commands.checks.has_permissions(administrator=True)
async def setup(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    await _ensure_roles(guild)
    bot.add_view(VerifyView())

    verify_ch   = discord.utils.get(guild.text_channels, name="verify")
    premium_gen = discord.utils.get(guild.text_channels, name="premium-general")

    if verify_ch:
        await post_verify_message(verify_ch)
    if premium_gen:
        await post_premium_welcome(premium_gen)

    # Create get-premium channel if it doesn't exist
    upgrade_cat = discord.utils.get(guild.categories, name="🛒 UPGRADE TO PREMIUM")
    if not upgrade_cat:
        upgrade_cat = await guild.create_category("🛒 UPGRADE TO PREMIUM")

    member_role_tmp  = discord.utils.get(guild.roles, name=ROLE_MEMBER)
    premium_role_tmp = discord.utils.get(guild.roles, name=ROLE_PREMIUM)
    everyone_tmp     = guild.default_role

    upgrade_ow = {
        everyone_tmp:     discord.PermissionOverwrite(read_messages=True,  send_messages=False),
        guild.me:         discord.PermissionOverwrite(read_messages=True,  send_messages=True),
    }
    if member_role_tmp:
        upgrade_ow[member_role_tmp] = discord.PermissionOverwrite(read_messages=True, send_messages=False)

    get_prem_ch = discord.utils.get(guild.text_channels, name="get-premium")
    if not get_prem_ch:
        get_prem_ch = await guild.create_text_channel(
            "get-premium",
            category=upgrade_cat,
            topic="💎 Upgrade to Premium — $50/month or $300/lifetime",
            overwrites=upgrade_ow,
        )
    else:
        await get_prem_ch.edit(overwrites=upgrade_ow)

    # Post the premium sales embed
    await update_channel(get_prem_ch)

    if guild.id not in data["guilds"]:
        data["guilds"].append(guild.id)
    save_data(data)

    await interaction.followup.send(
        f"Done! Data channels will auto-update every {UPDATE_MINS} minutes.\n"
        f"Use `/refresh` to push a data update to all channels right now.\n"
        f"Use `/lockdown` to lock all channels to verified members only.",
        ephemeral=True
    )


@tree.command(name="lockdown", description="Lock every channel - only verified Members can see them")
@app_commands.checks.has_permissions(administrator=True)
async def lockdown(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    await interaction.followup.send(
        f"Locking {len(guild.text_channels)} channels... this takes about a minute.",
        ephemeral=True
    )

    member_role, premium_role = await _ensure_roles(guild)
    updated = await _apply_permissions(guild, member_role, premium_role)

    await interaction.followup.send(
        f"Lockdown complete! Updated {updated} channels.\n\n"
        f"#verify - visible to everyone (for new members to verify)\n"
        f"#premium-* channels - {ROLE_PREMIUM} role only\n"
        f"All other channels - {ROLE_MEMBER} role only\n\n"
        f"New member flow: Join server > see only #verify > "
        f"click Verify > get Member role > access everything.\n\n"
        f"To grant premium: use /premium @user",
        ephemeral=True
    )


@tree.command(name="refresh", description="🔄 Force refresh all data channels right now")
@app_commands.checks.has_permissions(manage_messages=True)
async def refresh(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await update_all()
    await interaction.followup.send("✅ All channels refreshed!", ephemeral=True)

@tree.command(name="market", description="📊 Get a live market snapshot")
async def market_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    await interaction.followup.send(embed=await embed_market_data())

wl = app_commands.Group(name="watchlist", description="👀 Your personal watchlist")

@wl.command(name="add", description="Add a ticker to your watchlist")
@app_commands.describe(ticker="e.g. AAPL or BTC-USD")
async def wl_add(interaction: discord.Interaction, ticker: str):
    uid = str(interaction.user.id)
    t   = ticker.upper()
    if uid not in data["watchlists"]:
        data["watchlists"][uid] = []
    if t in data["watchlists"][uid]:
        await interaction.response.send_message(f"`{t}` already in your watchlist.", ephemeral=True)
        return
    if len(data["watchlists"][uid]) >= 20:
        await interaction.response.send_message("Watchlist is full (max 20).", ephemeral=True)
        return
    data["watchlists"][uid].append(t)
    save_data(data)
    await interaction.response.send_message(f"✅ Added `{t}` to your watchlist!", ephemeral=True)

@wl.command(name="remove", description="Remove a ticker")
@app_commands.describe(ticker="Ticker to remove")
async def wl_remove(interaction: discord.Interaction, ticker: str):
    uid = str(interaction.user.id)
    t   = ticker.upper()
    if uid not in data["watchlists"] or t not in data["watchlists"][uid]:
        await interaction.response.send_message(f"`{t}` not in your watchlist.", ephemeral=True)
        return
    data["watchlists"][uid].remove(t)
    save_data(data)
    await interaction.response.send_message(f"✅ Removed `{t}`.", ephemeral=True)

@wl.command(name="show", description="View your watchlist with live prices")
async def wl_show(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    uid     = str(interaction.user.id)
    tickers = data["watchlists"].get(uid, [])
    if not tickers:
        await interaction.followup.send("Empty watchlist. Use `/watchlist add AAPL`", ephemeral=True)
        return
    res = await many(tickers)
    e = discord.Embed(title=f"👀 {interaction.user.display_name}'s Watchlist",
                      color=0x00BFFF, timestamp=datetime.now(timezone.utc))
    for x in res:
        if x.get("ok"):
            e.add_field(name=x["t"],
                        value=f"{a(x['pct'])} **${x['price']:,.2f}**\n`{s(x['pct'])}{x['pct']:.2f}%`",
                        inline=True)
    e.set_footer(text="Yahoo Finance • Not financial advice")
    await interaction.followup.send(embed=e, ephemeral=True)

tree.add_command(wl)

@tree.command(name="alert", description="🔔 Set a price alert for any ticker")
@app_commands.describe(ticker="e.g. AAPL", price="Target price", direction="Above or below target")
@app_commands.choices(direction=[
    app_commands.Choice(name="Above", value="above"),
    app_commands.Choice(name="Below", value="below"),
])
async def alert_cmd(interaction: discord.Interaction, ticker: str, price: float, direction: str):
    uid = str(interaction.user.id)
    t   = ticker.upper()
    aid = f"{uid}_{t}_{int(price*100)}"
    data["alerts"][aid] = {
        "id": aid, "user_id": uid, "guild_id": str(interaction.guild_id),
        "ticker": t, "target": price, "direction": direction,
        "channel_id": str(interaction.channel_id), "triggered": False,
    }
    save_data(data)
    await interaction.response.send_message(
        f"🔔 Alert set! You'll be notified when **{t}** goes **{direction}** `${price:,.2f}`",
        ephemeral=True
    )

@tree.command(name="verify", description="✅ Verify yourself to access the server")
async def verify_cmd(interaction: discord.Interaction):
    """Slash command fallback in case the button doesn't work."""
    guild  = interaction.guild
    member = interaction.user
    role   = discord.utils.get(guild.roles, name=ROLE_MEMBER)
    if role is None:
        role = await guild.create_role(name=ROLE_MEMBER, color=discord.Color(0x00FF88))
    if role in member.roles:
        await interaction.response.send_message("You're already verified! ✅", ephemeral=True)
        return
    await member.add_roles(role, reason="Manual /verify command")
    await interaction.response.send_message(
        "✅ **Verified!** You now have full access to the server. Welcome! 🚀",
        ephemeral=True
    )


@tree.command(name="premium", description="💎 Grant premium access to a user (Admin only)")
@app_commands.describe(member="The user to grant premium access")
@app_commands.checks.has_permissions(administrator=True)
async def grant_premium(interaction: discord.Interaction, member: discord.Member):
    guild = interaction.guild
    role  = discord.utils.get(guild.roles, name=ROLE_PREMIUM)
    if role is None:
        role = await guild.create_role(name=ROLE_PREMIUM, color=discord.Color(0xFFD700), hoist=True)
    if role in member.roles:
        await interaction.response.send_message(f"{member.mention} already has Premium.", ephemeral=True)
        return
    await member.add_roles(role, reason=f"Premium granted by {interaction.user}")
    # DM the user
    try:
        e = discord.Embed(
            title="💎 Premium Access Granted!",
            description=(f"You now have **Premium** access in **{guild.name}**! 🎉\n\n"
                         f"Head to the 💎 PREMIUM category to access exclusive channels.\n\n"
                         f"*Thank you for your support!*"),
            color=0xFFD700
        )
        await member.send(embed=e)
    except Exception:
        pass
    await interaction.response.send_message(
        f"✅ Granted **{ROLE_PREMIUM}** to {member.mention}!", ephemeral=True
    )


@tree.command(name="unpremium", description="💎 Remove premium access from a user (Admin only)")
@app_commands.describe(member="The user to remove premium from")
@app_commands.checks.has_permissions(administrator=True)
async def revoke_premium(interaction: discord.Interaction, member: discord.Member):
    guild = interaction.guild
    role  = discord.utils.get(guild.roles, name=ROLE_PREMIUM)
    if not role or role not in member.roles:
        await interaction.response.send_message(f"{member.mention} doesn't have Premium.", ephemeral=True)
        return
    await member.remove_roles(role, reason=f"Premium revoked by {interaction.user}")
    await interaction.response.send_message(
        f"✅ Removed **{ROLE_PREMIUM}** from {member.mention}.", ephemeral=True
    )


@tree.command(name="suggest", description="💡 Submit a suggestion for the server")
@app_commands.describe(suggestion="Your idea")
async def suggest_cmd(interaction: discord.Interaction, suggestion: str):
    sid = f"{interaction.guild_id}_{len(data['suggestions'])}"
    data["suggestions"][sid] = {
        "id": sid, "user": str(interaction.user),
        "text": suggestion, "status": "pending",
        "ts": datetime.now().isoformat()
    }
    save_data(data)
    e = discord.Embed(title="💡 New Suggestion", description=suggestion,
                      color=0xFFD700, timestamp=datetime.now(timezone.utc))
    e.set_footer(text=f"From {interaction.user} • ID: {sid}")
    ch = discord.utils.get(interaction.guild.text_channels, name="suggestions")
    if ch:
        msg = await ch.send(embed=e)
        await msg.add_reaction("👍")
        await msg.add_reaction("👎")
        await interaction.response.send_message(f"✅ Posted in {ch.mention}!", ephemeral=True)
    else:
        await interaction.response.send_message("✅ Suggestion saved!", ephemeral=True)


# ── Events ────────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    log.info(f"Bot connected as {bot.user}")
    bot.add_view(VerifyView())
    # Wait before syncing - avoids hammering Discord on startup
    await asyncio.sleep(8)
    try:
        synced = await tree.sync()
        log.info(f"Synced {len(synced)} slash commands")
    except Exception as ex:
        log.error(f"Sync error: {ex}")
    # Stagger task starts to spread out API calls
    await asyncio.sleep(3)
    if not auto_update.is_running():
        auto_update.start()
    await asyncio.sleep(3)
    if not check_alerts.is_running():
        check_alerts.start()
    await asyncio.sleep(3)
    if not market_open.is_running():
        market_open.start()
    if not market_close.is_running():
        market_close.start()
    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.watching, name="📈 live market data"
    ))
    log.info(f"Ready. First auto-update in {UPDATE_MINS}min. Use /refresh to post data now.")


@bot.event
async def on_member_join(member: discord.Member):
    guild = member.guild

    # Send them to #verify — they cannot see anything else until verified
    verify_ch = discord.utils.get(guild.text_channels, name="verify")
    if verify_ch:
        try:
            e = discord.Embed(
                title=f"👋 Welcome to {guild.name}!",
                description=(
                    f"Hi {member.mention}! To access the server you need to **verify** first.\n\n"
                    f"Head to {verify_ch.mention} and click the **Verify** button.\n\n"
                    f"It takes one second and keeps bots out. 🛡️"
                ),
                color=0x00FF88,
            )
            e.set_thumbnail(url=member.display_avatar.url)
            await member.send(embed=e)
        except discord.Forbidden:
            pass   # DMs disabled — they'll see #verify when they land
    else:
        # Fallback: if no verify channel, post welcome in general-chat
        ch = discord.utils.get(guild.text_channels, name="general-chat")
        if ch:
            e = discord.Embed(
                title=f"👋 Welcome, {member.display_name}!",
                description=(f"Welcome to **{guild.name}**!\n\n"
                             f"📋 Read #rules and #getting-started\n"
                             f"📊 Use `/market` for live prices\n"
                             f"👀 Set up `/watchlist add AAPL`\n"
                             f"🔔 Set price alerts with `/alert`\n\n"
                             f"*Nothing here is financial advice. DYOR!*"),
                color=0x00FF88,
            )
            e.set_thumbnail(url=member.display_avatar.url)
            await ch.send(embed=e)

@tree.error
async def on_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ Missing permissions.", ephemeral=True)
    else:
        log.error(f"Command error: {error}")
        try:
            await interaction.response.send_message("❌ Something went wrong.", ephemeral=True)
        except Exception:
            pass


# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if BOT_TOKEN == "PASTE_YOUR_TOKEN_HERE":
        print("\n❌  No token set!")
        print("    Run with:  DISCORD_BOT_TOKEN=your_token python3 investment_data_bot.py")
        exit(1)
    bot.run(BOT_TOKEN)
