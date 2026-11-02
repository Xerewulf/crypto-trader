#!/usr/bin/env python3
"""Integration tests for Delta-Neutral Funding Arbitrage server endpoints and core logic."""

import json
import os
import sys
import threading
import time
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(ROOT, "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import server  # noqa: E402


class TestServerArbitrage(unittest.TestCase):
    def test_open_and_close_arbitrage_cash_and_carry(self):
        w = server.default_wallet(starting_cash=10000.0)
        # Open $2000 cash and carry on BNBUSDT at $700 spot and $700.50 perp mark, 0.02% funding
        pair = server._open_arbitrage_pair_locked(
            w,
            symbol="BNBUSDT",
            strategy="CASH_AND_CARRY",
            capital_usdt=2000.0,
            spot_price=700.0,
            perp_mark=700.50,
            funding_rate=0.00020,
        )

        self.assertIn("arbitrage_pairs", w)
        pair_id = pair["id"]
        self.assertIn(pair_id, w["arbitrage_pairs"])
        self.assertEqual(pair["strategy"], "CASH_AND_CARRY")
        self.assertEqual(pair["spot_leg"]["side"], "BUY")
        self.assertEqual(pair["perp_leg"]["side"], "SELL")

        # 50/50 capital split: $1000 spot notional, $1000 perp margin
        self.assertAlmostEqual(pair["spot_leg"]["notional_usdt"], 1000.0, places=1)
        self.assertAlmostEqual(pair["perp_leg"]["notional_usdt"], 1000.71, places=1)

        # Net delta should be virtually 0
        net_delta = pair["delta"]["net_delta_usdt"]
        self.assertTrue(abs(net_delta) < 2.0)
        self.assertTrue(pair["delta"]["is_delta_neutral"])

        # Cash should be reduced by capital ($2000) + fees (~$0.80)
        self.assertTrue(w["cash_usdt"] < 8000.0)

        # Close the pair
        close_res = server._close_arbitrage_pair_locked(
            w,
            pair_id=pair_id,
            spot_price=710.0,
            perp_mark=710.50,
            reason="Unwind test",
        )
        self.assertNotIn(pair_id, w["arbitrage_pairs"])
        self.assertEqual(close_res["strategy"], "CASH_AND_CARRY")
        # Cash should be credited back near $10000
        self.assertTrue(w["cash_usdt"] > 9990.0)

    def test_open_arbitrage_reverse_squeeze(self):
        w = server.default_wallet(starting_cash=10000.0)
        pair = server._open_arbitrage_pair_locked(
            w,
            symbol="PEPEUSDT",
            strategy="REVERSE_ARBITRAGE",
            capital_usdt=3000.0,
            spot_price=0.000010,
            perp_mark=0.000010,
            funding_rate=-0.00350,  # Deep negative squeeze (-328.5% APY)
        )

        self.assertEqual(pair["strategy"], "REVERSE_ARBITRAGE")
        self.assertEqual(pair["spot_leg"]["side"], "SELL")
        self.assertEqual(pair["perp_leg"]["side"], "BUY")
        self.assertAlmostEqual(pair["metrics"]["annualized_apy"], 383.25, places=0)


class TestServerArbitrageEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = 8798
        cls.httpd = server.ThreadingHTTPServer(("127.0.0.1", cls.port), server.AppHandler)
        cls.server_thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.server_thread.start()
        time.sleep(0.5)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _post(self, path, payload):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _get(self, path):
        url = f"http://127.0.0.1:{self.port}{path}"
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def test_opportunities_endpoint(self):
        res = self._get("/api/arbitrage/opportunities?min_apy=5.0")
        self.assertIn("opportunities", res)
        self.assertIn("cash_and_carry_opportunities", res)
        self.assertIn("reverse_arbitrage_opportunities", res)
        self.assertTrue(len(res["opportunities"]) > 0)

    def test_positions_and_execution_lifecycle(self):
        # 1. Check positions endpoint initially
        pos_res = self._get("/api/arbitrage/positions")
        self.assertIn("pairs", pos_res)
        self.assertIn("summary", pos_res)

        # 2. Execute a new arbitrage pair via POST
        exec_payload = {
            "symbol": "BNBUSDT",
            "strategy": "CASH_AND_CARRY",
            "capital_usdt": 1000.0,
        }
        exec_res = self._post("/api/arbitrage/execute", exec_payload)
        self.assertTrue(exec_res.get("ok"))
        pair = exec_res["result"]
        pair_id = pair["id"]
        self.assertTrue(pair_id.startswith("ARB-BNBUSDT"))

        # 3. Verify it is reflected in state and /api/arbitrage/positions
        pos_res_after = self._get("/api/arbitrage/positions")
        pair_ids = [p["pair_id"] for p in pos_res_after["pairs"]]
        self.assertIn(pair_id, pair_ids)

        # 4. Unwind / close the pair
        close_res = self._post("/api/arbitrage/close", {"pair_id": pair_id})
        self.assertTrue(close_res.get("ok"))

        # 5. Confirm pair is removed
        pos_res_final = self._get("/api/arbitrage/positions")
        pair_ids_final = [p["pair_id"] for p in pos_res_final["pairs"]]
        self.assertNotIn(pair_id, pair_ids_final)


if __name__ == "__main__":
    unittest.main()
