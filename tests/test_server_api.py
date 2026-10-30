#!/usr/bin/env python3
"""End-to-end HTTP integration test for app/server.py margin simulation endpoints."""

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


class TestServerEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = 8799
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

    def test_state_endpoint_and_bnb_conversion(self):
        state = self._get("/api/state?symbol=BNBUSDT")
        self.assertIn("bnb_price", state)
        self.assertGreater(state["bnb_price"], 0.0)
        self.assertIn("total_equity_bnb", state)
        self.assertIn("simulation_running", state)
        self.assertIn("cash_bnb", state)

    def test_simulation_start_and_stop_endpoints(self):
        # Stop
        res_stop = self._post("/api/simulation/stop", {})
        self.assertTrue(res_stop["ok"])
        self.assertFalse(res_stop["state"]["simulation_running"])

        # Start
        res_start = self._post("/api/simulation/start", {})
        self.assertTrue(res_start["ok"])
        self.assertTrue(res_start["state"]["simulation_running"])

    def test_margin_order_placement_and_close(self):
        # Reset wallet first to ensure clean starting cash
        self._post("/api/reset", {"starting_cash": 10000.0})

        # Place a 3x Margin LONG on BNBUSDT
        order_res = self._post("/api/order", {
            "symbol": "BNBUSDT",
            "side": "BUY",
            "type": "MARKET",
            "leverage": 3,
            "usdt_amount": 900.0,
            "stop_loss": 500.0,
            "take_profit": 1000.0
        })
        self.assertTrue(order_res["ok"])
        state = order_res["state"]
        self.assertEqual(len(state["positions"]), 1)
        pos = state["positions"][0]
        self.assertEqual(pos["symbol"], "BNBUSDT")
        self.assertEqual(pos["side"], "LONG")
        self.assertEqual(pos["leverage"], 3.0)
        self.assertIn("liquidation_price", pos)
        self.assertIn("margin_locked_bnb", pos)
        self.assertIn("is_positive", pos)

        # Close position
        close_res = self._post("/api/position/close", {
            "symbol": "BNBUSDT",
            "pct": 100
        })
        self.assertTrue(close_res["ok"])
        self.assertEqual(len(close_res["state"]["positions"]), 0)

    def test_html_serves_arbitrage_suite(self):
        url = f"http://127.0.0.1:{self.port}/"
        with urllib.request.urlopen(url, timeout=10) as resp:
            content = resp.read().decode("utf-8")
        self.assertIn("deskArbitrage", content)
        self.assertIn("Delta-Neutral Arbitrage", content)
        self.assertIn("arbScannerTableBody", content)


if __name__ == "__main__":
    unittest.main()
