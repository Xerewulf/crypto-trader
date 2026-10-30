import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE_DIR = os.path.join(ROOT, "trader", "live")
if LIVE_DIR not in sys.path:
    sys.path.insert(0, LIVE_DIR)

import paper_trader as pt
from binance_futures_api import BinanceFuturesAPI


class TestAutonomousTrader(unittest.TestCase):
    def test_precision_rounding(self):
        self.assertEqual(pt.round_qty(0.123456, "BTCUSDT"), 0.123)
        self.assertEqual(pt.round_qty(1.23456, "ETHUSDT"), 1.235)
        self.assertEqual(pt.round_qty(15.678, "SOLUSDT"), 15.68)
        self.assertEqual(pt.round_qty(1234.56, "XRPUSDT"), 1234.6)
        self.assertEqual(pt.round_qty(5432.1, "DOGEUSDT"), 5432.0)
        self.assertEqual(pt.round_qty(1000000.9, "1000PEPEUSDT"), 1000000.0)

    def test_scalper_bullish_cvd_burst(self):
        bars_5m = []
        base_p = 100.0
        for i in range(20):
            p = base_p + i * 0.5
            bars_5m.append({
                "t": 1700000000000 + i * 300000,
                "ct": 1700000000000 + i * 300000 + 299999,
                "o": p - 0.2,
                "h": p + 0.3,
                "l": p - 0.3,
                "c": p + 0.2,
                "vol_usd": 100000.0,
                "tb_usd": 65000.0,
                "tb": 65.0,
            })
        ob = {"imb_1pct": 25.0, "wall_bid": 109.0, "wall_ask": 112.0}
        fut = {"funding_8h": 0.0001}
        btc = {"bias": "BULLISH_LEAD", "btc_tb": 58.0}

        sig, diag = pt.evaluate_scalper("SOLUSDT", bars_5m, ob_depth=ob, fut_metrics=fut, btc_lead=btc, news_sentiment=2.0)
        self.assertIsNotNone(sig)
        self.assertEqual(sig["side"], "long")
        self.assertTrue(sig.get("is_scalp"))
        self.assertGreaterEqual(sig["score"], 85)
        self.assertLess(sig["stop"], bars_5m[-1]["c"])

    def test_scalper_bearish_cvd_flush(self):
        bars_5m = []
        base_p = 200.0
        for i in range(20):
            p = base_p - i * 0.5
            bars_5m.append({
                "t": 1700000000000 + i * 300000,
                "ct": 1700000000000 + i * 300000 + 299999,
                "o": p + 0.2,
                "h": p + 0.3,
                "l": p - 0.3,
                "c": p - 0.2,
                "vol_usd": 100000.0,
                "tb_usd": 35000.0,
                "tb": 35.0,
            })
        ob = {"imb_1pct": -25.0, "wall_bid": 188.0, "wall_ask": 192.0}
        fut = {"funding_8h": -0.0001}
        btc = {"bias": "BEARISH_LEAD", "btc_tb": 42.0}

        sig, diag = pt.evaluate_scalper("SOLUSDT", bars_5m, ob_depth=ob, fut_metrics=fut, btc_lead=btc, news_sentiment=-2.0)
        self.assertIsNotNone(sig)
        self.assertEqual(sig["side"], "short")
        self.assertTrue(sig.get("is_scalp"))
        self.assertGreaterEqual(sig["score"], 85)
        self.assertGreater(sig["stop"], bars_5m[-1]["c"])

    def test_state_modes_and_strategies(self):
        s = pt.fresh_state()
        self.assertEqual(s["mode"], "SIMULATION")
        self.assertEqual(s["strategy_profile"], "HYBRID")
        self.assertEqual(s["bot_status"], "RUNNING")

    def test_emergency_flatten_in_simulation(self):
        s = pt.fresh_state()
        s["positions"]["TESTUSDT"] = {
            "id": 99,
            "symbol": "TESTUSDT",
            "side": "long",
            "setup": "Unit Test Setup",
            "entry": 10.0,
            "stop": 9.5,
            "qty": 100.0,
            "notional": 1000.0,
            "margin": 333.33,
            "risk_usd": 50.0,
            "entry_fee": 0.4,
            "opened": pt.iso(),
            "opened_ms": pt.now_ms(),
            "checked_ms": pt.now_ms(),
        }
        s["marks"]["TESTUSDT"] = 10.2
        orig_spot_price = pt.spot_price
        pt.spot_price = lambda sym: 10.2 if sym == "TESTUSDT" else 1.0
        try:
            closed = pt.emergency_flatten(s)
            self.assertEqual(closed, 1)
            self.assertEqual(len(s["positions"]), 0)
            self.assertEqual(len(s["closed"]), 1)
            self.assertEqual(s["closed"][0]["outcome"], "EMERGENCY MANUAL FLATTEN")
        finally:
            pt.spot_price = orig_spot_price


if __name__ == "__main__":
    unittest.main()
