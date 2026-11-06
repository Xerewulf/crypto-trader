import json
import math
import os
import sys
import tempfile
import unittest

# Ensure repo root and trader-analyst are on sys.path
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
ANALYST_DIR = os.path.join(REPO_ROOT, "trader-analyst")
if ANALYST_DIR not in sys.path:
    sys.path.insert(0, ANALYST_DIR)

import predict
import test_accuracy


class TestPredictionIntervals(unittest.TestCase):
    def setUp(self):
        # Generate synthetic 1h and 5m klines for deterministic testing
        self.price = 75000.0
        self.k1h = []
        p = 74000.0
        for i in range(50):
            p = p * (1.0 + (0.001 if i % 2 == 0 else -0.0008))
            self.k1h.append([i * 3600000, str(p - 50), str(p + 50), str(p - 60), str(p), "1000", (i + 1) * 3600000, "75000000", 500, "500", "37500000", "0"])

        self.k5m = []
        p = 74800.0
        for i in range(120):
            p = p * (1.0 + (0.0003 if i % 2 == 0 else -0.0002))
            self.k5m.append([i * 300000, str(p - 20), str(p + 20), str(p - 25), str(p), "200", (i + 1) * 300000, "15000000", 100, "100", "7500000", "0"])

    def test_simulate_horizons_includes_10m_30m_60m(self):
        horizons = predict.simulate_horizons(self.k1h, self.price, composite_score=15.0, seed=42, klines_5m=self.k5m)
        self.assertIn("10m", horizons)
        self.assertIn("30m", horizons)
        self.assertIn("60m", horizons)
        self.assertIn("4h", horizons)
        self.assertIn("24h", horizons)
        self.assertIn("7d", horizons)

        # Check structure of 10m horizon
        h10 = horizons["10m"]
        self.assertEqual(h10["horizon"], "10m")
        self.assertEqual(h10["minutes"], 10)
        self.assertAlmostEqual(h10["hours"], 10 / 60.0, places=3)
        self.assertTrue(0.0 <= h10["prob_up_pct"] <= 100.0)
        self.assertTrue(0.0 <= h10["prob_down_pct"] <= 100.0)
        self.assertAlmostEqual(h10["prob_up_pct"] + h10["prob_down_pct"], 100.0, places=1)
        self.assertIn(h10["direction"], ("UP", "DOWN", "SIDEWAYS / COIN FLIP"))
        self.assertTrue(h10["range_80_low"] <= h10["range_50_low"] <= h10["expected_price"] <= h10["range_50_high"] <= h10["range_80_high"])

    def test_record_predictions_writes_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            rec_path = os.path.join(tmp, "prediction_records.json")
            sample_result = {
                "symbol": "BTCUSDT",
                "as_of": "2026-11-06T10:00:00Z",
                "price": 75000.0,
                "change_24h_pct": 1.25,
                "composite_score": 12.0,
                "verdict": "BULLISH",
                "horizons": predict.simulate_horizons(self.k1h, self.price, composite_score=12.0, seed=42, klines_5m=self.k5m)
            }
            predict.record_predictions([sample_result], path=rec_path)
            self.assertTrue(os.path.exists(rec_path))
            with open(rec_path, "r", encoding="utf-8") as fh:
                records = json.load(fh)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["symbol"], "BTCUSDT")
            self.assertIn("10m", records[0]["horizons"])
            self.assertIn("30m", records[0]["horizons"])
            self.assertIn("60m", records[0]["horizons"])
            self.assertIn("target_timestamp", records[0]["horizons"]["10m"])

    def test_evaluate_asset_accuracy_metrics(self):
        # Build 150 candles of 5m data to evaluate walk-forward accuracy
        synthetic_5m = []
        p = 70000.0
        for i in range(160):
            # Oscillating trend with slight upward drift
            p = p * (1.0004 if (i % 4 != 0) else 0.9990)
            synthetic_5m.append([i * 300000, str(p - 10), str(p + 15), str(p - 15), str(p), "100", (i + 1) * 300000, "7000000", 50, "60", "4200000", "0"])

        summary = test_accuracy.evaluate_asset_accuracy("BTC", klines=synthetic_5m, n_eval_points=20, lookback_window=50)
        self.assertIn("horizons", summary)
        for h in ("10m", "30m", "60m"):
            hz = summary["horizons"][h]
            self.assertIn("hit_rate_pct", hz)
            self.assertIn("band_80_coverage_pct", hz)
            self.assertIn("band_50_coverage_pct", hz)
            self.assertIn("mae_usd", hz)
            self.assertIn("mape_pct", hz)
            self.assertTrue(0.0 <= hz["band_80_coverage_pct"] <= 100.0)


if __name__ == "__main__":
    unittest.main()
