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
        self.assertIn("bankruptcy_price", plan)
        self.assertIn("maintenance_margin_usd", plan)
        self.assertIn("liquidation_clearance_fee_usd", plan)
        self.assertIn("carry_adjusted_liq_price", plan)
        self.assertIn("margin_call_price", plan)
        self.assertIn("adl_risk_quantile", plan)

    def test_tiered_brackets_and_maintenance_margin(self):
        # Tier 1 BTC (< 50,000 USDT): MMR = 0.004, CUM = 0
        brk1 = margin.get_tier_bracket("BTC", 25000.0)
        self.assertEqual(brk1["mmr"], 0.004)
        self.assertEqual(brk1["cum"], 0.0)
        mm1 = margin.maintenance_margin(25000.0, "BTC")
        self.assertEqual(mm1, 100.0)

        # Tier 2 BTC (50,000 to 250,000 USDT): MMR = 0.005, CUM = 50.0
        brk2 = margin.get_tier_bracket("BTC", 100000.0)
        self.assertEqual(brk2["mmr"], 0.005)
        self.assertEqual(brk2["cum"], 50.0)
        mm2 = margin.maintenance_margin(100000.0, "BTC")
        self.assertEqual(mm2, 450.0)

        # Tier 1 Altcoin (< 5,000 USDT): MMR = 0.020, CUM = 0
        brk_alt = margin.get_tier_bracket("DOGE", 3000.0)
        self.assertEqual(brk_alt["mmr"], 0.020)
        self.assertEqual(brk_alt["cum"], 0.0)
        mm_alt = margin.maintenance_margin(3000.0, "DOGE")
        self.assertEqual(mm_alt, 60.0)

    def test_bankruptcy_price(self):
        # Long entry $80,000, 5x leverage -> bankrupt at 80000 * (1 - 1/5) = $64,000
        bp_long = margin.bankruptcy_price(80000.0, "long", 5.0)
        self.assertEqual(bp_long, 64000.0)

        # Short entry $80,000, 5x leverage -> bankrupt at 80000 * (1 + 1/5) = $96,000
        bp_short = margin.bankruptcy_price(80000.0, "short", 5.0)
        self.assertEqual(bp_short, 96000.0)

        # Liquidation triggers strictly before bankruptcy price
        liq_long = margin.liquidation_price(80000.0, "long", 5.0, venue="perp", base="BTC")
        self.assertGreater(liq_long, bp_long)

        liq_short = margin.liquidation_price(80000.0, "short", 5.0, venue="perp", base="BTC")
        self.assertLess(liq_short, bp_short)

    def test_liquidation_clearance_fee(self):
        # BTC $50,000 notional: 1.0% fee
        fee_btc = margin.liquidation_clearance_fee(50000.0, "BTC")
        self.assertEqual(fee_btc, 500.0)

        # Altcoin $10,000 notional: 2.0% fee
        fee_alt = margin.liquidation_clearance_fee(10000.0, "DOGE")
        self.assertEqual(fee_alt, 200.0)

    def test_mark_last_basis(self):
        # Normal basis: 0.1% divergence
        b_norm = margin.mark_last_basis(80080.0, 80000.0)
        self.assertEqual(b_norm["risk_level"], "NORMAL")
        self.assertTrue(b_norm["safe"])

        # Extreme dislocation: 1.0% divergence (wick risk / illiquidity)
        b_ext = margin.mark_last_basis(80800.0, 80000.0)
        self.assertEqual(b_ext["risk_level"], "EXTREME_DISLOCATION")
        self.assertFalse(b_ext["safe"])

    def test_carry_adjusted_liquidation_price(self):
        # BTC Long at $80,000, 5x leverage, 72h hold with positive funding rate (0.0005 per 8h)
        base_liq = margin.liquidation_price(80000.0, "long", 5.0, venue="perp", base="BTC")
        adj_liq = margin.carry_adjusted_liquidation_price(
            80000.0, "long", 5.0, hold_hours=72.0, funding_8h=0.0005, venue="perp", base="BTC"
        )
        self.assertGreater(adj_liq, base_liq)

    def test_position_health_states(self):
        # Long at $80,000, 5x leverage, liq is $64,320
        # 1. Healthy state at $81,000
        h_good = margin.position_health(80000.0, 81000.0, "long", 5.0, venue="margin", base="BTC")
        self.assertEqual(h_good["status"], "HEALTHY")

        # 2. Margin call state around $70,000 (buffer remaining between 20% and 50%)
        h_call = margin.position_health(80000.0, 70000.0, "long", 5.0, venue="perp", base="BTC")
        self.assertEqual(h_call["status"], "MARGIN_CALL")

        # 3. Liquidated state at or below $64,000
        h_liq = margin.position_health(80000.0, 64000.0, "long", 5.0, venue="margin", base="BTC")
        self.assertEqual(h_liq["status"], "LIQUIDATED")

    def test_adl_priority_quantile(self):
        # Low leverage (2x) and low profit -> Quantile 1 (safest)
        adl_safe = margin.adl_priority_quantile(2.0, 0.05)
        self.assertEqual(adl_safe["quantile"], 1)

        # High leverage (20x) and massive profit -> Quantile 4 or 5
        adl_risky = margin.adl_priority_quantile(20.0, 0.50)
        self.assertGreaterEqual(adl_risky["quantile"], 4)

    def test_validate_margin_mode(self):
        iso = margin.validate_margin_mode("perp", "isolated")
        self.assertTrue(iso["institutional_approved"])

        cross = margin.validate_margin_mode("perp", "cross")
        self.assertFalse(cross["institutional_approved"])

    def test_macro_calendar_cpi_nfp(self):
        # 2026-06-10 8:30 AM NY is a CPI release
        cpi_ms = margin.cpi_decision_ms("2026-06-10")
        ev_block = margin.event_risk(cpi_ms - int(0.5 * margin.HOUR_MS))
        self.assertEqual(ev_block["level"], "BLOCK")


if __name__ == "__main__":
    unittest.main()
