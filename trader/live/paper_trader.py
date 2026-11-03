#!/usr/bin/env python3
"""Institutional Binance USDS-M Perpetual Futures Bot (Entry Wrapper).

This script now delegates directly to the simplified, backtest-verified USDS-M Perpetual
Futures engine in `trader/futures/bot.py`.

Commands:
    python trader/live/paper_trader.py run
    python trader/live/paper_trader.py once
    python trader/live/paper_trader.py status
    python trader/live/paper_trader.py backtest --days 45
    python trader/live/paper_trader.py reset --yes
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
FUT_DIR = os.path.join(ROOT, "trader", "futures")
if FUT_DIR not in sys.path:
    sys.path.insert(0, FUT_DIR)

import bot

if __name__ == "__main__":
    try:
        sys.exit(bot.main())
    except KeyboardInterrupt:
        sys.exit(0)
