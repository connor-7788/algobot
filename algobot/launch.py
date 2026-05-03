"""
launch.py — AlgoTrader v2.2 headless launcher (Railway-compatible)
- Reads ALL credentials from environment variables (no interactive prompts)
- Exposes dashboard_combined on $PORT (Railway injects this)
- Exposes dashboard_kalshi_bets on $PORT+1 (internal only — use combined view)
- Auto-restarts trading engine on crash
"""

import subprocess
import sys
import os
import time
import signal
import atexit
import traceback
from pathlib import Path

# ── Auto-install dependencies ─────────────────────────────────────────────────
def ensure_package(import_name: str, package_name: str):
    try:
        __import__(import_name)
    except ImportError:
        print(f"📦 Installing {package_name}...")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", package_name, "--quiet"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

print("🔍 Checking dependencies...")
for _imp, _pkg in [
    ("streamlit",        "streamlit>=1.37"),
    ("plotly",           "plotly"),
    ("pandas",           "pandas"),
    ("alpaca_trade_api", "alpaca-trade-api"),
    ("numpy",            "numpy"),
    ("requests",         "requests"),
    ("cryptography",     "cryptography"),
    ("rich",             "rich"),
    ("questionary",      "questionary"),
]:
    ensure_package(_imp, _pkg)

import cross_asset_trader  # noqa: E402

# ── Railway uses $PORT for the public-facing port ─────────────────────────────
PORT = int(os.getenv("PORT", "8501"))

PROCESSES = {}
_keep_running = True


def cleanup():
    print("\n⏹️  Shutting down dashboards...")
    for name, proc in PROCESSES.items():
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    print("✅ Done.")

atexit.register(cleanup)

def handle_signal(sig, frame):
    global _keep_running
    _keep_running = False
    cleanup()
    sys.exit(0)

try:
    signal.signal(signal.SIGINT,  handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
except ValueError:
    print("⚠️ Signal handling skipped: Running in a sub-thread (Streamlit).")


def _start_dashboard(script: str, port: int, name: str):
    old = PROCESSES.get(name)
    if old and old.poll() is None:
        old.terminate()
    try:
        proc = subprocess.Popen(
            [
                sys.executable, "-m", "streamlit", "run", script,
                "--server.port",              str(port),
                "--server.address",           "0.0.0.0",
                "--server.headless",          "true",
                "--server.enableCORS",        "false",
                "--server.enableXsrfProtection", "false",
                "--logger.level",             "error",
                "--client.showErrorDetails",  "false",
                "--browser.gatherUsageStats", "false",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        PROCESSES[name] = proc
        print(f"  ✅ {name} → port {port}")
        return proc
    except Exception as exc:
        print(f"  ⚠️  Could not start {name}: {exc}")
        return None


def main():
    global _keep_running

    print("\n" + "="*60)
    print("  🚀 ALGOTRADER v2.2 — Railway Cloud Deploy")
    print("="*60 + "\n")

    # ── Credentials from env vars only (headless) ─────────────────────────────
    creds = {
        "api_key":       os.getenv("ALPACA_API_KEY", ""),
        "secret_key":    os.getenv("ALPACA_SECRET_KEY", ""),
        "alpaca_paper":  os.getenv("ALPACA_PAPER", "true").lower() == "true",
        "kalshi_key":    os.getenv("KALSHI_API_KEY", ""),
        "kalshi_env":    os.getenv("KALSHI_ENV", "demo"),
        "odds_key":      os.getenv("ODDS_API_KEY", ""),
        "use_sports":    bool(os.getenv("KALSHI_API_KEY", "")),
    }

    if not creds["api_key"] or not creds["secret_key"]:
        print("❌ ERROR: Set ALPACA_API_KEY and ALPACA_SECRET_KEY as environment variables.")
        sys.exit(1)

    print(f"✅ Alpaca loaded — {'PAPER' if creds['alpaca_paper'] else 'LIVE'} mode")
    print(f"✅ Kalshi — {'enabled' if creds['use_sports'] else 'disabled (no KALSHI_API_KEY)'}")

    Path("logs").mkdir(exist_ok=True)

    # ── Dashboards ────────────────────────────────────────────────────────────
    print(f"\n📊 Starting dashboards on port {PORT}...\n")
    _start_dashboard("dashboard_combined.py",    PORT,     "dashboard_combined")
    if creds["use_sports"]:
        _start_dashboard("dashboard_kalshi_bets.py", PORT+1, "dashboard_kalshi")

    time.sleep(3)

    # ── Engine keep-alive loop ───────────────────────────────────────────────
    # Runs 24/7. When the engine exits cleanly (e.g. market close), we sleep
    # and relaunch automatically the next trading session. Only a SIGTERM /
    # Ctrl-C (KeyboardInterrupt) causes a true shutdown.
    print("\n⚙️  Starting trading engine (runs forever, auto-restarts)...\n")
    restart_count = 0
    MARKET_CLOSED_SLEEP_S = 60   # check every 60s when market is closed
    CRASH_RESTART_DELAY_S = 10   # delay after a crash before restarting

    while _keep_running:
        # Delay before restarting (skip on very first launch)
        if restart_count > 0:
            delay = CRASH_RESTART_DELAY_S
            print(f"🔄 Restart #{restart_count} in {delay}s...")
            for _ in range(delay):
                if not _keep_running:
                    break
                time.sleep(1)
            if not _keep_running:
                break

        try:
            cross_asset_trader.main(creds)

            # Engine returned cleanly — market probably closed.
            # Do NOT break — just sleep and loop back to relaunch.
            print("\n⏸️  Engine exited cleanly (market likely closed).")
            print(f"💤 Sleeping {MARKET_CLOSED_SLEEP_S}s then re-checking...")
            for _ in range(MARKET_CLOSED_SLEEP_S):
                if not _keep_running:
                    break
                time.sleep(1)

            if _keep_running:
                restart_count += 1  # counts as a scheduled relaunch, not a crash
                print("🔁 Relaunching engine for next session...")

        except KeyboardInterrupt:
            print("\n🛑 Manual stop requested (Ctrl-C).")
            _keep_running = False
            break
        except Exception as exc:
            restart_count += 1
            print(f"\n❌ Engine crashed: {exc}")
            traceback.print_exc()
            # Loop continues → will restart after CRASH_RESTART_DELAY_S

    cleanup()
    print("\n✅ AlgoTrader shutdown complete.")


if __name__ == "__main__":
    main()
