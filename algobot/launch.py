import subprocess
import sys

def start_engine():
    print("Initializing AlgoTrader v2.2...")
    subprocess.run(["python", "cross_asset_trader.py"])

if __name__ == "__main__":
    start_engine()
