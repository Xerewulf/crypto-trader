#!/usr/bin/env python3
"""
5-Coin Short-Term Intraday Forecaster (10m, 30m, 60m Estimation Engine)
trader-analyst/intraday_estimator.py

Dedicated exclusively to the 5 primary coins:
  - BNBUSDT (Binance Coin)
  - SOLUSDT (Solana)
  - BTCUSDT (Bitcoin)
  - ETHUSDT (Ethereum)
  - DOGEUSDT (Dogecoin)

Calculates real-time 10-minute, 30-minute, and 60-minute directional estimations:
  - Probability (%) price goes UP vs DOWN
  - Expected price move (%) and 80% confidence price range
  - LONG Outcome: Win % chance, expected gain %, stop risk %, and mathematical edge
  - SHORT Outcome: Win % chance, expected gain %, stop risk %, and mathematical edge
  - High-conviction recommendation (LONG or SHORT) with percentage advantage
"""

import os
import sys
import json
import math
import random
import statistics
import argparse
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import predict

TARGET_COINS = ["BNBUSDT", "SOLUSDT", "BTCUSDT", "ETHUSDT", "DOGEUSDT"]


def ema_series(values, period):
    if not values:
        return []
    k = 2.0 / (period + 1.0)
    ema = [values[0]]
    for v in values[1:]:
        ema.append(v * k + ema[-1] * (1.0 - k))
    return ema


def rsi_14(closes):
    if len(closes) < 15:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(0.0, diff))
        losses.append(max(0.0, -diff))
    avg_gain = sum(gains[-14:]) / 14.0
    avg_loss = sum(losses[-14:]) / 14.0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 2)


def compute_intraday_microstructure_score(k1m, k5m, depth, fut):
    """
    Computes a high-frequency composite score (-100 to +100) combining:
      1. 1m & 5m Spot Taker CVD and Taker Buy % (weight: 35%)
      2. Order Book Depth Imbalance +-0.5% and +-1.0% (weight: 25%)
      3. Fast Momentum (EMA 9/21 crossover & RSI 14 on 1m/5m) (weight: 25%)
      4. Perpetual Futures Taker & Whale Flow (weight: 15%)
    """
    score = 0.0

    # 1. Spot Order Flow & Taker Buy %
    if k1m and len(k1m) >= 15:
        recent_1m = k1m[-15:]
        tot_vol = sum(float(k[7]) for k in recent_1m)
        buy_vol = sum(float(k[10]) for k in recent_1m)
        sell_vol = max(0.0, tot_vol - buy_vol)
        taker_ratio = (buy_vol / tot_vol) if tot_vol > 0 else 0.5
        cvd_net = buy_vol - sell_vol

        # Taker ratio contribution (-20 to +20)
        score += (taker_ratio - 0.5) * 80.0

        # CVD slope contribution (-15 to +15)
        if len(recent_1m) >= 6:
            cvd_first_half = sum(float(k[10]) - (float(k[7]) - float(k[10])) for k in recent_1m[:7])
            cvd_second_half = sum(float(k[10]) - (float(k[7]) - float(k[10])) for k in recent_1m[7:])
            if cvd_second_half > cvd_first_half:
                score += 10.0
            elif cvd_second_half < cvd_first_half:
                score -= 10.0

    # 2. Order Book Depth Imbalance
    if depth and "bids" in depth and "asks" in depth and depth["bids"] and depth["asks"]:
        try:
            best_bid = float(depth["bids"][0][0])
            best_ask = float(depth["asks"][0][0])
            mid = (best_bid + best_ask) / 2.0
            
            # Sum within 1.0%
            bids_1pct = sum(float(p) * float(q) for p, q in depth["bids"] if float(p) >= mid * 0.99)
            asks_1pct = sum(float(p) * float(q) for p, q in depth["asks"] if float(p) <= mid * 1.01)
            tot_depth = bids_1pct + asks_1pct
            if tot_depth > 0:
                imb = (bids_1pct - asks_1pct) / tot_depth  # -1.0 to +1.0
                score += imb * 25.0
        except Exception:
            pass

    # 3. Fast Momentum (1m & 5m EMA9 / EMA21 and RSI)
    if k1m and len(k1m) >= 30:
        closes_1m = [float(k[4]) for k in k1m]
        ema9 = ema_series(closes_1m, 9)[-1]
        ema21 = ema_series(closes_1m, 21)[-1]
        rsi = rsi_14(closes_1m)

        # EMA ribbon trend (-12.5 to +12.5)
        if ema9 > ema21:
            score += min(12.5, ((ema9 - ema21) / ema21) * 2000.0)
        else:
            score -= min(12.5, ((ema21 - ema9) / ema21) * 2000.0)

        # RSI momentum (-12.5 to +12.5)
        if rsi > 60:
            score += min(12.5, (rsi - 50.0) * 0.6)
        elif rsi < 40:
            score -= min(12.5, (50.0 - rsi) * 0.6)

    # 4. Perpetual Futures Flow
    if fut and isinstance(fut, dict):
        taker_ls = float(fut.get("taker_buy_sell_ratio") or 1.0)
        whale_ls = float(fut.get("top_whale_ls_ratio") or 1.0)
        if taker_ls > 1.05:
            score += min(7.5, (taker_ls - 1.0) * 20.0)
        elif taker_ls < 0.95:
            score -= min(7.5, (1.0 - taker_ls) * 20.0)
        
        if whale_ls > 1.2:
            score += 7.5
        elif whale_ls < 0.8:
            score -= 7.5

    return round(max(-100.0, min(100.0, score)), 1)


def simulate_intraday_horizons(k1m, current_price, composite_score, seed=42):
    """
    Runs 5,000-path Monte Carlo simulations across 10m, 30m, and 60m horizons.
    Calculates:
      - Prob(UP) and Prob(DOWN)
      - Expected Price and Expected Move %
      - 80% confidence interval
      - Detailed LONG outcome metrics (% chance, expected gain, stop risk, math edge)
      - Detailed SHORT outcome metrics (% chance, expected gain, stop risk, math edge)
      - Recommended Action & Win Advantage %
    """
    closes = [float(k[4]) for k in k1m]
    rets = []
    for i in range(1, len(closes)):
        if closes[i - 1] > 0:
            rets.append(math.log(closes[i] / closes[i - 1]))
    
    if len(rets) < 20:
        rets = [0.0002, -0.0002] * 20

    sigma_1m = statistics.pstdev(rets) or 0.0008
    hist_drift_1m = statistics.mean(rets)
    
    norm_score = max(-1.0, min(1.0, composite_score / 100.0))

    # Horizon configs: (name, minutes, alpha_drift_boost)
    # Higher alpha for 10m where microstructure momentum dominates; decaying by 60m
    horizons_cfg = [
        ("10m", 10, 0.45),
        ("30m", 30, 0.35),
        ("60m", 60, 0.25)
    ]

    rng = random.Random(seed)
    horizons_res = {}

    for name, minutes, alpha in horizons_cfg:
        # Microstructure-driven drift
        adj_drift = (0.25 * hist_drift_1m) + (alpha * norm_score * sigma_1m)
        
        finals = []
        up_count = 0
        down_count = 0
        
        # 5,000 simulated paths
        for _ in range(5000):
            log_r = 0.0
            for _m in range(minutes):
                r = rng.choice(rets) - hist_drift_1m + adj_drift
                log_r += r
            p_end = current_price * math.exp(log_r)
            finals.append(p_end)
            if p_end >= current_price:
                up_count += 1
            else:
                down_count += 1

        finals.sort()
        p_up = round((up_count / 5000.0) * 100.0, 1)
        p_down = round(100.0 - p_up, 1)
        
        exp_price = round(statistics.mean(finals), 4)
        exp_move_pct = round(((exp_price - current_price) / current_price) * 100.0, 2)
        
        conf_80_low = round(finals[int(5000 * 0.10)], 4)
        conf_80_high = round(finals[int(5000 * 0.90)], 4)

        # Volatility over horizon H: sigma_H = sigma_1m * sqrt(minutes)
        vol_h_pct = (sigma_1m * math.sqrt(minutes)) * 100.0
        
        # -------------------------------------------------------------
        # LONG Outcome Analysis
        # -------------------------------------------------------------
        # Expected gain if price moves upward: based on positive simulation drift
        long_win_pct = p_up
        long_exp_gain_pct = round(max(0.12, abs(exp_move_pct) if exp_move_pct > 0 else (vol_h_pct * 0.85)), 2)
        long_stop_risk_pct = round(max(0.15, vol_h_pct * 1.50), 2)
        long_target_price = round(current_price * (1.0 + (long_exp_gain_pct / 100.0)), 4)
        long_stop_price = round(current_price * (1.0 - (long_stop_risk_pct / 100.0)), 4)
        # Expected Value in R: E[R] = (Win% * (Gain/Risk)) - (Loss% * 1.0)
        long_rr = round(long_exp_gain_pct / max(0.01, long_stop_risk_pct), 2)
        long_edge_r = round(((long_win_pct / 100.0) * long_rr) - ((1.0 - (long_win_pct / 100.0)) * 1.0), 2)

        # -------------------------------------------------------------
        # SHORT Outcome Analysis
        # -------------------------------------------------------------
        short_win_pct = p_down
        short_exp_gain_pct = round(max(0.12, abs(exp_move_pct) if exp_move_pct < 0 else (vol_h_pct * 0.85)), 2)
        short_stop_risk_pct = round(max(0.15, vol_h_pct * 1.50), 2)
        short_target_price = round(current_price * (1.0 - (short_exp_gain_pct / 100.0)), 4)
        short_stop_price = round(current_price * (1.0 + (short_exp_gain_pct / 100.0)), 4)
        short_rr = round(short_exp_gain_pct / max(0.01, short_stop_risk_pct), 2)
        short_edge_r = round(((short_win_pct / 100.0) * short_rr) - ((1.0 - (short_win_pct / 100.0)) * 1.0), 2)

        # Determine which action is more profitable
        if p_up > p_down and long_edge_r >= short_edge_r:
            preferred_action = "LONG"
            advantage_pct = round(p_up - p_down, 1)
            action_win_prob = p_up
            action_expected_gain = long_exp_gain_pct
            action_edge = long_edge_r
        else:
            preferred_action = "SHORT"
            advantage_pct = round(p_down - p_up, 1)
            action_win_prob = p_down
            action_expected_gain = short_exp_gain_pct
            action_edge = short_edge_r

        horizons_res[name] = {
            "horizon": name,
            "minutes": minutes,
            "prob_up_pct": p_up,
            "prob_down_pct": p_down,
            "expected_price": exp_price,
            "expected_move_pct": exp_move_pct,
            "conf_80_low": conf_80_low,
            "conf_80_high": conf_80_high,
            "preferred_action": preferred_action,
            "advantage_pct": advantage_pct,
            "long_outcome": {
                "action": "LONG",
                "win_probability_pct": long_win_pct,
                "expected_gain_pct": long_exp_gain_pct,
                "stop_risk_pct": long_stop_risk_pct,
                "target_price": long_target_price,
                "stop_price": long_stop_price,
                "reward_risk_ratio": long_rr,
                "math_edge_R": long_edge_r,
                "profitable": long_edge_r > 0
            },
            "short_outcome": {
                "action": "SHORT",
                "win_probability_pct": short_win_pct,
                "expected_gain_pct": short_exp_gain_pct,
                "stop_risk_pct": short_stop_risk_pct,
                "target_price": short_target_price,
                "stop_price": short_stop_price,
                "reward_risk_ratio": short_rr,
                "math_edge_R": short_edge_r,
                "profitable": short_edge_r > 0
            }
        }

    return horizons_res


def estimate_coin_intraday(symbol):
    bsym = predict.to_binance(symbol)
    try:
        # 1. Fetch live spot ticker
        tk = predict.spot_get(f"/api/v3/ticker/24hr?symbol={bsym}")
        price = float(tk.get("lastPrice", 0.0))
        chg_24h = float(tk.get("priceChangePercent", 0.0))
        vol_usd_24h = float(tk.get("quoteVolume", 0.0))

        # 2. Fetch 1m and 5m klines
        k1m = predict.spot_get(f"/api/v3/klines?symbol={bsym}&interval=1m&limit=60")
        k5m = predict.spot_get(f"/api/v3/klines?symbol={bsym}&interval=5m&limit=60")

        # 3. Fetch L2 Depth
        depth = predict.spot_get(f"/api/v3/depth?symbol={bsym}&limit=100")

        # 4. Fetch Perpetual Futures ratios
        fut_data = {}
        try:
            r1 = predict.fut_get(f"/fapi/v1/takerlongshortRatio?symbol={bsym}&period=1h&limit=1")
            if r1 and isinstance(r1, list):
                fut_data["taker_buy_sell_ratio"] = float(r1[-1].get("buySellRatio", 1.0))
            r2 = predict.fut_get(f"/fapi/v1/topLongShortPositionRatio?symbol={bsym}&period=1h&limit=1")
            if r2 and isinstance(r2, list):
                fut_data["top_whale_ls_ratio"] = float(r2[-1].get("longShortRatio", 1.0))
        except Exception:
            pass

        # Compute Microstructure Composite Score
        comp_score = compute_intraday_microstructure_score(k1m, k5m, depth, fut_data)

        # Run 10m, 30m, 60m Monte Carlo
        horizons = simulate_intraday_horizons(k1m, price, comp_score)

        # Primary 30m decision as default baseline
        h30 = horizons["30m"]
        primary_action = h30["preferred_action"]
        primary_win_prob = h30["long_outcome"]["win_probability_pct"] if primary_action == "LONG" else h30["short_outcome"]["win_probability_pct"]
        primary_edge = h30["long_outcome"]["math_edge_R"] if primary_action == "LONG" else h30["short_outcome"]["math_edge_R"]

        return {
            "symbol": bsym,
            "price": price,
            "change_24h_pct": chg_24h,
            "volume_24h_usd": vol_usd_24h,
            "intraday_composite_score": comp_score,
            "primary_action_30m": primary_action,
            "primary_win_prob_30m": primary_win_prob,
            "primary_edge_30m": primary_edge,
            "horizons": horizons
        }
    except Exception as e:
        sys.stderr.write(f"Error estimating {bsym}: {e}\n")
        return None


def estimate_all_5_coins(symbols=None, write_path=None):
    if not symbols:
        symbols = TARGET_COINS
    
    results = {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(estimate_coin_intraday, s): s for s in symbols}
        for f in futures:
            res = f.result()
            if res:
                results[res["symbol"]] = res

    # Sort results strictly in TARGET_COINS order
    ordered = [results[s] for s in TARGET_COINS if s in results]

    if write_path:
        os.makedirs(os.path.dirname(os.path.abspath(write_path)), exist_ok=True)
        with open(write_path, "w", encoding="utf-8") as fh:
            json.dump(ordered, fh, indent=2)

    return ordered


def render_intraday_ascii_table(data_list):
    lines = []
    lines.append("=" * 96)
    lines.append("5-COIN INTRADAY DIRECTION & PROBABILITY ESTIMATOR (10m, 30m, 60m)")
    lines.append("=" * 96)

    for coin in data_list:
        sym = coin["symbol"]
        price = coin["price"]
        chg = coin["change_24h_pct"]
        score = coin["intraday_composite_score"]
        h = coin["horizons"]

        lines.append(f"COIN: {sym:<10} Spot: ${price:,.2f} ({chg:+.2f}% 24h) | Microstructure Score: {score:+.1f} / 100")
        lines.append(f"{'HORIZON':<10}{'CALL':<8}{'UP PROB':<10}{'DOWN PROB':<12}{'EXPECTED MOVE':<15}{'IF LONG (Win/Gain)':<22}{'IF SHORT (Win/Gain)':<22}")
        lines.append("-" * 96)

        for tf in ["10m", "30m", "60m"]:
            hz = h[tf]
            call_str = f"{hz['preferred_action']}"
            up_str = f"{hz['prob_up_pct']:.1f}%"
            down_str = f"{hz['prob_down_pct']:.1f}%"
            exp_str = f"{hz['expected_move_pct']:+.2f}%"
            
            l = hz["long_outcome"]
            s = hz["short_outcome"]
            long_info = f"{l['win_probability_pct']:.1f}% win (+{l['expected_gain_pct']:.2f}%)"
            short_info = f"{s['win_probability_pct']:.1f}% win (+{s['expected_gain_pct']:.2f}%)"
            
            lines.append(f"{tf:<10}{call_str:<8}{up_str:<10}{down_str:<12}{exp_str:<15}{long_info:<22}{short_info:<22}")

        lines.append("-" * 96)

    lines.append("=" * 96)
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="5-Coin Intraday Forecaster (10m, 30m, 60m)")
    parser.add_argument("symbols", nargs="*", default=TARGET_COINS, help="Coins to estimate (defaults to 5 coins)")
    parser.add_argument("--write", nargs="?", const="trader/intraday_5coins.json", help="Save estimation to JSON")
    args = parser.parse_args()

    results = estimate_all_5_coins(args.symbols, write_path=args.write)
    print(render_intraday_ascii_table(results))


if __name__ == "__main__":
    main()
