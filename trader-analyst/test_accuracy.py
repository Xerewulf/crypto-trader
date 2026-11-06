#!/usr/bin/env python3
"""
Prediction Accuracy Test Suite (10m, 30m, 60m intervals).
Empirically tests directional hit rate, confidence interval coverage (50% and 80% bands),
MAE, MAPE, and edge using walk-forward evaluation on real Binance klines.
"""

import argparse
import datetime
import json
import math
import os
import random
import statistics
import sys
import urllib.request


def fetch_klines_binance(symbol, interval="5m", limit=1000):
    """Fetch historical OHLCV klines directly from Binance Spot API."""
    bsym = symbol.upper()
    if not bsym.endswith("USDT") and not bsym.endswith("FDUSD"):
        bsym += "USDT"
    url = f"https://api.binance.com/api/v3/klines?symbol={bsym}&interval={interval}&limit={limit}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def compute_micro_indicators(klines_slice):
    """Computes technical and microstructure momentum score (-100 to +100) using only past data."""
    closes = [float(k[4]) for k in klines_slice]
    if len(closes) < 30:
        return 0.0

    # 1. 5m RSI (14 period)
    diffs = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(0.0, d) for d in diffs[-14:]]
    losses = [max(0.0, -d) for d in diffs[-14:]]
    avg_gain = sum(gains) / 14.0 if gains else 0.0
    avg_loss = sum(losses) / 14.0 if losses else 0.0
    if avg_loss == 0:
        rsi = 100.0
    else:
        rs = avg_gain / avg_loss
        rsi = 100.0 - (100.0 / (1.0 + rs))

    rsi_score = max(-25.0, min(25.0, (rsi - 50.0) * 1.5))

    # 2. EMA 9 vs EMA 21 crossover
    def calc_ema(series, period):
        alpha = 2.0 / (period + 1.0)
        e = series[0]
        for val in series[1:]:
            e = alpha * val + (1.0 - alpha) * e
        return e

    ema9 = calc_ema(closes[-25:], 9)
    ema21 = calc_ema(closes[-25:], 21)
    ema_diff_pct = (ema9 - ema21) / ema21 * 100.0
    ema_score = max(-25.0, min(25.0, ema_diff_pct * 30.0))

    # 3. Volume and Taker Flow Momentum (last 12 bars = 60m)
    recent_bars = klines_slice[-12:]
    total_vol = sum(float(k[5]) for k in recent_bars)
    taker_buy_vol = sum(float(k[9]) for k in recent_bars)
    buy_ratio = (taker_buy_vol / total_vol) if total_vol > 0 else 0.5
    flow_score = max(-25.0, min(25.0, (buy_ratio - 0.5) * 100.0))

    # 4. Price Momentum (return over last 6 bars = 30m)
    mom_30m = (closes[-1] / closes[-7] - 1.0) * 100.0 if len(closes) >= 7 else 0.0
    mom_score = max(-25.0, min(25.0, mom_30m * 20.0))

    composite = rsi_score + ema_score + flow_score + mom_score
    return round(max(-100.0, min(100.0, composite)), 1)


def simulate_prediction(klines_slice, price, score, horizon_name, steps, alpha, n_paths=2000, seed=None):
    """Simulates a forward horizon using bootstrap of returns and microstructure drift."""
    rets = []
    for i in range(1, len(klines_slice)):
        p0, p1 = float(klines_slice[i - 1][4]), float(klines_slice[i][4])
        if p0 > 0:
            rets.append(math.log(p1 / p0))
    if len(rets) < 12:
        rets = [0.0005, -0.0005] * 12

    sigma = statistics.pstdev(rets) or 0.0015
    hist_drift = statistics.mean(rets)
    norm_score = max(-1.0, min(1.0, score / 100.0))
    adj_drift = 0.40 * hist_drift + alpha * norm_score * sigma

    rng = random.Random(seed or 42)
    finals = []
    up_count = 0
    for _ in range(n_paths):
        log_r = 0.0
        for _s in range(steps):
            r = rng.choice(rets) - hist_drift + adj_drift
            log_r += r
        fp = price * math.exp(log_r)
        finals.append(fp)
        if fp > price:
            up_count += 1

    finals.sort()
    p_up = round(100.0 * up_count / len(finals), 1)
    p_down = round(100.0 - p_up, 1)
    med = finals[len(finals) // 2]
    p10 = finals[int(0.10 * len(finals))]
    p25 = finals[int(0.25 * len(finals))]
    p75 = finals[int(0.75 * len(finals))]
    p90 = finals[int(0.90 * len(finals))]

    direction = "UP" if p_up >= 53.0 else ("DOWN" if p_up <= 47.0 else "SIDEWAYS")
    return {
        "horizon": horizon_name,
        "steps": steps,
        "direction": direction,
        "prob_up_pct": p_up,
        "prob_down_pct": p_down,
        "expected_price": round(med, 8),
        "range_50_low": round(p25, 8),
        "range_50_high": round(p75, 8),
        "range_80_low": round(p10, 8),
        "range_80_high": round(p90, 8),
    }


def evaluate_asset_accuracy(symbol, klines=None, n_eval_points=120, lookback_window=100):
    """
    Evaluates prediction accuracy across 10m (2 bars), 30m (6 bars), and 60m (12 bars).
    """
    if klines is None:
        klines = fetch_klines_binance(symbol, interval="5m", limit=1000)

    total_bars = len(klines)
    # Required forward bars: 12 bars (60m)
    max_forward = 12
    earliest_idx = lookback_window
    latest_idx = total_bars - max_forward - 1
    if latest_idx <= earliest_idx:
        raise ValueError(f"Not enough klines: {total_bars} bars available, need at least {earliest_idx + max_forward}")

    step_size = max(1, (latest_idx - earliest_idx) // n_eval_points)
    eval_indices = list(range(earliest_idx, latest_idx, step_size))[:n_eval_points]

    horizons_config = {
        "10m": {"steps": 2, "alpha": 0.20, "minutes": 10},
        "30m": {"steps": 6, "alpha": 0.16, "minutes": 30},
        "60m": {"steps": 12, "alpha": 0.12, "minutes": 60},
    }

    results = {
        "symbol": symbol.upper(),
        "total_evaluated_points": len(eval_indices),
        "horizons": {
            "10m": {"total": 0, "correct_direction": 0, "calls_made": 0, "high_conv_total": 0, "high_conv_correct": 0,
                    "in_80_band": 0, "in_50_band": 0, "abs_errors": [], "pct_errors": [], "pnl_pcts": []},
            "30m": {"total": 0, "correct_direction": 0, "calls_made": 0, "high_conv_total": 0, "high_conv_correct": 0,
                    "in_80_band": 0, "in_50_band": 0, "abs_errors": [], "pct_errors": [], "pnl_pcts": []},
            "60m": {"total": 0, "correct_direction": 0, "calls_made": 0, "high_conv_total": 0, "high_conv_correct": 0,
                    "in_80_band": 0, "in_50_band": 0, "abs_errors": [], "pct_errors": [], "pnl_pcts": []},
        }
    }

    for idx in eval_indices:
        history = klines[idx - lookback_window: idx + 1]
        entry_price = float(history[-1][4])
        score = compute_micro_indicators(history)

        for h_name, cfg in horizons_config.items():
            fwd_steps = cfg["steps"]
            pred = simulate_prediction(history, entry_price, score, h_name, fwd_steps, cfg["alpha"], seed=idx)
            actual_candle = klines[idx + fwd_steps]
            actual_price = float(actual_candle[4])

            price_change = actual_price - entry_price
            price_change_pct = (price_change / entry_price) * 100.0
            actual_dir = "UP" if price_change > 0 else ("DOWN" if price_change < 0 else "SIDEWAYS")

            h_res = results["horizons"][h_name]
            h_res["total"] += 1

            # Directional hit test
            if pred["direction"] in ("UP", "DOWN"):
                h_res["calls_made"] += 1
                if pred["direction"] == actual_dir:
                    h_res["correct_direction"] += 1
                # Strategy PnL: long if UP, short if DOWN
                ret = price_change_pct if pred["direction"] == "UP" else -price_change_pct
                h_res["pnl_pcts"].append(ret)

            # High confidence hit test (|prob - 50| >= 4%)
            high_conv = abs(pred["prob_up_pct"] - 50.0) >= 4.0
            if high_conv and pred["direction"] in ("UP", "DOWN"):
                h_res["high_conv_total"] += 1
                if pred["direction"] == actual_dir:
                    h_res["high_conv_correct"] += 1

            # Confidence interval calibration
            if pred["range_80_low"] <= actual_price <= pred["range_80_high"]:
                h_res["in_80_band"] += 1
            if pred["range_50_low"] <= actual_price <= pred["range_50_high"]:
                h_res["in_50_band"] += 1

            # Error metrics
            abs_err = abs(pred["expected_price"] - actual_price)
            pct_err = (abs_err / actual_price) * 100.0
            h_res["abs_errors"].append(abs_err)
            h_res["pct_errors"].append(pct_err)

    # Aggregate metrics
    summary = {
        "symbol": results["symbol"],
        "eval_points": results["total_evaluated_points"],
        "horizons": {}
    }

    for h_name, d in results["horizons"].items():
        calls = d["calls_made"]
        hit_rate = round((d["correct_direction"] / calls * 100.0), 1) if calls > 0 else 0.0
        hc_calls = d["high_conv_total"]
        hc_hit_rate = round((d["high_conv_correct"] / hc_calls * 100.0), 1) if hc_calls > 0 else 0.0
        cov_80 = round((d["in_80_band"] / d["total"] * 100.0), 1) if d["total"] > 0 else 0.0
        cov_50 = round((d["in_50_band"] / d["total"] * 100.0), 1) if d["total"] > 0 else 0.0
        mae = round(statistics.mean(d["abs_errors"]), 4) if d["abs_errors"] else 0.0
        mape = round(statistics.mean(d["pct_errors"]), 3) if d["pct_errors"] else 0.0
        avg_pnl = round(statistics.mean(d["pnl_pcts"]), 3) if d["pnl_pcts"] else 0.0
        win_loss_ratio = round(hit_rate / (100.0 - hit_rate), 2) if hit_rate < 100.0 and hit_rate > 0 else 1.0

        summary["horizons"][h_name] = {
            "eval_count": d["total"],
            "calls_made": calls,
            "hit_rate_pct": hit_rate,
            "high_conv_calls": hc_calls,
            "high_conv_hit_rate_pct": hc_hit_rate,
            "band_80_coverage_pct": cov_80,
            "band_50_coverage_pct": cov_50,
            "mae_usd": mae,
            "mape_pct": mape,
            "avg_trade_pnl_pct": avg_pnl,
            "win_loss_ratio": win_loss_ratio,
        }

    return summary


def run_full_accuracy_test(symbols=None, n_points=100, write_report=True):
    """Runs accuracy tests across multiple symbols and produces aggregate statistics."""
    if symbols is None:
        symbols = ["BTC", "ETH", "SOL", "BNB"]

    print("=" * 80)
    print(f"PREDICTION ACCURACY BENCHMARK (10m, 30m, 60m Intervals) - {datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("Evaluating walk-forward out-of-sample directional hit rate & confidence coverage...")
    print("=" * 80)

    asset_reports = {}
    for sym in symbols:
        try:
            print(f"Fetching Binance 5m klines and evaluating {sym}...")
            report = evaluate_asset_accuracy(sym, n_eval_points=n_points)
            asset_reports[sym] = report
        except Exception as e:
            print(f"Error testing {sym}: {e}", file=sys.stderr)

    if not asset_reports:
        print("No test results collected.")
        return None

    # Print summary tables
    print("\n" + "=" * 80)
    print("1. DIRECTIONAL HIT RATE ACCURACY (Was predicted direction UP / DOWN accurate?)")
    print("=" * 80)
    print(f"{'SYMBOL':<10} {'10m HIT RATE':<15} {'30m HIT RATE':<15} {'60m HIT RATE':<15} {'AVG DIRECTIONAL HIT':<20}")
    print("-" * 80)

    agg_hits = {"10m": [], "30m": [], "60m": []}
    agg_cov80 = {"10m": [], "30m": [], "60m": []}
    agg_cov50 = {"10m": [], "30m": [], "60m": []}
    agg_mape = {"10m": [], "30m": [], "60m": []}

    for sym, r in asset_reports.items():
        hz = r["horizons"]
        h10 = hz["10m"]["hit_rate_pct"]
        h30 = hz["30m"]["hit_rate_pct"]
        h60 = hz["60m"]["hit_rate_pct"]
        avg_h = round((h10 + h30 + h60) / 3.0, 1)

        agg_hits["10m"].append(h10)
        agg_hits["30m"].append(h30)
        agg_hits["60m"].append(h60)

        agg_cov80["10m"].append(hz["10m"]["band_80_coverage_pct"])
        agg_cov80["30m"].append(hz["30m"]["band_80_coverage_pct"])
        agg_cov80["60m"].append(hz["60m"]["band_80_coverage_pct"])

        agg_cov50["10m"].append(hz["10m"]["band_50_coverage_pct"])
        agg_cov50["30m"].append(hz["30m"]["band_50_coverage_pct"])
        agg_cov50["60m"].append(hz["60m"]["band_50_coverage_pct"])

        agg_mape["10m"].append(hz["10m"]["mape_pct"])
        agg_mape["30m"].append(hz["30m"]["mape_pct"])
        agg_mape["60m"].append(hz["60m"]["mape_pct"])

        print(f"{sym:<10} {h10:>6.1f}%          {h30:>6.1f}%          {h60:>6.1f}%          {avg_h:>6.1f}%")

    print("-" * 80)
    avg_10 = round(statistics.mean(agg_hits["10m"]), 1)
    avg_30 = round(statistics.mean(agg_hits["30m"]), 1)
    avg_60 = round(statistics.mean(agg_hits["60m"]), 1)
    total_avg = round((avg_10 + avg_30 + avg_60) / 3.0, 1)
    print(f"{'AGGREGATE':<10} {avg_10:>6.1f}%          {avg_30:>6.1f}%          {avg_60:>6.1f}%          {total_avg:>6.1f}%")

    print("\n" + "=" * 80)
    print("2. CONFIDENCE INTERVAL CALIBRATION (Did actual price land within predicted bands?)")
    print("=" * 80)
    print(f"{'HORIZON':<10} {'TARGET BAND':<15} {'ACTUAL COVERAGE':<18} {'CALIBRATION STATUS':<20} {'AVG MAPE (%)':<15}")
    print("-" * 80)

    for h_name, exp_val in [("10m", 80.0), ("30m", 80.0), ("60m", 80.0)]:
        cov80 = round(statistics.mean(agg_cov80[h_name]), 1)
        cov50 = round(statistics.mean(agg_cov50[h_name]), 1)
        mape = round(statistics.mean(agg_mape[h_name]), 3)
        status_80 = "EXCELLENT" if 75.0 <= cov80 <= 88.0 else ("GOOD" if 70.0 <= cov80 <= 92.0 else "FAIR")
        status_50 = "EXCELLENT" if 45.0 <= cov50 <= 58.0 else ("GOOD" if 40.0 <= cov50 <= 65.0 else "FAIR")
        print(f"{h_name:<10} {'80% Band':<15} {cov80:>6.1f}%            {status_80:<20} {mape:>6.3f}%")
        print(f"{'':<10} {'50% Band':<15} {cov50:>6.1f}%            {status_50:<20} {'-':<15}")

    final_payload = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tested_symbols": symbols,
        "eval_points_per_symbol": n_points,
        "aggregate_summary": {
            "10m": {
                "directional_hit_rate_pct": avg_10,
                "band_80_coverage_pct": round(statistics.mean(agg_cov80["10m"]), 1),
                "band_50_coverage_pct": round(statistics.mean(agg_cov50["10m"]), 1),
                "avg_mape_pct": round(statistics.mean(agg_mape["10m"]), 3),
            },
            "30m": {
                "directional_hit_rate_pct": avg_30,
                "band_80_coverage_pct": round(statistics.mean(agg_cov80["30m"]), 1),
                "band_50_coverage_pct": round(statistics.mean(agg_cov50["30m"]), 1),
                "avg_mape_pct": round(statistics.mean(agg_mape["30m"]), 3),
            },
            "60m": {
                "directional_hit_rate_pct": avg_60,
                "band_80_coverage_pct": round(statistics.mean(agg_cov80["60m"]), 1),
                "band_50_coverage_pct": round(statistics.mean(agg_cov50["60m"]), 1),
                "avg_mape_pct": round(statistics.mean(agg_mape["60m"]), 3),
            },
            "overall_directional_hit_rate_pct": total_avg,
        },
        "asset_reports": asset_reports,
    }

    if write_report:
        report_path = "trader/prediction_accuracy_report.json"
        os.makedirs("trader", exist_ok=True)
        with open(report_path, "w", encoding="utf-8") as fh:
            json.dump(final_payload, fh, indent=2)
        print(f"\nSaved detailed prediction accuracy report to {report_path}")

    return final_payload


def main():
    parser = argparse.ArgumentParser(description="Test prediction accuracy for 10m, 30m, and 60m horizons.")
    parser.add_argument("symbols", nargs="*", default=["BTC", "ETH", "SOL", "BNB"], help="Symbols to test")
    parser.add_argument("--points", type=int, default=100, help="Number of rolling test points per symbol (default: 100)")
    parser.add_argument("--no-write", action="store_true", help="Do not write report to trader/prediction_accuracy_report.json")
    args = parser.parse_args()

    run_full_accuracy_test(symbols=args.symbols, n_points=args.points, write_report=not args.no_write)


if __name__ == "__main__":
    main()
