#!/usr/bin/env python3
"""Unit tests for the Institutional Crypto Historical Backtester (trader/backtest_crypto.py)."""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TRADER = os.path.join(ROOT, "trader")
if TRADER not in sys.path:
    sys.path.insert(0, TRADER)

import backtest_crypto


class TestCryptoBacktester(unittest.TestCase):
    def setUp(self):
        # Create deterministic synthetic 1h klines
        self.bars = []
        base_price = 100.0
        start_t = 1700000000000
        for i in range(200):
            t = start_t + i * 3600000
            # Small sine wave price path
            o = base_price + (i % 10) * 0.5
            h = o + 1.0
            l = o - 0.8
            c = o + 0.2
            qv = 100000.0
            tb_q = 55000.0 if i % 2 == 0 else 45000.0
            self.bars.append({
                "t": t, "ct": t + 3599999,
                "o": o, "h": h, "l": l, "c": c,
                "vol_usd": qv, "tb_usd": tb_q,
                "tb": (tb_q / qv) * 100.0
            })

    def test_indicators_computation(self):
        closes = [b["c"] for b in self.bars]
        e20 = backtest_crypto.ema(closes, 20)
        self.assertEqual(len(e20), len(closes))
        self.assertAlmostEqual(e20[0], closes[0])

        a = backtest_crypto.atr(self.bars, 14)
        self.assertEqual(len(a), len(self.bars))
        self.assertGreater(a[-1], 0.0)

        vwap = backtest_crypto.compute_vwap(self.bars, 24)
        self.assertIsNotNone(vwap)
        self.assertGreater(vwap, 0.0)

    def test_volume_profile_calculation(self):
        vp = backtest_crypto.compute_volume_profile(self.bars, window=168, bins=35)
        self.assertIn("poc", vp)
        self.assertIn("val", vp)
        self.assertIn("vah", vp)
        self.assertIsNotNone(vp["poc"])
        self.assertGreaterEqual(vp["vah"], vp["poc"])
        self.assertLessEqual(vp["val"], vp["poc"])

    def test_signal_generation(self):
        closes = [b["c"] for b in self.bars]
        e20 = backtest_crypto.ema(closes, 20)
        e50 = backtest_crypto.ema(closes, 50)
        a = backtest_crypto.atr(self.bars, 14)

        # Before warmup index 168, must return None
        sig_early = backtest_crypto.evaluate_signals_for_bar(self.bars, 50, e20, e50, a)
        self.assertIsNone(sig_early)

        # Craft an artificial Wyckoff Spring on the last bar
        last_idx = len(self.bars) - 1
        hist = self.bars[max(0, last_idx - 168):last_idx + 1]
        vp = backtest_crypto.compute_volume_profile(hist, 168)
        prior_low = min(b["l"] for b in self.bars[last_idx-6:last_idx])
        ref_low = min(prior_low, vp["val"]) if vp["val"] else prior_low

        self.bars[-1]["l"] = ref_low - 0.2  # sweeps below ref_low
        self.bars[-1]["c"] = ref_low + 0.5  # reclaims above ref_low
        self.bars[-1]["o"] = ref_low + 0.1  # closes green
        self.bars[-1]["tb"] = 60.0          # high absorption

        sig = backtest_crypto.evaluate_signals_for_bar(self.bars, last_idx, e20, e50, a)
        self.assertIsNotNone(sig)
        self.assertEqual(sig["side"], "long")
        self.assertIn("Spring", sig["setup"])
        self.assertLess(sig["stop"], self.bars[-1]["c"])

    def test_simulation_execution_and_metrics(self):
        engine = backtest_crypto.CryptoBacktester(
            symbols=["TESTUSDT"],
            days=10,
            initial_equity=5000.0,
            risk_per_trade=50.0,
            leverage=3.0
        )
        engine.all_klines["TESTUSDT"] = self.bars

        # Simulate
        engine.run()
        metrics = engine.calculate_metrics()
        self.assertIsInstance(metrics, dict)
        if "error" not in metrics:
            self.assertIn("final_equity", metrics)
            self.assertIn("win_rate", metrics)
            self.assertIn("profit_factor", metrics)
            self.assertIn("avg_r", metrics)


if __name__ == "__main__":
    unittest.main()
