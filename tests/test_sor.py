#!/usr/bin/env python3
"""Unit tests for Phase 4: Smart Order Router (SOR), Circuit Breakers & Deribit GEX."""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from _load import load

sor = load("trader-analyst", "sor")


class TestSmartOrderRouter(unittest.TestCase):
    def test_depth_slippage_calculation(self):
        # Asks: 100 @ 10, 101 @ 10, 102 @ 10
        # Total available at first 2 levels: 100*10 + 101*10 = 1000 + 1010 = 2010 USD
        asks = [[100.0, 10.0], [101.0, 10.0], [102.0, 10.0]]
        # Order $500 should fill completely at 100.0 with 0 slippage
        res_small = sor.estimate_depth_slippage(asks, notional_usd=500.0, side="BUY")
        self.assertAlmostEqual(res_small["vwap_price"], 100.0, places=2)
        self.assertAlmostEqual(res_small["slippage_usd"], 0.0, places=2)

        # Order $1500: fills $1000 @ 100 (10 qty) + $500 @ 101 (4.95 qty)
        res_walk = sor.estimate_depth_slippage(asks, notional_usd=1500.0, side="BUY")
        self.assertTrue(res_walk["vwap_price"] > 100.0)
        self.assertTrue(res_walk["slippage_usd"] > 0.0)
        self.assertTrue(res_walk["slippage_bps"] > 0.0)

    def test_venue_cost_comparison(self):
        # Binance taker 0.04%, Bybit taker 0.055%
        cost_bin = sor.calculate_venue_cost("BINANCE", "BNBUSDT", "BUY", 10000.0, 740.0, 740.1, funding_rate_8h=0.0001)
        cost_byb = sor.calculate_venue_cost("BYBIT", "BNBUSDT", "BUY", 10000.0, 740.0, 740.1, funding_rate_8h=0.0001)
        self.assertIn("total_cost_usd", cost_bin)
        self.assertIn("total_cost_usd", cost_byb)
        # Bybit has higher taker fee so total cost on market order should be higher
        self.assertGreater(cost_byb["fee_usd"], cost_bin["fee_usd"])

    def test_smart_route_order_selection(self):
        quotes = {
            "BINANCE": {"bid": 740.0, "ask": 740.05, "funding_rate_8h": 0.0002},
            "BYBIT": {"bid": 739.9, "ask": 740.02, "funding_rate_8h": 0.00005},
            "OKX": {"bid": 739.8, "ask": 740.15, "funding_rate_8h": 0.00025},
        }
        routed = sor.smart_route_order("BNBUSDT", "BUY", notional=2000.0, venue_quotes=quotes)
        self.assertIn("primary_venue", routed)
        self.assertIn(routed["primary_venue"], ["BINANCE", "BYBIT", "OKX"])
        self.assertEqual(routed["recommended_action"], "EXECUTE_SINGLE_VENUE")
        self.assertTrue(len(routed["routing_split"]) >= 1)

    def test_smart_route_large_order_split(self):
        quotes = {
            "BINANCE": {"bid": 740.0, "ask": 740.05, "funding_rate_8h": 0.0001},
            "BYBIT": {"bid": 740.0, "ask": 740.06, "funding_rate_8h": 0.0001},
            "OKX": {"bid": 739.9, "ask": 740.08, "funding_rate_8h": 0.0001},
        }
        # Large order >= 5000 should trigger multi-venue split
        routed_large = sor.smart_route_order("BNBUSDT", "BUY", notional=10000.0, venue_quotes=quotes)
        self.assertEqual(routed_large["recommended_action"], "SMART_MULTI_VENUE_SPLIT")
        self.assertEqual(len(routed_large["routing_split"]), 3)
        total_split = sum(s["notional_usd"] for s in routed_large["routing_split"])
        self.assertAlmostEqual(total_split, 10000.0, places=1)


class TestCircuitBreakers(unittest.TestCase):
    def test_clear_circuit_breaker(self):
        wallet = {
            "starting_cash": 10000.0,
            "cash_usdt": 10000.0,
            "hwm_equity": 10000.0,
            "positions": {},
            "history": [],
        }
        res = sor.evaluate_circuit_breakers(wallet)
        self.assertEqual(res["status"], "CLEAR")
        self.assertTrue(res["allowed_to_trade"])
        self.assertFalse(res["circuit_breaker_active"])
        self.assertAlmostEqual(res["drawdown_pct"], 0.0, places=2)

    def test_hard_drawdown_circuit_breaker(self):
        # 3.5% drawdown ($10,000 -> $9,650)
        wallet = {
            "starting_cash": 10000.0,
            "cash_usdt": 9650.0,
            "hwm_equity": 10000.0,
            "positions": {},
            "history": [],
        }
        res = sor.evaluate_circuit_breakers(wallet)
        self.assertEqual(res["status"], "HARD_CIRCUIT_BREAKER_ACTIVE")
        self.assertFalse(res["allowed_to_trade"])
        self.assertTrue(res["circuit_breaker_active"])
        self.assertGreaterEqual(res["drawdown_pct"], 3.0)
        self.assertTrue(any("DRAWDOWN LIMIT EXCEEDED" in r for r in res["reasons"]))

    def test_caution_drawdown(self):
        # 2.0% drawdown ($10,000 -> $9,800)
        wallet = {
            "starting_cash": 10000.0,
            "cash_usdt": 9800.0,
            "hwm_equity": 10000.0,
            "positions": {},
            "history": [],
        }
        res = sor.evaluate_circuit_breakers(wallet)
        self.assertEqual(res["status"], "CAUTION_DRAWDOWN")
        self.assertTrue(res["allowed_to_trade"])
        self.assertFalse(res["circuit_breaker_active"])

    def test_consecutive_stop_run_cooloff(self):
        # 2 consecutive stops in history
        wallet = {
            "starting_cash": 10000.0,
            "cash_usdt": 9900.0,
            "hwm_equity": 10000.0,
            "positions": {},
            "history": [
                {"action": "CLOSE LONG", "reason": "Stop-Loss triggered", "realized_pnl": -50.0},
                {"action": "CLOSE SHORT", "reason": "Stop-Loss hit", "realized_pnl": -50.0},
            ],
        }
        res = sor.evaluate_circuit_breakers(wallet)
        self.assertEqual(res["status"], "CONSECUTIVE_STOP_RUN_COOLOFF")
        self.assertFalse(res["allowed_to_trade"])
        self.assertTrue(res["cooling_off"])

    def test_gross_leverage_breach(self):
        # Equity = $10,000, but open notional = $25,000 (2.5x > 2.0x limit)
        wallet = {
            "starting_cash": 10000.0,
            "cash_usdt": 7500.0,
            "hwm_equity": 10000.0,
            "positions": {
                "BNBUSDT": {
                    "margin_usdt": 2500.0,
                    "usdt_total": 25000.0,
                    "unrealized_pnl_usdt": 0.0,
                }
            },
            "history": [],
        }
        res = sor.evaluate_circuit_breakers(wallet)
        self.assertEqual(res["status"], "GROSS_LEVERAGE_BREACH")
        self.assertFalse(res["allowed_to_trade"])
        self.assertGreater(res["gross_leverage"], 2.0)


class TestDeribitMaxPain(unittest.TestCase):
    def test_max_pain_calculation(self):
        # Simple options chain:
        # Calls at 90 (OI=10), 100 (OI=20), 110 (OI=10)
        # Puts at 90 (OI=10), 100 (OI=20), 110 (OI=10)
        # Symmetric around 100 -> Max pain strike must be 100
        chain = [
            {"strike": 90.0, "type": "C", "open_interest": 10.0},
            {"strike": 100.0, "type": "C", "open_interest": 20.0},
            {"strike": 110.0, "type": "C", "open_interest": 10.0},
            {"strike": 90.0, "type": "P", "open_interest": 10.0},
            {"strike": 100.0, "type": "P", "open_interest": 20.0},
            {"strike": 110.0, "type": "P", "open_interest": 10.0},
        ]
        res = sor.calculate_max_pain(chain, spot_price=100.0)
        self.assertEqual(res["max_pain_strike"], 100.0)
        self.assertAlmostEqual(res["put_call_ratio"], 1.0, places=2)
        self.assertAlmostEqual(res["dist_to_max_pain_pct"], 0.0, places=2)
        self.assertIn("HIGH_PINNING_MAGNET", res["pinning_hazard"])

    def test_deribit_analytics_fetch(self):
        analytics = sor.fetch_deribit_options_analytics("BTC", spot_price=68000.0)
        self.assertEqual(analytics["currency"], "BTC")
        self.assertGreater(analytics["max_pain_strike"], 0)
        self.assertIn("dealer_net_gamma_usd_m", analytics)
        self.assertIn("dealer_gamma_regime", analytics)
        self.assertIn("hours_to_monthly_expiry", analytics)


if __name__ == "__main__":
    unittest.main()
