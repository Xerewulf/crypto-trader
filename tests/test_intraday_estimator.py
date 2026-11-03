import unittest
from unittest.mock import patch
import os
import sys

from _load import load

intraday_estimator = load("trader-analyst", "intraday_estimator")


class TestIntradayEstimator(unittest.TestCase):
    def test_target_coins(self):
        self.assertEqual(intraday_estimator.TARGET_COINS, [
            "BNBUSDT", "SOLUSDT", "BTCUSDT", "ETHUSDT", "DOGEUSDT", "XRPUSDT"
        ])

    def test_microstructure_score_bounds(self):
        # Mock 1m klines
        # [open_time, open, high, low, close, volume, close_time, quote_volume, trades, taker_buy_base, taker_buy_quote, ignore]
        mock_1m = [
            [i * 60000, "100.0", "101.0", "99.5", str(100.0 + i * 0.1), "10.0", (i + 1) * 60000, "1000.0", 50, "6.0", "600.0", "0"]
            for i in range(35)
        ]
        mock_depth = {
            "bids": [["100.0", "5.0"], ["99.5", "10.0"]],
            "asks": [["100.5", "4.0"], ["101.0", "8.0"]]
        }
        mock_fut = {
            "taker_buy_sell_ratio": 1.15,
            "top_whale_ls_ratio": 1.30
        }

        score = intraday_estimator.compute_intraday_microstructure_score(mock_1m, mock_1m, mock_depth, mock_fut)
        self.assertIsInstance(score, float)
        self.assertGreaterEqual(score, -100.0)
        self.assertLessEqual(score, 100.0)

    def test_simulate_intraday_horizons(self):
        mock_1m = [
            [i * 60000, "100.0", "101.0", "99.5", str(100.0 + (i % 3) * 0.2), "10.0", (i + 1) * 60000, "1000.0", 50, "5.5", "550.0", "0"]
            for i in range(40)
        ]
        horizons = intraday_estimator.simulate_intraday_horizons(mock_1m, 100.0, 25.0, seed=123)
        
        self.assertIn("10m", horizons)
        self.assertIn("30m", horizons)
        self.assertIn("60m", horizons)

        for name in ["10m", "30m", "60m"]:
            h = horizons[name]
            self.assertAlmostEqual(h["prob_up_pct"] + h["prob_down_pct"], 100.0, places=1)
            self.assertIn(h["preferred_action"], ["LONG", "SHORT"])
            self.assertGreater(h["expected_price"], 0)
            
            # LONG outcome checks
            l = h["long_outcome"]
            self.assertEqual(l["action"], "LONG")
            self.assertEqual(l["win_probability_pct"], h["prob_up_pct"])
            self.assertGreater(l["target_price"], 100.0)
            self.assertLess(l["stop_price"], 100.0)
            self.assertIsInstance(l["math_edge_R"], float)

            # SHORT outcome checks
            s = h["short_outcome"]
            self.assertEqual(s["action"], "SHORT")
            self.assertEqual(s["win_probability_pct"], h["prob_down_pct"])
            self.assertLess(s["target_price"], 100.0)
            self.assertGreater(s["stop_price"], 100.0)
            self.assertIsInstance(s["math_edge_R"], float)

    @patch("intraday_estimator.estimate_coin_intraday")
    def test_estimate_all_5_coins_mock(self, mock_estimate):
        mock_estimate.side_effect = lambda sym: {
            "symbol": sym,
            "price": 100.0,
            "change_24h_pct": 1.5,
            "volume_24h_usd": 1000000.0,
            "intraday_composite_score": 15.0,
            "primary_action_30m": "LONG",
            "primary_win_prob_30m": 58.0,
            "primary_edge_30m": 0.25,
            "horizons": {
                tf: {
                    "horizon": tf,
                    "preferred_action": "LONG",
                    "prob_up_pct": 58.0,
                    "prob_down_pct": 42.0,
                    "expected_move_pct": 0.05,
                    "long_outcome": {"win_probability_pct": 58.0, "expected_gain_pct": 0.15},
                    "short_outcome": {"win_probability_pct": 42.0, "expected_gain_pct": 0.12}
                }
                for tf in ["10m", "30m", "60m"]
            }
        }
        results = intraday_estimator.estimate_all_5_coins()
        self.assertEqual(len(results), 6)
        symbols = [r["symbol"] for r in results]
        self.assertEqual(symbols, ["BNBUSDT", "SOLUSDT", "BTCUSDT", "ETHUSDT", "DOGEUSDT", "XRPUSDT"])

        table = intraday_estimator.render_intraday_ascii_table(results)
        self.assertIn("BNBUSDT", table)
        self.assertIn("SOLUSDT", table)
        self.assertIn("10m", table)
        self.assertIn("30m", table)
        self.assertIn("60m", table)

    @patch("predict._fetch_ten_year_daily_bars")
    def test_ten_year_cycle_and_knn_analogs(self, mock_bars):
        # Generate 1,500 synthetic daily bars (~4.1 years) to test 200W SMA, Mayer, Power-Law, and k-NN Analogs
        bars = []
        base_ms = 1514764800000  # 2018-01-01
        for i in range(1500):
            c = 100.0 * (1.0 + 0.0005 * i + 0.08 * ((i % 60) / 60.0))
            bars.append({
                "t": base_ms + i * 86400000,
                "o": c * 0.99,
                "h": c * 1.02,
                "l": c * 0.98,
                "c": c,
                "vol_usd": 50000000.0,
                "tb_usd": 26500000.0,
                "src": "binance",
            })
        mock_bars.return_value = bars
        ty = intraday_estimator.predict.analyze_ten_year_cycle("BNBUSDT", bars[-1]["c"])
        self.assertTrue(ty["available"])
        self.assertEqual(ty["total_days"], 1500)
        self.assertGreater(ty["sma_200w"], 0.0)
        self.assertGreater(ty["mayer_multiple"], 0.0)
        self.assertIn("knn_analogs", ty)
        self.assertGreater(ty["knn_analogs"]["analogs_count"], 0)
        self.assertIn("7d", ty["knn_analogs"])
        self.assertIn("30d", ty["knn_analogs"])
        self.assertIn("90d", ty["knn_analogs"])
        self.assertGreaterEqual(ty["score"], -15.0)
        self.assertLessEqual(ty["score"], 15.0)


if __name__ == "__main__":
    unittest.main()
