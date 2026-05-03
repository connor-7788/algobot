#!/bin/bash
# AlgoTrader v2.2 — Terminal launcher (Mac/Linux)
cd "$(dirname "$0")" || exit

echo "============================================================"
echo "  📈  AlgoTrader v2.2 — Cross-Asset Trading Engine"
echo "============================================================"
echo ""

# Check Python 3
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 not found."
    echo "   Download from: https://www.python.org/downloads/"
    exit 1
fi

echo "✅ Python: $(python3 --version)"
echo ""

# Install dependencies
echo "🔍 Installing dependencies..."
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

# Run
python3 launch.py
