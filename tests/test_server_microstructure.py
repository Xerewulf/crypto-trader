#!/usr/bin/env python3
"""End-to-end integration test for Phase 3 /api/microstructure server endpoint."""

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


class TestServerMicrostructureEndpoint(unittest.TestCase):
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

    def _get(self, path):
        url = f"http://127.0.0.1:{self.port}{path}"
        with urllib.request.urlopen(url, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def test_microstructure_endpoint_bnb(self):
        data = self._get("/api/microstructure?symbol=BNBUSDT")
        self.assertIn("symbol", data)
        self.assertEqual(data["symbol"], "BNBUSDT")
        self.assertIn("vpin", data)
        self.assertIn("wyckoff", data)
        self.assertIn("adaptive_kelly", data)
        self.assertIn("bb_width_pct", data)

        vpin = data["vpin"]
        self.assertIn("vpin_pct", vpin)
        self.assertIn("regime", vpin)
        self.assertIn("toxicity_status", vpin)
        self.assertIn("adverse_selection_risk", vpin)

        wyckoff = data["wyckoff"]
        self.assertIn("signals", wyckoff)
        self.assertIn("patterns", wyckoff)
        self.assertIn("dominant_regime", wyckoff)

        ak = data["adaptive_kelly"]
        self.assertIn("base_quarter_kelly_pct", ak)
        self.assertIn("adaptive_kelly_pct", ak)
        self.assertIn("composite_multiplier", ak)
        self.assertIn("sizing_rationale", ak)


if __name__ == "__main__":
    unittest.main()
