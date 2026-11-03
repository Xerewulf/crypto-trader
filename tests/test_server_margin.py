#!/usr/bin/env python3
"""Unit tests for the Margin Trading Simulator & Binance Coin Values Engine in app/server.py."""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(ROOT, "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import server  # noqa: E402


class TestMarginSimulator(unittest.TestCase):
    def test_calc_liquidation_price(self):
        # Long 3x at $100 entry: MMR=0.01 -> buffer = 0.99 / 3 = 0.33 -> liq = $67.0
        liq_long_3x = server.calc_liquidation_price(100.0, "LONG", 3.0)
        self.assertAlmostEqual(liq_long_3x, 67.0, places=1)

        # Long 5x at $100 entry: buffer = 0.99 / 5 = 0.198 -> liq = $80.2
        liq_long_5x = server.calc_liquidation_price(100.0, "LONG", 5.0)
        self.assertAlmostEqual(liq_long_5x, 80.2, places=1)

        # Short 3x at $100 entry: buffer = 0.99 / 3 = 0.33 -> liq = $133.0
        liq_short_3x = server.calc_liquidation_price(100.0, "SHORT", 3.0)
        self.assertAlmostEqual(liq_short_3x, 133.0, places=1)

        # 1x leverage long has 0.0 liq
        self.assertEqual(server.calc_liquidation_price(100.0, "LONG", 1.0), 0.0)

    def test_open_and_close_margin_long(self):
        w = server.default_wallet(starting_cash=10000.0)
        # Open Long 3x on BNBUSDT with $3000 notional ($1000 margin) @ $600
        rec = server._open_margin_position_locked(w, "BNBUSDT", "LONG", 600.0, notional_usdt=3000.0, leverage=3.0)
        self.assertEqual(rec["side"], "LONG")
        self.assertEqual(rec["leverage"], 3.0)
        self.assertIn("BNBUSDT", w["positions"])
        pos = w["positions"]["BNBUSDT"]
        self.assertEqual(pos["side"], "LONG")
        self.assertEqual(pos["qty"], 5.0)  # 3000 / 600 = 5 BNB
        self.assertEqual(pos["margin_locked_usdt"], 1000.0)
        # Cash should decrease by margin ($1000) + fee ($1.20)
        self.assertAlmostEqual(w["cash_usdt"], 8998.80, places=2)

        # Close Long @ $660 (+10% gain on price = +$300 gross PnL)
        close_rec = server._close_margin_position_locked(w, "BNBUSDT", 660.0, close_pct=100.0)
        self.assertEqual(close_rec["side"], "LONG")
        self.assertNotIn("BNBUSDT", w["positions"])
        self.assertGreater(close_rec["pnl_usdt"], 290.0)
        self.assertGreater(w["cash_usdt"], 10000.0)
        self.assertEqual(w["winning_trades"], 1)

    def test_open_and_close_margin_short(self):
        w = server.default_wallet(starting_cash=10000.0)
        # Open Short 3x on ETHUSDT with $3000 notional ($1000 margin) @ $3000
        rec = server._open_margin_position_locked(w, "ETHUSDT", "SHORT", 3000.0, notional_usdt=3000.0, leverage=3.0)
        self.assertEqual(rec["side"], "SHORT")
        self.assertIn("ETHUSDT", w["positions"])
        pos = w["positions"]["ETHUSDT"]
        self.assertEqual(pos["side"], "SHORT")
        self.assertEqual(pos["qty"], 1.0)  # 3000 / 3000 = 1 ETH
        self.assertEqual(pos["margin_locked_usdt"], 1000.0)

        # Close Short @ $2700 (-10% price drop = +$300 gain for short)
        close_rec = server._close_margin_position_locked(w, "ETHUSDT", 2700.0, close_pct=100.0)
        self.assertEqual(close_rec["side"], "SHORT")
        self.assertNotIn("ETHUSDT", w["positions"])
        self.assertGreater(close_rec["pnl_usdt"], 290.0)
        self.assertGreater(w["cash_usdt"], 10000.0)

    def test_simulation_toggle(self):
        w = server.default_wallet(starting_cash=10000.0)
        self.assertTrue(w["simulation_running"])
        w["simulation_running"] = False
        self.assertFalse(w["simulation_running"])


if __name__ == "__main__":
    unittest.main()
