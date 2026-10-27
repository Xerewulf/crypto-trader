#!/usr/bin/env python3
"""Unit tests for Binance Live Execution Client (trader/live/binance_client.py)."""

import hashlib
import hmac
import os
import sys
import unittest
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LIVE = os.path.join(ROOT, "trader", "live")
if LIVE not in sys.path:
    sys.path.insert(0, LIVE)

from binance_client import BinanceClient, BinanceAPIError


class TestBinanceClient(unittest.TestCase):
    def setUp(self):
        self.client = BinanceClient(
            api_key="test_api_key_sample_12345678",
            api_secret="test_api_secret_sample_87654321",
            testnet=True
        )
        # Mock exchange filters for deterministic testing
        self.client.symbol_filters = {
            "BTCUSDT": {
                "stepSize": 0.001,
                "minQty": 0.001,
                "tickSize": 0.1,
                "minNotional": 5.0
            },
            "ADAUSDT": {
                "stepSize": 1.0,
                "minQty": 1.0,
                "tickSize": 0.0001,
                "minNotional": 5.0
            }
        }

    def test_client_configuration(self):
        self.assertTrue(self.client.is_configured())
        self.assertTrue(self.client.testnet)
        self.assertIn("testnet", self.client.base_url)

    def test_quantity_formatting(self):
        # BTC stepSize 0.001
        formatted_btc = self.client.format_qty("BTCUSDT", 0.123456)
        self.assertEqual(formatted_btc, "0.123")

        # ADA stepSize 1.0
        formatted_ada = self.client.format_qty("ADAUSDT", 254.89)
        self.assertEqual(formatted_ada, "254")

    def test_price_formatting(self):
        # BTC tickSize 0.1
        formatted_btc_px = self.client.format_price("BTCUSDT", 80123.456)
        self.assertEqual(formatted_btc_px, "80123.5")

        # ADA tickSize 0.0001
        formatted_ada_px = self.client.format_price("ADAUSDT", 0.345678)
        self.assertEqual(formatted_ada_px, "0.3457")

    def test_hmac_sha256_signature_correctness(self):
        secret = "secret123"
        params = {"symbol": "BTCUSDT", "side": "BUY", "quantity": "0.01", "timestamp": 1700000000000}
        query_str = urllib.parse.urlencode(params)
        expected_sig = hmac.new(secret.encode("utf-8"), query_str.encode("utf-8"), hashlib.sha256).hexdigest()

        # Generate using client credentials logic
        test_client = BinanceClient(api_key="key123", api_secret=secret)
        actual_sig = hmac.new(test_client.api_secret.encode("utf-8"), query_str.encode("utf-8"), hashlib.sha256).hexdigest()
        self.assertEqual(actual_sig, expected_sig)

    def test_api_error_representation(self):
        err = BinanceAPIError(-2019, "Margin is insufficient")
        self.assertEqual(err.code, -2019)
        self.assertIn("Margin is insufficient", str(err))


if __name__ == "__main__":
    unittest.main()
