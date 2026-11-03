#!/usr/bin/env python3
"""Integration tests for Phase 5 Server endpoints: Depth Heatmap, Whale Radar, PWA Service Worker."""

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


class TestServerHeatmapEndpoints(unittest.TestCase):
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

    def _get_json(self, path):
        url = f"http://127.0.0.1:{self.port}{path}"
        with urllib.request.urlopen(url, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _get_raw(self, path):
        url = f"http://127.0.0.1:{self.port}{path}"
        with urllib.request.urlopen(url, timeout=15) as resp:
            headers = resp.headers
            body = resp.read()
            return resp.status, headers, body

    def test_heatmap_depth_endpoint(self):
        data = self._get_json("/api/heatmap/depth?symbol=BNBUSDT&range_pct=0.03&bins=40&slices=30")
        self.assertIn("symbol", data)
        self.assertEqual(data["symbol"], "BNBUSDT")
        self.assertIn("price_bins", data)
        self.assertEqual(len(data["price_bins"]), 40)
        self.assertIn("timestamps", data)
        self.assertIn("price_trajectory", data)
        self.assertIn("grid", data)
        self.assertIn("stats", data)
        self.assertIn("total_bid_usd", data["stats"])
        self.assertIn("total_ask_usd", data["stats"])

    def test_trades_blocks_endpoint(self):
        data = self._get_json("/api/trades/blocks?symbol=BNBUSDT&min_notional=100000")
        self.assertIn("symbol", data)
        self.assertEqual(data["symbol"], "BNBUSDT")
        self.assertIn("total_blocks", data)
        self.assertIn("whale_buy_vol_usd", data)
        self.assertIn("whale_sell_vol_usd", data)
        self.assertIn("whale_net_flow_usd", data)
        self.assertIn("recent_blocks", data)
        self.assertIsInstance(data["recent_blocks"], list)

    def test_sw_service_worker_asset(self):
        status, headers, body = self._get_raw("/sw.js")
        self.assertEqual(status, 200)
        self.assertIn("application/javascript", headers.get("content-type", "").lower())
        self.assertEqual(headers.get("service-worker-allowed"), "/")
        self.assertIn(b"godtrader-pwa-v5", body)

    def test_manifest_asset(self):
        status, headers, body = self._get_raw("/manifest.json")
        self.assertEqual(status, 200)
        self.assertIn("application/manifest+json", headers.get("content-type", "").lower())
        manifest = json.loads(body.decode("utf-8"))
        self.assertIn("name", manifest)
        self.assertIn("GodTrader", manifest["name"])
        self.assertEqual(manifest["display"], "standalone")
        self.assertIn("icons", manifest)


if __name__ == "__main__":
    unittest.main()
