import os
import sys
import unittest
import math

from _load import load

price_forecaster = load("trader-analyst", "price_forecaster")


class TestPriceForecaster(unittest.TestCase):
    def setUp(self):
        # Generate 250 synthetic 1m klines:
        # [open_time, open, high, low, close, volume, close_time, quote_volume, trades, taker_buy_base, taker_buy_quote, ignore]
        self.mock_klines = []
        p = 100.0
        for i in range(250):
            o = p
            c = p + 0.1 * math.sin(i / 5.0)
            h = max(o, c) + 0.05
            l = min(o, c) - 0.05
            v = 10.0
            qv = v * c
            tbq = 0.55 * qv
            self.mock_klines.append([
                i * 60000, str(o), str(h), str(l), str(c), str(v),
                (i + 1) * 60000, str(qv), 50, str(v * 0.55), str(tbq), "0"
            ])
            p = c

    def test_feature_rows_extraction(self):
        rows = price_forecaster._feature_rows(self.mock_klines)
        self.assertTrue(len(rows) > 0)
        t, feat, sig = rows[0]
        self.assertEqual(len(feat), 6)
        self.assertGreater(sig, 0)

    def test_fit_models_walk_forward(self):
        models = price_forecaster.fit_models(self.mock_klines)
        self.assertIn("10m", models)
        self.assertIn("30m", models)
        self.assertIn("60m", models)

        m10 = models["10m"]
        if m10:
            self.assertIn("weights", m10)
            self.assertIn("shrink", m10)
            self.assertIn("oos_mae_pct", m10)
            self.assertIn("skill_vs_naive_pct", m10)
            self.assertIn("resid_q", m10)

    def test_forecast_prices_structure(self):
        current_price = float(self.mock_klines[-1][4])
        mock_fetch = lambda bsym: self.mock_klines

        forecasts = price_forecaster.forecast_prices("BTCUSDT", self.mock_klines, current_price, mock_fetch)
        self.assertIn("10m", forecasts)
        self.assertIn("30m", forecasts)
        self.assertIn("60m", forecasts)

        f10 = forecasts["10m"]
        self.assertIn("predicted_price", f10)
        self.assertIn("predicted_move_pct", f10)
        self.assertIn("typical_error_pct", f10)
        self.assertIn("range_80_low", f10)
        self.assertIn("range_80_high", f10)
        self.assertGreater(f10["predicted_price"], 0)
        self.assertLess(f10["range_80_low"], f10["range_80_high"])


if __name__ == "__main__":
    unittest.main()
