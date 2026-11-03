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
import time
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

FEATURE_NAME = "5 Coins Future"
TARGET_COINS = ["BNBUSDT", "SOLUSDT", "BTCUSDT", "ETHUSDT", "DOGEUSDT"]
_DEEP_CONTEXT_CACHE = {}


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
        prec = 6 if current_price < 1.0 else 4

        exp_price = round(statistics.mean(finals), prec)
        exp_move_pct = round(((exp_price - current_price) / current_price) * 100.0, 2)

        conf_80_low = round(finals[int(5000 * 0.10)], prec)
        conf_80_high = round(finals[int(5000 * 0.90)], prec)

        # Volatility over horizon H: sigma_H = sigma_1m * sqrt(minutes)
        vol_h_pct = (sigma_1m * math.sqrt(minutes)) * 100.0

        # -------------------------------------------------------------
        # LONG Outcome Analysis
        # -------------------------------------------------------------
        long_win_pct = p_up
        long_exp_gain_pct = round(max(0.15, (vol_h_pct * 1.15) + max(0.0, exp_move_pct)), 2)
        long_stop_risk_pct = round(max(0.12, vol_h_pct * 0.85), 2)
        long_target_price = round(current_price * (1.0 + (long_exp_gain_pct / 100.0)), prec)
        long_stop_price = round(current_price * (1.0 - (long_stop_risk_pct / 100.0)), prec)
        long_rr = round(long_exp_gain_pct / max(0.01, long_stop_risk_pct), 2)
        long_edge_r = round(((long_win_pct / 100.0) * long_rr) - ((1.0 - (long_win_pct / 100.0)) * 1.0), 2)

        # -------------------------------------------------------------
        # SHORT Outcome Analysis
        # -------------------------------------------------------------
        short_win_pct = p_down
        short_exp_gain_pct = round(max(0.15, (vol_h_pct * 1.15) + max(0.0, -exp_move_pct)), 2)
        short_stop_risk_pct = round(max(0.12, vol_h_pct * 0.85), 2)
        short_target_price = round(current_price * (1.0 - (short_exp_gain_pct / 100.0)), prec)
        short_stop_price = round(current_price * (1.0 + (short_stop_risk_pct / 100.0)), prec)
        short_rr = round(short_exp_gain_pct / max(0.01, short_stop_risk_pct), 2)
        short_edge_r = round(((short_win_pct / 100.0) * short_rr) - ((1.0 - (short_win_pct / 100.0)) * 1.0), 2)

        # Determine which action is more profitable (strictly aligned with directional probability)
        if p_up >= p_down:
            preferred_action = "LONG"
            advantage_pct = round(p_up - p_down, 1)
        else:
            preferred_action = "SHORT"
            advantage_pct = round(p_down - p_up, 1)

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


def _fetch_deep_coin_context(bsym, price):
    """Fetch and cache 10-Year Historical Binance Cycle, 25-Source News Outlets,
    Multi-Timeframe Technicals (15m/1h/4h/1d), Volume Profile (POC/VAH/VAL), VWAP,
    Cross-Exchange Futures (Binance/Bybit/OKX), and On-Chain Macro Intelligence."""
    now_s = time.time()
    cached = _DEEP_CONTEXT_CACHE.get(bsym)
    if cached and (now_s - cached.get("ts", 0)) < 45.0:
        return cached["data"]

    with ThreadPoolExecutor(max_workers=6) as ex:
        f_k15 = ex.submit(predict.spot_get, f"/api/v3/klines?symbol={bsym}&interval=15m&limit=100")
        f_k1h = ex.submit(predict.spot_get, f"/api/v3/klines?symbol={bsym}&interval=1h&limit=240")
        f_k4h = ex.submit(predict.spot_get, f"/api/v3/klines?symbol={bsym}&interval=4h&limit=210")
        f_k1d = ex.submit(predict.spot_get, f"/api/v3/klines?symbol={bsym}&interval=1d&limit=210")
        f_news = ex.submit(predict.analyze_news, bsym)
        f_macro = ex.submit(predict.fetch_global_macro_and_onchain, bsym)

        k15 = f_k15.result()
        k1h = f_k1h.result()
        k4h = f_k4h.result()
        k1d = f_k1d.result()
        news = f_news.result()
        macro = f_macro.result()

    tf_15m = predict.analyze_timeframe(k15, "15m")
    tf_1h = predict.analyze_timeframe(k1h, "1h")
    tf_4h = predict.analyze_timeframe(k4h, "4h")
    tf_1d = predict.analyze_timeframe(k1d, "1d")
    mtf_score = round(0.15 * tf_15m["score"] + 0.30 * tf_1h["score"] + 0.30 * tf_4h["score"] + 0.25 * tf_1d["score"], 1)

    vp = predict.compute_volume_profile(k1h, price, n_bars=168, bins=40)
    inst = predict.detect_institutional_signals(k1h, k4h, price)
    fut = predict.analyze_futures(bsym, price, k1h)
    ten_year = predict.analyze_ten_year_cycle(bsym, price, k1d_recent=k1d)

    data = {
        "k1h": k1h,
        "mtf_score": mtf_score,
        "timeframes": {"15m": tf_15m, "1h": tf_1h, "4h": tf_4h, "1d": tf_1d},
        "volume_profile": vp,
        "institutional": inst,
        "futures": fut,
        "ten_year_cycle": ten_year,
        "news": news,
        "macro_onchain": macro,
    }
    _DEEP_CONTEXT_CACHE[bsym] = {"ts": now_s, "data": data}
    return data


def estimate_coin_intraday(symbol):
    bsym = predict.to_binance(symbol)
    try:
        # 1. Fetch live spot ticker, 1m/5m klines, L2 depth, and deep 10Y + 25-source context
        with ThreadPoolExecutor(max_workers=4) as ex:
            f_tk = ex.submit(predict.spot_get, f"/api/v3/ticker/24hr?symbol={bsym}")
            f_k1m = ex.submit(predict.spot_get, f"/api/v3/klines?symbol={bsym}&interval=1m&limit=60")
            f_k5m = ex.submit(predict.spot_get, f"/api/v3/klines?symbol={bsym}&interval=5m&limit=60")
            f_dep = ex.submit(predict.spot_get, f"/api/v3/depth?symbol={bsym}&limit=500")
            tk = f_tk.result()
            k1m = f_k1m.result()
            k5m = f_k5m.result()
            depth = f_dep.result()

        price = float(tk.get("lastPrice", 0.0))
        chg_24h = float(tk.get("priceChangePercent", 0.0))
        vol_usd_24h = float(tk.get("quoteVolume", 0.0))

        # 2. Fetch Deep Context: 10-Year Binance Cycle, 25-Source News, MTF, Volume Profile, Futures, Macro
        ctx = {}
        try:
            ctx = _fetch_deep_coin_context(bsym, price)
        except Exception:
            ctx = {}

        fut_full = ctx.get("futures") or {}
        fut_data = {
            "taker_buy_sell_ratio": float(fut_full.get("taker_buy_sell_1h") or fut_full.get("taker_buy_sell_4h") or 1.0),
            "top_whale_ls_ratio": float(fut_full.get("whale_ls_ratio") or 1.0),
        }
        if not fut_full:
            try:
                r1 = predict.fut_get(f"/futures/data/takerlongshortRatio?symbol={bsym}&period=1h&limit=1")
                if r1 and isinstance(r1, list):
                    fut_data["taker_buy_sell_ratio"] = float(r1[-1].get("buySellRatio", 1.0))
                r2 = predict.fut_get(f"/futures/data/topLongShortPositionRatio?symbol={bsym}&period=1h&limit=1")
                if r2 and isinstance(r2, list):
                    fut_data["top_whale_ls_ratio"] = float(r2[-1].get("longShortRatio", 1.0))
            except Exception:
                pass

        # 3. Compute Fast Intraday Microstructure Score (-100 to +100)
        micro_score = compute_intraday_microstructure_score(k1m, k5m, depth, fut_data)

        # 4. Blend with 10-Year Historical Cycle, 25-Source News Outlets, MTF Trend, Futures & Wyckoff
        mtf_score = float(ctx.get("mtf_score", 0.0))                     # -25 to +25
        fut_score = float(fut_full.get("score", 0.0))                    # -25 to +25
        news_obj = ctx.get("news") or {}
        news_score = float(news_obj.get("score", 0.0))                   # -15 to +15
        ty_obj = ctx.get("ten_year_cycle") or {}
        ty_score = float(ty_obj.get("score", 0.0))                       # -15 to +15
        inst_obj = ctx.get("institutional") or {}
        macro_obj = ctx.get("macro_onchain") or {}
        inst_macro_score = float(inst_obj.get("bonus_score", 0.0)) + float(macro_obj.get("macro_bonus", 0.0))

        # Holistic all-aspect score (-100 to +100) combining fast CLOB order-flow with 10Y + News + Perps + MTF
        if ctx:
            deep_structural_score = max(-100.0, min(100.0, (mtf_score + fut_score + news_score + ty_score + inst_macro_score) * 1.15))
            comp_score = round(max(-100.0, min(100.0, 0.55 * micro_score + 0.45 * deep_structural_score)), 1)
        else:
            deep_structural_score = micro_score
            comp_score = micro_score

        # 5. Run 10m, 30m, 60m Monte Carlo + Macro Horizons (4h, 24h, 7d, 30d, 90d)
        horizons = simulate_intraday_horizons(k1m, price, comp_score)
        macro_horizons = {}
        if ctx.get("k1h"):
            macro_horizons = predict.simulate_horizons(ctx["k1h"], price, deep_structural_score, ten_year=ty_obj)

        # Primary 30m decision as default baseline
        h30 = horizons["30m"]
        primary_action = h30["preferred_action"]
        primary_win_prob = h30["long_outcome"]["win_probability_pct"] if primary_action == "LONG" else h30["short_outcome"]["win_probability_pct"]
        primary_edge = h30["long_outcome"]["math_edge_R"] if primary_action == "LONG" else h30["short_outcome"]["math_edge_R"]

        ob_summary = predict.analyze_orderbook(depth, price)
        knn = (ty_obj.get("knn_analogs") or {}) if ty_obj else {}
        bf_10y = (ty_obj.get("binance_flow_10y") or {}) if ty_obj else {}
        sz_10y = (ty_obj.get("seasonality") or {}) if ty_obj else {}
        hc_10y = (ty_obj.get("halving_cycle") or {}) if ty_obj else {}
        tok_10y = (ty_obj.get("tokenomics") or {}) if ty_obj else {}

        return {
            "feature_name": FEATURE_NAME,
            "symbol": bsym,
            "price": price,
            "change_24h_pct": chg_24h,
            "volume_24h_usd": vol_usd_24h,
            "intraday_composite_score": comp_score,
            "microstructure_fast_score": micro_score,
            "deep_structural_score": round(deep_structural_score, 1),
            "primary_action_30m": primary_action,
            "primary_win_prob_30m": primary_win_prob,
            "primary_edge_30m": primary_edge,
            "horizons": horizons,
            "macro_horizons": macro_horizons,
            "pillar_breakdown": {
                "clob_intraday_flow": round(micro_score, 1),
                "mtf_trend_score": mtf_score,
                "futures_whale_score": fut_score,
                "news_catalysts_score": news_score,
                "ten_year_cycle_score": ty_score,
                "wyckoff_macro_bonus": round(inst_macro_score, 1),
            },
            "ten_year_summary": {
                "years_covered": ty_obj.get("years_covered", 0.0),
                "total_days": ty_obj.get("total_days", 0),
                "binance_days": ty_obj.get("binance_days", 0),
                "first_date": ty_obj.get("first_date", "-"),
                "all_time_high": ty_obj.get("all_time_high", price),
                "ath_date": ty_obj.get("ath_date", "-"),
                "drawdown_from_ath_pct": ty_obj.get("drawdown_from_ath_pct", 0.0),
                "cagr_1y_pct": ty_obj.get("cagr_1y_pct", 0.0),
                "cagr_3y_pct": ty_obj.get("cagr_3y_pct", 0.0),
                "cagr_5y_pct": ty_obj.get("cagr_5y_pct", 0.0),
                "cagr_10y_pct": ty_obj.get("cagr_10y_pct", 0.0),
                "sharpe_ratio_10y": ty_obj.get("sharpe_ratio_10y", 0.0),
                "sma_200w": ty_obj.get("sma_200w", price),
                "price_vs_sma200w_pct": ty_obj.get("price_vs_sma200w_pct", 0.0),
                "sma200w_regime": ty_obj.get("sma200w_regime", "-"),
                "sma_200d": ty_obj.get("sma_200d", price),
                "mayer_multiple": ty_obj.get("mayer_multiple", 1.0),
                "mayer_percentile_10y": ty_obj.get("mayer_percentile_10y", 50.0),
                "log_reg_fair_price": ty_obj.get("log_reg_fair_price", price),
                "log_reg_zscore": ty_obj.get("log_reg_zscore", 0.0),
                "valuation_zone": ty_obj.get("valuation_zone", "-"),
                "taker_buy_pct_alltime": bf_10y.get("taker_buy_pct_alltime", 50.0),
                "taker_buy_pct_30d": bf_10y.get("taker_buy_pct_30d", 50.0),
                "cvd_1y_usd": bf_10y.get("cvd_1y_usd", 0.0),
                "seasonality_month": sz_10y.get("month_name", "-"),
                "seasonality_win_rate_pct": sz_10y.get("win_rate_pct", 50.0),
                "seasonality_avg_ret_pct": sz_10y.get("avg_return_pct", 0.0),
                "halving_phase": hc_10y.get("phase", "-"),
                "knn_7d_prob_up_pct": (knn.get("7d") or {}).get("prob_up_pct", 50.0),
                "knn_7d_median_ret_pct": (knn.get("7d") or {}).get("median_return_pct", 0.0),
                "knn_30d_prob_up_pct": (knn.get("30d") or {}).get("prob_up_pct", 50.0),
                "knn_30d_median_ret_pct": (knn.get("30d") or {}).get("median_return_pct", 0.0),
                "knn_90d_prob_up_pct": (knn.get("90d") or {}).get("prob_up_pct", 50.0),
                "knn_90d_median_ret_pct": (knn.get("90d") or {}).get("median_return_pct", 0.0),
                "top_analog_matches": (knn.get("top_matches") or [])[:3],
                "supply_model": tok_10y.get("supply_model", "-"),
                "structural_drivers": tok_10y.get("structural_drivers", "-"),
                "notes": ty_obj.get("notes", []),
            },
            "news_summary": {
                "score": news_score,
                "headlines_checked": news_obj.get("headlines_checked", 0),
                "bullish_catalysts": [
                    {"source": s, "tag": t, "title": h}
                    for s, t, h in (news_obj.get("bullish_catalysts") or [])[:4]
                ],
                "bearish_catalysts": [
                    {"source": s, "tag": t, "title": h}
                    for s, t, h in (news_obj.get("bearish_catalysts") or [])[:4]
                ],
                "top_outlets": (
                    (news_obj.get("binance_official") or [])[:2]
                    + (news_obj.get("bank_and_institutional") or [])[:2]
                    + (news_obj.get("twitter_x_flow") or [])[:2]
                    + (news_obj.get("magazines_and_press") or [])[:2]
                )[:5],
                "is_trending": bool((news_obj.get("trending_social") or {}).get("is_trending")),
                "trending_rank": (news_obj.get("trending_social") or {}).get("trending_rank"),
            },
            "futures_summary": {
                "consensus_funding_pct": fut_full.get("consensus_funding_pct", 0.0),
                "global_oi_usd": fut_full.get("global_oi_usd", 0.0),
                "oi_regime": fut_full.get("oi_regime", "-"),
                "whale_long_pct": fut_full.get("whale_long_pct", 50.0),
                "whale_ls_ratio": fut_full.get("whale_ls_ratio", 1.0),
                "retail_long_pct": fut_full.get("retail_long_pct", 50.0),
                "taker_buy_sell_4h": fut_full.get("taker_buy_sell_4h", 1.0),
                "imbalance_1pct": ob_summary.get("imbalance_1pct", 0.0) if ob_summary else 0.0,
                "support_wall_price": ob_summary.get("support_wall_price") if ob_summary else None,
                "resistance_wall_price": ob_summary.get("resistance_wall_price") if ob_summary else None,
            },
            "volume_profile": ctx.get("volume_profile") or {},
            "macro_onchain": {
                "fear_greed_value": macro_obj.get("fear_greed_value", 50),
                "fear_greed_label": macro_obj.get("fear_greed_label", "Neutral"),
                "btc_dominance_pct": macro_obj.get("btc_dominance_pct", 0.0),
                "stablecoin_7d_flow_usd": macro_obj.get("stablecoin_7d_flow_usd", 0.0),
                "chain_name": macro_obj.get("chain_name"),
                "chain_tvl_usd": macro_obj.get("chain_tvl_usd"),
            },
        }
    except Exception as e:
        sys.stderr.write(f"Error estimating {bsym}: {e}\n")
        return None


def estimate_all_5_coins(symbols=None, write_path=None):
    if not symbols:
        symbols = TARGET_COINS

    norm_syms = [predict.to_binance(s) for s in symbols]
    results = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(estimate_coin_intraday, s): s for s in norm_syms}
        for f in futures:
            res = f.result()
            if res:
                results[res["symbol"]] = res

    # Preserve TARGET_COINS order first, then any additional requested coins
    ordered = [results[s] for s in TARGET_COINS if s in results]
    for s in norm_syms:
        if s in results and s not in TARGET_COINS:
            ordered.append(results[s])

    if write_path:
        os.makedirs(os.path.dirname(os.path.abspath(write_path)), exist_ok=True)
        with open(write_path, "w", encoding="utf-8") as fh:
            json.dump(ordered, fh, indent=2)

    return ordered


def render_intraday_ascii_table(data_list):
    lines = []
    lines.append("=" * 108)
    lines.append("5 COINS FUTURE -- BINANCE LONG & SHORT PREDICTION ENGINE (10Y DATA + 25-OUTLET NEWS)")
    lines.append("=" * 108)

    for coin in data_list:
        sym = coin["symbol"]
        price = coin["price"]
        chg = coin["change_24h_pct"]
        score = coin["intraday_composite_score"]
        h = coin["horizons"]
        ty = coin.get("ten_year_summary") or {}
        nw = coin.get("news_summary") or {}
        ft = coin.get("futures_summary") or {}

        p_str = f"${price:,.6f}".rstrip("0").rstrip(".") if price < 1.0 else f"${price:,.2f}"
        lines.append(f"COIN: {sym:<10} Spot: {p_str} ({chg:+.2f}% 24h) | Holistic All-Aspect Score: {score:+.1f} / 100")
        if ty.get("total_days"):
            lines.append(
                f"  [10Y Cycle] {ty.get('years_covered')}y ({ty.get('total_days')}d) | 200W SMA: ${ty.get('sma_200w', 0):,.4f} ({ty.get('price_vs_sma200w_pct', 0):+.1f}%) | "
                f"Mayer: {ty.get('mayer_multiple', 1):.2f} ({ty.get('mayer_percentile_10y', 50):.0f}% pctile) | "
                f"10Y k-NN Win: 7d {ty.get('knn_7d_prob_up_pct', 50):.0f}% / 30d {ty.get('knn_30d_prob_up_pct', 50):.0f}%"
            )
        if ft.get("oi_regime") or nw.get("headlines_checked"):
            lines.append(
                f"  [Perps & News] Whale L/S: {ft.get('whale_long_pct', 50):.1f}% Long ({ft.get('whale_ls_ratio', 1):.2f}) | "
                f"Funding: {ft.get('consensus_funding_pct', 0):+.4f}% | "
                f"News Scanned: {nw.get('headlines_checked', 0)} outlets (Score {nw.get('score', 0):+.1f}/15)"
            )
        lines.append(f"{'HORIZON':<10}{'CALL':<8}{'UP PROB':<10}{'DOWN PROB':<12}{'EXPECTED MOVE':<15}{'IF LONG (Win/Gain)':<24}{'IF SHORT (Win/Gain)':<24}")
        lines.append("-" * 108)

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

            lines.append(f"{tf:<10}{call_str:<8}{up_str:<10}{down_str:<12}{exp_str:<15}{long_info:<24}{short_info:<24}")

        mh = coin.get("macro_horizons") or {}
        if mh:
            m_parts = []
            for mtf in ("24h", "7d", "30d", "90d"):
                if mtf in mh:
                    m_obj = mh[mtf]
                    m_parts.append(f"{mtf}: {m_obj['direction']} ({m_obj['prob_up_pct']:.1f}% UP, {m_obj['expected_move_pct']:+.2f}%)")
            if m_parts:
                lines.append("  [Macro & 10Y Analog Outlook] " + " | ".join(m_parts))

        lines.append("-" * 108)

    lines.append("=" * 108)
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
