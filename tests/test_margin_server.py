#!/usr/bin/env python3
"""Unit tests for app.server institutional margin execution & trigger engine."""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app.server as server


class ServerMarginEngineTests(unittest.TestCase):
    def setUp(self):
        self.w = server.default_wallet(10000.0)

    def test_margin_long_open_and_close(self):
        # Open 3x Margin Long on BNBUSDT at $700 with $3000 notional
        # Collateral = $1000 + fee
        rec = server._exec_margin_open_locked(
            self.w, "BNBUSDT", "long", 700.0, 3000.0, leverage=3.0,
            stop_loss=680.0, take_profit=740.0, setup="Test Long"
        )
        self.assertEqual(rec["action"], "MARGIN LONG")
        self.assertIn("BNBUSDT", self.w["positions"])
        pos = self.w["positions"]["BNBUSDT"]
        self.assertEqual(pos["side"], "long")
        self.assertEqual(pos["leverage"], 3.0)
        self.assertAlmostEqual(pos["margin_usdt"], 1000.0, places=1)
        self.assertLess(pos["liquidation_price"], 680.0)  # Liquidation below stop

        # Close position at $735 (+5% move, +15% ROE)
        close_rec = server._exec_margin_close_locked(self.w, "BNBUSDT", 735.0, close_pct=100.0)
        self.assertEqual(close_rec["action"], "CLOSE LONG")
        self.assertNotIn("BNBUSDT", self.w["positions"])
        self.assertGreater(close_rec["pnl_usdt"], 140.0)  # Profit around $150 minus fees
        self.assertEqual(self.w["winning_trades"], 1)

    def test_margin_short_open_and_close(self):
        # Open 2x Margin Short on ETHUSDT at $2400 with $2000 notional
        # Collateral = $1000 + fee
        rec = server._exec_margin_open_locked(
            self.w, "ETHUSDT", "short", 2400.0, 2000.0, leverage=2.0,
            stop_loss=2500.0, take_profit=2200.0, setup="Test Short"
        )
        self.assertEqual(rec["action"], "MARGIN SHORT")
        self.assertIn("ETHUSDT", self.w["positions"])
        pos = self.w["positions"]["ETHUSDT"]
        self.assertEqual(pos["side"], "short")
        self.assertGreater(pos["liquidation_price"], 2500.0)  # Liquidation above stop for short

        # Close position at $2300 (falling price is profit for short)
        close_rec = server._exec_margin_close_locked(self.w, "ETHUSDT", 2300.0, close_pct=100.0)
        self.assertEqual(close_rec["action"], "CLOSE SHORT")
        self.assertNotIn("ETHUSDT", self.w["positions"])
        self.assertGreater(close_rec["pnl_usdt"], 75.0)
        self.assertEqual(self.w["winning_trades"], 1)

    def test_liquidation_safety_guard_rejects_tight_stop(self):
        # Long entry $100, Stop $50 (50% stop distance), but 5x leverage liquidates at ~ $81.
        # Stop is below liquidation price, which is dangerous!
        with self.assertRaises(ValueError):
            server._exec_margin_open_locked(
                self.w, "BNBUSDT", "long", 100.0, 500.0, leverage=5.0,
                stop_loss=50.0  # Liquidation would trigger before stop!
            )


if __name__ == "__main__":
    unittest.main()
