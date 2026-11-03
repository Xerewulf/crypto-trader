import os
import sys
import unittest
from unittest.mock import patch

from _load import load

profit_planner = load("trader-analyst", "profit_planner")

class TestProfitPlanner(unittest.TestCase):
    def test_default_coins(self):
        self.assertIn("BNB", profit_planner.DEFAULT_COINS)
        self.assertIn("SOL", profit_planner.DEFAULT_COINS)
        self.assertEqual(len(profit_planner.DEFAULT_COINS), 6)

    @patch("predict.forecast_symbol")
    def test_plan_coin_profits_mocked(self, mock_forecast):
        mock_forecast.side_effect = lambda sym: {
            "symbol": sym,
            "price": 700.0,
            "composite_score": -40.0,
            "verdict": "STRONG BEARISH",
            "horizons": {
                "24h": {
                    "direction": "DOWN",
                    "prob_up_pct": 38.0,
                    "prob_down_pct": 62.0,
                    "expected_move_pct": -0.80
                },
                "7d": {
                    "direction": "DOWN",
                    "prob_up_pct": 30.0,
                    "prob_down_pct": 70.0,
                    "expected_move_pct": -3.50
                }
            },
            "kelly_trade_plan": {
                "bias": "SHORT / HEDGE",
                "limit_pullback_entry": 700.0,
                "stop_loss": 710.0,
                "tp1": 680.0,
                "tp2": 665.0,
                "expected_value_R": 0.55,
                "reward_risk_ratio": 1.5,
                "margin_plan": {
                    "leverage": 2.0
                }
            }
        }

        results = profit_planner.plan_coin_profits(["BNBUSDT"])
        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertEqual(r["symbol"], "BNBUSDT")
        self.assertEqual(r["action"], "SHORT")
        self.assertEqual(r["action_probability_pct"], 62.0)
        self.assertEqual(r["24h_expected_profit_pct"], 0.80)
        self.assertEqual(r["7d_expected_profit_pct"], 3.50)
        self.assertGreater(r["profitability_score"], 0)

    def test_render_table(self):
        dummy_results = [
            {
                "symbol": "BNBUSDT",
                "action": "SHORT",
                "action_probability_pct": 61.5,
                "24h_expected_profit_pct": 0.62,
                "7d_expected_profit_pct": 2.77,
                "tp1_gain_pct": 4.39,
                "math_edge_R": 0.54,
                "profitability_score": 37.1,
                "entry_price": 700.5,
                "stop_loss": 708.5,
                "tp1": 669.1,
                "tp2": 653.3
            }
        ]
        table = profit_planner.render_table(dummy_results)
        self.assertIn("BNBUSDT", table)
        self.assertIn("SHORT", table)
        self.assertIn("61.5%", table)
        self.assertIn("MOST PROFITABLE SETUP", table)

if __name__ == "__main__":
    unittest.main()
