#!/usr/bin/env python3
"""End-to-end integration tests for Phase 4: SOR & Multi-Venue Risk Guard endpoints."""

import json
import os
import sys
import threading
import time
import unittest
import urllib.request
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(ROOT, "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import server  # noqa: E402


class TestServerSOREndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = 8797
        cls.httpd = server.ThreadingHTTPServer(("127.0.0.1", cls.port), server.AppHandler)
        cls.server_thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.server_thread.start()
        time.sleep(0.5)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _get(self, path):
        url = f"http://127.0.0.1:{self.port}{path}"
        with urllib.request.urlopen(url, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _post(self, path, payload):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def test_sor_quote_endpoint(self):
        data = self._get("/api/sor/quote?symbol=BNBUSDT&side=BUY&notional=2500")
        self.assertIn("symbol", data)
        self.assertEqual(data["symbol"], "BNBUSDT")
        self.assertIn("primary_venue", data)
        self.assertIn(data["primary_venue"], ["BINANCE", "BYBIT", "OKX"])
        self.assertIn("primary_quote", data)
        self.assertIn("venue_comparison", data)
        self.assertIn("routing_split", data)
        self.assertIn("cost_savings_usd", data)

    def test_deribit_options_endpoint(self):
        data = self._get("/api/deribit/options?currency=BTC")
        self.assertEqual(data["currency"], "BTC")
        self.assertIn("max_pain_strike", data)
        self.assertGreater(data["max_pain_strike"], 0)
        self.assertIn("put_call_ratio", data)
        self.assertIn("dealer_net_gamma_usd_m", data)
        self.assertIn("pinning_hazard", data)

    def test_circuit_breakers_endpoint(self):
        cb = self._get("/api/risk/circuit-breakers")
        self.assertIn("status", cb)
        self.assertIn("allowed_to_trade", cb)
        self.assertIn("drawdown_pct", cb)
        self.assertIn("hwm_equity", cb)
        self.assertIn("gross_leverage", cb)


if __name__ == "__main__":
    unittest.main()
