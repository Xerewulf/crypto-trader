#!/usr/bin/env python3
"""Comprehensive Unit Tests for Phase 5: High-Performance Depth Heatmap & Whale Block Trades."""

import os
import sys
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRADER_DIR = os.path.join(ROOT, "trader-analyst")
if TRADER_DIR not in sys.path:
    sys.path.insert(0, TRADER_DIR)

import heatmap  # noqa: E402


class TestHeatmapEngine(unittest.TestCase):
    def setUp(self):
        self.engine = heatmap.DepthHeatmapEngine(max_snapshots=50)

    def test_depth_snapshot_initialization(self):
        bids = [(740.0, 10.0), (739.0, 20.0)]
        asks = [(741.0, 15.0), (742.0, 25.0)]
        snap = heatmap.DepthSnapshot("BNBUSDT", 1700000000.0, 740.5, bids, asks)
        self.assertEqual(snap.symbol, "BNBUSDT")
        self.assertEqual(snap.mid_price, 740.5)
        self.assertEqual(len(snap.bids), 2)
        self.assertEqual(len(snap.asks), 2)

    def test_add_snapshot_valid(self):
        depth_doc = {
            "bids": [["740.00", "12.50"], ["739.50", "30.00"]],
            "asks": [["740.50", "15.00"], ["741.00", "22.00"]],
        }
        snap = self.engine.add_snapshot("BNBUSDT", depth_doc, timestamp=1000.0)
        self.assertIsNotNone(snap)
        self.assertEqual(snap.mid_price, 740.25)
        self.assertEqual(self.engine.get_snapshot_count("BNBUSDT"), 1)

    def test_add_snapshot_invalid_or_empty(self):
        res1 = self.engine.add_snapshot("BNBUSDT", {})
        self.assertIsNone(res1)
        res2 = self.engine.add_snapshot("BNBUSDT", None)
        self.assertIsNone(res2)
        res3 = self.engine.add_snapshot("BNBUSDT", {"bids": [], "asks": []})
        self.assertIsNone(res3)

    def test_generate_heatmap_matrix_structure(self):
        # Add a couple of realistic snapshots
        depth_doc = {
            "bids": [[f"{740 - i * 0.5:.2f}", "10.0"] for i in range(1, 20)],
            "asks": [[f"{740 + i * 0.5:.2f}", "10.0"] for i in range(1, 20)],
        }
        self.engine.add_snapshot("BNBUSDT", depth_doc, mid_price=740.0, timestamp=1001.0)
        self.engine.add_snapshot("BNBUSDT", depth_doc, mid_price=740.5, timestamp=1002.0)

        matrix = self.engine.generate_heatmap_matrix("BNBUSDT", range_pct=0.03, price_bins=40, time_slices=20)
        self.assertEqual(matrix["symbol"], "BNBUSDT")
        self.assertIn("min_price", matrix)
        self.assertIn("max_price", matrix)
        self.assertIn("price_bins", matrix)
        self.assertEqual(len(matrix["price_bins"]), 40)
        self.assertIn("timestamps", matrix)
        self.assertIn("price_trajectory", matrix)
        self.assertIn("grid", matrix)
        self.assertIn("stats", matrix)

    def test_generate_heatmap_matrix_normalization(self):
        # Ensure intensity values are bounded between 0.0 and 1.0
        depth_doc = {
            "bids": [["735.00", "500.0"], ["730.00", "1000.0"]],
            "asks": [["745.00", "300.0"], ["750.00", "800.0"]],
        }
        self.engine.add_snapshot("BNBUSDT", depth_doc, mid_price=740.0)
        matrix = self.engine.generate_heatmap_matrix("BNBUSDT", range_pct=0.03, price_bins=30)
        for col in matrix["grid"]:
            for cell in col:
                self.assertGreaterEqual(cell["intensity"], 0.0)
                self.assertLessEqual(cell["intensity"], 1.0)
                self.assertIn(cell["side"], ["BID", "ASK", "SPREAD"])

    def test_generate_heatmap_matrix_stats(self):
        depth_doc = {
            "bids": [["738.00", "50.0"], ["735.00", "200.0"]],
            "asks": [["742.00", "30.0"], ["745.00", "100.0"]],
        }
        self.engine.add_snapshot("BNBUSDT", depth_doc, mid_price=740.0)
        matrix = self.engine.generate_heatmap_matrix("BNBUSDT", range_pct=0.03)
        stats = matrix["stats"]
        self.assertIn("total_bid_usd", stats)
        self.assertIn("total_ask_usd", stats)
        self.assertIn("imbalance_pct", stats)
        self.assertIn("largest_bid_wall", stats)
        self.assertIn("largest_ask_wall", stats)
        self.assertGreater(stats["total_bid_usd"], 0)
        self.assertGreater(stats["total_ask_usd"], 0)


class TestBlockTradeDetector(unittest.TestCase):
    def setUp(self):
        self.detector = heatmap.BlockTradeDetector(max_history=50)

    def test_tier_classification(self):
        self.assertEqual(heatmap.BlockTradeDetector.classify_tier(100_000), "WHALE")
        self.assertEqual(heatmap.BlockTradeDetector.classify_tier(249_999), "WHALE")
        self.assertEqual(heatmap.BlockTradeDetector.classify_tier(250_000), "SHARK")
        self.assertEqual(heatmap.BlockTradeDetector.classify_tier(499_999), "SHARK")
        self.assertEqual(heatmap.BlockTradeDetector.classify_tier(500_000), "MEGALODON")
        self.assertEqual(heatmap.BlockTradeDetector.classify_tier(999_999), "MEGALODON")
        self.assertEqual(heatmap.BlockTradeDetector.classify_tier(1_000_000), "TITAN")
        self.assertEqual(heatmap.BlockTradeDetector.classify_tier(5_000_000), "TITAN")

    def test_tier_badges(self):
        self.assertIn("TITAN", heatmap.BlockTradeDetector.get_tier_badge("TITAN"))
        self.assertIn("MEGALODON", heatmap.BlockTradeDetector.get_tier_badge("MEGALODON"))
        self.assertIn("SHARK", heatmap.BlockTradeDetector.get_tier_badge("SHARK"))
        self.assertIn("WHALE", heatmap.BlockTradeDetector.get_tier_badge("WHALE"))

    def test_process_raw_trades_sides_and_filtering(self):
        raw_trades = [
            # Below 100k threshold -> ignored
            {"a": 1, "p": "740.00", "q": "10.00", "m": False, "T": 1000},
            # 740 * 200 = 148,000 -> WHALE BUY (m=False -> taker buy)
            {"a": 2, "p": "740.00", "q": "200.00", "m": False, "T": 1010},
            # 740 * 500 = 370,000 -> SHARK SELL (m=True -> taker sell)
            {"a": 3, "p": "740.00", "q": "500.00", "m": True, "T": 1020},
            # 740 * 1500 = 1,110,000 -> TITAN BUY
            {"a": 4, "p": "740.00", "q": "1500.00", "m": False, "T": 1030},
        ]

        detected = self.detector.process_raw_trades("BNBUSDT", raw_trades, min_notional=100_000.0)
        self.assertEqual(len(detected), 3)

        # Check order and fields
        self.assertEqual(detected[0]["side"], "BUY")
        self.assertEqual(detected[0]["tier"], "WHALE")
        self.assertEqual(detected[0]["notional_usd"], 148_000.0)

        self.assertEqual(detected[1]["side"], "SELL")
        self.assertEqual(detected[1]["tier"], "SHARK")
        self.assertEqual(detected[1]["notional_usd"], 370_000.0)

        self.assertEqual(detected[2]["side"], "BUY")
        self.assertEqual(detected[2]["tier"], "TITAN")
        self.assertEqual(detected[2]["notional_usd"], 1_110_000.0)

    def test_deduplication(self):
        raw = [{"a": 999, "p": "740.00", "q": "200.00", "m": False, "T": 1000}]
        first = self.detector.process_raw_trades("BNBUSDT", raw, min_notional=100_000)
        second = self.detector.process_raw_trades("BNBUSDT", raw, min_notional=100_000)
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 0)  # Duplicate filtered out

    def test_flow_metrics_calculation(self):
        raw = [
            {"a": 10, "p": "740.00", "q": "200.00", "m": False, "T": 1000},  # 148k BUY
            {"a": 11, "p": "740.00", "q": "500.00", "m": True, "T": 1010},   # 370k SELL
            {"a": 12, "p": "740.00", "q": "1500.00", "m": False, "T": 1020}, # 1,110k BUY
        ]
        self.detector.process_raw_trades("BNBUSDT", raw, min_notional=100_000)
        metrics = self.detector.get_block_trade_metrics("BNBUSDT")

        self.assertEqual(metrics["total_blocks"], 3)
        self.assertEqual(metrics["whale_buy_vol_usd"], 148_000 + 1_110_000)
        self.assertEqual(metrics["whale_sell_vol_usd"], 370_000)
        self.assertEqual(metrics["whale_net_flow_usd"], (148_000 + 1_110_000) - 370_000)
        self.assertEqual(metrics["whale_buy_count"], 2)
        self.assertEqual(metrics["whale_sell_count"], 1)
        self.assertEqual(metrics["largest_print"]["tier"], "TITAN")
        self.assertEqual(metrics["largest_print"]["notional_usd"], 1_110_000.0)

    def test_synthesize_sample_blocks(self):
        blocks = heatmap._synthesize_sample_blocks("BNBUSDT")
        self.assertGreater(len(blocks), 0)
        for b in blocks:
            self.assertIn("p", b)
            self.assertIn("q", b)
            self.assertIn("m", b)


if __name__ == "__main__":
    unittest.main()
