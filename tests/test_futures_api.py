import hashlib
import hmac
import os
import sys
import unittest
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE_DIR = os.path.join(ROOT, "trader", "live")
if LIVE_DIR not in sys.path:
    sys.path.insert(0, LIVE_DIR)

from binance_futures_api import BinanceFuturesAPI


class TestBinanceFuturesAPI(unittest.TestCase):
    def test_init_unconfigured(self):
        client = BinanceFuturesAPI()
        self.assertFalse(client.is_configured)
        self.assertEqual(client.base_url, "https://fapi.binance.com")

    def test_init_configured(self):
        client = BinanceFuturesAPI(api_key="my_key", api_secret="my_secret")
        self.assertTrue(client.is_configured)
        self.assertEqual(client.api_key, "my_key")
        self.assertEqual(client.api_secret, "my_secret")

    def test_testnet_url(self):
        client = BinanceFuturesAPI(testnet=True)
        self.assertEqual(client.base_url, "https://testnet.binancefuture.com")

    def test_signature_calculation(self):
        secret = "test_secret_12345"
        client = BinanceFuturesAPI(api_key="key", api_secret=secret)
        params = {"symbol": "BTCUSDT", "side": "BUY", "timestamp": 1600000000000}
        signed_qs = client._sign(params)

        parsed = urllib.parse.parse_qs(signed_qs)
        self.assertIn("signature", parsed)
        self.assertEqual(parsed["symbol"][0], "BTCUSDT")
        self.assertEqual(parsed["side"][0], "BUY")

        # Verify signature mathematically
        qs_without_sig = urllib.parse.urlencode(params)
        expected_sig = hmac.new(
            secret.encode("utf-8"),
            qs_without_sig.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()
        self.assertEqual(parsed["signature"][0], expected_sig)

    def test_unconfigured_signed_request_raises(self):
        client = BinanceFuturesAPI()
        with self.assertRaises(ValueError) as ctx:
            client.get_balance()
        self.assertIn("credentials missing", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
