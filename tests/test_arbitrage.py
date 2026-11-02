#!/usr/bin/env python3
"""Unit tests for Phase 2: Delta-Neutral Funding & Cash-Carry Arbitrage."""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from _load import load

arbitrage = load("trader-analyst", "arbitrage")


class TestDeltaNeutralArbitrage(unittest.TestCase):
    def test_basis_calculation(self):
        # Perp higher than Spot (Contango)
        res = arbitrage.calculate_basis(100.0, 101.0)
        self.assertEqual(res["spot_price"], 100.0)
        self.assertEqual(res["perp_mark"], 101.0)
        self.assertEqual(res["basis_abs"], 1.0)
        self.assertEqual(res["basis_pct"], 1.0)

        # Spot higher than Perp (Backwardation)
        res2 = arbitrage.calculate_basis(600.0, 597.0)
        self.assertEqual(res2["basis_abs"], -3.0)
        self.assertEqual(res2["basis_pct"], -0.5)

    def test_funding_to_apy(self):
        # 0.01% / 8h is standard neutral rate
        apy_baseline = arbitrage.funding_to_apy(0.0001)
        self.assertAlmostEqual(apy_baseline, 10.95, places=2)

        # 0.05% / 8h
        apy_high = arbitrage.funding_to_apy(0.0005)
        self.assertAlmostEqual(apy_high, 54.75, places=2)

        # -0.30% / 8h deep negative
        apy_deep = arbitrage.funding_to_apy(0.0030)
        self.assertAlmostEqual(apy_deep, 328.5, places=2)

    def test_classify_opportunity(self):
        # Positive funding -> Cash & Carry
        opp_cc = arbitrage.classify_arbitrage_opportunity("BNBUSDT", 600.0, 600.5, 0.0003)
        self.assertEqual(opp_cc["strategy"], "CASH_AND_CARRY")
        self.assertEqual(opp_cc["spot_action"], "BUY_LONG")
        self.assertEqual(opp_cc["perp_action"], "SELL_SHORT")
        self.assertTrue(opp_cc["viable"])
        self.assertFalse(opp_cc["is_deep_squeeze"])

        # Negative funding -> Reverse Arbitrage
        opp_rev = arbitrage.classify_arbitrage_opportunity("ETHUSDT", 2500.0, 2498.0, -0.0008)
        self.assertEqual(opp_rev["strategy"], "REVERSE_ARBITRAGE")
        self.assertEqual(opp_rev["spot_action"], "SELL_SHORT")
        self.assertEqual(opp_rev["perp_action"], "BUY_LONG")
        self.assertTrue(opp_rev["viable"])
        self.assertFalse(opp_rev["is_deep_squeeze"])

        # Deep negative funding (< -0.30%/8h) -> Deep Negative Squeeze Harvester
        opp_squeeze = arbitrage.classify_arbitrage_opportunity("PEPEUSDT", 0.00001, 0.00001, -0.0035)
        self.assertEqual(opp_squeeze["strategy"], "REVERSE_ARBITRAGE")
        self.assertEqual(opp_squeeze["regime"], "DEEP_NEGATIVE_SQUEEZE")
        self.assertTrue(opp_squeeze["is_deep_squeeze"])

        # Flat / Neutral
        opp_flat = arbitrage.classify_arbitrage_opportunity("SOLUSDT", 150.0, 150.01, 0.00002)
        self.assertEqual(opp_flat["strategy"], "NEUTRAL")
        self.assertFalse(opp_flat["viable"])

    def test_build_delta_neutral_pair_cash_and_carry(self):
        cap = 2000.0  # $2000 total capital
        spot_p = 500.0
        perp_p = 500.2
        fr = 0.0002  # +0.02% / 8h (+21.9% APY)
        pair = arbitrage.build_delta_neutral_pair(
            symbol="BNBUSDT",
            strategy="CASH_AND_CARRY",
            capital_usdt=cap,
            spot_price=spot_p,
            perp_mark=perp_p,
            funding_rate_8h=fr,
        )

        self.assertEqual(pair["strategy"], "CASH_AND_CARRY")
        self.assertEqual(pair["spot_leg"]["side"], "BUY")
        self.assertEqual(pair["perp_leg"]["side"], "SELL")

        # Spot capital is $1000 -> 2 BNB
        self.assertEqual(pair["spot_leg"]["qty"], 2.0)
        self.assertEqual(pair["perp_leg"]["qty"], 2.0)

        # Net Delta should be near $0.00
        net_delta = pair["delta"]["net_delta_usdt"]
        self.assertTrue(abs(net_delta) < 1.0)
        self.assertTrue(pair["delta"]["is_delta_neutral"])

        # Projected 8h funding: 2 * 500.20 * 0.0002 = 0.20008 USDT
        self.assertAlmostEqual(pair["metrics"]["gross_8h_funding_usdt"], 0.2001, places=3)
        self.assertAlmostEqual(pair["metrics"]["annualized_apy"], 21.9, places=1)

    def test_build_delta_neutral_pair_reverse_arbitrage(self):
        cap = 5000.0
        spot_p = 2500.0
        perp_p = 2495.0
        fr = -0.0010  # -0.10% / 8h (-109.5% APY)
        pair = arbitrage.build_delta_neutral_pair(
            symbol="ETHUSDT",
            strategy="REVERSE_ARBITRAGE",
            capital_usdt=cap,
            spot_price=spot_p,
            perp_mark=perp_p,
            funding_rate_8h=fr,
        )

        self.assertEqual(pair["strategy"], "REVERSE_ARBITRAGE")
        self.assertEqual(pair["spot_leg"]["side"], "SELL")
        self.assertEqual(pair["perp_leg"]["side"], "BUY")
        self.assertEqual(pair["spot_leg"]["qty"], 1.0)
        self.assertEqual(pair["perp_leg"]["qty"], 1.0)
        self.assertTrue(pair["delta"]["is_delta_neutral"])

    def test_market_movement_delta_neutrality_and_pnl(self):
        # Build Cash & Carry pair
        cap = 2000.0
        spot_entry = 100.0
        perp_entry = 100.0
        fr = 0.0003  # 0.03% / 8h
        pair = arbitrage.build_delta_neutral_pair(
            symbol="TESTUSDT",
            strategy="CASH_AND_CARRY",
            capital_usdt=cap,
            spot_price=spot_entry,
            perp_mark=perp_entry,
            funding_rate_8h=fr,
        )

        # Scenario 1: Price pumps +20% (Spot = 120, Perp = 120) with 0 elapsed seconds
        pnl_pump = arbitrage.compute_pair_realtime_pnl(
            pair, current_spot_price=120.0, current_perp_mark=120.0, elapsed_seconds=0
        )
        self.assertEqual(pnl_pump["spot_pnl_usdt"], 200.0)    # 10 Spot bought at 100 -> +$200
        self.assertEqual(pnl_pump["perp_pnl_usdt"], -200.0)   # 10 Perp sold at 100 -> -$200
        self.assertEqual(pnl_pump["basis_pnl_usdt"], 0.0)     # Perfectly offset!
        self.assertTrue(pnl_pump["is_delta_neutral"])

        # Scenario 2: Price dumps -30% (Spot = 70, Perp = 70)
        pnl_dump = arbitrage.compute_pair_realtime_pnl(
            pair, current_spot_price=70.0, current_perp_mark=70.0, elapsed_seconds=0
        )
        self.assertEqual(pnl_dump["spot_pnl_usdt"], -300.0)   # Spot down $300
        self.assertEqual(pnl_dump["perp_pnl_usdt"], 300.0)    # Perp short up $300
        self.assertEqual(pnl_dump["basis_pnl_usdt"], 0.0)     # Perfectly offset!

        # Scenario 3: Time advances 8 hours (28,800s) -> One full funding payment accrued
        pnl_8h = arbitrage.compute_pair_realtime_pnl(
            pair, current_spot_price=100.0, current_perp_mark=100.0, elapsed_seconds=28800
        )
        # 1000 notional * 0.0003 = 0.30 USDT
        self.assertAlmostEqual(pnl_8h["accrued_funding_usdt"], 0.30, places=2)
        self.assertTrue(pnl_8h["total_net_pnl_usdt"] > -pair["metrics"]["initial_entry_fees_usdt"])

    def test_scan_funding_arbitrage_with_mock_cache(self):
        spot_cache = {
            "BNBUSDT": 600.0,
            "BTCUSDT": 90000.0,
            "ETHUSDT": 2500.0,
            "PEPEUSDT": 0.00001,
        }
        premium_cache = [
            {"symbol": "BNBUSDT", "markPrice": "600.5", "lastFundingRate": "0.0002", "nextFundingTime": 1800000000000},
            {"symbol": "BTCUSDT", "markPrice": "90050.0", "lastFundingRate": "0.0001", "nextFundingTime": 1800000000000},
            {"symbol": "ETHUSDT", "markPrice": "2498.0", "lastFundingRate": "-0.0006", "nextFundingTime": 1800000000000},
            {"symbol": "PEPEUSDT", "markPrice": "0.00001", "lastFundingRate": "-0.0035", "nextFundingTime": 1800000000000},
        ]

        scan_res = arbitrage.scan_funding_arbitrage(
            symbols=["BNBUSDT", "BTCUSDT", "ETHUSDT", "PEPEUSDT"],
            spot_prices_cache=spot_cache,
            premium_indices_cache=premium_cache,
        )

        self.assertEqual(scan_res["total_scanned"], 4)
        self.assertEqual(scan_res["cash_and_carry_opportunities"], 2)  # BNB and BTC
        self.assertEqual(scan_res["reverse_arbitrage_opportunities"], 2)  # ETH and PEPE

        # PEPE is deep negative squeeze (-0.35%/8h) -> should be first in sorted opportunities
        top_opp = scan_res["opportunities"][0]
        self.assertEqual(top_opp["symbol"], "PEPEUSDT")
        self.assertEqual(top_opp["regime"], "DEEP_NEGATIVE_SQUEEZE")
        self.assertTrue(top_opp["is_deep_squeeze"])


if __name__ == "__main__":
    unittest.main()
