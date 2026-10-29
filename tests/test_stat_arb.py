#!/usr/bin/env python3
"""Unit tests for Phase 6: Dynamic Kalman Filter & Ornstein-Uhlenbeck (OU) Pairs Statistical Arbitrage."""

import math
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from _load import load

stat_arb = load("trader-analyst", "stat_arb")


class TestStatArb(unittest.TestCase):
    def test_kalman_filter_convergence(self):
        # Generate synthetic relationship: y = 2.0 * x + 5.0 + noise
        kf = stat_arb.OnlineKalmanFilter(delta=1e-4, r_variance=1e-3)
        kf.theta = [0.0, 1.0]

        for i in range(1, 150):
            x = float(i)
            noise = (i % 5 - 2) * 0.1
            y = 2.0 * x + 5.0 + noise
            alpha, beta, err = kf.update(y, x)

        # After 150 steps, beta should be close to 2.0
        self.assertAlmostEqual(beta, 2.0, delta=0.1)

    def test_ornstein_uhlenbeck_mean_reversion(self):
        # Generate stationary mean-reverting AR(1) process: S_t = 0.5 * S_{t-1} + 1.0 + noise
        spread = [2.0]
        for i in range(100):
            noise = (i % 7 - 3) * 0.2
            s_next = 0.5 * spread[-1] + 1.0 + noise
            spread.append(s_next)

        ou = stat_arb.fit_ornstein_uhlenbeck(spread, dt=1.0)
        self.assertTrue(ou["is_mean_reverting"])
        self.assertTrue(0.3 < ou["ar1_b"] < 0.7)
        self.assertTrue(ou["half_life_units"] > 0)
        self.assertAlmostEqual(ou["mu"], 2.0, delta=0.5)

    def test_ornstein_uhlenbeck_trending_detection(self):
        # Strongly trending series (not mean-reverting)
        spread = [float(i * 5) for i in range(50)]
        ou = stat_arb.fit_ornstein_uhlenbeck(spread, dt=1.0)
        # Trending spread should have b >= 1.0 and be marked as non-mean-reverting
        self.assertFalse(ou["is_mean_reverting"])

    def test_pair_stat_arb_signals(self):
        # Create cointegrated price series with realistic spread noise
        prices_b = [100.0 + (i % 10) * 0.5 for i in range(50)]
        prices_a = [2.0 * b + (i % 5 - 2) * 0.2 for i, b in enumerate(prices_b)]
        prices_a[-1] -= 1.5  # Spread dislocation Z ~ -2.9

        res = stat_arb.compute_pair_stat_arb(prices_a, prices_b, ("ASSET_A", "ASSET_B"), interval="1h")
        self.assertIn("signal", res)
        self.assertIn("kalman", res)
        self.assertIn("ornstein_uhlenbeck", res)
        self.assertEqual(res["signal"], "LONG_SPREAD")
        self.assertTrue(res["z_score"] <= -2.0)
        self.assertEqual(res["status"], "ACTIVE_OPPORTUNITY")

    def test_pair_stat_arb_breakdown_stop(self):
        # Severe divergence past 3.5-sigma
        prices_b = [100.0 + (i % 10) * 0.5 for i in range(50)]
        prices_a = [2.0 * b + (i % 5 - 2) * 0.2 for i, b in enumerate(prices_b)]
        prices_a[-1] -= 3.5  # Catastrophic spread divergence Z <= -3.5

        res = stat_arb.compute_pair_stat_arb(prices_a, prices_b, ("COIN_A", "COIN_B"), interval="1h")
        self.assertEqual(res["signal"], "COINTEGRATION_BREAKDOWN_STOP")
        self.assertEqual(res["status"], "RISK_ALERT")


if __name__ == "__main__":
    unittest.main()
