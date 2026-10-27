#!/usr/bin/env python3
"""Unit tests for Phase 3: Machine Learning & VPIN Microstructure Suite."""

import math
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from _load import load

microstructure = load("trader-analyst", "microstructure")


class TestMicrostructureVPIN(unittest.TestCase):
    def test_norm_cdf_and_bvc(self):
        # norm_cdf(0) should be 0.5
        self.assertAlmostEqual(microstructure.norm_cdf(0.0), 0.5, places=4)
        self.assertAlmostEqual(microstructure.norm_cdf(1.96), 0.975, places=2)
        self.assertAlmostEqual(microstructure.norm_cdf(-1.96), 0.025, places=2)

        # Bulk Volume Classification
        b_vol, s_vol = microstructure.classify_trade_flow(1.0, 1.0, 100.0)
        self.assertTrue(b_vol > 50.0)
        self.assertTrue(s_vol < 50.0)
        self.assertAlmostEqual(b_vol + s_vol, 100.0, places=4)

    def test_vpin_calculation_synthetic_balanced(self):
        # Perfectly balanced trade stream (50% buy, 50% sell)
        # Should yield low VPIN (~0.0)
        records = []
        for i in range(100):
            records.append({
                "price": 100.0 + (i % 2) * 0.1,
                "volume": 10.0,
                "buy_vol": 5.0,
                "sell_vol": 5.0,
            })
        res = microstructure.compute_vpin(records, num_buckets=10)
        self.assertAlmostEqual(res["vpin"], 0.0, places=2)
        self.assertEqual(res["regime"], "LOW_TOXICITY")
        self.assertEqual(res["toxicity_status"], "UNINFORMED_FLOW_DOMINANT")

    def test_vpin_calculation_synthetic_toxic(self):
        # Extremely one-sided informed flow (100% buy)
        # Should yield VPIN close to 1.0 (100% toxicity)
        records = []
        for i in range(100):
            records.append({
                "price": 100.0 + i * 0.5,
                "volume": 20.0,
                "buy_vol": 20.0,
                "sell_vol": 0.0,
            })
        res = microstructure.compute_vpin(records, num_buckets=10)
        self.assertAlmostEqual(res["vpin"], 1.0, places=2)
        self.assertEqual(res["regime"], "TOXIC_SURGE")
        self.assertEqual(res["adverse_selection_risk"], "CRITICAL")
        self.assertIn("toxicity alert", res["warning"].lower())

    def test_vpin_with_kline_format(self):
        # Binance kline format list:
        # [open_time, open, high, low, close, volume, close_time, quote_vol, trades, taker_buy_base, ...]
        klines = []
        for i in range(50):
            # 60% buy, 40% sell
            klines.append([
                1700000000000 + i * 60000,
                "100.0", "101.0", "99.0", "100.5",
                "100.0",  # volume
                1700000059999,
                "10050.0",
                50,
                "60.0",   # taker_buy_base (60%)
                "6030.0"
            ])
        res = microstructure.compute_vpin(klines, num_buckets=10)
        # 60 buy vs 40 sell -> imbalance = 20 per 100 vol -> VPIN = 0.20
        self.assertAlmostEqual(res["vpin"], 0.20, places=2)
        self.assertEqual(res["regime"], "LOW_TOXICITY")


class TestWyckoffCVDDivergence(unittest.TestCase):
    def test_wyckoff_spring_detection(self):
        # Generate 30 bars of 1h klines
        # Range high 105, Range low 95.
        # Bar 28 wicks down to 93 (sweep below 95), but bar 29-30 closes back at 96 with 65% buy volume
        klines = []
        for i in range(25):
            klines.append([
                i, "100", "102", "98", "100", "1000", i, "100000", 100, "500", "50000"
            ])
        # Prior swing low is 98. Add a sweep down to 94 then reclaim 99
        klines.append([25, "99", "100", "98", "98.5", "1000", 25, "98500", 100, "480", "48000"])
        klines.append([26, "98.5", "99", "94.0", "97.0", "2500", 26, "242500", 200, "1600", "155200"])
        klines.append([27, "97.0", "100", "96.5", "99.5", "1800", 27, "179100", 150, "1200", "119400"])

        vp = {"val": 98.0, "poc": 100.0, "vah": 102.0}
        res = microstructure.detect_wyckoff_cvd_divergence(klines, vp=vp, current_price=99.5)
        self.assertTrue(len(res["patterns"]) > 0)
        spring_pats = [p for p in res["patterns"] if p["pattern"] == "WYCKOFF_SPRING"]
        self.assertTrue(len(spring_pats) >= 1)
        self.assertEqual(spring_pats[0]["type"], "BULLISH_LIQUIDITY_SWEEP")
        self.assertTrue(res["bonus_score"] > 0)

    def test_wyckoff_upthrust_detection(self):
        # Range high 102.
        # Recent bar spikes to 105 then rejects back down to 101 with collapsed taker buy
        klines = []
        for i in range(25):
            klines.append([
                i, "100", "102", "98", "100", "1000", i, "100000", 100, "500", "50000"
            ])
        klines.append([25, "101", "102.5", "100.5", "101.5", "1000", 25, "101500", 100, "520", "52000"])
        klines.append([26, "101.5", "106.0", "101.0", "101.8", "3000", 26, "305400", 250, "1200", "122160"])
        klines.append([27, "101.8", "102.0", "100.5", "100.8", "2000", 27, "201600", 180, "800", "80640"])

        vp = {"val": 98.0, "poc": 100.0, "vah": 102.0}
        res = microstructure.detect_wyckoff_cvd_divergence(klines, vp=vp, current_price=100.8)
        upthrust_pats = [p for p in res["patterns"] if p["pattern"] == "WYCKOFF_UPTHRUST"]
        self.assertTrue(len(upthrust_pats) >= 1)
        self.assertEqual(upthrust_pats[0]["type"], "BEARISH_STOP_HUNT_SFP")
        self.assertTrue(res["bonus_score"] < 0)


class TestVolatilityAdaptiveKelly(unittest.TestCase):
    def test_adaptive_kelly_baseline(self):
        # Normal baseline: win_prob=0.55, rr=2.0 -> Full Kelly = 0.55 - 0.45/2.0 = 0.325
        # Base Quarter Kelly = 8.125% -> capped at 5.0%
        # Let's test with p=0.50, rr=2.0 -> Full Kelly = 0.50 - 0.50/2.0 = 0.25
        # Quarter Kelly = 6.25%
        res = microstructure.compute_adaptive_kelly(
            win_prob=0.50,
            reward_risk=2.0,
            atr_1h=2.0,
            atr_baseline=2.0,
            bb_width_pct=6.0,
            is_ttm_squeeze=False,
            vpin=0.30,
            bias="LONG",
            capital_usdt=10000.0,
            max_loss_cap=150.0,
        )
        self.assertAlmostEqual(res["base_full_kelly"], 0.25, places=2)
        self.assertEqual(res["squeeze_multiplier"], 1.0)
        self.assertEqual(res["atr_multiplier"], 1.0)
        self.assertEqual(res["vpin_multiplier"], 1.0)
        self.assertEqual(res["composite_multiplier"], 1.0)

    def test_adaptive_kelly_ttm_squeeze_boost(self):
        # In TTM Squeeze, directional trade should receive 1.20x boost
        res = microstructure.compute_adaptive_kelly(
            win_prob=0.45,
            reward_risk=2.0,
            atr_1h=2.0,
            atr_baseline=2.0,
            bb_width_pct=3.0,
            is_ttm_squeeze=True,
            vpin=0.30,
            bias="LONG",
        )
        self.assertEqual(res["squeeze_multiplier"], 1.20)
        self.assertTrue(res["adaptive_kelly_pct"] > res["base_quarter_kelly_pct"])

    def test_adaptive_kelly_volatility_shock_dampening(self):
        # ATR expands by 2.25x (shock) -> multiplier should dampen sizing (1 / sqrt(2.25) = 1 / 1.5 = 0.67x)
        res = microstructure.compute_adaptive_kelly(
            win_prob=0.55,
            reward_risk=2.0,
            atr_1h=4.5,
            atr_baseline=2.0,
            bb_width_pct=7.0,
            is_ttm_squeeze=False,
            vpin=0.35,
            bias="LONG",
        )
        self.assertAlmostEqual(res["atr_multiplier"], 0.667, places=2)
        self.assertTrue(res["adaptive_kelly_pct"] < res["base_quarter_kelly_pct"])

    def test_adaptive_kelly_vpin_toxic_counter_penalty(self):
        # Toxic surge (vpin=0.75) while attempting a counter-trend range scalp
        res = microstructure.compute_adaptive_kelly(
            win_prob=0.50,
            reward_risk=2.0,
            atr_1h=2.0,
            atr_baseline=2.0,
            bb_width_pct=6.0,
            is_ttm_squeeze=False,
            vpin=0.75,
            bias="WAIT / RANGE SCALP",
        )
        self.assertEqual(res["vpin_multiplier"], 0.50)
        self.assertTrue(res["adaptive_kelly_pct"] <= res["base_quarter_kelly_pct"] * 0.55)


if __name__ == "__main__":
    unittest.main()
