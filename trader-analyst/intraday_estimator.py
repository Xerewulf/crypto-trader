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

try:
    import price_forecaster
except Exception:  # pragma: no cover
    price_forecaster = None

FEATURE_NAME = "6 Coins Future"
TARGET_COINS = ["BNBUSDT", "SOLUSDT", "BTCUSDT", "ETHUSDT", "DOGEUSDT", "XRPUSDT"]
_DEEP_CONTEXT_CACHE = {}
_PREV_ORDERBOOK_STATE = {}
_BTC_IMPULSE_CACHE = {"ts": 0.0, "k1m": [], "impulse_score": 0.0, "ret_5m_pct": 0.0, "taker_buy_5m_pct": 50.0}


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



def compute_microprice_and_ofi(depth, symbol=None):
    """Step 3: Compute Stoikov Volume-Weighted Micro-Price, Bid-Ask Spread (bps),
    and Cont-Kukanov-Stoikov Dynamic Order-Flow Imbalance (Delta-OFI)."""
    if not depth or "bids" not in depth or "asks" not in depth or not depth["bids"] or not depth["asks"]:
        return {
            "available": False,
            "mid_price": 0.0,
            "microprice": 0.0,
            "microprice_premium_bps": 0.0,
            "microprice_tilt": 0.0,
            "spread_bps": 2.0,
            "imbalance_1pct_ratio": 0.0,
            "delta_ofi_norm": 0.0,
        }
    try:
        best_bid = float(depth["bids"][0][0])
        best_bid_q = float(depth["bids"][0][1])
        best_ask = float(depth["asks"][0][0])
        best_ask_q = float(depth["asks"][0][1])
        mid = (best_bid + best_ask) / 2.0
        if mid <= 0:
            raise ValueError("Invalid mid price")

        # Top-3 level weighted queues for robust Stoikov Micro-Price
        bid_w_q = sum(float(q) * (0.6 ** idx) for idx, (_, q) in enumerate(depth["bids"][:3]))
        ask_w_q = sum(float(q) * (0.6 ** idx) for idx, (_, q) in enumerate(depth["asks"][:3]))
        tot_top_q = bid_w_q + ask_w_q
        if tot_top_q > 0:
            microprice = (bid_w_q * best_ask + ask_w_q * best_bid) / tot_top_q
            microprice_tilt = (bid_w_q - ask_w_q) / tot_top_q  # -1.0 to +1.0
        else:
            microprice = mid
            microprice_tilt = 0.0

        microprice_premium_bps = ((microprice - mid) / mid) * 10000.0
        spread_bps = max(0.1, ((best_ask - best_bid) / mid) * 10000.0)

        bids_1pct = sum(float(p) * float(q) for p, q in depth["bids"] if float(p) >= mid * 0.99)
        asks_1pct = sum(float(p) * float(q) for p, q in depth["asks"] if float(p) <= mid * 1.01)
        tot_depth = bids_1pct + asks_1pct
        imb_ratio = ((bids_1pct - asks_1pct) / tot_depth) if tot_depth > 0 else 0.0

        # Cont-Kukanov-Stoikov Delta-OFI across consecutive snapshots
        bid_top5_usd = sum(float(p) * float(q) for p, q in depth["bids"][:5])
        ask_top5_usd = sum(float(p) * float(q) for p, q in depth["asks"][:5])
        delta_ofi_norm = 0.0
        if symbol:
            prev = _PREV_ORDERBOOK_STATE.get(symbol)
            if prev:
                if best_bid > prev["best_bid"]:
                    e_b = bid_top5_usd
                elif best_bid == prev["best_bid"]:
                    e_b = bid_top5_usd - prev["bid_top5_usd"]
                else:
                    e_b = -prev["bid_top5_usd"]

                if best_ask < prev["best_ask"]:
                    e_a = ask_top5_usd
                elif best_ask == prev["best_ask"]:
                    e_a = ask_top5_usd - prev["ask_top5_usd"]
                else:
                    e_a = -prev["ask_top5_usd"]

                raw_ofi = e_b - e_a
                denom = max(1.0, bid_top5_usd + ask_top5_usd, prev["bid_top5_usd"] + prev["ask_top5_usd"])
                delta_ofi_norm = max(-1.0, min(1.0, raw_ofi / denom))

            _PREV_ORDERBOOK_STATE[symbol] = {
                "best_bid": best_bid,
                "best_ask": best_ask,
                "bid_top5_usd": bid_top5_usd,
                "ask_top5_usd": ask_top5_usd,
            }

        return {
            "available": True,
            "mid_price": mid,
            "microprice": round(microprice, 6 if mid < 1.0 else 4),
            "microprice_premium_bps": round(microprice_premium_bps, 3),
            "microprice_tilt": round(microprice_tilt, 4),
            "spread_bps": round(spread_bps, 2),
            "imbalance_1pct_ratio": round(imb_ratio, 4),
            "delta_ofi_norm": round(delta_ofi_norm, 4),
        }
    except Exception:
        return {
            "available": False,
            "mid_price": 0.0,
            "microprice": 0.0,
            "microprice_premium_bps": 0.0,
            "microprice_tilt": 0.0,
            "spread_bps": 2.0,
            "imbalance_1pct_ratio": 0.0,
            "delta_ofi_norm": 0.0,
        }



def compute_btc_lead_lag_spillover(bsym, k1m_alt, btc_k1m=None):
    """Step 4: Compute BTC 1m/5m Order-Flow Impulse and propagate into altcoins
    weighted by rolling 60m empirical Beta and 1-minute Granger lead-lag correlation."""
    global _BTC_IMPULSE_CACHE
    now_s = time.time()
    if bsym == "BTCUSDT" and k1m_alt and len(k1m_alt) >= 15:
        btc_k1m = k1m_alt
    elif btc_k1m is None:
        if (now_s - _BTC_IMPULSE_CACHE.get("ts", 0.0)) < 5.0 and _BTC_IMPULSE_CACHE.get("k1m"):
            btc_k1m = _BTC_IMPULSE_CACHE["k1m"]
        else:
            try:
                btc_k1m = predict.spot_get("/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=60", timeout=2.5)
            except Exception:
                btc_k1m = _BTC_IMPULSE_CACHE.get("k1m") or []

    if not btc_k1m or len(btc_k1m) < 15:
        return {"btc_impulse_score": 0.0, "beta_to_btc": 1.0, "lag1_corr": 0.0, "spillover_score": 0.0}

    try:
        btc_closes = [float(k[4]) for k in btc_k1m]
        btc_rets = [math.log(btc_closes[i] / btc_closes[i - 1]) for i in range(1, len(btc_closes)) if btc_closes[i - 1] > 0]
        rec_5m = btc_k1m[-5:]
        tot_v5 = sum(float(k[7]) for k in rec_5m)
        buy_v5 = sum(float(k[10]) for k in rec_5m)
        tb_5m_pct = (buy_v5 / tot_v5 * 100.0) if tot_v5 > 0 else 50.0
        ret_5m_pct = ((btc_closes[-1] - btc_closes[-6]) / btc_closes[-6] * 100.0) if len(btc_closes) >= 6 and btc_closes[-6] > 0 else 0.0

        # BTC 5m impulse score (-100 to +100)
        impulse = (tb_5m_pct - 50.0) * 1.6 + (ret_5m_pct * 45.0)
        impulse = max(-100.0, min(100.0, impulse))
        _BTC_IMPULSE_CACHE = {
            "ts": now_s,
            "k1m": btc_k1m,
            "impulse_score": round(impulse, 2),
            "ret_5m_pct": round(ret_5m_pct, 3),
            "taker_buy_5m_pct": round(tb_5m_pct, 1),
        }

        if bsym == "BTCUSDT" or not k1m_alt or len(k1m_alt) < 15:
            return {"btc_impulse_score": round(impulse, 2), "beta_to_btc": 1.0, "lag1_corr": 0.0, "spillover_score": 0.0}

        alt_closes = [float(k[4]) for k in k1m_alt]
        alt_rets = [math.log(alt_closes[i] / alt_closes[i - 1]) for i in range(1, len(alt_closes)) if alt_closes[i - 1] > 0]
        n = min(len(btc_rets), len(alt_rets))
        if n < 12:
            return {"btc_impulse_score": round(impulse, 2), "beta_to_btc": 1.1, "lag1_corr": 0.25, "spillover_score": round(impulse * 0.08, 2)}

        b_slice = btc_rets[-n:]
        a_slice = alt_rets[-n:]
        b_mean = statistics.mean(b_slice)
        a_mean = statistics.mean(a_slice)
        b_var = sum((x - b_mean) ** 2 for x in b_slice) / n
        cov_0 = sum((b_slice[i] - b_mean) * (a_slice[i] - a_mean) for i in range(n)) / n
        beta = max(0.3, min(2.5, cov_0 / b_var)) if b_var > 1e-12 else 1.1

        # 1-minute Granger lead-lag correlation: BTC[t-1] -> Alt[t]
        b_lead = b_slice[:-1]
        a_lag = a_slice[1:]
        std_b = statistics.pstdev(b_lead) or 1e-8
        std_a = statistics.pstdev(a_lag) or 1e-8
        cov_lag1 = sum((b_lead[i] - b_mean) * (a_lag[i] - a_mean) for i in range(len(b_lead))) / max(1, len(b_lead))
        lag1_corr = max(-1.0, min(1.0, cov_lag1 / (std_b * std_a)))

        # Spillover boost when BTC has a strong impulse that the altcoin has not yet fully priced in
        alt_ret_5m = ((alt_closes[-1] - alt_closes[-6]) / alt_closes[-6] * 100.0) if len(alt_closes) >= 6 and alt_closes[-6] > 0 else 0.0
        expected_alt_5m = ret_5m_pct * beta
        catchup_gap = expected_alt_5m - alt_ret_5m
        # Only boost if altcoin is lagging behind BTC's impulse direction
        aligned_gap = catchup_gap if (impulse * catchup_gap > 0) else 0.0
        lead_weight = max(0.35, 0.6 + 0.4 * max(0.0, lag1_corr))
        spillover = max(-12.0, min(12.0, (impulse * 0.10 * beta * lead_weight) + (aligned_gap * 4.0)))

        return {
            "btc_impulse_score": round(impulse, 2),
            "beta_to_btc": round(beta, 2),
            "lag1_corr": round(lag1_corr, 2),
            "spillover_score": round(spillover, 2),
        }
    except Exception:
        return {"btc_impulse_score": 0.0, "beta_to_btc": 1.0, "lag1_corr": 0.0, "spillover_score": 0.0}


def detect_microstructure_trend_veto(k1m, k5m=None):
    """Detects active intraday markdown cascades or markup breakouts.
    Returns: 'BEARISH_BREAKDOWN', 'BULLISH_BREAKOUT', or None."""
    if not k1m or len(k1m) < 25:
        return None
    try:
        closes_1m = [float(k[4]) for k in k1m]
        ema9_1m = ema_series(closes_1m, 9)[-1]
        ema21_1m = ema_series(closes_1m, 21)[-1]
        c1m = closes_1m[-1]

        recent_1m = k1m[-15:]
        tot_v = sum(float(k[7]) for k in recent_1m)
        buy_v = sum(float(k[10]) for k in recent_1m)
        tb_ratio = (buy_v / tot_v) if tot_v > 0 else 0.5

        is_5m_bear = False
        is_5m_bull = False
        if k5m and len(k5m) >= 12:
            closes_5m = [float(k[4]) for k in k5m]
            ema21_5m = ema_series(closes_5m, 21)[-1]
            c5m = closes_5m[-1]
            if c5m < ema21_5m:
                is_5m_bear = True
            elif c5m > ema21_5m:
                is_5m_bull = True
        else:
            ema50_1m = ema_series(closes_1m, min(len(closes_1m), 50))[-1]
            if c1m < ema50_1m:
                is_5m_bear = True
            elif c1m > ema50_1m:
                is_5m_bull = True

        if c1m < ema9_1m < ema21_1m and is_5m_bear and tb_ratio < 0.47:
            return "BEARISH_BREAKDOWN"
        if c1m > ema9_1m > ema21_1m and is_5m_bull and tb_ratio > 0.53:
            return "BULLISH_BREAKOUT"
        return None
    except Exception:
        return None


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

    # 2. Order Book Depth Imbalance + Step 3: Stoikov Micro-Price & Dynamic Delta-OFI
    ob_metrics = compute_microprice_and_ofi(depth, symbol=fut.get("symbol") if isinstance(fut, dict) else None)
    if ob_metrics["available"]:
        # Static +-1% depth imbalance (-16 to +16)
        score += ob_metrics["imbalance_1pct_ratio"] * 16.0
        # Stoikov Micro-Price top-of-book pressure (-5 to +5)
        score += max(-5.0, min(5.0, ob_metrics["microprice_tilt"] * 5.0))
        # Cont-Kukanov-Stoikov Delta-OFI queue velocity (-4 to +4)
        score += max(-4.0, min(4.0, ob_metrics["delta_ofi_norm"] * 4.0))

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

    # 5. Microstructure Trend Veto penalty / boost
    veto = detect_microstructure_trend_veto(k1m, k5m)
    if veto == "BEARISH_BREAKDOWN":
        score -= 25.0
    elif veto == "BULLISH_BREAKOUT":
        score += 25.0

    return round(max(-100.0, min(100.0, score)), 1)



def calibrate_intraday_model(k1m):
    """Step 6: Walk-Forward Out-of-Sample Probability Calibration & Brier Score Tracking.
    Evaluates rolling historical 10-minute windows inside k1m to measure empirical hit rate,
    Brier score, and compute self-correcting Platt conviction shrink and probability bounds."""
    if not k1m or len(k1m) < 30:
        return {
            "samples": 0,
            "empirical_hit_rate_pct": 50.0,
            "brier_score": 0.25,
            "cal_alpha_mult": 1.0,
            "conviction_shrink": 0.85,
            "max_prob_deviation": 20.0,
            "reliability_tier": "BASELINE",
        }
    try:
        closes = [float(k[4]) for k in k1m]
        ema9_all = ema_series(closes, 9)
        ema21_all = ema_series(closes, 21)
        horizon = 10
        hits = 0
        total = 0
        brier_sum = 0.0

        for idx in range(15, len(k1m) - horizon):
            window = k1m[idx - 10 : idx + 1]
            tot_v = sum(float(x[7]) for x in window)
            buy_v = sum(float(x[10]) for x in window)
            tb_ratio = (buy_v / tot_v) if tot_v > 0 else 0.5
            trend_sign = 1.0 if ema9_all[idx] >= ema21_all[idx] else -1.0
            sig_score = (tb_ratio - 0.5) * 120.0 + (trend_sign * 12.0)
            p_pred = max(0.15, min(0.85, 0.5 + (sig_score / 200.0)))
            actual_up = 1.0 if closes[idx + horizon] >= closes[idx] else 0.0
            pred_up = 1.0 if p_pred >= 0.5 else 0.0
            if pred_up == actual_up:
                hits += 1
            brier_sum += (p_pred - actual_up) ** 2
            total += 1

        if total == 0:
            return {
                "samples": 0,
                "empirical_hit_rate_pct": 50.0,
                "brier_score": 0.25,
                "cal_alpha_mult": 1.0,
                "conviction_shrink": 0.85,
                "max_prob_deviation": 20.0,
                "reliability_tier": "BASELINE",
            }

        hit_rate = (hits / float(total)) * 100.0
        brier = brier_sum / float(total)

        # Mathematical Platt calibration tiers:
        if hit_rate >= 58.0:
            tier = "HIGH_RELIABILITY"
            cal_mult = min(1.25, 1.0 + ((hit_rate - 50.0) / 100.0) * 0.8)
            conviction_shrink = 1.0
            max_prob_dev = 32.0
        elif hit_rate >= 50.0:
            tier = "CALIBRATED"
            cal_mult = 1.0
            conviction_shrink = 0.85
            max_prob_dev = 20.0
        elif hit_rate >= 44.0:
            tier = "CHOPPY_DEGRADED"
            cal_mult = max(0.15, (hit_rate - 42.0) / 12.0)
            conviction_shrink = 0.35
            max_prob_dev = 8.0
        else:
            # Hit rate < 44%: inverted or failing regime.
            # Shut down false drift and clamp probability strictly near neutral.
            tier = "INVERTED_REGIME"
            cal_mult = 0.0
            conviction_shrink = 0.15
            max_prob_dev = 4.0

        return {
            "samples": total,
            "empirical_hit_rate_pct": round(hit_rate, 1),
            "brier_score": round(brier, 4),
            "cal_alpha_mult": round(cal_mult, 3),
            "conviction_shrink": round(conviction_shrink, 3),
            "max_prob_deviation": round(max_prob_dev, 1),
            "reliability_tier": tier,
        }
    except Exception:
        return {
            "samples": 0,
            "empirical_hit_rate_pct": 50.0,
            "brier_score": 0.25,
            "cal_alpha_mult": 1.0,
            "conviction_shrink": 0.85,
            "max_prob_deviation": 20.0,
            "reliability_tier": "BASELINE",
        }


def simulate_intraday_horizons(k1m, current_price, composite_score, seed=42, orderbook_walls=None, spread_bps=2.0, cal_alpha_mult=1.0, calibration=None, trend_veto=None):
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
    bar_tuples = []  # (log_ret, up_wick_log, down_wick_log)
    for i in range(1, len(k1m)):
        c_prev = float(k1m[i - 1][4])
        o_cur = float(k1m[i][1])
        h_cur = float(k1m[i][2])
        l_cur = float(k1m[i][3])
        c_cur = float(k1m[i][4])
        if c_prev > 0 and c_cur > 0:
            lr = math.log(c_cur / c_prev)
            rets.append(lr)
            ref_top = max(o_cur, c_cur)
            ref_bot = min(o_cur, c_cur)
            uw = math.log(max(h_cur, ref_top) / ref_top) if ref_top > 0 else 0.0
            dw = math.log(ref_bot / max(1e-12, min(l_cur, ref_bot))) if ref_bot > 0 else 0.0
            bar_tuples.append((lr, min(0.015, uw), min(0.015, dw)))

    if len(rets) < 20:
        rets = [0.0002, -0.0002] * 20
        bar_tuples = [(r, 0.0003, 0.0003) for r in rets]

    sigma_1m_raw = statistics.pstdev(rets) or 0.0008
    hist_drift_1m = statistics.mean(rets)

    # Step 2: EWMA Volatility Clustering (lambda = 0.90 weighting recent 10-15m bars)
    lam = 0.90
    ewma_var = sigma_1m_raw ** 2
    for r in rets:
        ewma_var = lam * ewma_var + (1.0 - lam) * ((r - hist_drift_1m) ** 2)
    sigma_ewma = math.sqrt(max(1e-12, ewma_var))
    ewma_vol_ratio = max(0.70, min(1.60, sigma_ewma / max(1e-8, sigma_1m_raw)))
    sigma_1m = sigma_1m_raw * (0.55 + 0.45 * ewma_vol_ratio)
    n_bars = len(bar_tuples)
    block_len = 3 if n_bars >= 6 else 1
    
    ref_score = float(composite_score.get("30m", 0.0)) if isinstance(composite_score, dict) else float(composite_score)
    norm_score = max(-1.0, min(1.0, ref_score / 100.0))

    # Step 5: Compute 60m Swing High/Low Liquidation Magnet Clusters & CLOB Wall Reflection Coordinates
    highs_1m = [float(k[2]) for k in k1m] if k1m else [current_price]
    lows_1m = [float(k[3]) for k in k1m] if k1m else [current_price]
    swing_high_60m = max(highs_1m) if highs_1m else current_price
    swing_low_60m = min(lows_1m) if lows_1m else current_price
    dist_to_high_pct = ((swing_high_60m - current_price) / current_price * 100.0) if current_price > 0 else 1.0
    dist_to_low_pct = ((current_price - swing_low_60m) / current_price * 100.0) if current_price > 0 else 1.0

    liq_magnet_bias = 0.0
    if 0.0 < dist_to_high_pct <= 0.40 and ref_score > 8.0:
        # Short-squeeze liquidation cluster magnet above 60m swing high
        liq_magnet_bias = 0.08 * sigma_1m * (1.0 - dist_to_high_pct / 0.40)
    elif 0.0 < dist_to_low_pct <= 0.40 and ref_score < -8.0:
        # Long-liquidation cascade magnet below 60m swing low
        liq_magnet_bias = -0.08 * sigma_1m * (1.0 - dist_to_low_pct / 0.40)

    sup_wall_log = None
    res_wall_log = None
    if isinstance(orderbook_walls, dict) and current_price > 0:
        sw = orderbook_walls.get("support_wall_price")
        rw = orderbook_walls.get("resistance_wall_price")
        if sw and 0 < sw < current_price:
            sup_wall_log = math.log(float(sw) / current_price)
        if rw and rw > current_price:
            res_wall_log = math.log(float(rw) / current_price)

    # Horizon configs: (name, minutes, alpha_drift_boost)
    # Higher alpha for 10m where microstructure momentum dominates; decaying by 60m
    horizons_cfg = [
        ("10m", 10, 0.45),
        ("30m", 30, 0.35),
        ("60m", 60, 0.25)
    ]

    rng = random.Random(seed)
    horizons_res = {}

    conviction_shrink = float((calibration or {}).get("conviction_shrink", 1.0))
    max_prob_dev = float((calibration or {}).get("max_prob_deviation", 35.0))
    cal_mult = float(cal_alpha_mult if cal_alpha_mult is not None else (calibration or {}).get("cal_alpha_mult", 1.0))

    for name, minutes, alpha in horizons_cfg:
        hz_score = composite_score.get(name, composite_score) if isinstance(composite_score, dict) else composite_score
        norm_score = max(-1.0, min(1.0, float(hz_score) / 100.0))

        # Microstructure-driven drift scaled per horizon (Step 6 Platt calibrated)
        eff_alpha = alpha * max(0.0, min(1.35, cal_mult))
        vol_h_pct = (sigma_1m * math.sqrt(minutes)) * 100.0

        # Total expected alpha return over horizon H in log-space
        tot_alpha_drift = eff_alpha * norm_score * (sigma_1m * math.sqrt(minutes))
        step_drift = (tot_alpha_drift / float(minutes)) + (liq_magnet_bias / float(minutes))
        prelim_exp_move_pct = (math.exp(tot_alpha_drift) - 1.0) * 100.0

        # Pre-calculate barrier levels (log-space) for First-Passage simulation
        long_exp_gain_pct = round(max(0.15, (vol_h_pct * 1.15) + max(0.0, prelim_exp_move_pct)), 2)
        long_stop_risk_pct = round(max(0.12, vol_h_pct * 0.85), 2)
        short_exp_gain_pct = round(max(0.15, (vol_h_pct * 1.15) + max(0.0, -prelim_exp_move_pct)), 2)
        short_stop_risk_pct = round(max(0.12, vol_h_pct * 0.85), 2)

        long_tp_log = math.log(1.0 + long_exp_gain_pct / 100.0)
        long_sl_log = -math.log(1.0 / max(1e-6, 1.0 - long_stop_risk_pct / 100.0))
        short_tp_log = -math.log(1.0 / max(1e-6, 1.0 - short_exp_gain_pct / 100.0))
        short_sl_log = math.log(1.0 + short_stop_risk_pct / 100.0)

        finals = []
        up_count = 0
        down_count = 0
        long_tp_first = 0
        long_sl_first = 0
        long_fp_wins = 0
        short_tp_first = 0
        short_sl_first = 0
        short_fp_wins = 0

        # 5,000 simulated paths with Step 1 (First-Passage) + Step 2 (3-Bar Stationary Block Bootstrap & EWMA Vol)
        vol_scale = sigma_1m / max(1e-8, sigma_1m_raw)
        max_start = max(0, n_bars - block_len)
        for _ in range(5000):
            log_r = 0.0
            l_state = 0  # 0 = active, 1 = TP hit first, -1 = SL hit first
            s_state = 0  # 0 = active, 1 = TP hit first, -1 = SL hit first
            m_idx = 0
            while m_idx < minutes:
                b_start = rng.randint(0, max_start)
                for b_off in range(block_len):
                    if m_idx >= minutes:
                        break
                    lr, uw, dw = bar_tuples[b_start + b_off]
                    step_r = (lr * vol_scale) + step_drift
                    # Step 5: Passive CLOB Limit Wall Reflection when path touches major Bid/Ask wall
                    if sup_wall_log is not None and abs(log_r - sup_wall_log) <= 0.0015:
                        step_r += 0.12 * sigma_1m
                    elif res_wall_log is not None and abs(log_r - res_wall_log) <= 0.0015:
                        step_r -= 0.12 * sigma_1m
                    prev_r = log_r
                    log_r += step_r
                    high_r = max(prev_r, log_r) + (uw * vol_scale * 0.5)
                    low_r = min(prev_r, log_r) - (dw * vol_scale * 0.5)

                    if l_state == 0:
                        if low_r <= long_sl_log:
                            l_state = -1
                        elif high_r >= long_tp_log:
                            l_state = 1
                    if s_state == 0:
                        if high_r >= short_sl_log:
                            s_state = -1
                        elif low_r <= short_tp_log:
                            s_state = 1
                    m_idx += 1

            p_end = current_price * math.exp(log_r)
            finals.append(p_end)
            if p_end >= current_price:
                up_count += 1
            else:
                down_count += 1

            if l_state == 1:
                long_tp_first += 1
                long_fp_wins += 1
            elif l_state == -1:
                long_sl_first += 1
            elif p_end >= current_price:
                long_fp_wins += 1

            if s_state == 1:
                short_tp_first += 1
                short_fp_wins += 1
            elif s_state == -1:
                short_sl_first += 1
            elif p_end < current_price:
                short_fp_wins += 1

        finals.sort()
        terminal_p_up = (up_count / 5000.0) * 100.0
        long_fp_win_pct = round((long_fp_wins / 5000.0) * 100.0, 1)
        short_fp_win_pct = round((short_fp_wins / 5000.0) * 100.0, 1)
        long_tp_first_pct = round((long_tp_first / 5000.0) * 100.0, 1)
        long_sl_first_pct = round((long_sl_first / 5000.0) * 100.0, 1)
        short_tp_first_pct = round((short_tp_first / 5000.0) * 100.0, 1)
        short_sl_first_pct = round((short_sl_first / 5000.0) * 100.0, 1)

        # Blend Terminal Probability (55%) with Path-Dependent First-Passage Survival Probability (45%)
        fp_net_up = 50.0 + 0.5 * (long_fp_win_pct - short_fp_win_pct)
        raw_p_up = 0.55 * terminal_p_up + 0.45 * fp_net_up

        dev_from_50 = (raw_p_up - 50.0) * conviction_shrink
        dev_from_50 = max(-max_prob_dev, min(max_prob_dev, dev_from_50))
        p_up = 50.0 + dev_from_50

        # Microstructure Trend Veto
        if trend_veto == "BEARISH_BREAKDOWN" and name in ("10m", "30m"):
            p_up = min(46.0, p_up)
        elif trend_veto == "BULLISH_BREAKOUT" and name in ("10m", "30m"):
            p_up = max(54.0, p_up)

        p_up = round(max(5.0, min(95.0, p_up)), 1)
        p_down = round(100.0 - p_up, 1)
        prec = 6 if current_price < 1.0 else 4

        exp_price = round(statistics.mean(finals), prec)
        exp_move_pct = round(((exp_price - current_price) / current_price) * 100.0, 2)

        # Directional harmony check: ensure expected price move agrees with calibrated probability
        if p_up >= 52.0 and exp_move_pct < 0:
            exp_move_pct = abs(exp_move_pct)
            exp_price = round(current_price * (1.0 + exp_move_pct / 100.0), prec)
        elif p_up <= 48.0 and exp_move_pct > 0:
            exp_move_pct = -abs(exp_move_pct)
            exp_price = round(current_price * (1.0 + exp_move_pct / 100.0), prec)

        conf_80_low = round(finals[int(5000 * 0.10)], prec)
        conf_80_high = round(finals[int(5000 * 0.90)], prec)

        # Step 7: Round-Trip Execution Fee (4 bps Maker/BNB discount) + Live Bid-Ask Spread Deduction
        eff_spread_pct = max(0.005, min(0.10, float(spread_bps or 2.0) / 100.0))
        round_trip_cost_pct = round(0.04 + eff_spread_pct, 4)

        # -------------------------------------------------------------
        # LONG Outcome Analysis (with Step 1 First-Passage & Step 7 Net-of-Fee Edge)
        # -------------------------------------------------------------
        long_win_pct = p_up
        long_exp_gain_pct = round(max(0.15, (vol_h_pct * 1.15) + max(0.0, exp_move_pct)), 2)
        long_stop_risk_pct = round(max(0.12, vol_h_pct * 0.85), 2)
        long_net_gain_pct = round(max(0.03, long_exp_gain_pct - round_trip_cost_pct), 2)
        long_net_stop_pct = round(long_stop_risk_pct + round_trip_cost_pct, 2)
        long_target_price = round(current_price * (1.0 + (long_exp_gain_pct / 100.0)), prec)
        long_stop_price = round(current_price * (1.0 - (long_stop_risk_pct / 100.0)), prec)
        long_rr = round(long_exp_gain_pct / max(0.01, long_stop_risk_pct), 2)
        long_net_rr = round(long_net_gain_pct / max(0.01, long_net_stop_pct), 2)
        long_edge_r = round(((long_win_pct / 100.0) * long_net_rr) - ((1.0 - (long_win_pct / 100.0)) * 1.0), 2)

        # -------------------------------------------------------------
        # SHORT Outcome Analysis (with Step 1 First-Passage & Step 7 Net-of-Fee Edge)
        # -------------------------------------------------------------
        short_win_pct = p_down
        short_exp_gain_pct = round(max(0.15, (vol_h_pct * 1.15) + max(0.0, -exp_move_pct)), 2)
        short_stop_risk_pct = round(max(0.12, vol_h_pct * 0.85), 2)
        short_net_gain_pct = round(max(0.03, short_exp_gain_pct - round_trip_cost_pct), 2)
        short_net_stop_pct = round(short_stop_risk_pct + round_trip_cost_pct, 2)
        short_target_price = round(current_price * (1.0 - (short_exp_gain_pct / 100.0)), prec)
        short_stop_price = round(current_price * (1.0 + (short_stop_risk_pct / 100.0)), prec)
        short_rr = round(short_exp_gain_pct / max(0.01, short_stop_risk_pct), 2)
        short_net_rr = round(short_net_gain_pct / max(0.01, short_net_stop_pct), 2)
        short_edge_r = round(((short_win_pct / 100.0) * short_net_rr) - ((1.0 - (short_win_pct / 100.0)) * 1.0), 2)

        # Determine which action is more profitable (strictly aligned with directional probability)
        if p_up > 51.5:
            preferred_action = "LONG"
            advantage_pct = round(p_up - p_down, 1)
        elif p_up < 48.5:
            preferred_action = "SHORT"
            advantage_pct = round(p_down - p_up, 1)
        else:
            preferred_action = "NEUTRAL"
            advantage_pct = 0.0

        horizons_res[name] = {
            "horizon": name,
            "minutes": minutes,
            "ewma_vol_ratio": round(ewma_vol_ratio, 3),
            "block_size_min": block_len,
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
                "first_passage_win_pct": long_fp_win_pct,
                "tp_hit_first_pct": long_tp_first_pct,
                "sl_hit_first_pct": long_sl_first_pct,
                "expected_gain_pct": long_exp_gain_pct,
                "net_expected_gain_pct": long_net_gain_pct,
                "stop_risk_pct": long_stop_risk_pct,
                "round_trip_cost_pct": round_trip_cost_pct,
                "target_price": long_target_price,
                "stop_price": long_stop_price,
                "reward_risk_ratio": long_rr,
                "net_reward_risk_ratio": long_net_rr,
                "math_edge_R": long_edge_r,
                "profitable": long_edge_r > 0
            },
            "short_outcome": {
                "action": "SHORT",
                "win_probability_pct": short_win_pct,
                "first_passage_win_pct": short_fp_win_pct,
                "tp_hit_first_pct": short_tp_first_pct,
                "sl_hit_first_pct": short_sl_first_pct,
                "expected_gain_pct": short_exp_gain_pct,
                "net_expected_gain_pct": short_net_gain_pct,
                "stop_risk_pct": short_stop_risk_pct,
                "round_trip_cost_pct": round_trip_cost_pct,
                "target_price": short_target_price,
                "stop_price": short_stop_price,
                "reward_risk_ratio": short_rr,
                "net_reward_risk_ratio": short_net_rr,
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


def _fetch_1m_history(bsym):
    """~50h of 1m klines (3 paged requests) used to fit and validate the price forecaster.
    The forecaster caches the fitted model for 60s, so this runs at most once a minute per coin."""
    try:
        limit = price_forecaster.HISTORY_LIMIT
        pages = getattr(price_forecaster, "HISTORY_PAGES", 1)
        out = predict.spot_get(f"/api/v3/klines?symbol={bsym}&interval=1m&limit={limit}") or []
        for _ in range(pages - 1):
            if not out:
                break
            end_ms = int(out[0][0]) - 1
            older = predict.spot_get(f"/api/v3/klines?symbol={bsym}&interval=1m&limit={limit}&endTime={end_ms}") or []
            if not older:
                break
            out = older + out
        return out
    except Exception:
        return []


def estimate_coin_intraday(symbol):
    bsym = predict.to_binance(symbol)
    try:
        # 1. Fetch live spot ticker, 1m/5m klines, L2 depth, and deep 10Y + 25-source context
        with ThreadPoolExecutor(max_workers=4) as ex:
            f_tk = ex.submit(predict.spot_get, f"/api/v3/ticker/24hr?symbol={bsym}")
            f_k1m = ex.submit(predict.spot_get, f"/api/v3/klines?symbol={bsym}&interval=1m&limit=90")
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

        # 3. Compute Fast Intraday Microstructure Score (-100 to +100) + Step 3 Micro-Price/OFI + Step 4 BTC Lead-Lag
        fut_data["symbol"] = bsym
        ob_live = compute_microprice_and_ofi(depth, symbol=bsym)
        micro_score = compute_intraday_microstructure_score(k1m, k5m, depth, fut_data)
        btc_lead_lag = compute_btc_lead_lag_spillover(bsym, k1m)
        btc_spillover = float(btc_lead_lag.get("spillover_score", 0.0))

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

        # Holistic all-aspect score (-100 to +100) combining fast CLOB order-flow + BTC Lead-Lag with 10Y + News + Perps + MTF
        if ctx:
            deep_structural_score = max(-100.0, min(100.0, (mtf_score + fut_score + news_score + ty_score + inst_macro_score) * 1.15))
            comp_score = round(max(-100.0, min(100.0, 0.55 * micro_score + 0.45 * deep_structural_score + btc_spillover)), 1)
        else:
            deep_structural_score = micro_score
            comp_score = round(max(-100.0, min(100.0, micro_score + btc_spillover)), 1)

        # Microstructure Trend Veto
        trend_veto = detect_microstructure_trend_veto(k1m, k5m)

        # Horizon-adaptive score blending
        score_10m = max(-100.0, min(100.0, 0.85 * micro_score + 0.15 * mtf_score + btc_spillover))
        score_30m = max(-100.0, min(100.0, 0.65 * micro_score + 0.35 * deep_structural_score + 0.7 * btc_spillover))
        score_60m = max(-100.0, min(100.0, 0.45 * micro_score + 0.55 * deep_structural_score + 0.4 * btc_spillover))
        hz_scores = {"10m": round(score_10m, 1), "30m": round(score_30m, 1), "60m": round(score_60m, 1)}

        ob_summary = predict.analyze_orderbook(depth, price)
        calibration = calibrate_intraday_model(k1m)

        # 5. Run 10m, 30m, 60m Monte Carlo with horizon-adaptive scores and calibration bounds
        horizons = simulate_intraday_horizons(
            k1m,
            price,
            hz_scores,
            orderbook_walls=ob_summary,
            spread_bps=ob_live.get("spread_bps", 2.0),
            cal_alpha_mult=calibration.get("cal_alpha_mult", 1.0),
            calibration=calibration,
            trend_veto=trend_veto,
        )
        # 5b. Calibrated 10m / 30m / 60m price targets (walk-forward validated, live-scored)
        price_targets = {}
        if price_forecaster:
            try:
                price_targets = price_forecaster.forecast_prices(bsym, k1m, price, _fetch_1m_history, mc_horizons=horizons)
            except Exception as pf_err:
                sys.stderr.write(f"price forecaster error {bsym}: {pf_err}\n")
                price_targets = {}
        for hz_name, pt in price_targets.items():
            if hz_name in horizons:
                horizons[hz_name]["price_target"] = pt
                # Harmonization: If forecaster has no out-of-sample edge, force MC probability to neutral
                if pt.get("method") == "no-change (model had no out-of-sample edge)":
                    horizons[hz_name]["prob_up_pct"] = 50.0
                    horizons[hz_name]["prob_down_pct"] = 50.0
                    horizons[hz_name]["advantage_pct"] = 0.0
                    horizons[hz_name]["preferred_action"] = "NEUTRAL"
                    horizons[hz_name]["expected_move_pct"] = 0.0
                    horizons[hz_name]["expected_price"] = price
                    horizons[hz_name]["long_outcome"]["win_probability_pct"] = 50.0
                    horizons[hz_name]["long_outcome"]["math_edge_R"] = 0.0
                    horizons[hz_name]["long_outcome"]["profitable"] = False
                    horizons[hz_name]["short_outcome"]["win_probability_pct"] = 50.0
                    horizons[hz_name]["short_outcome"]["math_edge_R"] = 0.0
                    horizons[hz_name]["short_outcome"]["profitable"] = False
                elif pt.get("method") == "walk-forward ridge (validated)":
                    pt_move = float(pt.get("predicted_move_pct", 0.0))
                    if pt_move < -0.02 and horizons[hz_name]["prob_up_pct"] > 50.0:
                        p_rev = 100.0 - horizons[hz_name]["prob_up_pct"]
                        horizons[hz_name]["prob_up_pct"] = min(48.0, p_rev)
                        horizons[hz_name]["prob_down_pct"] = 100.0 - horizons[hz_name]["prob_up_pct"]
                        horizons[hz_name]["preferred_action"] = "SHORT"
                    elif pt_move > 0.02 and horizons[hz_name]["prob_down_pct"] > 50.0:
                        p_rev = 100.0 - horizons[hz_name]["prob_down_pct"]
                        horizons[hz_name]["prob_down_pct"] = min(48.0, p_rev)
                        horizons[hz_name]["prob_up_pct"] = 100.0 - horizons[hz_name]["prob_down_pct"]
                        horizons[hz_name]["preferred_action"] = "LONG" 

        macro_horizons = {}
        if ctx.get("k1h"):
            macro_horizons = predict.simulate_horizons(ctx["k1h"], price, deep_structural_score, ten_year=ty_obj)

        # Primary 30m decision as default baseline
        h30 = horizons["30m"]
        primary_action = h30["preferred_action"]
        primary_win_prob = h30["long_outcome"]["win_probability_pct"] if primary_action == "LONG" else h30["short_outcome"]["win_probability_pct"]
        primary_edge = h30["long_outcome"]["math_edge_R"] if primary_action == "LONG" else h30["short_outcome"]["math_edge_R"]
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
            "calibration": calibration,
            "horizons": horizons,
            "macro_horizons": macro_horizons,
            "pillar_breakdown": {
                "clob_intraday_flow": round(micro_score, 1),
                "btc_lead_lag_spillover": round(btc_spillover, 2),
                "beta_to_btc_60m": btc_lead_lag.get("beta_to_btc", 1.0),
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
                "microprice": ob_live.get("microprice", price),
                "microprice_tilt_pct": round(ob_live.get("microprice_tilt", 0.0) * 100.0, 1),
                "delta_ofi_pct": round(ob_live.get("delta_ofi_norm", 0.0) * 100.0, 1),
                "spread_bps": ob_live.get("spread_bps", 2.0),
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
    lines.append("6 COINS FUTURE -- BINANCE LONG & SHORT PREDICTION ENGINE (10Y DATA + 25-OUTLET NEWS)")
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
        lines.append(f"{'HORIZON':<8}{'CALL':<7}{'PREDICTED PRICE':<18}{'EXP MOVE':<11}{'PROB(UP/DN)':<16}{'IF LONG (Target/Stop)':<25}{'IF SHORT (Target/Stop)':<25}")
        lines.append("-" * 110)

        for tf in ["10m", "30m", "60m"]:
            hz = h[tf]
            pt = hz.get("price_target") or {}
            pred_p = pt.get("predicted_price", hz.get("expected_price", price))
            p_prec = 6 if pred_p < 1.0 else (4 if pred_p < 100.0 else 2)
            pred_p_str = f"${pred_p:,.{p_prec}f}"
            call_str = f"{hz['preferred_action']}"
            probs_str = f"{hz['prob_up_pct']:.0f}% / {hz['prob_down_pct']:.0f}%"
            exp_str = f"{hz['expected_move_pct']:+.2f}%"

            l = hz["long_outcome"]
            s = hz["short_outcome"]
            l_t = l.get("target_price", 0)
            l_s = l.get("stop_price", 0)
            s_t = s.get("target_price", 0)
            s_s = s.get("stop_price", 0)
            long_info = f"${l_t:,.{p_prec}f} (${l_s:,.{p_prec}f})"
            short_info = f"${s_t:,.{p_prec}f} (${s_s:,.{p_prec}f})"

            lines.append(f"{tf:<8}{call_str:<7}{pred_p_str:<18}{exp_str:<11}{probs_str:<16}{long_info:<25}{short_info:<25}")

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
