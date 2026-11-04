import os
import sys
import unittest

# Ensure we can load modules from parent and sibling directories
from _load import load

predict = load("trader-analyst", "predict")


class TestCryptoPredictionEngine(unittest.TestCase):
    def setUp(self):
        # Generate synthetic 1h and 4h klines: [open_time, open, high, low, close, volume, ...]
        self.flat_klines = []
        base_p = 100.0
        for i in range(100):
            o = base_p + (i % 3) * 0.1
            c = base_p + ((i + 1) % 3) * 0.1
            h = max(o, c) + 0.2
            l = min(o, c) - 0.2
            v = 1000.0 + (i * 10)
            self.flat_klines.append([i * 3600000, str(o), str(h), str(l), str(c), str(v)])

        self.uptrend_klines = []
        p = 100.0
        for i in range(100):
            o = p
            c = p + 1.2
            h = c + 0.5
            l = o - 0.2
            v = 2000.0 + (i * 20)
            self.uptrend_klines.append([i * 3600000, str(o), str(h), str(l), str(c), str(v)])
            p = c

    def test_compute_adx_flat_market(self):
        adx, pdi, mdi = predict.compute_adx(self.flat_klines, period=14)
        self.assertIsInstance(adx, float)
        self.assertLess(adx, 25.0, "Flat market should have low ADX (< 25)")

    def test_compute_adx_uptrend_market(self):
        adx, pdi, mdi = predict.compute_adx(self.uptrend_klines, period=14)
        self.assertIsInstance(adx, float)
        self.assertGreater(adx, 25.0, "Uptrend market should have high ADX (> 25)")
        self.assertGreater(pdi, mdi, "+DI should exceed -DI in an uptrend")

    def test_compute_market_regime_trending(self):
        curr_p = float(self.uptrend_klines[-1][4])
        reg = predict.compute_market_regime(self.uptrend_klines, self.uptrend_klines, curr_p)
        self.assertIn("regime", reg)
        self.assertEqual(reg["regime"], "BULLISH_TREND_EXPANSION")
        self.assertGreater(reg["multipliers"]["technicals"], 1.0)
        self.assertGreater(reg["score_delta"], 0.0)

    def test_compute_market_regime_range(self):
        curr_p = float(self.flat_klines[-1][4])
        reg = predict.compute_market_regime(self.flat_klines, self.flat_klines, curr_p)
        self.assertIn(reg["regime"], ("ROTATIONAL_RANGE_BOUND", "VOLATILITY_COMPRESSION_SQUEEZE", "TRANSITIONAL_CHOP"))
        self.assertIn("multipliers", reg)
        self.assertGreaterEqual(reg["multipliers"]["volume_profile"], 1.0)

    def test_estimate_liquidation_clusters(self):
        curr_p = float(self.uptrend_klines[-1][4])
        fut_data = {
            "available": True,
            "open_interest_usd": 500000000.0,
            "whale_long_pct": 60.0,
            "retail_long_pct": 55.0,
        }
        res = predict.estimate_liquidation_clusters(self.uptrend_klines, curr_p, fut_data)
        self.assertTrue(res["available"])
        self.assertIn("top_longs", res)
        self.assertIn("top_shorts", res)
        self.assertTrue(len(res["top_longs"]) > 0)
        self.assertTrue(len(res["top_shorts"]) > 0)
        self.assertIn("magnetic_pull", res)
        self.assertIn("magnetic_ratio", res)

        # Ensure long liq is below price, short liq is above price
        p_long = res["top_longs"][0]["price"]
        p_short = res["top_shorts"][0]["price"]
        self.assertLess(p_long, curr_p)
        self.assertGreater(p_short, curr_p)

    def test_estimate_liquidation_clusters_unavailable(self):
        res = predict.estimate_liquidation_clusters(self.uptrend_klines, 60000.0, None)
        self.assertFalse(res["available"])

    def test_compute_kelly_trade_plan_with_liquidation_guards(self):
        price = 60000.0
        horizons = {"24h": {"prob_up_pct": 65.0, "prob_down_pct": 35.0}}
        vp = {"val": 59000.0, "poc": 60000.0, "vah": 61000.0, "vwap_24h": 59800.0}
        ob = {"support_wall_price": 59500.0, "resistance_wall_price": 61200.0}
        tf_1h = {"atr14": 400.0}
        composite = 40.0
        liq = {
            "available": True,
            "primary_long_liq": 59200.0,
            "primary_short_liq": 61800.0,
        }
        regime = {"regime": "BULLISH_TREND_EXPANSION"}

        plan = predict.compute_kelly_trade_plan(
            price, horizons, vp, ob, tf_1h, composite, liq=liq, regime=regime
        )
        self.assertEqual(plan["bias"], "LONG")
        # Stop loss should be positioned below or respecting the primary long liquidation
        self.assertLess(plan["stop_loss"], price)
        self.assertGreater(plan["tp1"], price)
        self.assertGreaterEqual(plan["tp2"], 61000.0)

    def test_render_report_with_liquidation_and_regime(self):
        d = {
            "symbol": "BTCUSDT",
            "as_of": "2026-11-04T12:00:00Z",
            "price": 70000.0,
            "change_24h_pct": 2.5,
            "high_24h": 71000.0,
            "low_24h": 68500.0,
            "volume_24h_usd": 1500000000.0,
            "composite_score": 42.0,
            "verdict": "STRONG BULLISH",
            "horizons": {
                "4h": {"horizon": "4h", "direction": "UP", "prob_up_pct": 62.0, "prob_down_pct": 38.0, "expected_price": 70500.0, "expected_move_pct": 0.7, "range_80_low": 69500.0, "range_80_high": 71500.0},
                "24h": {"horizon": "24h", "direction": "UP", "prob_up_pct": 68.0, "prob_down_pct": 32.0, "expected_price": 71800.0, "expected_move_pct": 2.5, "range_80_low": 68000.0, "range_80_high": 73500.0},
                "7d": {"horizon": "7d", "direction": "UP", "prob_up_pct": 60.0, "prob_down_pct": 40.0, "expected_price": 74000.0, "expected_move_pct": 5.7, "range_80_low": 65000.0, "range_80_high": 78000.0},
            },
            "pillar_scores": {
                "technicals_mtf": {"score": 18.0},
                "spot_orderflow_depth": {"score": 12.0},
                "futures_leverage": {"score": 15.0},
                "relative_strength_btc": {"score": 0.0},
                "news_catalysts": {"score": 5.0},
                "institutional_bonus": {"score": 4.0},
            },
            "futures": {"available": False, "note": "Futures test"},
            "liquidation_heatmap": {
                "available": True,
                "magnetic_pull": "STRONG UPWARD PULL (Short Squeeze Fuel)",
                "magnetic_ratio": 1.75,
                "near_long_pool_3pct_m": 45.0,
                "near_short_pool_3pct_m": 78.5,
                "pull_note": "Short liquidations outweigh long liquidations.",
                "top_shorts": [{"price": 71500.0, "dist_pct": 2.14, "est_usd_m": 35.0}],
                "top_longs": [{"price": 68500.0, "dist_pct": -2.14, "est_usd_m": 22.0}],
            },
            "market_regime": {
                "regime": "BULLISH_TREND_EXPANSION",
                "description": "Strong Bullish Momentum",
                "adx_1h": 32.5,
                "pdi_1h": 28.0,
                "mdi_1h": 14.0,
                "adx_4h": 29.0,
                "multipliers": {"technicals": 1.3, "orderflow": 1.2, "volume_profile": 0.75},
            },
            "spot_flow": {
                "1h": {"buy_pct": 55.0, "cvd_usd": 5000000.0, "total_usd": 20000000.0},
                "4h": {"buy_pct": 53.0, "cvd_usd": 12000000.0, "total_usd": 80000000.0},
                "24h": {"buy_pct": 52.0, "cvd_usd": 40000000.0, "total_usd": 500000000.0},
                "7d": {"buy_pct": 51.0, "cvd_usd": 90000000.0, "total_usd": 2000000000.0},
                "orderbook": {
                    "bid_1pct_usd": 15000000.0, "ask_1pct_usd": 10000000.0, "imbalance_1pct": 20.0,
                    "bid_2pct_usd": 30000000.0, "ask_2pct_usd": 25000000.0, "imbalance_2pct": 9.1,
                    "support_wall_price": 69000.0, "support_wall_usd": 8000000.0,
                    "resistance_wall_price": 72000.0, "resistance_wall_usd": 6000000.0,
                },
            },
            "timeframes": {
                "15m": {"regime": "BULL", "ema20": 69900.0, "ema50": 69700.0, "sma200": 69000.0, "rsi14": 58.0, "macd_hist": 12.0, "macd_rising": True, "atr14": 150.0, "atr_pct": 0.21},
                "1h": {"regime": "BULL", "ema20": 69500.0, "ema50": 69000.0, "sma200": 68000.0, "rsi14": 62.0, "macd_hist": 25.0, "macd_rising": True, "atr14": 300.0, "atr_pct": 0.43},
                "4h": {"regime": "BULL", "ema20": 68500.0, "ema50": 67500.0, "sma200": 65000.0, "rsi14": 65.0, "macd_hist": 45.0, "macd_rising": True, "atr14": 650.0, "atr_pct": 0.93},
                "1d": {"regime": "BULL", "ema20": 66000.0, "ema50": 63000.0, "sma200": 58000.0, "rsi14": 68.0, "macd_hist": 80.0, "macd_rising": True, "atr14": 1800.0, "atr_pct": 2.57},
            },
            "relative_strength": {"alpha_24h_pct": 0.0, "alpha_7d_pct": 0.0, "return_7d_pct": 5.0, "btc_7d_pct": 5.0},
            "levels": {"supports": [69000.0, 68500.0], "resistances": [71000.0, 72000.0]},
            "volume_profile": {"val": 68000.0, "poc": 69500.0, "vah": 71000.0, "state": "INSIDE_VALUE", "vwap_24h": 69800.0, "vwap_24h_dist_pct": 0.29, "vwap_7d": 68500.0},
            "institutional": {"squeeze_4h": "NORMAL", "bb_width_4h_pct": 4.5, "signals": ["TEST SIGNAL"]},
            "macro_onchain": {
                "fear_greed_value": 65, "fear_greed_label": "Greed", "fear_greed_prev": 60,
                "total_mcap_usd": 2500000000000.0, "mcap_24h_change_pct": 1.8,
                "btc_dominance_pct": 58.0, "eth_dominance_pct": 14.0, "market_season": "Bitcoin Season",
                "stablecoin_total_usd": 160000000000.0, "usdt_supply_usd": 115000000000.0, "usdc_supply_usd": 35000000000.0,
                "stablecoin_24h_flow_usd": 250000000.0, "stablecoin_7d_flow_usd": 1200000000.0,
                "macro_notes": [],
            },
            "news": {"score": 5.0, "headlines": [], "headlines_checked": 25},
            "kelly_trade_plan": {
                "bias": "LONG", "spot_entry": 70000.0, "limit_pullback_entry": 69800.0,
                "stop_loss": 68400.0, "tp1": 71500.0, "tp2": 73000.0,
                "reward_risk_ratio": 2.1, "win_prob_pct": 68.0, "expected_value_R": 1.1, "quarter_kelly_risk_pct": 2.5,
            },
        }

        report = predict.render_report(d)
        self.assertIn("LIQUIDATION HEATMAP & CASCADE TARGETS", report)
        self.assertIn("REGIME FILTER", report)
        self.assertIn("BULLISH_TREND_EXPANSION", report)
        self.assertIn("Magnetic Pull", report)


if __name__ == "__main__":
    unittest.main()
