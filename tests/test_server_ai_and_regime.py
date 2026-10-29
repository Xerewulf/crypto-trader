#!/usr/bin/env python3
"""Integration tests for Phase 6 Server endpoints: AI Multi-Agent Debate, HMM Regimes, and Stat-Arb Pairs."""

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


class TestServerAiAndRegimeEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = 8802
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
        with urllib.request.urlopen(url, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def test_api_ai_debate(self):
        data = self._get_json("/api/ai/debate?symbol=BTCUSDT")
        self.assertIn("symbol", data)
        self.assertIn("verdict", data)
        self.assertIn("net_conviction_score", data)
        self.assertIn("debate", data)
        self.assertIn("bull_advocate", data["debate"])
        self.assertIn("bear_advocate", data["debate"])

    def test_api_regime_hmm(self):
        data = self._get_json("/api/regime/hmm?symbol=BTCUSDT&interval=1h&limit=50")
        self.assertIn("current_regime", data)
        self.assertIn(data["current_regime"], (0, 1, 2))
        self.assertIn("posterior_probabilities", data)
        self.assertIn("adaptations", data)
        self.assertIn("strategy", data["adaptations"])

    def test_api_stat_arb_pairs(self):
        data = self._get_json("/api/stat-arb/pairs?interval=1h&limit=30")
        self.assertIn("scanned_pairs_count", data)
        self.assertIn("all_pairs", data)
        self.assertTrue(isinstance(data["all_pairs"], list))


if __name__ == "__main__":
    unittest.main()
