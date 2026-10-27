#!/usr/bin/env python3
"""Unit tests for margin trading doctrine (trader-analyst/margin.py)."""

import datetime
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from _load import load

margin = load("trader-analyst", "margin")


class MarginDoctrineTests(unittest.TestCase):
    def test_leverage_tier_caps(self):
        self.assertEqual(margin.leverage_cap("BTC"), 5.0)
        self.assertEqual(margin.leverage_cap("ETH"), 4.0)
        self.assertEqual(margin.leverage_cap("SOL"), 3.0)
        self.assertEqual(margin.leverage_cap("BNB"), 3.0)
        self.assertEqual(margin.leverage_cap("XRP"), 3.0)
        self.assertEqual(margin.leverage_cap("WLD"), 2.0)
        self.assertEqual(margin.leverage_cap("ADA"), 2.0)

    def test_tokenized_stocks_filter(self):
        # bStocks ending with 'B' on Binance: SNDKB, NVDAB, TSLAB, SPYB, etc.
        self.assertTrue(margin.is_tokenized_stock("SNDKBUSDT"))
        self.assertTrue(margin.is_tokenized_stock("NVDABUSDT"))
        self.assertTrue(margin.is_tokenized_stock("TSLABUSDT"))
        self.assertTrue(margin.is_tokenized_stock("SPYBUSDT"))
        # Real cryptos that end with B must NOT be filtered out:
        self.assertFalse(margin.is_tokenized_stock("BNBUSDT"))
        self.assertFalse(margin.is_tokenized_stock("ARBUSDT"))
        self.assertFalse(margin.is_tokenized_stock("SHIBUSDT"))
        self.assertFalse(margin.is_tokenized_stock("BTCUSDT"))
        self.assertFalse(margin.is_tokenized_stock("ETHUSDT"))

    def test_perp_liquidation_calculation(self):
        # BTC Perp long at $80,000, 5x leverage, MMR=0.004
        # Long liq = 80000 * (1 - 1/5 + 0.004) = 80000 * 0.804 = 64320.0
        liq_long = margin.liquidation_price(80000.0, "long", 5.0, venue="perp", base="BTC")
        self.assertAlmostEqual(liq_long, 64320.0, places=1)

        # BTC Perp short at $80,000, 5x leverage
        # Short liq = 80000 * (1 + 1/5 - 0.004) = 80000 * 1.196 = 95680.0
        liq_short = margin.liquidation_price(80000.0, "short", 5.0, venue="perp", base="BTC")
        self.assertAlmostEqual(liq_short, 95680.0, places=1)

    def test_margin_liquidation_calculation(self):
        # Isolated spot margin long at $100, 3x leverage, margin level 1.1
        # Liq = 100 * (1 - 1/3) * 1.1 = 100 * (2/3) * 1.1 = 73.333
        liq = margin.liquidation_price(100.0, "long", 3.0, venue="margin")
        self.assertAlmostEqual(liq, 73.3333, places=2)

    def test_max_safe_leverage_enforces_buffer(self):
        # Entry $100, Stop $95 (5% stop distance).
        # We require liquidation buffer to be at least 2.0x stop distance (10% away).
        safe_lev = margin.max_safe_leverage(100.0, 95.0, "long", venue="perp", base="BTC", buffer_mult=2.0)
        # Denom = 2.0 * 0.05 + 0.004 = 0.104 -> Lev <= 1 / 0.104 = 9.61x
        self.assertGreater(safe_lev, 8.0)
        self.assertLess(safe_lev, 10.0)

        # Test liquidation safety validation
        ok, liq = margin.stop_is_liquidation_safe(100.0, 95.0, "long", leverage=5.0, venue="perp", base="BTC")
        self.assertTrue(ok)
        self.assertLess(liq, 95.0)  # Liquidation is below the stop

    def test_event_risk_fomc_calendar(self):
        # 2026-10-28 2:00 PM NY time is an FOMC rate decision
        # Check event risk 1 hour before decision
        fomc_ms = margin.fomc_decision_ms("2026-10-28")
        one_hour_before = fomc_ms - 1 * margin.HOUR_MS
        ev_block = margin.event_risk(one_hour_before)
        self.assertEqual(ev_block["level"], "BLOCK")
        self.assertEqual(ev_block["risk_mult"], 0.0)

        # Check event risk 12 hours before decision (caution window)
        twelve_hours_before = fomc_ms - 12 * margin.HOUR_MS
        ev_caution = margin.event_risk(twelve_hours_before)
        self.assertEqual(ev_caution["level"], "CAUTION")
        self.assertEqual(ev_caution["risk_mult"], 0.5)

        # Check event risk 48 hours before decision (clear)
        two_days_before = fomc_ms - 48 * margin.HOUR_MS
        ev_clear = margin.event_risk(two_days_before)
        self.assertEqual(ev_clear["level"], "CLEAR")

    def test_correlation_guard(self):
        positions = [
            {"symbol": "SOLUSDT", "side": "long", "notional": 2000.0},
            {"symbol": "ADAUSDT", "side": "long", "notional": 2000.0},
        ]
        # Trying to open a 3rd long should hit the correlation cap (max 2 same side)
        err = margin.correlation_guard(positions, "long", equity=5000.0, new_notional=1500.0, max_same_side=2)
        self.assertIsNotNone(err)
        self.assertIn("Correlation cap", err)

        # Opening a short is allowed
        err_short = margin.correlation_guard(positions, "short", equity=5000.0, new_notional=1500.0, max_same_side=2)
        self.assertIsNone(err_short)

        # Excess gross leverage (e.g. > 2.0x equity)
        err_gross = margin.correlation_guard(positions, "short", equity=2000.0, new_notional=2500.0, max_gross_leverage=2.0)
        self.assertIsNotNone(err_gross)
        self.assertIn("Gross leverage cap", err_gross)

    def test_margin_plan_generation(self):
        # BTC Trade Plan
        plan = margin.margin_plan(
            equity=5000.0,
            risk_usd=50.0,
            entry=80000.0,
            stop=78400.0,  # $1,600 stop (2.0% stop dist)
            side="long",
            base="BTC",
            funding_8h=0.0001,
            hold_hours=24.0,
            venue="perp",
        )
        self.assertTrue(plan["ok"])
        self.assertEqual(plan["side"], "long")
        self.assertEqual(plan["risk_usd"], 50.0)
        self.assertGreater(plan["qty"], 0.0)
        self.assertLessEqual(plan["leverage"], 5.0)
        self.assertGreater(plan["liq_to_stop_ratio"], 2.0)
        self.assertIn("liquidation_price", plan)


if __name__ == "__main__":
    unittest.main()
