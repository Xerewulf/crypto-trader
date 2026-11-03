#!/usr/bin/env python3
"""Dynamic Kalman Filter & Ornstein-Uhlenbeck (OU) Pairs Statistical Arbitrage Engine.
Standard library only (math, statistics, json, time). Zero external C-dependencies.

Mathematical & Quantitative Foundations:
  1. Rudolf E. Kálmán (1960): "A New Approach to Linear Filtering and Prediction Problems"
     - Online recursive state-space tracking of time-varying cointegration hedge ratio (beta_t)
       and level offset (alpha_t) with zero look-ahead bias.
  2. Leonard Ornstein & George Uhlenbeck (1930): "On the Theory of the Brownian Motion"
     - Stochastic Differential Equation (SDE) modeling mean-reverting spread dynamics:
       dX_t = theta * (mu - X_t) dt + sigma * dW_t
     - Analytical calibration of mean-reversion speed (theta), spread half-life (tau_1/2),
       equilibrium mean (mu), and stationary variance (sigma_eq^2).
  3. Statistical Arbitrage Execution:
     - Generates exact Z-Scores, entry triggers (|Z| >= 2.0), profit convergence (|Z| <= 0.25),
       and cointegration breakdown stop-loss thresholds (|Z| >= 3.5).
"""

from __future__ import annotations

import argparse
import datetime
import json
import math
import os
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


# Canonical crypto pairs with strong economic or structural co-movement
DEFAULT_PAIRS = [
    ("ETHUSDT", "BTCUSDT"),
    ("SOLUSDT", "ETHUSDT"),
    ("SOLUSDT", "BTCUSDT"),
    ("BNBUSDT", "BTCUSDT"),
    ("AVAXUSDT", "SOLUSDT"),
    ("DOGEUSDT", "BTCUSDT"),
    ("XRPUSDT", "BTCUSDT"),
    ("LINKUSDT", "ETHUSDT"),
    ("SUIUSDT", "SOLUSDT"),
    ("ADAUSDT", "BTCUSDT"),
]


# --------------------------------------------------------------------------- Online Kalman Filter

class OnlineKalmanFilter:
    """Recursive Online Kalman Filter estimating dynamic hedge ratio beta_t and alpha_t.
    
    Model:
        y_t = [1, x_t] * [alpha_t, beta_t]^T + epsilon_t,  epsilon_t ~ N(0, R)
        theta_t = theta_{t-1} + w_t,                         w_t ~ N(0, Q)
    """

    def __init__(self, delta: float = 1e-4, r_variance: float = 1e-3):
        # State vector: [alpha, beta]
        self.theta = [0.0, 1.0]
        # State covariance P (2x2 matrix initialized with high uncertainty)
        self.P = [
            [1.0, 0.0],
            [0.0, 1.0],
        ]
        # Process noise covariance Q: Q = (delta / (1 - delta)) * I
        q_val = delta / (1.0 - delta) if delta < 1.0 else 1e-4
        self.Q = [
            [q_val, 0.0],
            [0.0, q_val],
        ]
        # Measurement noise variance R
        self.R = float(r_variance)

    def update(self, y: float, x: float) -> Tuple[float, float, float]:
        """Update filter with new observation pair (y, x).
        
        Returns:
            (alpha_t, beta_t, spread_error)
        """
        # Observation row vector F = [1.0, x]
        f0 = 1.0
        f1 = float(x)
        y_val = float(y)

        # 1. State Prediction: theta_{t|t-1} = theta_{t-1}
        # 2. Covariance Prediction: P_{t|t-1} = P_{t-1} + Q
        p00 = self.P[0][0] + self.Q[0][0]
        p01 = self.P[0][1] + self.Q[0][1]
        p10 = self.P[1][0] + self.Q[1][0]
        p11 = self.P[1][1] + self.Q[1][1]

        # 3. Measurement Prediction: y_hat = F * theta
        y_hat = f0 * self.theta[0] + f1 * self.theta[1]
        error = y_val - y_hat

        # 4. Innovation Covariance: S = F * P * F^T + R
        # F * P = [f0*p00 + f1*p10, f0*p01 + f1*p11]
        fp0 = f0 * p00 + f1 * p10
        fp1 = f0 * p01 + f1 * p11
        S = (fp0 * f0 + fp1 * f1) + self.R
        S = max(1e-9, S)

        # 5. Kalman Gain: K = P * F^T / S = [p00*f0 + p01*f1, p10*f0 + p11*f1]^T / S
        k0 = (p00 * f0 + p01 * f1) / S
        k1 = (p10 * f0 + p11 * f1) / S

        # 6. State Update: theta = theta + K * error
        self.theta[0] += k0 * error
        self.theta[1] += k1 * error

        # 7. Covariance Update: P = (I - K * F) * P_pred
        # K * F = [[k0*f0, k0*f1], [k1*f0, k1*f1]]
        ikf00 = 1.0 - k0 * f0
        ikf01 = -k0 * f1
        ikf10 = -k1 * f0
        ikf11 = 1.0 - k1 * f1

        self.P[0][0] = ikf00 * p00 + ikf01 * p10
        self.P[0][1] = ikf00 * p01 + ikf01 * p11
        self.P[1][0] = ikf10 * p00 + ikf11 * p10
        self.P[1][1] = ikf10 * p01 + ikf11 * p11

        return self.theta[0], self.theta[1], error


# --------------------------------------------------------------------------- Ornstein-Uhlenbeck Fitter

def fit_ornstein_uhlenbeck(spread: List[float], dt: float = 1.0) -> Dict[str, Any]:
    """Fit Ornstein-Uhlenbeck SDE parameters via discrete AR(1) calibration.
    
    AR(1) model:
        S_t = a + b * S_{t-1} + eta_t
    
    OU parameters:
        theta = -ln(b) / dt         (mean-reversion speed)
        half_life = ln(2) / theta   (reversion half-life)
        mu = a / (1 - b)            (long-term equilibrium level)
        sigma_eq = sqrt(Var(eta) / (1 - b^2)) (equilibrium standard deviation)
    """
    n = len(spread)
    if n < 10:
        return {
            "is_mean_reverting": False,
            "error": "Insufficient spread samples (minimum 10 required)",
            "z_score": 0.0,
            "theta": 0.0,
            "half_life": float("inf"),
            "mu": 0.0,
            "sigma": 0.0,
        }

    # Extract lagged pairs
    x_lag = spread[:-1]
    y_curr = spread[1:]
    k = len(x_lag)

    mean_x = sum(x_lag) / k
    mean_y = sum(y_curr) / k

    num = sum((x_lag[i] - mean_x) * (y_curr[i] - mean_y) for i in range(k))
    den = sum((x_lag[i] - mean_x) ** 2 for i in range(k))

    if den <= 1e-12:
        return {
            "is_mean_reverting": False,
            "error": "Degenerate zero-variance spread",
            "z_score": 0.0,
            "theta": 0.0,
            "half_life": float("inf"),
            "mu": mean_x,
            "sigma": 0.0,
        }

    b = num / den
    a = mean_y - b * mean_x

    # Residuals and variance
    residuals = [y_curr[i] - (a + b * x_lag[i]) for i in range(k)]
    var_eta = sum(r * r for r in residuals) / max(1, (k - 2))

    # Check mean-reversion stability: 0 < b < 1
    if 0.0001 < b < 0.9999:
        is_mean_reverting = True
        theta = -math.log(b) / dt
        half_life = math.log(2.0) / theta
        mu = a / (1.0 - b)
        var_eq = var_eta / (1.0 - b * b)
        sigma_eq = math.sqrt(max(1e-12, var_eq))
    else:
        # Non-stationary / trending drift
        is_mean_reverting = False
        theta = 0.0
        half_life = float("inf")
        mu = mean_y
        sigma_eq = math.sqrt(max(1e-12, var_eta))

    last_val = spread[-1]
    z_score = (last_val - mu) / sigma_eq if sigma_eq > 1e-8 else 0.0

    return {
        "is_mean_reverting": is_mean_reverting,
        "ar1_b": round(b, 6),
        "ar1_a": round(a, 6),
        "theta": round(theta, 6),
        "half_life_units": round(half_life, 2) if half_life != float("inf") else None,
        "mu": round(mu, 6),
        "sigma_equilibrium": round(sigma_eq, 6),
        "last_spread": round(last_val, 6),
        "z_score": round(z_score, 4),
    }


# --------------------------------------------------------------------------- Pairs Stat-Arb Model

def compute_pair_stat_arb(
    prices_a: List[float],
    prices_b: List[float],
    pair_name: Tuple[str, str],
    interval: str = "1h",
) -> Dict[str, Any]:
    """Execute Kalman filter + Ornstein-Uhlenbeck analysis for a single asset pair."""
    sym_a, sym_b = pair_name
    n = min(len(prices_a), len(prices_b))
    if n < 15:
        return {"error": f"Insufficient aligned price data for {sym_a}-{sym_b} ({n} bars)"}

    # Normalize series lengths
    pa = prices_a[-n:]
    pb = prices_b[-n:]

    # Run Online Kalman Filter
    kf = OnlineKalmanFilter(delta=1e-4, r_variance=1e-3)
    # Warm up hedge ratio using initial ratio
    kf.theta = [0.0, pa[0] / max(1e-8, pb[0])]

    spread_series = []
    alphas = []
    betas = []

    for t in range(n):
        alpha_t, beta_t, err = kf.update(pa[t], pb[t])
        spread_val = pa[t] - (alpha_t + beta_t * pb[t])
        spread_series.append(spread_val)
        alphas.append(alpha_t)
        betas.append(beta_t)

    # Fit Ornstein-Uhlenbeck on Kalman residual spread
    ou = fit_ornstein_uhlenbeck(spread_series, dt=1.0)
    z = ou.get("z_score", 0.0)
    current_beta = betas[-1]
    current_alpha = alphas[-1]

    # Actionable signal logic
    if z <= -2.0:
        signal = "LONG_SPREAD"
        conviction = min(100, int(abs(z) * 30))
        action = f"BUY {sym_a} / SELL {current_beta:.4f} {sym_b} (Spread undervalued at Z={z:.2f})"
        status = "ACTIVE_OPPORTUNITY"
    elif z >= 2.0:
        signal = "SHORT_SPREAD"
        conviction = min(100, int(abs(z) * 30))
        action = f"SELL {sym_a} / BUY {current_beta:.4f} {sym_b} (Spread overvalued at Z={z:.2f})"
        status = "ACTIVE_OPPORTUNITY"
    elif abs(z) <= 0.25:
        signal = "MEAN_REVERSION_EXIT"
        conviction = 0
        action = "Spread near equilibrium mean (|Z| <= 0.25). Take profit and flatten spread."
        status = "NEUTRAL_EQUILIBRIUM"
    else:
        signal = "HOLD_NEUTRAL"
        conviction = 0
        action = f"Spread within normal bounds (Z={z:.2f}). No trade trigger."
        status = "MONITORING"

    # Cointegration Breakdown Stop-Loss Guard
    if abs(z) >= 3.5:
        signal = "COINTEGRATION_BREAKDOWN_STOP"
        action = f"CRITICAL: Spread diverged past 3.5-sigma (Z={z:.2f}). Cut losses; hedge relationship severed."
        status = "RISK_ALERT"

    return {
        "pair": f"{sym_a}/{sym_b}",
        "asset_a": sym_a,
        "asset_b": sym_b,
        "interval": interval,
        "sample_bars": n,
        "price_a": pa[-1],
        "price_b": pb[-1],
        "kalman": {
            "hedge_ratio_beta": round(current_beta, 6),
            "offset_alpha": round(current_alpha, 6),
            "last_spread": round(spread_series[-1], 6),
        },
        "ornstein_uhlenbeck": ou,
        "z_score": round(z, 2),
        "signal": signal,
        "conviction_pct": conviction,
        "status": status,
        "action": action,
        "half_life_bars": ou.get("half_life_units"),
        "expected_holding_time": f"~{ou.get('half_life_units')} {interval} bars" if ou.get("half_life_units") else "N/A",
    }


# --------------------------------------------------------------------------- Multi-Pair Scanner

def scan_stat_arb_universe(
    pairs: Optional[List[Tuple[str, str]]] = None,
    interval: str = "1h",
    limit: int = 150,
) -> Dict[str, Any]:
    """Scan candidate cryptocurrency pairs on Binance for dynamic Kalman + OU arbitrage."""
    target_pairs = pairs or DEFAULT_PAIRS

    if predict is None:
        raise RuntimeError("trader-analyst/predict.py is required to fetch live market klines")

    # Gather unique symbols to fetch klines efficiently
    unique_symbols = set()
    for sa, sb in target_pairs:
        unique_symbols.add(sa)
        unique_symbols.add(sb)

    klines_cache = {}
    for s in unique_symbols:
        try:
            raw = predict.spot_get(f"/api/v3/klines?symbol={s}&interval={interval}&limit={limit}")
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            closes = [float(k[4]) for k in parsed if len(k) >= 5]
            klines_cache[s] = closes
        except Exception:
            klines_cache[s] = []

    results = []
    opportunities = []

    for sa, sb in target_pairs:
        pa = klines_cache.get(sa, [])
        pb = klines_cache.get(sb, [])
        if len(pa) >= 20 and len(pb) >= 20:
            res = compute_pair_stat_arb(pa, pb, (sa, sb), interval=interval)
            results.append(res)
            if res.get("status") in ("ACTIVE_OPPORTUNITY", "RISK_ALERT"):
                opportunities.append(res)

    # Sort opportunities by absolute Z-Score descending
    opportunities.sort(key=lambda x: abs(x.get("z_score", 0.0)), reverse=True)

    return {
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "interval": interval,
        "scanned_pairs_count": len(results),
        "active_opportunities_count": len(opportunities),
        "opportunities": opportunities,
        "all_pairs": results,
    }


# --------------------------------------------------------------------------- CLI Driver

def main():
    parser = argparse.ArgumentParser(description="Dynamic Kalman Filter & Ornstein-Uhlenbeck Pairs Statistical Arbitrage")
    parser.add_argument("pair", nargs="?", default=None, help="Specific pair like ETH/BTC or SOL/ETH")
    parser.add_argument("--interval", default="1h", choices=["15m", "1h", "4h", "1d"], help="Kline interval")
    parser.add_argument("--limit", type=int, default=150, help="Number of candles (default: 150)")
    parser.add_argument("--scan", action="store_true", help="Scan full canonical pairs universe")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    args = parser.parse_args()

    if args.scan or args.pair is None:
        report = scan_stat_arb_universe(interval=args.interval, limit=args.limit)
    else:
        parts = args.pair.replace("-", "/").replace("_", "/").split("/")
        if len(parts) == 2:
            sym_a = predict.to_binance(parts[0]) if predict else parts[0].upper() + "USDT"
            sym_b = predict.to_binance(parts[1]) if predict else parts[1].upper() + "USDT"
            report = scan_stat_arb_universe(pairs=[(sym_a, sym_b)], interval=args.interval, limit=args.limit)
        else:
            report = scan_stat_arb_universe(interval=args.interval, limit=args.limit)

    if args.json:
        print(json.dumps(report, indent=2))
        return

    print("=" * 82)
    print(f"  DYNAMIC KALMAN FILTER & ORNSTEIN-UHLENBECK STATISTICAL ARBITRAGE ({args.interval})")
    print("=" * 82)
    print(f"  Scanned Pairs : {report.get('scanned_pairs_count')}  |  Active Signals (|Z| >= 2.0): {report.get('active_opportunities_count')}")
    print("-" * 82)
    print(f"  {'PAIR':<16} {'Z-SCORE':<9} {'KALMAN BETA':<13} {'HALF-LIFE':<12} {'SIGNAL':<22} {'STATUS'}")
    print("-" * 82)

    for item in report.get("all_pairs", []):
        pair = item.get("pair", "")
        z = item.get("z_score", 0.0)
        beta = item.get("kalman", {}).get("hedge_ratio_beta", 0.0)
        hl = item.get("half_life_bars")
        hl_str = f"{hl:.1f} bars" if hl is not None else "N/A"
        sig = item.get("signal", "")
        status = item.get("status", "")
        print(f"  {pair:<16} {z:>+6.2f}    {beta:>10.4f}   {hl_str:>10}   {sig:<22} {status}")

    opps = report.get("opportunities", [])
    if opps:
        print("\n" + "=" * 82)
        print("  ACTIVE ACTIONABLE STAT-ARB OPPORTUNITIES:")
        print("=" * 82)
        for op in opps:
            print(f"  [>] {op.get('pair')}: {op.get('action')}")
            print(f"      Hedge Ratio: 1 {op.get('asset_a')} to {op.get('kalman', {}).get('hedge_ratio_beta')} {op.get('asset_b')}")
            print(f"      Mean-Reversion Half-Life: {op.get('expected_holding_time')}")
            print("-" * 82)


if __name__ == "__main__":
    main()
