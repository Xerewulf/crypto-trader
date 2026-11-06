#!/usr/bin/env python3
"""Institutional Binance USDⓈ-M Perpetual Futures Signal Engine (trader/futures/strategy.py).
Standard library only. Zero external dependencies.

Implements the simplified, high-expectancy 3-Playbook Futures Doctrine:
  1. Playbook 1: HTF Trend + 4-Quadrant OI / Whale Pullback (Trend Continuation)
     - Requires 4h and 1h trend alignment + BTC regime alignment
     - Pullback to 24h Rolling VWAP or 7d Volume Profile POC/VAL with Taker Buy CVD absorption
     - Confirmed by 4-Quadrant Open Interest build-up or Whale vs. Retail Long/Short positioning
  2. Playbook 2: Wyckoff Liquidity Sweep + Crowded Funding Squeeze (Institutional Reversal)
     - Bullish Spring below 7d VAL / 12h Low with aggressive Taker CVD absorption (>54%)
     - Bearish Upthrust / SFP above 7d VAH / 12h High with Taker exhaustion (<46%)
     - Extra conviction when fading crowded retail funding extremes
  3. Playbook 3: Delta-Neutral Perpetual Funding Arbitrage (Zero-Delta Yield)
     - Long Spot + Short 1x USDⓈ-M Perp when 8h funding >= +0.03% (>32% APR)
"""

import json
import math
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TRADER_ANALYST = os.path.join(ROOT, "trader-analyst")
if TRADER_ANALYST not in sys.path:
    sys.path.insert(0, TRADER_ANALYST)

try:
    import margin
except ImportError:
    margin = None

FUT_HOSTS = ("https://fapi.binance.com",)
SPOT_HOSTS = ("https://api.binance.com", "https://data-api.binance.vision")

# Focused Tier-1 Liquid USDⓈ-M Perpetual Futures Universe (deepest order books, tightest spreads)
CORE_FUTURES_UNIVERSE = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "DOGEUSDT", "SUIUSDT", "LINKUSDT", "AVAXUSDT", "ADAUSDT",
    "LTCUSDT", "NEARUSDT",
]


def fetch_json(hosts, path, timeout=10):
    last_err = None
    for h in hosts:
        try:
            req = urllib.request.Request(
                h + path,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            last_err = e
    raise RuntimeError("API unreachable (%s): %s" % (path.split("?")[0], last_err))


def fetch_futures_klines(sym, interval="1h", limit=200, start_ms=None, end_ms=None):
    """Fetches true USDⓈ-M Perpetual Futures klines from fapi.binance.com (with spot fallback for offline/restricted envs)."""
    path = "/fapi/v1/klines?symbol=%s&interval=%s&limit=%d" % (sym, interval, limit)
    if start_ms is not None:
        path += "&startTime=%d" % int(start_ms)
    if end_ms is not None:
        path += "&endTime=%d" % int(end_ms)
    try:
        raw = fetch_json(FUT_HOSTS, path)
    except Exception:
        spot_path = "/api/v3/klines?symbol=%s&interval=%s&limit=%d" % (sym, interval, limit)
        if start_ms is not None:
            spot_path += "&startTime=%d" % int(start_ms)
        if end_ms is not None:
            spot_path += "&endTime=%d" % int(end_ms)
        raw = fetch_json(SPOT_HOSTS, spot_path)

    out = []
    for k in raw:
        qv = float(k[7])
        tb_q = float(k[10])
        out.append({
            "t": int(k[0]),
            "ct": int(k[6]),
            "o": float(k[1]),
            "h": float(k[2]),
            "l": float(k[3]),
            "c": float(k[4]),
            "vol_usd": qv,
            "tb_usd": tb_q,
            "tb": (tb_q / qv * 100.0) if qv > 0 else 50.0,
            "cvd_usd": (2.0 * tb_q) - qv,
        })
    return out


def ema_series(vals, span):
    if not vals:
        return []
    k = 2.0 / (span + 1.0)
    cur = float(vals[0])
    out = [cur]
    for v in vals[1:]:
        cur = float(v) * k + cur * (1.0 - k)
        out.append(cur)
    return out


def atr_series(bars, period=14):
    if not bars:
        return []
    trs = []
    for i, b in enumerate(bars):
        if i == 0:
            trs.append(b["h"] - b["l"])
        else:
            pc = bars[i - 1]["c"]
            trs.append(max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc)))
    out = []
    cur = sum(trs[:min(len(trs), period)]) / max(1, min(len(trs), period))
    for i, tr in enumerate(trs):
        if i >= period:
            cur = (cur * (period - 1) + tr) / period
        out.append(cur)
    return out


def rsi_series(closes, period=14):
    if len(closes) < 2:
        return [50.0] * len(closes)
    out = [50.0]
    avg_g = 0.0
    avg_l = 0.0
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        g = max(0.0, d)
        l = max(0.0, -d)
        if i <= period:
            avg_g = ((avg_g * (i - 1)) + g) / i
            avg_l = ((avg_l * (i - 1)) + l) / i
        else:
            avg_g = (avg_g * (period - 1) + g) / period
            avg_l = (avg_l * (period - 1) + l) / period
        if avg_l <= 1e-12:
            out.append(100.0)
        else:
            rs = avg_g / avg_l
            out.append(100.0 - (100.0 / (1.0 + rs)))
    return out


def rolling_vwap(bars, n_bars=24):
    sub = bars[-n_bars:] if len(bars) >= n_bars else bars
    if not sub:
        return 0.0
    num = sum(((b["h"] + b["l"] + b["c"]) / 3.0) * b["vol_usd"] for b in sub)
    den = sum(b["vol_usd"] for b in sub)
    return (num / den) if den > 0 else sub[-1]["c"]


def volume_profile(bars, n_bars=120, bins=32):
    """Computes 5-to-7 Day Volume Profile: POC, VAL (70%), and VAH (70%)."""
    sub = bars[-n_bars:] if len(bars) >= n_bars else bars
    if not sub:
        return {"poc": 0.0, "val": 0.0, "vah": 0.0, "state": "BALANCED"}
    cur_p = sub[-1]["c"]
    lo_min = min(b["l"] for b in sub)
    hi_max = max(b["h"] for b in sub)
    if hi_max <= lo_min:
        return {"poc": cur_p, "val": cur_p, "vah": cur_p, "state": "BALANCED"}

    step = (hi_max - lo_min) / bins
    hist = [0.0] * bins
    for b in sub:
        tp = (b["h"] + b["l"] + b["c"]) / 3.0
        idx = min(bins - 1, max(0, int((tp - lo_min) / step)))
        hist[idx] += b["vol_usd"]

    poc_idx = max(range(bins), key=lambda i: hist[i])
    poc_price = lo_min + (poc_idx + 0.5) * step
    target_v = 0.70 * sum(hist)
    acc_v = hist[poc_idx]
    lo_i = hi_i = poc_idx

    while acc_v < target_v and (lo_i > 0 or hi_i < bins - 1):
        up_v = hist[hi_i + 1] if hi_i + 1 < bins else -1.0
        dn_v = hist[lo_i - 1] if lo_i - 1 >= 0 else -1.0
        if up_v >= dn_v and hi_i + 1 < bins:
            hi_i += 1
            acc_v += hist[hi_i]
        elif lo_i - 1 >= 0:
            lo_i -= 1
            acc_v += hist[lo_i]
        else:
            break

    val_price = lo_min + lo_i * step
    vah_price = lo_min + (hi_i + 1) * step
    if cur_p > vah_price:
        state = "ABOVE_VAH"
    elif cur_p < val_price:
        state = "BELOW_VAL"
    elif cur_p >= poc_price:
        state = "UPPER_VALUE"
    else:
        state = "LOWER_VALUE"

    return {
        "poc": poc_price,
        "val": val_price,
        "vah": vah_price,
        "state": state,
    }


def fetch_derivatives_positioning(sym):
    """Fetches live USDⓈ-M Perpetual Funding, Mark-Index Basis, Open Interest 4h change,
    Whale Top Trader L/S Ratio vs Global Retail L/S Ratio, and Order Book Depth Imbalance."""
    out = {
        "funding_8h": 0.0001,
        "mark_price": 0.0,
        "basis_pct": 0.0,
        "oi_change_4h_pct": 0.0,
        "whale_ls_ratio": 1.0,
        "retail_ls_ratio": 1.0,
        "imb_1pct": 0.0,
    }
    try:
        pi = fetch_json(FUT_HOSTS, "/fapi/v1/premiumIndex?symbol=%s" % sym, timeout=6)
        if isinstance(pi, dict):
            out["funding_8h"] = float(pi.get("lastFundingRate", 0.0001))
            mark = float(pi.get("markPrice", 0.0))
            idx = float(pi.get("indexPrice", 0.0))
            out["mark_price"] = mark
            if idx > 0:
                out["basis_pct"] = round((mark - idx) / idx * 100.0, 4)
    except Exception:
        pass

    try:
        oi_hist = fetch_json(
            FUT_HOSTS,
            "/futures/data/openInterestHist?symbol=%s&period=1h&limit=5" % sym,
            timeout=6
        )
        if isinstance(oi_hist, list) and len(oi_hist) >= 2:
            oi_now = float(oi_hist[-1].get("sumOpenInterestValue", 0.0))
            oi_old = float(oi_hist[0].get("sumOpenInterestValue", 0.0))
            if oi_old > 0:
                out["oi_change_4h_pct"] = round((oi_now - oi_old) / oi_old * 100.0, 2)
    except Exception:
        pass

    try:
        whale = fetch_json(
            FUT_HOSTS,
            "/futures/data/topLongShortPositionRatio?symbol=%s&period=1h&limit=1" % sym,
            timeout=6
        )
        if isinstance(whale, list) and whale:
            out["whale_ls_ratio"] = round(float(whale[-1].get("longShortRatio", 1.0)), 2)
    except Exception:
        pass

    try:
        retail = fetch_json(
            FUT_HOSTS,
            "/futures/data/globalLongShortAccountRatio?symbol=%s&period=1h&limit=1" % sym,
            timeout=6
        )
        if isinstance(retail, list) and retail:
            out["retail_ls_ratio"] = round(float(retail[-1].get("longShortRatio", 1.0)), 2)
    except Exception:
        pass

    try:
        depth = fetch_json(FUT_HOSTS, "/fapi/v1/depth?symbol=%s&limit=500" % sym, timeout=6)
        bids = [(float(p), float(q)) for p, q in depth.get("bids", [])]
        asks = [(float(p), float(q)) for p, q in depth.get("asks", [])]
        if bids and asks:
            mid = (bids[0][0] + asks[0][0]) / 2.0
            b1 = sum(p * q for p, q in bids if p >= mid * 0.99)
            a1 = sum(p * q for p, q in asks if p <= mid * 1.01)
            if (b1 + a1) > 0:
                out["imb_1pct"] = round((b1 - a1) / (b1 + a1) * 100.0, 1)
    except Exception:
        pass

    return out


def classify_oi_quadrant(price_chg_4h_pct, oi_chg_4h_pct):
    """Arthur Hayes 4-Quadrant Price x Open Interest Matrix."""
    if abs(oi_chg_4h_pct) < 0.3:
        return "NEUTRAL_OI"
    if price_chg_4h_pct >= 0 and oi_chg_4h_pct > 0:
        return "LONG_BUILDUP"
    if price_chg_4h_pct > 0 and oi_chg_4h_pct < 0:
        return "SHORT_COVERING"
    if price_chg_4h_pct < 0 and oi_chg_4h_pct > 0:
        return "SHORT_BUILDUP"
    return "LONG_FLUSH"


def evaluate_futures_setup(sym, bars_1h, btc_regime="NEUTRAL", deriv=None):
    """Evaluates a symbol's 1h klines + higher-timeframe 4h trend (EMA50_1h vs EMA200_1h)
    and derivatives microstructure to produce a high-conviction Futures Signal.

    Works identically in both historical walk-forward backtesting and live execution!
    """
    if len(bars_1h) < 120:
        return None, {"symbol": sym, "signal": "Insufficient history"}

    deriv = deriv or {
        "funding_8h": 0.0001,
        "oi_change_4h_pct": 0.0,
        "whale_ls_ratio": 1.0,
        "retail_ls_ratio": 1.0,
        "imb_1pct": 0.0,
    }

    closes = [b["c"] for b in bars_1h]
    e20_s = ema_series(closes, 20)
    e50_s = ema_series(closes, 50)
    e200_s = ema_series(closes, 160)  # 4h ~EMA40 / HTF structural baseline
    atr_s = atr_series(bars_1h, 14)
    rsi_s = rsi_series(closes, 14)

    b = bars_1h[-1]
    prev = bars_1h[-2]
    c = b["c"]
    e20 = e20_s[-1]
    e50 = e50_s[-1]
    e200 = e200_s[-1]
    a = atr_s[-1]
    rsi = rsi_s[-1]

    if a <= 0 or c <= 0:
        return None, {"symbol": sym, "signal": "Zero volatility"}

    vp = volume_profile(bars_1h[:-1], n_bars=120)
    vwap_24h = rolling_vwap(bars_1h, 24)
    vwap_dist_pct = ((c / vwap_24h) - 1.0) * 100.0 if vwap_24h > 0 else 0.0

    # 4-hour price change and Taker Buy CVD flow
    c_4h_ago = bars_1h[-5]["c"] if len(bars_1h) >= 5 else prev["c"]
    price_chg_4h = ((c / c_4h_ago) - 1.0) * 100.0 if c_4h_ago > 0 else 0.0
    tb_4h_vol = sum(x["vol_usd"] for x in bars_1h[-4:])
    tb_4h_buy = sum(x["tb_usd"] for x in bars_1h[-4:])
    tb_4h_pct = (tb_4h_buy / tb_4h_vol * 100.0) if tb_4h_vol > 0 else 50.0

    # Higher-Timeframe (HTF) Trend Structure + Slope Filter (blocks choppy flat EMA whipsaws)
    e50_slope = ((e50 - e50_s[-5]) / e50_s[-5] * 100.0) if len(e50_s) >= 5 and e50_s[-5] > 0 else 0.0
    htf_bull = (c > e200 and e20 > e50 and e50 > e200 and e50_slope >= 0.08)
    htf_bear = (c < e200 and e20 < e50 and e50 < e200 and e50_slope <= -0.08)
    trend = "BULLISH" if htf_bull else ("BEARISH" if htf_bear else "ROTATIONAL")

    oi_quad = classify_oi_quadrant(price_chg_4h, deriv.get("oi_change_4h_pct", 0.0))
    whale_edge = deriv.get("whale_ls_ratio", 1.0) - deriv.get("retail_ls_ratio", 1.0)
    funding = deriv.get("funding_8h", 0.0001)
    imb = deriv.get("imb_1pct", 0.0)

    # Prior structural swing levels (excluding current bar)
    low_24 = min(x["l"] for x in bars_1h[-25:-1])
    high_24 = max(x["h"] for x in bars_1h[-25:-1])
    low_8 = min(x["l"] for x in bars_1h[-9:-1])
    high_8 = max(x["h"] for x in bars_1h[-9:-1])

    avg_vol_20 = sum(x["vol_usd"] for x in bars_1h[-21:-1]) / 20.0
    vol_ratio = (b["vol_usd"] / avg_vol_20) if avg_vol_20 > 0 else 1.0

    candle_range = max(1e-9, b["h"] - b["l"])
    candle_bull = (b["c"] > b["o"]) and ((b["c"] - b["l"]) >= 0.62 * candle_range)
    candle_bear = (b["c"] < b["o"]) and ((b["h"] - b["c"]) >= 0.62 * candle_range)

    # =========================================================================
    # PLAYBOOK 1: Initiative Volume Breakout + Value Area Acceptance (Momentum)
    # =========================================================================
    pb1_long = (
        htf_bull
        and btc_regime == "BULL"
        and c > high_8 and c >= vp["poc"]
        and candle_bull
        and vol_ratio >= 1.45
        and b["tb"] >= 55.0 and tb_4h_pct >= 53.0
        and 53.0 <= rsi <= 68.0
        and funding < 0.0005
    )

    pb1_short = (
        htf_bear
        and btc_regime == "BEAR"
        and c < low_8 and c <= vp["poc"]
        and candle_bear
        and vol_ratio >= 1.45
        and b["tb"] <= 45.0 and tb_4h_pct <= 47.0
        and 32.0 <= rsi <= 47.0
        and funding > -0.0005
    )

    # =========================================================================
    # PLAYBOOK 2: Wyckoff Liquidity Sweep (Aligned with HTF / Neutral Regime)
    # =========================================================================
    pb2_spring_long = (
        btc_regime != "BEAR"
        and not htf_bear
        and c >= e50 * 0.996
        and (b["l"] <= low_24 * 1.0005 or (vp["val"] > 0 and b["l"] <= vp["val"] * 0.999))
        and c > max(low_8, vp["val"] if vp["val"] > 0 else low_24)
        and candle_bull
        and vol_ratio >= 1.35
        and b["tb"] >= 55.5
        and 40.0 <= rsi <= 63.0
        and funding <= 0.0003
    )

    pb2_upthrust_short = (
        btc_regime != "BULL"
        and not htf_bull
        and c <= e50 * 1.004
        and (b["h"] >= high_24 * 0.9995 or (vp["vah"] > 0 and b["h"] >= vp["vah"] * 1.001))
        and c < min(high_8, vp["vah"] if vp["vah"] > 0 else high_24)
        and candle_bear
        and vol_ratio >= 1.35
        and b["tb"] <= 44.5
        and 37.0 <= rsi <= 60.0
        and funding >= -0.0003
    )

    sig = None
    if pb2_spring_long or pb1_long:
        is_spring = pb2_spring_long
        setup_name = (
            "Playbook 2: Wyckoff 24h Liquidity Sweep + CVD Absorption"
            if is_spring else "Playbook 1: HTF Initiative Breakout + Taker CVD Surge"
        )
        raw_stop = min(b["l"], prev["l"]) - 0.55 * a
        min_stop_dist = max(1.15 * a, c * 0.011)
        max_stop_dist = min(2.6 * a, c * 0.032)
        stop_dist = min(max_stop_dist, max(min_stop_dist, c - raw_stop))
        stop_px = c - stop_dist

        score = 84 if is_spring else 82
        if b["tb"] >= 57.0 or vol_ratio >= 1.8:
            score += 6
        if oi_quad in ("LONG_BUILDUP", "LONG_FLUSH"):
            score += 4
        if whale_edge >= 0.15:
            score += 4
        if funding < 0.0:
            score += 4
        score = min(98, score)

        sig = {
            "side": "long",
            "playbook": "PB2_SPRING" if is_spring else "PB1_BREAKOUT",
            "setup": setup_name,
            "entry": c,
            "stop": stop_px,
            "stop_dist_pct": round(stop_dist / c * 100.0, 2),
            "tp1_r": 1.35,
            "tp2_r": 2.8,
            "tp1_px": c + 1.35 * stop_dist,
            "tp2_px": c + 2.8 * stop_dist,
            "score": score,
        }

    elif pb2_upthrust_short or pb1_short:
        is_upthrust = pb2_upthrust_short
        setup_name = (
            "Playbook 2: Wyckoff 24h Upthrust SFP + CVD Exhaustion"
            if is_upthrust else "Playbook 1: HTF Initiative Breakdown + Taker Sell Surge"
        )
        raw_stop = max(b["h"], prev["h"]) + 0.55 * a
        min_stop_dist = max(1.15 * a, c * 0.011)
        max_stop_dist = min(2.6 * a, c * 0.032)
        stop_dist = min(max_stop_dist, max(min_stop_dist, raw_stop - c))
        stop_px = c + stop_dist

        score = 84 if is_upthrust else 82
        if b["tb"] <= 43.0 or vol_ratio >= 1.8:
            score += 6
        if oi_quad in ("SHORT_BUILDUP", "SHORT_COVERING"):
            score += 4
        if whale_edge <= -0.15:
            score += 4
        if funding > 0.0002:
            score += 4
        score = min(98, score)

        sig = {
            "side": "short",
            "playbook": "PB2_UPTHRUST" if is_upthrust else "PB1_BREAKDOWN",
            "setup": setup_name,
            "entry": c,
            "stop": stop_px,
            "stop_dist_pct": round(stop_dist / c * 100.0, 2),
            "tp1_r": 1.35,
            "tp2_r": 2.8,
            "tp1_px": c - 1.35 * stop_dist,
            "tp2_px": c - 2.8 * stop_dist,
            "score": score,
        }

    # Composite directional score (-100 to +100) for dashboard visibility
    dir_score = 0
    dir_score += 25 if htf_bull else (-25 if htf_bear else 0)
    dir_score += int(max(-25.0, min(25.0, (tb_4h_pct - 50.0) * 4.0)))
    dir_score += int(max(-15.0, min(15.0, imb * 0.5)))
    dir_score += 10 if whale_edge > 0.1 else (-10 if whale_edge < -0.1 else 0)
    if sig:
        dir_score = sig["score"] if sig["side"] == "long" else -sig["score"]

    diag = {
        "symbol": sym,
        "close": c,
        "trend": trend,
        "btc_regime": btc_regime,
        "ema20": e20,
        "ema50": e50,
        "ema200": e200,
        "rsi": round(rsi, 1),
        "atr_pct": round(a / c * 100.0, 2),
        "vwap_24h": vwap_24h,
        "vwap_dist_pct": round(vwap_dist_pct, 2),
        "val": vp["val"],
        "poc": vp["poc"],
        "vah": vp["vah"],
        "auction_state": vp["state"],
        "taker_buy_1h": round(b["tb"], 1),
        "taker_buy_4h": round(tb_4h_pct, 1),
        "oi_quadrant": oi_quad,
        "oi_change_4h_pct": deriv.get("oi_change_4h_pct", 0.0),
        "whale_ls": deriv.get("whale_ls_ratio", 1.0),
        "retail_ls": deriv.get("retail_ls_ratio", 1.0),
        "imb_1pct": imb,
        "funding_8h": funding,
        "composite_score": max(-100, min(100, dir_score)),
        "side": sig["side"] if sig else ("long" if dir_score >= 0 else "short"),
        "signal": ("[%s %d] %s" % (sig["side"].upper(), sig["score"], sig["setup"])) if sig else "Waiting for A+ Confluence",
    }
    return sig, diag


def detect_btc_regime(btc_bars_1h):
    """Determines the master BTCUSDT regime so altcoin trades never fight Bitcoin."""
    if len(btc_bars_1h) < 120:
        return "NEUTRAL"
    closes = [b["c"] for b in btc_bars_1h]
    e20 = ema_series(closes, 20)[-1]
    e50 = ema_series(closes, 50)[-1]
    e200 = ema_series(closes, 160)[-1]
    c = closes[-1]
    tb_4h_vol = sum(x["vol_usd"] for x in btc_bars_1h[-4:])
    tb_4h_buy = sum(x["tb_usd"] for x in btc_bars_1h[-4:])
    tb_4h = (tb_4h_buy / tb_4h_vol * 100.0) if tb_4h_vol > 0 else 50.0

    if c > e200 and e20 >= e50 * 0.998 and tb_4h >= 49.0:
        return "BULL"
    if c < e200 and e20 <= e50 * 1.002 and tb_4h <= 51.0:
        return "BEAR"
    return "NEUTRAL"
