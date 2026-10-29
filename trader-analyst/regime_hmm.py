#!/usr/bin/env python3
"""Regime-Switching Hidden Markov Model (HMM) & Volatility Clustering Engine.
Standard library only (math, statistics, json, time). Zero external C-dependencies.

Mathematical & Quantitative Foundations:
  1. James D. Hamilton (1989): "A New Approach to the Economic Analysis of Nonstationary
     Time Series and the Business Cycle" (Econometrica) - Regime-Switching Models.
  2. Lawrence R. Rabiner (1989): "A Tutorial on Hidden Markov Models and Selected
     Applications in Speech Recognition" (IEEE Proceedings) - Forward-Backward,
     Baum-Welch (EM), and Viterbi algorithms.
  3. Market Microstructure Latent Regimes in Crypto:
     - Regime 0: Low-Vol Bullish Momentum / Trend Expansion (steady flow, low variance, positive drift)
     - Regime 1: Mean-Reverting Range / Auction Rotation (bounded between VAL/VAH, oscillation)
     - Regime 2: Toxic Volatility Shock / Liquidation Cascade (high variance, spiking VPIN, fat tails)

Provides:
  - Unsupervised Gaussian HMM model calibration with Baum-Welch (EM)
  - Viterbi algorithm for global optimal state path decoding
  - Forward-Backward algorithm for instantaneous posterior state probabilities
  - Regime-adaptive trade strategy adjustments (Kelly multiplier, stop buffer, target R).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import statistics
import sys
import time
from typing import Any, Dict, List, Optional, Tuple, Union

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

try:
    from . import predict
except ImportError:
    try:
        import predict
    except ImportError:
        predict = None


REGIME_NAMES = {
    0: "BULL_MOMENTUM",      # Low-Vol Trend Expansion / Positive Drift
    1: "ROTATIONAL_CHOP",    # Mean-Reverting Range / Auction Rotation
    2: "VOLATILE_SHOCK",     # Toxic Volatility Shock / Liquidation Cascade
}

REGIME_DESCRIPTIONS = {
    0: "Low-Volatility Bullish Momentum: Strong directional trend, positive drift, tight spreads.",
    1: "Mean-Reverting Range: Bounded rotational chop inside Value Area, fading extremes.",
    2: "Toxic Volatility Shock: Liquidity vacuum, cascading liquidations, wide bid-ask spread.",
}


# --------------------------------------------------------------------------- Gaussian PDF & Math Helpers

def gaussian_pdf(x: float, mean: float, variance: float) -> float:
    """Univariate Gaussian probability density function with variance flooring."""
    var = max(1e-8, float(variance))
    diff = float(x) - float(mean)
    denom = math.sqrt(2.0 * math.pi * var)
    exponent = -0.5 * (diff * diff) / var
    # Prevent numerical underflow
    if exponent < -700.0:
        return 1e-300
    return max(1e-300, (1.0 / denom) * math.exp(exponent))


def log_gaussian_pdf(x: float, mean: float, variance: float) -> float:
    """Log Gaussian PDF for numerically stable Viterbi decoding."""
    var = max(1e-8, float(variance))
    diff = float(x) - float(mean)
    return -0.5 * math.log(2.0 * math.pi * var) - 0.5 * (diff * diff) / var


# --------------------------------------------------------------------------- Gaussian HMM Engine

class GaussianHMM:
    """3-State Discrete-Time Hidden Markov Model with Gaussian Emissions.
    
    Self-contained pure Python implementation with Baum-Welch (EM) parameter
    estimation, Viterbi path decoding, and Forward-Backward posterior inference.
    """

    def __init__(
        self,
        n_states: int = 3,
        means: Optional[List[float]] = None,
        variances: Optional[List[float]] = None,
        trans_mat: Optional[List[List[float]]] = None,
        start_prob: Optional[List[float]] = None,
    ):
        self.n_states = int(n_states)
        self.means = list(means) if means is not None else [0.001, 0.0, 0.0]
        self.variances = list(variances) if variances is not None else [0.0001, 0.0004, 0.0025]
        
        if trans_mat is not None:
            self.trans_mat = [list(row) for row in trans_mat]
        else:
            # Default sticky transition matrix (crypto regimes exhibit persistence)
            self.trans_mat = [
                [0.85, 0.12, 0.03],
                [0.10, 0.82, 0.08],
                [0.08, 0.22, 0.70],
            ]
            
        self.start_prob = list(start_prob) if start_prob is not None else [0.4, 0.4, 0.2]
        self._normalize_matrices()

    def _normalize_matrices(self) -> None:
        """Ensure all probability distributions sum exactly to 1.0."""
        s = sum(self.start_prob)
        if s > 0:
            self.start_prob = [p / s for p in self.start_prob]
        else:
            self.start_prob = [1.0 / self.n_states] * self.n_states

        for i in range(self.n_states):
            row_sum = sum(self.trans_mat[i])
            if row_sum > 0:
                self.trans_mat[i] = [p / row_sum for p in self.trans_mat[i]]
            else:
                self.trans_mat[i] = [1.0 / self.n_states] * self.n_states

    def initialize_from_data(self, observations: List[float]) -> None:
        """Heuristically initialize means and variances from empirical quantiles."""
        if len(observations) < 10:
            return

        sorted_obs = sorted(observations)
        n = len(sorted_obs)
        # Partition data into tertiles to initialize state parameters
        q1 = sorted_obs[int(n * 0.33)]
        q2 = sorted_obs[int(n * 0.66)]

        low_group = [x for x in sorted_obs if x <= q1]
        mid_group = [x for x in sorted_obs if q1 < x <= q2]
        high_group = [x for x in sorted_obs if x > q2]

        def get_stat(group: List[float], def_m: float, def_v: float) -> Tuple[float, float]:
            if len(group) >= 2:
                m = statistics.mean(group)
                v = max(1e-8, statistics.variance(group))
                return m, v
            return def_m, def_v

        # Sort states by volatility:
        # State 0: Low Volatility (typically trend expansion)
        # State 1: Medium Volatility (rotational chop)
        # State 2: High Volatility (liquidation shock)
        overall_mean = statistics.mean(observations)
        overall_var = statistics.variance(observations) if len(observations) > 1 else 0.001

        m_low, v_low = get_stat(mid_group, overall_mean, overall_var * 0.3)
        m_mid, v_mid = get_stat(high_group, overall_mean, overall_var * 0.8)
        m_high, v_high = get_stat(low_group, overall_mean, overall_var * 2.5)

        # Assign variances in increasing order
        self.means = [m_low, m_mid, m_high]
        self.variances = [max(1e-7, v_low), max(1e-7, v_mid), max(1e-7, v_high)]
        # Ensure distinct ordered variances
        if self.variances[0] >= self.variances[1]:
            self.variances[1] = self.variances[0] * 1.8
        if self.variances[1] >= self.variances[2]:
            self.variances[2] = self.variances[1] * 2.2

    def forward(self, observations: List[float]) -> Tuple[List[List[float]], List[float]]:
        """Forward algorithm with scaling factors to prevent underflow."""
        T = len(observations)
        N = self.n_states
        alpha = [[0.0] * N for _ in range(T)]
        scale = [0.0] * T

        # Initialization at t = 0
        obs0 = observations[0]
        for i in range(N):
            alpha[0][i] = self.start_prob[i] * gaussian_pdf(obs0, self.means[i], self.variances[i])
        scale[0] = sum(alpha[0])
        if scale[0] == 0:
            scale[0] = 1e-300
        for i in range(N):
            alpha[0][i] /= scale[0]

        # Induction for t = 1 to T - 1
        for t in range(1, T):
            obs_t = observations[t]
            for j in range(N):
                sum_trans = sum(alpha[t - 1][i] * self.trans_mat[i][j] for i in range(N))
                alpha[t][j] = sum_trans * gaussian_pdf(obs_t, self.means[j], self.variances[j])
            scale[t] = sum(alpha[t])
            if scale[t] == 0:
                scale[t] = 1e-300
            for j in range(N):
                alpha[t][j] /= scale[t]

        return alpha, scale

    def backward(self, observations: List[float], scale: List[float]) -> List[List[float]]:
        """Backward algorithm using scaling factors from forward pass."""
        T = len(observations)
        N = self.n_states
        beta = [[0.0] * N for _ in range(T)]

        # Initialization at t = T - 1
        for i in range(N):
            beta[T - 1][i] = 1.0

        # Induction for t = T - 2 down to 0
        for t in range(T - 2, -1, -1):
            obs_next = observations[t + 1]
            scale_t = scale[t + 1] if scale[t + 1] > 0 else 1e-300
            for i in range(N):
                beta[t][i] = sum(
                    self.trans_mat[i][j] * gaussian_pdf(obs_next, self.means[j], self.variances[j]) * beta[t + 1][j]
                    for j in range(N)
                ) / scale_t

        return beta

    def posterior_probabilities(self, observations: List[float]) -> List[List[float]]:
        """Calculate smoothed posterior regime probabilities P(S_t = i | O_{1:T})."""
        if not observations:
            return []
        alpha, scale = self.forward(observations)
        beta = self.backward(observations, scale)
        T = len(observations)
        N = self.n_states
        gamma = [[0.0] * N for _ in range(T)]

        for t in range(T):
            row_sum = sum(alpha[t][i] * beta[t][i] for i in range(N))
            if row_sum > 0:
                for i in range(N):
                    gamma[t][i] = (alpha[t][i] * beta[t][i]) / row_sum
            else:
                for i in range(N):
                    gamma[t][i] = 1.0 / N

        return gamma

    def viterbi(self, observations: List[float]) -> List[int]:
        """Viterbi algorithm for global most likely sequence of hidden states."""
        T = len(observations)
        if T == 0:
            return []
        N = self.n_states

        viterbi_mat = [[-float("inf")] * N for _ in range(T)]
        backpointer = [[0] * N for _ in range(T)]

        # Initialization at t = 0 (in log space)
        obs0 = observations[0]
        for i in range(N):
            p_start = max(1e-12, self.start_prob[i])
            viterbi_mat[0][i] = math.log(p_start) + log_gaussian_pdf(obs0, self.means[i], self.variances[i])

        # Recursion for t = 1 to T - 1
        for t in range(1, T):
            obs_t = observations[t]
            for j in range(N):
                log_emission = log_gaussian_pdf(obs_t, self.means[j], self.variances[j])
                best_prob = -float("inf")
                best_state = 0
                for i in range(N):
                    p_trans = max(1e-12, self.trans_mat[i][j])
                    prob = viterbi_mat[t - 1][i] + math.log(p_trans) + log_emission
                    if prob > best_prob:
                        best_prob = prob
                        best_state = i
                viterbi_mat[t][j] = best_prob
                backpointer[t][j] = best_state

        # Termination & Path Reconstruction
        best_last_state = max(range(N), key=lambda i: viterbi_mat[T - 1][i])
        best_path = [0] * T
        best_path[T - 1] = best_last_state
        for t in range(T - 2, -1, -1):
            best_path[t] = backpointer[t + 1][best_path[t + 1]]

        return best_path

    def fit(self, observations: List[float], max_iter: int = 25, tol: float = 1e-4) -> "GaussianHMM":
        """Baum-Welch (Expectation-Maximization) parameter estimation."""
        T = len(observations)
        if T < 15:
            return self

        self.initialize_from_data(observations)
        N = self.n_states
        prev_log_lik = -float("inf")

        for iteration in range(max_iter):
            # E-step: Compute forward, backward, gamma, and xi
            alpha, scale = self.forward(observations)
            beta = self.backward(observations, scale)

            log_lik = sum(math.log(max(1e-300, s)) for s in scale)
            if abs(log_lik - prev_log_lik) < tol and iteration > 3:
                break
            prev_log_lik = log_lik

            gamma = [[0.0] * N for _ in range(T)]
            for t in range(T):
                denom = sum(alpha[t][i] * beta[t][i] for i in range(N))
                denom = denom if denom > 0 else 1e-300
                for i in range(N):
                    gamma[t][i] = (alpha[t][i] * beta[t][i]) / denom

            xi = [[[0.0] * N for _ in range(N)] for _ in range(T - 1)]
            for t in range(T - 1):
                obs_next = observations[t + 1]
                denom = sum(
                    alpha[t][i] * self.trans_mat[i][j] * gaussian_pdf(obs_next, self.means[j], self.variances[j]) * beta[t + 1][j]
                    for i in range(N)
                    for j in range(N)
                )
                denom = denom if denom > 0 else 1e-300
                for i in range(N):
                    for j in range(N):
                        numer = alpha[t][i] * self.trans_mat[i][j] * gaussian_pdf(obs_next, self.means[j], self.variances[j]) * beta[t + 1][j]
                        xi[t][i][j] = numer / denom

            # M-step: Update start_prob, trans_mat, means, variances
            # 1. Start probabilities
            for i in range(N):
                self.start_prob[i] = max(1e-4, gamma[0][i])
            s_start = sum(self.start_prob)
            self.start_prob = [p / s_start for p in self.start_prob]

            # 2. Transition matrix
            for i in range(N):
                gamma_sum_T1 = sum(gamma[t][i] for t in range(T - 1))
                gamma_sum_T1 = max(1e-8, gamma_sum_T1)
                for j in range(N):
                    xi_sum = sum(xi[t][i][j] for t in range(T - 1))
                    self.trans_mat[i][j] = max(1e-4, xi_sum / gamma_sum_T1)
                row_sum = sum(self.trans_mat[i])
                self.trans_mat[i] = [p / row_sum for p in self.trans_mat[i]]

            # 3. Means & Variances
            for i in range(N):
                gamma_sum_all = sum(gamma[t][i] for t in range(T))
                gamma_sum_all = max(1e-8, gamma_sum_all)
                new_mean = sum(gamma[t][i] * observations[t] for t in range(T)) / gamma_sum_all
                new_var = sum(gamma[t][i] * ((observations[t] - new_mean) ** 2) for t in range(T)) / gamma_sum_all
                self.means[i] = float(new_mean)
                self.variances[i] = max(1e-7, float(new_var))

            # Re-sort states by variance to maintain consistent regime labels:
            # State 0 (lowest var) -> State 1 (mid var) -> State 2 (highest var)
            indexed_vars = sorted(enumerate(self.variances), key=lambda x: x[1])
            new_order = [idx for idx, _ in indexed_vars]
            if new_order != list(range(N)):
                self.means = [self.means[idx] for idx in new_order]
                self.variances = [self.variances[idx] for idx in new_order]
                self.start_prob = [self.start_prob[idx] for idx in new_order]
                self.trans_mat = [[self.trans_mat[i][j] for j in new_order] for i in new_order]

        return self


# --------------------------------------------------------------------------- Strategy Adaptations

def derive_regime_adaptations(
    regime_id: int,
    probabilities: List[float],
    volatility: float,
) -> Dict[str, Any]:
    """Calculate institutional risk and trade adaptations according to current regime."""
    p_trend = probabilities[0] if len(probabilities) > 0 else 0.33
    p_chop = probabilities[1] if len(probabilities) > 1 else 0.33
    p_shock = probabilities[2] if len(probabilities) > 2 else 0.33

    if regime_id == 0:  # Bullish Momentum / Trend Expansion
        strategy = "TREND_FOLLOWING_EXPANSION"
        kelly_factor = 1.0
        target_r = 2.5
        stop_buffer_mult = 1.0
        recommended_action = "Ride pullbacks to VWAP/VAL; trail stops along EMA20; maximize Sharpe."
        is_safe_to_trade = True
        risk_level = "LOW_MODERATE"
    elif regime_id == 1:  # Rotational Chop / Mean-Reversion
        strategy = "ROTATIONAL_AUCTION_REVERSION"
        kelly_factor = 0.70
        target_r = 1.5
        stop_buffer_mult = 1.25
        recommended_action = "Fade Value Area High/Low boundaries; target Point of Control (POC); tighten targets."
        is_safe_to_trade = True
        risk_level = "MODERATE"
    else:  # Volatile Shock / Liquidation Cascade
        strategy = "DEFENSIVE_CAPITAL_PRESERVATION"
        kelly_factor = 0.25
        target_r = 1.0
        stop_buffer_mult = 0.50
        recommended_action = "HALT new breakout entries; de-lever to 1x-2x; wait for VPIN and CVD to stabilize."
        is_safe_to_trade = False
        risk_level = "HIGH_CRITICAL"

    return {
        "regime_id": regime_id,
        "regime_name": REGIME_NAMES.get(regime_id, "UNKNOWN"),
        "description": REGIME_DESCRIPTIONS.get(regime_id, ""),
        "probabilities": {
            "bull_momentum": round(p_trend, 4),
            "rotational_chop": round(p_chop, 4),
            "volatile_shock": round(p_shock, 4),
        },
        "strategy": strategy,
        "kelly_factor": kelly_factor,
        "target_r": target_r,
        "stop_buffer_mult": stop_buffer_mult,
        "is_safe_to_trade": is_safe_to_trade,
        "risk_level": risk_level,
        "recommended_action": recommended_action,
        "volatility_annualized": round(volatility * math.sqrt(365 * 24) * 100.0, 2),
    }


# --------------------------------------------------------------------------- High-Level Analysis Function

def detect_regimes(
    klines: List[Union[Dict[str, Any], List[Any]]],
    interval: str = "1h",
) -> Dict[str, Any]:
    """Fit HMM on candle log-returns and decode latent regime dynamics."""
    if not klines or len(klines) < 15:
        return {
            "error": "Insufficient kline data (minimum 15 bars required)",
            "current_regime": 1,
            "regime_name": REGIME_NAMES[1],
            "probabilities": {"bull_momentum": 0.33, "rotational_chop": 0.34, "volatile_shock": 0.33},
        }

    # Extract closing prices
    closes = []
    for k in klines:
        if isinstance(k, dict):
            c = float(k.get("close", k.get("c", 0)))
        elif isinstance(k, (list, tuple)) and len(k) >= 5:
            c = float(k[4])
        else:
            continue
        if c > 0:
            closes.append(c)

    if len(closes) < 15:
        return {
            "error": "Insufficient valid closing prices",
            "current_regime": 1,
            "regime_name": REGIME_NAMES[1],
        }

    # Calculate log returns: r_t = ln(P_t / P_{t-1})
    returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    volatility = statistics.stdev(returns) if len(returns) > 1 else 0.01

    # Fit Gaussian HMM
    hmm = GaussianHMM(n_states=3)
    hmm.fit(returns, max_iter=30)

    # Decode most likely path and get posterior probabilities
    viterbi_states = hmm.viterbi(returns)
    posteriors = hmm.posterior_probabilities(returns)

    current_state = viterbi_states[-1] if viterbi_states else 1
    current_probs = posteriors[-1] if posteriors else [0.33, 0.34, 0.33]

    # Calculate state distribution over the lookback window
    state_counts = {0: 0, 1: 0, 2: 0}
    for s in viterbi_states:
        state_counts[s] = state_counts.get(s, 0) + 1
    total_bars = len(viterbi_states)
    state_percentages = {
        REGIME_NAMES[k]: round((v / total_bars) * 100.0, 1) for k, v in state_counts.items()
    }

    adaptations = derive_regime_adaptations(current_state, current_probs, volatility)

    return {
        "symbol": "CRYPTO",
        "interval": interval,
        "sample_bars": len(closes),
        "current_regime": current_state,
        "regime_name": REGIME_NAMES.get(current_state, "UNKNOWN"),
        "description": REGIME_DESCRIPTIONS.get(current_state, ""),
        "posterior_probabilities": {
            "bull_momentum": round(current_probs[0], 4),
            "rotational_chop": round(current_probs[1], 4),
            "volatile_shock": round(current_probs[2], 4),
        },
        "transition_matrix": [
            [round(p, 4) for p in row] for row in hmm.trans_mat
        ],
        "state_means": [round(m, 6) for m in hmm.means],
        "state_volatilities": [round(math.sqrt(v), 6) for v in hmm.variances],
        "state_distribution_pct": state_percentages,
        "adaptations": adaptations,
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def analyze_symbol_regime(symbol: str = "BTCUSDT", interval: str = "1h", limit: int = 150) -> Dict[str, Any]:
    """Fetch live candles from Binance and compute real-time HMM regime classification."""
    clean_sym = symbol.upper()
    if not clean_sym.endswith(("USDT", "FDUSD", "USDC")):
        clean_sym += "USDT"

    if predict is None:
        raise RuntimeError("trader-analyst/predict.py is required to fetch live market klines")

    raw_klines = predict.spot_get(f"/api/v3/klines?symbol={clean_sym}&interval={interval}&limit={limit}")
    parsed = json.loads(raw_klines) if isinstance(raw_klines, str) else raw_klines
    res = detect_regimes(parsed, interval=interval)
    res["symbol"] = clean_sym
    return res


# --------------------------------------------------------------------------- CLI Driver

def main():
    parser = argparse.ArgumentParser(description="Regime-Switching Hidden Markov Model (HMM) & Volatility Clustering")
    parser.add_argument("symbol", nargs="?", default="BTCUSDT", help="Crypto symbol (e.g. BTC, ETH, SOL, BNB)")
    parser.add_argument("--interval", default="1h", choices=["15m", "1h", "4h", "1d"], help="Kline interval")
    parser.add_argument("--limit", type=int, default=150, help="Number of candles (default: 150)")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    args = parser.parse_args()

    try:
        report = analyze_symbol_regime(args.symbol, interval=args.interval, limit=args.limit)
    except Exception as e:
        print(f"Error fetching market data: {e}", file=sys.stderr)
        # Fallback synthetic demo run
        dummy_klines = []
        p = 50000.0
        for i in range(150):
            p += random.gauss(10.0, 500.0)
            dummy_klines.append([0, p - 50, p + 50, p - 80, p, 100])
        report = detect_regimes(dummy_klines, interval=args.interval)
        report["symbol"] = args.symbol.upper()
        report["note"] = "Offline synthetic demonstration"

    if args.json:
        print(json.dumps(report, indent=2))
        return

    ad = report.get("adaptations", {})
    probs = report.get("posterior_probabilities", {})
    print("=" * 78)
    print(f"  QUANTITATIVE HMM MARKET REGIME CLASSIFIER: {report.get('symbol')} ({args.interval})")
    print("=" * 78)
    print(f"  Current Latent State : [{report.get('current_regime')}] {report.get('regime_name')}")
    print(f"  Description          : {report.get('description')}")
    print(f"  Risk Profile         : {ad.get('risk_level')}")
    print(f"  Safe To Trade?       : {'YES' if ad.get('is_safe_to_trade') else 'NO - DEFENSIVE HALT'}")
    print("-" * 78)
    print("  Posterior State Probabilities P(S_t | O_1:T):")
    print(f"    - Bullish Momentum  (Regime 0): {probs.get('bull_momentum', 0) * 100:.1f}%")
    print(f"    - Rotational Chop   (Regime 1): {probs.get('rotational_chop', 0) * 100:.1f}%")
    print(f"    - Volatile Shock    (Regime 2): {probs.get('volatile_shock', 0) * 100:.1f}%")
    print("-" * 78)
    print("  Regime-Adaptive Trade Strategy Parameters:")
    print(f"    - Active Strategy   : {ad.get('strategy')}")
    print(f"    - Kelly Multiplier  : {ad.get('kelly_factor')}x (Position Sizing)")
    print(f"    - Target R-Multiple : {ad.get('target_r')}R")
    print(f"    - Stop Buffer Multi : {ad.get('stop_buffer_mult')}x")
    print(f"    - Annualized Vol    : {ad.get('volatility_annualized')}%")
    print(f"    - Recommendation    : {ad.get('recommended_action')}")
    print("=" * 78)


if __name__ == "__main__":
    main()
