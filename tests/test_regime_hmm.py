#!/usr/bin/env python3
"""Unit tests for Phase 6: Regime-Switching Hidden Markov Model (HMM) & Volatility Clustering Engine."""

import math
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from _load import load

regime_hmm = load("trader-analyst", "regime_hmm")


class TestGaussianHMM(unittest.TestCase):
    def test_gaussian_pdf_basics(self):
        # Peak of standard normal at 0 should be 1 / sqrt(2*pi) ~ 0.398942
        pdf_0 = regime_hmm.gaussian_pdf(0.0, 0.0, 1.0)
        self.assertAlmostEqual(pdf_0, 0.398942, places=4)

        # Symmetry
        self.assertAlmostEqual(
            regime_hmm.gaussian_pdf(1.5, 0.0, 1.0),
            regime_hmm.gaussian_pdf(-1.5, 0.0, 1.0),
            places=5,
        )

        # Log PDF consistency
        log_pdf = regime_hmm.log_gaussian_pdf(1.0, 0.0, 1.0)
        self.assertAlmostEqual(math.exp(log_pdf), regime_hmm.gaussian_pdf(1.0, 0.0, 1.0), places=5)

    def test_hmm_initialization_and_normalization(self):
        hmm = regime_hmm.GaussianHMM(n_states=3)
        self.assertEqual(hmm.n_states, 3)
        # Check start prob sum
        self.assertAlmostEqual(sum(hmm.start_prob), 1.0, places=5)
        # Check rows sum to 1.0
        for row in hmm.trans_mat:
            self.assertAlmostEqual(sum(row), 1.0, places=5)

    def test_viterbi_state_decoding_synthetic(self):
        hmm = regime_hmm.GaussianHMM(
            n_states=3,
            means=[0.005, 0.0, -0.010],
            variances=[0.00005, 0.0004, 0.0040],
            trans_mat=[
                [0.90, 0.08, 0.02],
                [0.10, 0.85, 0.05],
                [0.05, 0.15, 0.80],
            ],
            start_prob=[0.7, 0.2, 0.1],
        )

        # Synthetic observations: 20 tight positive returns (Regime 0), followed by 25 volatile shocks (Regime 2)
        obs_trend = [0.005 + (i % 2) * 0.001 for i in range(25)]
        obs_shock = [(-0.08 if i % 2 == 0 else 0.08) for i in range(25)]
        all_obs = obs_trend + obs_shock

        path = hmm.viterbi(all_obs)
        self.assertEqual(len(path), 50)
        # First section should primarily be decoded as State 0
        state0_count = sum(1 for s in path[:20] if s == 0)
        self.assertTrue(state0_count >= 15, f"Expected state 0 dominant in first part, got {state0_count}")
        # Second section should decode as State 2
        state2_count = sum(1 for s in path[25:] if s == 2)
        self.assertTrue(state2_count >= 15, f"Expected state 2 dominant in shock part, got {state2_count}")

    def test_forward_backward_posteriors(self):
        hmm = regime_hmm.GaussianHMM(n_states=3)
        obs = [0.001 * (i % 5 - 2) for i in range(30)]
        posteriors = hmm.posterior_probabilities(obs)
        self.assertEqual(len(posteriors), 30)
        for t_post in posteriors:
            self.assertEqual(len(t_post), 3)
            self.assertAlmostEqual(sum(t_post), 1.0, places=4)

    def test_baum_welch_fit(self):
        hmm = regime_hmm.GaussianHMM(n_states=3)
        # Create multimodal return distribution
        returns = [0.002] * 20 + [0.0] * 20 + [0.035, -0.040, 0.028, -0.031] * 5
        hmm.fit(returns, max_iter=15)
        # Verify variances remain positive and strictly ordered
        self.assertTrue(hmm.variances[0] <= hmm.variances[1])
        self.assertTrue(hmm.variances[1] <= hmm.variances[2])
        for v in hmm.variances:
            self.assertTrue(v > 0)

    def test_derive_regime_adaptations(self):
        ad_trend = regime_hmm.derive_regime_adaptations(0, [0.8, 0.15, 0.05], 0.01)
        self.assertEqual(ad_trend["regime_name"], "BULL_MOMENTUM")
        self.assertEqual(ad_trend["kelly_factor"], 1.0)
        self.assertEqual(ad_trend["target_r"], 2.5)
        self.assertTrue(ad_trend["is_safe_to_trade"])

        ad_shock = regime_hmm.derive_regime_adaptations(2, [0.05, 0.15, 0.80], 0.05)
        self.assertEqual(ad_shock["regime_name"], "VOLATILE_SHOCK")
        self.assertEqual(ad_shock["kelly_factor"], 0.25)
        self.assertFalse(ad_shock["is_safe_to_trade"])
        self.assertEqual(ad_shock["risk_level"], "HIGH_CRITICAL")

    def test_detect_regimes_from_candles(self):
        candles = []
        p = 100.0
        for i in range(50):
            p += (i % 3 - 1) * 0.5
            candles.append([i * 3600000, p - 0.2, p + 0.5, p - 0.5, p, 1000.0])

        report = regime_hmm.detect_regimes(candles, interval="1h")
        self.assertIn("current_regime", report)
        self.assertIn(report["current_regime"], (0, 1, 2))
        self.assertIn("posterior_probabilities", report)
        self.assertIn("adaptations", report)
        self.assertTrue(report["sample_bars"] >= 45)


if __name__ == "__main__":
    unittest.main()
