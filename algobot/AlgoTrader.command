#!/bin/bash
# AlgoTrader v2.2 — macOS double-click launcher
cd "$(dirname "$0")"
clear

echo "============================================================"
echo "  📈  AlgoTrader v2.2 — Cross-Asset Trading Engine"
echo "============================================================"
echo ""

# Check Python 3
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 not found."
    echo "   Download from: https://www.python.org/downloads/"
    echo ""
    read -p "Press Enter to exit..."
    exit 1
fi

echo "✅ Python: $(python3 --version)"
echo ""

# Install core dependencies silently if missing
echo "🔍 Checking dependencies..."
python3 -m pip install --quiet --upgrade pip
python3 -m pip install --quiet \
    alpaca-trade-api \
    pandas \
    numpy \
    requests \
    cryptography \
    rich \
    "streamlit>=1.37" \
    plotly \
    questionary

echo "✅ Dependencies ready."
echo ""

# Launch
python3 launch.py

echo ""
echo "Bot exited."
read -p "Press Enter to close..."
