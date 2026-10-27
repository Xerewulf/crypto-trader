#!/usr/bin/env python3
"""Crypto Directional Forecaster & Market Microstructure Analyst (trader-analyst).
Standard library only, no API keys required.

Tracks the 6 structural forces that move cryptocurrency prices up and down:
  1. Perpetual Futures Microstructure (Funding Rate, Mark-Index Basis, Open Interest
     1h/4h/24h regime, Top Trader Whale Position L/S Ratio vs Retail Account L/S Ratio,
     and Futures Taker Buy/Sell Volume Ratio) from fapi.binance.com.
  2. Spot Order-Flow & Whale Depth (Spot Cumulative Volume Delta / Taker Buy % across
     15m/1h/4h/1d klines, plus live Order-Book Bid/Ask Depth Imbalance within +-1% and
     +-2% and largest Bid Support / Ask Supply Walls) from api.binance.com.
  3. Multi-Timeframe Trend & Momentum (15m, 1h, 4h, 1d EMA20/EMA50/SMA200, RSI-14,
     MACD histogram, Bollinger Band position, and ATR volatility).
  4. Market Hierarchy & Capital Rotation (24h and 7d Relative Strength Alpha & Beta
     against BTCUSDT).
  5. News & Macro Catalyst Sentiment (Yahoo Finance Crypto RSS for the token + BTC macro
     headlines scored for burns, upgrades, ETF flows, Fed/CPI/BOJ, unlocks, and risk).
  6. Probabilistic Price Forecast (Composite Score -100 to +100 + 5,000-path drift-adjusted
     Monte Carlo simulation for Next 4 Hours, Next 24 Hours, and Next 7 Days).

Usage:
    python3 trader-analyst/predict.py BNB
    python3 trader-analyst/predict.py BNB BTC ETH SOL --write
    python3 trader-analyst/predict.py BNB --json
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime
import email.utils
import gzip
import json
import math
import os
import random
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
try:
    import margin
except ImportError:
    margin = None

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
SPOT_HOSTS = ("https://api.binance.com", "https://data-api.binance.vision")
FUT_HOSTS = ("https://fapi.binance.com",)
YAHOO_RSS = "https://feeds.finance.yahoo.com/rss/2.0/headline?s=%s&region=US&lang=en-US"

# Headline patterns. Generic names ("ETF", "JPMorgan", "launch", "unlock") are NOT
# directional on their own: "ETF outflows", "Unlock BNB Rewards" and "Fed enforcement
# action against a card issuer" used to score as crypto catalysts. Only flow, direction
# and crypto-specific supply words count now.
BULL_PATTERNS = [
    ("burn/buyback", r"\btoken burn[s]?\b|\bburn(?:s|ed)? [\d.,]+\b|\bbuyback[s]?\b|\bdeflation"),
    ("ETF/institutional inflow", r"\binflow[s]?\b|\bETF[s]? (?:approv|launch|debut|demand)|\bapprov(?:es|ed|al)\b.*\bETF\b|\badd(?:s|ed)? [\d,.]+ (?:BTC|ETH|SOL|bitcoin|ether)\b|\bbought [\d,.]+ (?:BTC|ETH|SOL|bitcoin|ether)\b|\bcustody\b|\btokeniz"),
    ("Binance listing/launch", r"\bBinance Will List\b|\bBinance Will Add\b|\bBinance Futures Will Launch\b|\bLaunchpool\b|\bMegadrop\b|\bHODLer Airdrop\b|\bNew Cryptocurrency Listing\b"),
    ("upgrade/partnership", r"\bupgrade\b|\bmainnet\b|\bhard fork\b|\bpartnership\b|\bintegration\b"),
    ("whale/CT accumulation", r"\baccumulat(?:e|es|ed|ing|ion)\b|\bwhale[s]? bought\b|\bwithdrew\b|\bLookonchain\b.*\bbought\b|\bfresh wallet\b"),
    ("bullish/breakout", r"\bbreakout\b|\brall(?:y|ies|ied)\b|\bsurge[sd]?\b|\bbullish\b|\bbull market\b|\ball-time high\b|\bATH\b|\bsoar(?:s|ed)?\b|\brebound[s]?\b"),
    ("Fed/macro tailwind", r"\brate cut[s]?\b|\bdovish\b|\bstimulus\b|\bQE\b|\bliquidity injection\b|\bregulatory clarity\b"),
]
BEAR_PATTERNS = [
    ("unlock/dilution", r"\btoken unlock[s]?\b|\bcliff unlock\b|\bunlock(?:s|ed)? (?:of |worth )?\$?[\d.,]+\s?(?:[mkb]|million|billion)?\b|\bvesting\b|\bdilution\b|\bdump(?:s|ed|ing)?\b|\bsell-?off\b"),
    ("whale deposit/dump", r"\bdeposited .* to Binance\b|\bwhale[s]? (?:sold|dump)|\btransferred .* to exchange\b"),
    ("hack/exploit", r"\bhack(?:ed|s|ers)?\b|\bexploit(?:ed)?\b|\bbreach\b|\bdrain(?:ed)?\b|\boutage\b|\bcyberattack\b"),
    ("regulatory/delist", r"\blawsuit\b|\bsued\b|\bSEC charges\b|\bprobe\b|\bdelist(?:s|ed|ing)?\b|\bban(?:s|ned)?\b|\bcrackdown\b"),
    ("outflow/liquidation", r"\boutflow[s]?\b|\bliquidat(?:ed|ion|ions)\b|\bcrash(?:es|ed)?\b|\bplunge[sd]?\b|\bbearish\b|\bslide[s]?\b|\btumble[sd]?\b|\bslump[s]?\b|\brisk[- ]off\b"),
    ("treasury buying pause", r"\bpause[sd]? (?:its )?(?:bitcoin |BTC )?(?:purchases|buying|buys)\b|\bskip(?:s|ped)? (?:bitcoin|BTC) (?:buys|purchases)\b|\bno (?:bitcoin|BTC) purchases\b"),
    ("Fed/macro headwind", r"\brate hike[s]?\b|\bhawkish\b|\btariff[s]?\b|\bhot inflation\b|\brecession\b|\bhigher for longer\b"),
]
# Scheduled volatility: not bullish or bearish, but a margin trader de-levers into it.
EVENT_PATTERN = r"\bFOMC\b|\bFed(?:eral Reserve)? (?:meeting|decision|policy meeting)\b|\bPowell\b|\bCPI\b|\bpayrolls\b|\bjobs report\b|\boptions expir"


class ForecastError(Exception):
    pass


def to_binance(sym):
    s = re.sub(r"[\s/\-_]+", "", str(sym or "").upper())
    if s.endswith(".USD"):
        s = s[:-4] + "USDT"
    elif s.endswith("USD") and not s.endswith(("USDT", "FDUSD", "BUSD", "TUSD", "PYUSD", "RLUSD", "XUSD", "BFUSD")):
        s = s[:-3] + "USDT"
    elif not s.endswith(("USDT", "USDC", "FDUSD", "BUSD")) and not (len(s) > 4 and s.endswith(("BTC", "ETH", "BNB"))):
        s = s + "USDT"
    return s


def base_asset(sym):
    s = to_binance(sym)
    for q in ("USDT", "USDC", "FDUSD", "BUSD"):
        if s.endswith(q) and len(s) > len(q):
            s = s[:-len(q)]
            break
    if s.startswith("1000") and len(s) > 4:
        s = s[4:]
    return s


def fmt_p(x):
    if x is None:
        return "-"
    f = float(x)
    if 0 < abs(f) < 0.0001:
        return "$" + ("%.8f" % f).rstrip("0").rstrip(".")
    if 0 < abs(f) < 0.01:
        return "$" + ("%.6f" % f).rstrip("0").rstrip(".")
    if 0 < abs(f) < 1.0:
        return "$" + ("%.4f" % f).rstrip("0").rstrip(".")
    return "$" + "{:,.2f}".format(f)


def fmt_usd(x, signed=False):
    if x is None:
        return "-"
    f = float(x)
    sign = ("+" if f > 0 else ("-" if f < 0 else "")) if signed else ("-" if f < 0 else "")
    a = abs(f)
    if a >= 1e12:
        return "%s$%.2fT" % (sign, a / 1e12)
    if a >= 1e9:
        return "%s$%.2fB" % (sign, a / 1e9)
    if a >= 1e6:
        return "%s$%.2fM" % (sign, a / 1e6)
    if a >= 1e3:
        return "%s$%.1fK" % (sign, a / 1e3)
    return "%s$%.0f" % (sign, a)


def _http_get(url, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip" or data[:2] == b"\x1f\x8b":
            try:
                data = gzip.decompress(data)
            except Exception:
                pass
        return data.decode("utf-8", "replace")


def spot_get(path):
    last = "no answer"
    for host in SPOT_HOSTS:
        try:
            return json.loads(_http_get(host + path))
        except Exception as e:
            last = str(e)
    raise ForecastError("Binance Spot did not answer for %s (%s)" % (path.split("?")[0], last))


def fut_get(path):
    for host in FUT_HOSTS:
        try:
            return json.loads(_http_get(host + path))
        except Exception:
            return None
    return None


# ---------------------------------------------------------------- technicals

def ema_series(vals, span):
    if not vals:
        return []
    k = 2.0 / (span + 1.0)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(v * k + out[-1] * (1.0 - k))
    return out


def rsi_14(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g = sum(gains) / period
    avg_l = sum(losses) / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * (period - 1) + max(d, 0.0)) / period
        avg_l = (avg_l * (period - 1) + max(-d, 0.0)) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100.0 - (100.0 / (1.0 + rs))


def macd_hist(closes):
    if len(closes) < 35:
        return 0.0, 0.0
    e12 = ema_series(closes, 12)
    e26 = ema_series(closes, 26)
    line = [a - b for a, b in zip(e12, e26)]
    sig = ema_series(line, 9)
    hist = [l - s for l, s in zip(line, sig)]
    return hist[-1], hist[-1] - hist[-2]


def atr_14(klines, period=14):
    if len(klines) < 2:
        return 0.0
    trs = []
    prev_c = float(klines[0][4])
    for k in klines[1:]:
        h, l, c = float(k[2]), float(k[3]), float(k[4])
        trs.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
        prev_c = c
    use = trs[-period:]
    return sum(use) / len(use) if use else 0.0


def parse_klines_flow(klines, n_bars=None):
    """Extract OHLC, total quote volume, taker buy quote volume, and net CVD."""
    sub = klines[-n_bars:] if n_bars else klines
    total_q = 0.0
    buy_q = 0.0
    for k in sub:
        q_vol = float(k[7])
        tb_q = float(k[10])
        total_q += q_vol
        buy_q += tb_q
    sell_q = max(0.0, total_q - buy_q)
    cvd = buy_q - sell_q
    buy_pct = (100.0 * buy_q / total_q) if total_q > 0 else 50.0
    return {"total_usd": total_q, "buy_usd": buy_q, "sell_usd": sell_q, "cvd_usd": cvd, "buy_pct": round(buy_pct, 2)}


def compute_vwap(klines, n_bars=None):
    """Volume-Weighted Average Price (Institutional Execution Benchmark)."""
    sub = klines[-n_bars:] if n_bars else klines
    num = 0.0
    den = 0.0
    for k in sub:
        tp = (float(k[2]) + float(k[3]) + float(k[4])) / 3.0
        qv = float(k[7])
        num += tp * qv
        den += qv
    return (num / den) if den > 0 else float(sub[-1][4])


def compute_volume_profile(klines, price, n_bars=168, bins=40):
    """Market Profile / Volume Profile (Dalton & Villahermosa Wyckoff 2.0):
    Computes POC (Point of Control), VAH (70% Value Area High), and VAL (70% Value Area Low)."""
    sub = klines[-n_bars:] if len(klines) >= n_bars else klines
    low_min = min(float(k[3]) for k in sub)
    high_max = max(float(k[2]) for k in sub)
    if high_max <= low_min:
        return {"poc": price, "vah": price, "val": price, "vwap_24h": price, "vwap_7d": price, "state": "BALANCED"}
    step = (high_max - low_min) / bins
    hist = [0.0] * bins
    for k in sub:
        tp = (float(k[2]) + float(k[3]) + float(k[4])) / 3.0
        qv = float(k[7])
        idx = min(bins - 1, max(0, int((tp - low_min) / step)))
        hist[idx] += qv
    poc_idx = max(range(bins), key=lambda i: hist[i])
    poc_price = low_min + (poc_idx + 0.5) * step
    total_v = sum(hist)
    target_v = 0.70 * total_v
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
    val_price = low_min + lo_i * step
    vah_price = low_min + (hi_i + 1) * step
    vwap_24h = compute_vwap(klines, 24)
    vwap_7d = compute_vwap(klines, 168)
    if price > vah_price:
        state = "INITIATIVE BREAKOUT ABOVE VALUE AREA (> VAH)"
    elif price < val_price:
        state = "BELOW VALUE AREA (< VAL: Discount / Breakdown Zone)"
    elif price >= poc_price:
        state = "UPPER VALUE AREA (Between POC & VAH: Bullish Rotational)"
    else:
        state = "LOWER VALUE AREA (Between VAL & POC: Mean-Reverting toward POC)"
    return {
        "poc": round(poc_price, 8),
        "vah": round(vah_price, 8),
        "val": round(val_price, 8),
        "vwap_24h": round(vwap_24h, 8),
        "vwap_7d": round(vwap_7d, 8),
        "vwap_24h_dist_pct": round((price / vwap_24h - 1.0) * 100.0, 2) if vwap_24h else 0.0,
        "state": state,
    }


def detect_institutional_signals(klines_1h, klines_4h, price):
    """Detect CVD Absorption/Exhaustion Divergence, Wyckoff Springs/Upthrusts (Liquidity Sweeps),
    and Bollinger-Keltner Volatility Squeezes."""
    signals = []
    bonus_score = 0.0

    # 1. CVD Divergence over last 16 hours (first 8h vs last 8h)
    if len(klines_1h) >= 16:
        w1 = klines_1h[-16:-8]
        w2 = klines_1h[-8:]
        p1_hi, p2_hi = max(float(k[2]) for k in w1), max(float(k[2]) for k in w2)
        p1_lo, p2_lo = min(float(k[3]) for k in w1), min(float(k[3]) for k in w2)
        f1 = parse_klines_flow(w1)
        f2 = parse_klines_flow(w2)
        if p2_hi > p1_hi * 1.001 and f2["buy_pct"] < 46.5 and f2["buy_pct"] < f1["buy_pct"] - 2.0:
            signals.append("BEARISH CVD EXHAUSTION DIVERGENCE: Price made a higher high over 8h while Spot Taker Buy dropped to %.1f%% (Passive whales distributing into retail buyers)" % f2["buy_pct"])
            bonus_score -= 4.0
        elif p2_lo < p1_lo * 0.999 and f2["buy_pct"] > 53.5 and f2["buy_pct"] > f1["buy_pct"] + 2.0:
            signals.append("BULLISH CVD ABSORPTION DIVERGENCE: Price wicked lower while Spot Taker Buy rose to %.1f%% (Whale absorption of panic sellers)" % f2["buy_pct"])
            bonus_score += 4.0

    # 2. Wyckoff Spring / Upthrust (Liquidity Sweep / SFP on 1h or 4h)
    if len(klines_1h) >= 24:
        prior_lo = min(float(k[3]) for k in klines_1h[-24:-3])
        prior_hi = max(float(k[2]) for k in klines_1h[-24:-3])
        recent_lo = min(float(k[3]) for k in klines_1h[-3:])
        recent_hi = max(float(k[2]) for k in klines_1h[-3:])
        if recent_lo < prior_lo and price > prior_lo:
            signals.append("WYCKOFF SPRING / BULLISH LIQUIDITY SWEEP: Swept 24h swing low (%s) to hunt stops and reclaimed above it" % fmt_p(prior_lo))
            bonus_score += 5.0
        elif recent_hi > prior_hi and price < prior_hi:
            signals.append("WYCKOFF UPTHRUST (SFP) / BEARISH STOP-HUNT: Swept 24h swing high (%s) and rejected back inside range" % fmt_p(prior_hi))
            bonus_score -= 5.0

    # 3. Bollinger Band / Keltner Channel Volatility Squeeze (4h)
    squeeze_state = "NORMAL VOLATILITY"
    bb_width_pct = 0.0
    if len(klines_4h) >= 20:
        closes_4h = [float(k[4]) for k in klines_4h[-20:]]
        mean_20 = statistics.mean(closes_4h)
        sd_20 = statistics.pstdev(closes_4h)
        atr_4h = atr_14(klines_4h, 20)
        bb_upper, bb_lower = mean_20 + 2.0 * sd_20, mean_20 - 2.0 * sd_20
        kc_upper, kc_lower = mean_20 + 1.5 * atr_4h, mean_20 - 1.5 * atr_4h
        bb_width_pct = round((bb_upper - bb_lower) / mean_20 * 100.0, 2) if mean_20 > 0 else 0.0
        if bb_upper <= kc_upper and bb_lower >= kc_lower:
            squeeze_state = "TTM SQUEEZE ACTIVE (Bollinger inside Keltner - Explosive Breakout Coiling)"
            signals.append("4H VOLATILITY SQUEEZE ACTIVE (BB Width %.2f%% inside Keltner): Coiling for large directional expansion" % bb_width_pct)
        elif bb_width_pct < 3.5:
            squeeze_state = "TIGHT COMPRESSION (BB Width %.2f%% - Breakout Imminent)" % bb_width_pct
        elif bb_width_pct > 12.0:
            squeeze_state = "HIGH VOLATILITY EXPANSION (BB Width %.2f%% - Mean-Reversion Risk)" % bb_width_pct

    return {
        "squeeze_4h": squeeze_state,
        "bb_width_4h_pct": bb_width_pct,
        "signals": signals,
        "bonus_score": round(max(-10.0, min(10.0, bonus_score)), 1),
    }


def compute_kelly_trade_plan(price, horizons, vp, ob, tf_1h, composite):
    """Build an institutional execution plan with Quarter-Kelly (0.25x) position sizing."""
    atr = tf_1h["atr14"] or (price * 0.01)
    h24 = horizons["24h"]
    bias = "LONG" if composite >= 10 else ("SHORT / HEDGE" if composite <= -10 else "WAIT / RANGE SCALP")
    if bias == "LONG":
        # Ideal pullback entry near 24h VWAP, POC, or Bid Support Wall
        pullback = max(vp["val"], min(price, max(vp["vwap_24h"], ob["support_wall_price"] if ob else price - 0.5 * atr)))
        stop = min(price - 1.5 * atr, (ob["support_wall_price"] * 0.996) if ob and ob["support_wall_price"] < price else price - 1.5 * atr)
        risk = max(price - stop, atr * 0.5)
        tp1 = max(price + 1.5 * risk, vp["vah"])
        tp2 = price + 2.5 * risk
        win_prob = h24["prob_up_pct"] / 100.0
    elif bias == "SHORT / HEDGE":
        pullback = min(vp["vah"], max(price, min(vp["vwap_24h"], ob["resistance_wall_price"] if ob else price + 0.5 * atr)))
        stop = max(price + 1.5 * atr, (ob["resistance_wall_price"] * 1.004) if ob and ob["resistance_wall_price"] > price else price + 1.5 * atr)
        risk = max(stop - price, atr * 0.5)
        tp1 = min(price - 1.5 * risk, vp["val"])
        tp2 = price - 2.5 * risk
        win_prob = h24["prob_down_pct"] / 100.0
    else:
        pullback = vp["val"]
        stop = vp["val"] - 1.2 * atr
        risk = max(pullback - stop, atr * 0.5)
        tp1 = vp["poc"]
        tp2 = vp["vah"]
        win_prob = max(h24["prob_up_pct"], h24["prob_down_pct"]) / 100.0
        reward = abs(tp2 - pullback)
        rr = round(reward / risk, 2) if risk > 0 else 1.5
        full_kelly = max(0.0, win_prob - (1.0 - win_prob) / max(rr, 0.5))
        quarter_kelly_pct = round(min(2.5, full_kelly * 0.25 * 100.0), 2)
        ev_r = round(win_prob * rr - (1.0 - win_prob) * 1.0, 2)
        return {
            "bias": bias,
            "spot_entry": round(price, 8),
            "limit_pullback_entry": round(pullback, 8),
            "stop_loss": round(stop, 8),
            "tp1": round(tp1, 8),
            "tp2": round(tp2, 8),
            "reward_risk_ratio": rr,
            "win_prob_pct": round(win_prob * 100.0, 1),
            "expected_value_R": ev_r,
            "quarter_kelly_risk_pct": quarter_kelly_pct,
        }

    reward = abs(tp1 - price)
    rr = round(reward / risk, 2) if risk > 0 else 1.5
    # Kelly formula: f* = p - (1 - p) / b
    full_kelly = max(0.0, win_prob - (1.0 - win_prob) / max(rr, 0.5))
    quarter_kelly_pct = round(min(5.0, full_kelly * 0.25 * 100.0), 2)
    ev_r = round(win_prob * rr - (1.0 - win_prob) * 1.0, 2)
    return {
        "bias": bias,
        "spot_entry": round(price, 8),
        "limit_pullback_entry": round(pullback, 8),
        "stop_loss": round(stop, 8),
        "tp1": round(tp1, 8),
        "tp2": round(tp2, 8),
        "reward_risk_ratio": rr,
        "win_prob_pct": round(win_prob * 100.0, 1),
        "expected_value_R": ev_r,
        "quarter_kelly_risk_pct": quarter_kelly_pct,
    }


def analyze_timeframe(klines, label):
    """Analyze one timeframe's closed bars + current forming bar."""
    closes = [float(k[4]) for k in klines]
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    price = closes[-1]
    e20 = ema_series(closes, 20)[-1] if len(closes) >= 20 else price
    e50 = ema_series(closes, 50)[-1] if len(closes) >= 50 else price
    s200 = (sum(closes[-200:]) / 200.0) if len(closes) >= 200 else (sum(closes) / len(closes))
    r = rsi_14(closes)
    mh, mh_delta = macd_hist(closes)
    a14 = atr_14(klines)

    # Trend score for this timeframe (-25 to +25)
    score = 0.0
    if price > e20:
        score += 6.0
    else:
        score -= 6.0
    if price > e50:
        score += 6.0
    else:
        score -= 6.0
    if e20 > e50:
        score += 5.0
    else:
        score -= 5.0
    if price > s200:
        score += 4.0
    else:
        score -= 4.0
    if mh > 0 and mh_delta > 0:
        score += 4.0
    elif mh > 0:
        score += 1.5
    elif mh < 0 and mh_delta < 0:
        score -= 4.0
    else:
        score -= 1.5

    # RSI adjustment (momentum vs extreme exhaustion)
    if 52 <= r <= 68:
        score += 2.0
    elif r > 78:
        score -= 3.0  # overbought pullback risk
    elif 32 <= r <= 48:
        score -= 2.0
    elif r < 25:
        score += 3.0  # oversold bounce candidate

    regime = "BULLISH" if score >= 8 else ("BEARISH" if score <= -8 else "NEUTRAL")
    return {
        "timeframe": label,
        "price": price,
        "ema20": round(e20, 8),
        "ema50": round(e50, 8),
        "sma200": round(s200, 8),
        "rsi14": round(r, 1),
        "macd_hist": round(mh, 8),
        "macd_rising": mh_delta > 0,
        "atr14": round(a14, 8),
        "atr_pct": round(100.0 * a14 / price, 2) if price else 0.0,
        "high_20": max(highs[-20:]),
        "low_20": min(lows[-20:]),
        "regime": regime,
        "score": round(max(-25.0, min(25.0, score)), 1),
    }


# ---------------------------------------------------------------- orderbook & futures

def analyze_orderbook(depth_doc, price):
    if not isinstance(depth_doc, dict) or not depth_doc.get("bids") or not depth_doc.get("asks"):
        return None
    bids = [(float(p), float(q)) for p, q in depth_doc["bids"]]
    asks = [(float(p), float(q)) for p, q in depth_doc["asks"]]
    bid_1pct = sum(p * q for p, q in bids if p >= price * 0.99)
    ask_1pct = sum(p * q for p, q in asks if p <= price * 1.01)
    bid_2pct = sum(p * q for p, q in bids if p >= price * 0.98)
    ask_2pct = sum(p * q for p, q in asks if p <= price * 1.02)

    tot_1 = bid_1pct + ask_1pct
    imb_1pct = ((bid_1pct - ask_1pct) / tot_1 * 100.0) if tot_1 > 0 else 0.0
    tot_2 = bid_2pct + ask_2pct
    imb_2pct = ((bid_2pct - ask_2pct) / tot_2 * 100.0) if tot_2 > 0 else 0.0

    near_bids = [x for x in bids if x[0] >= price * 0.97] or bids
    near_asks = [x for x in asks if x[0] <= price * 1.03] or asks
    top_bid = max(near_bids, key=lambda x: x[0] * x[1])
    top_ask = max(near_asks, key=lambda x: x[0] * x[1])
    return {
        "bid_1pct_usd": round(bid_1pct, 2),
        "ask_1pct_usd": round(ask_1pct, 2),
        "imbalance_1pct": round(imb_1pct, 1),
        "bid_2pct_usd": round(bid_2pct, 2),
        "ask_2pct_usd": round(ask_2pct, 2),
        "imbalance_2pct": round(imb_2pct, 1),
        "support_wall_price": top_bid[0],
        "support_wall_usd": round(top_bid[0] * top_bid[1], 2),
        "resistance_wall_price": top_ask[0],
        "resistance_wall_usd": round(top_ask[0] * top_ask[1], 2),
    }


def _json_or_none(url, timeout=8):
    try:
        return json.loads(_http_get(url, timeout=timeout))
    except Exception:
        return None


def analyze_futures(bsym, spot_price, klines_1h):
    """Fetch and interpret perpetual futures positioning across Binance USD-M, Binance Coin-M, Bybit V5, and OKX."""
    base = base_asset(bsym)
    fsym = bsym
    prem = fut_get("/fapi/v1/premiumIndex?symbol=%s" % fsym)
    if not isinstance(prem, dict) or prem.get("lastFundingRate") is None:
        alt = "1000" + bsym if not bsym.startswith("1000") else bsym[4:]
        prem = fut_get("/fapi/v1/premiumIndex?symbol=%s" % alt)
        if isinstance(prem, dict) and prem.get("lastFundingRate") is not None:
            fsym = alt
        else:
            return {"available": False, "note": "No perpetual futures contract on Binance for %s" % bsym}

    okx_inst = "%s-USDT-SWAP" % base
    coinm_sym = "%sUSD_PERP" % base
    with ThreadPoolExecutor(max_workers=8) as ex:
        f_oi = ex.submit(fut_get, "/futures/data/openInterestHist?symbol=%s&period=1h&limit=25" % fsym)
        f_top = ex.submit(fut_get, "/futures/data/topLongShortPositionRatio?symbol=%s&period=1h&limit=6" % fsym)
        f_glob = ex.submit(fut_get, "/futures/data/globalLongShortAccountRatio?symbol=%s&period=1h&limit=6" % fsym)
        f_tak = ex.submit(fut_get, "/futures/data/takerlongshortRatio?symbol=%s&period=1h&limit=24" % fsym)
        f_bybit = ex.submit(_json_or_none, "https://api.bybit.com/v5/market/tickers?category=linear&symbol=%s" % fsym)
        f_okx_f = ex.submit(_json_or_none, "https://www.okx.com/api/v5/public/funding-rate?instId=%s" % okx_inst)
        f_okx_oi = ex.submit(_json_or_none, "https://www.okx.com/api/v5/public/open-interest?instType=SWAP&instId=%s" % okx_inst)
        f_coinm = ex.submit(_json_or_none, "https://dapi.binance.com/dapi/v1/premiumIndex?symbol=%s" % coinm_sym)

        oi_hist = f_oi.result() or []
        top_ls = f_top.result() or []
        glob_ls = f_glob.result() or []
        taker_ls = f_tak.result() or []
        bybit_doc = f_bybit.result()
        okx_f_doc = f_okx_f.result()
        okx_oi_doc = f_okx_oi.result()
        coinm_doc = f_coinm.result()

    funding_pct = float(prem["lastFundingRate"]) * 100.0
    mark_p = float(prem.get("markPrice") or spot_price)
    index_p = float(prem.get("indexPrice") or spot_price)
    basis_pct = ((mark_p - index_p) / index_p * 100.0) if index_p > 0 else 0.0

    # Parse Bybit Linear Perp
    bybit_funding_pct = None
    bybit_oi_usd = 0.0
    try:
        bb_item = ((bybit_doc or {}).get("result") or {}).get("list", [])[0]
        if bb_item.get("fundingRate"):
            bybit_funding_pct = round(float(bb_item["fundingRate"]) * 100.0, 4)
        if bb_item.get("openInterestValue"):
            bybit_oi_usd = float(bb_item["openInterestValue"])
    except Exception:
        pass

    # Parse OKX Swap
    okx_funding_pct = None
    okx_oi_usd = 0.0
    try:
        ok_f_item = ((okx_f_doc or {}).get("data") or [])[0]
        if ok_f_item.get("fundingRate"):
            okx_funding_pct = round(float(ok_f_item["fundingRate"]) * 100.0, 4)
    except Exception:
        pass
    try:
        ok_oi_item = ((okx_oi_doc or {}).get("data") or [])[0]
        if ok_oi_item.get("oiUsd"):
            okx_oi_usd = float(ok_oi_item["oiUsd"])
    except Exception:
        pass

    # Parse Binance Coin-M Inverse Perp (whale/miner crypto-margined hedge)
    coinm_funding_pct = None
    coinm_basis_pct = None
    try:
        cm_item = coinm_doc[0] if isinstance(coinm_doc, list) and coinm_doc else None
        if cm_item and cm_item.get("lastFundingRate") is not None:
            coinm_funding_pct = round(float(cm_item["lastFundingRate"]) * 100.0, 4)
            cm_mark = float(cm_item.get("markPrice") or spot_price)
            cm_idx = float(cm_item.get("indexPrice") or spot_price)
            if cm_idx > 0:
                coinm_basis_pct = round((cm_mark - cm_idx) / cm_idx * 100.0, 4)
    except Exception:
        pass

    oi_usd = float(oi_hist[-1]["sumOpenInterestValue"]) if oi_hist else 0.0
    global_oi_usd = oi_usd + bybit_oi_usd + okx_oi_usd
    oi_1h_chg = ((float(oi_hist[-1]["sumOpenInterest"]) / float(oi_hist[-2]["sumOpenInterest"]) - 1.0) * 100.0) if len(oi_hist) >= 2 else 0.0
    oi_4h_chg = ((float(oi_hist[-1]["sumOpenInterest"]) / float(oi_hist[-5]["sumOpenInterest"]) - 1.0) * 100.0) if len(oi_hist) >= 5 else 0.0
    oi_24h_chg = ((float(oi_hist[-1]["sumOpenInterest"]) / float(oi_hist[0]["sumOpenInterest"]) - 1.0) * 100.0) if len(oi_hist) >= 12 else 0.0

    p_4h_chg = ((float(klines_1h[-1][4]) / float(klines_1h[-5][4]) - 1.0) * 100.0) if len(klines_1h) >= 5 else 0.0
    if p_4h_chg >= 0.15 and oi_4h_chg >= 0.5:
        oi_regime = "LONG BUILD-UP (Price up + OI up -> fresh leveraged longs entering, bullish)"
    elif p_4h_chg >= 0.15 and oi_4h_chg < -0.5:
        oi_regime = "SHORT COVERING (Price up + OI down -> shorts closing / profit taking)"
    elif p_4h_chg <= -0.15 and oi_4h_chg >= 0.5:
        oi_regime = "SHORT BUILD-UP (Price down + OI up -> aggressive shorts entering)"
    elif p_4h_chg <= -0.15 and oi_4h_chg < -0.5:
        oi_regime = "LONG LIQUIDATION / FLUSH (Price down + OI down -> leveraged longs washed out)"
    else:
        oi_regime = "CONSOLIDATION (OI & price steady over 4h)"

    whale_long_pct = float(top_ls[-1]["longAccount"]) * 100.0 if top_ls else 50.0
    whale_ratio = float(top_ls[-1]["longShortRatio"]) if top_ls else 1.0
    retail_long_pct = float(glob_ls[-1]["longAccount"]) * 100.0 if glob_ls else 50.0
    retail_ratio = float(glob_ls[-1]["longShortRatio"]) if glob_ls else 1.0

    taker_1h = float(taker_ls[-1]["buySellRatio"]) if taker_ls else 1.0
    taker_4h = statistics.mean(float(x["buySellRatio"]) for x in taker_ls[-4:]) if len(taker_ls) >= 4 else taker_1h
    taker_24h = statistics.mean(float(x["buySellRatio"]) for x in taker_ls) if taker_ls else 1.0

    # Cross-exchange funding consensus
    fund_rates = [r for r in (funding_pct, bybit_funding_pct, okx_funding_pct) if r is not None]
    consensus_funding_pct = statistics.mean(fund_rates) if fund_rates else funding_pct

    # Score derivatives (-25 to +25)
    score = 0.0
    notes = []
    # 1. Funding & squeeze dynamics
    if consensus_funding_pct < -0.015:
        score += 6.0
        notes.append("Cross-exchange negative funding (%+.4f%% avg): shorts paying longs across venues (short-squeeze fuel)" % consensus_funding_pct)
    elif consensus_funding_pct > 0.045:
        score -= 6.0
        notes.append("Crowded positive funding (%+.4f%% avg): overleveraged longs vulnerable to a flush" % consensus_funding_pct)
    elif 0.001 <= consensus_funding_pct <= 0.025:
        score += 2.0
        notes.append("Healthy neutral-positive cross-venue funding (%+.4f%% avg): sustainable carry" % consensus_funding_pct)

    if coinm_funding_pct is not None and coinm_funding_pct < -0.01 and funding_pct > 0:
        notes.append("Coin-M inverse funding is negative (%+.4f%%) while USDT-M is positive: crypto-native whales/miners hedging spot inventory" % coinm_funding_pct)
        score -= 2.0

    # 2. Price x OI regime
    if "LONG BUILD-UP" in oi_regime:
        score += 6.0
    elif "SHORT BUILD-UP" in oi_regime:
        score -= 5.0
    elif "SHORT COVERING" in oi_regime:
        score += 2.0
    elif "LONG LIQUIDATION" in oi_regime:
        score -= 2.0

    # 3. Whale vs Retail divergence
    whale_vs_retail = whale_long_pct - retail_long_pct
    if whale_vs_retail > 5.0:
        score += 5.0
        notes.append("Smart-money bullish divergence: Top traders are %.1f%% long vs %.1f%% retail" % (whale_long_pct, retail_long_pct))
    elif whale_vs_retail < -12.0 and retail_long_pct > 65.0:
        score -= 4.0
        notes.append("Retail long crowding: %.1f%% of retail accounts are long while top whales are only %.1f%% long" % (retail_long_pct, whale_long_pct))

    # 4. Futures Taker Buy/Sell Ratio
    if taker_4h > 1.04:
        score += 6.0
        notes.append("Aggressive futures market buying (4h taker buy/sell ratio %.2f)" % taker_4h)
    elif taker_4h < 0.96:
        score -= 6.0
        notes.append("Aggressive futures market selling (4h taker buy/sell ratio %.2f)" % taker_4h)

    return {
        "available": True,
        "symbol": fsym,
        "funding_rate_pct": round(funding_pct, 4),
        "bybit_funding_pct": bybit_funding_pct,
        "okx_funding_pct": okx_funding_pct,
        "coinm_funding_pct": coinm_funding_pct,
        "coinm_basis_pct": coinm_basis_pct,
        "consensus_funding_pct": round(consensus_funding_pct, 4),
        "annualized_funding_pct": round(consensus_funding_pct * 3 * 365, 2),
        "basis_pct": round(basis_pct, 4),
        "open_interest_usd": round(oi_usd, 2),
        "bybit_oi_usd": round(bybit_oi_usd, 2),
        "okx_oi_usd": round(okx_oi_usd, 2),
        "global_oi_usd": round(global_oi_usd, 2),
        "oi_change_1h_pct": round(oi_1h_chg, 2),
        "oi_change_4h_pct": round(oi_4h_chg, 2),
        "oi_change_24h_pct": round(oi_24h_chg, 2),
        "oi_regime": oi_regime,
        "whale_long_pct": round(whale_long_pct, 1),
        "whale_ls_ratio": round(whale_ratio, 2),
        "retail_long_pct": round(retail_long_pct, 1),
        "retail_ls_ratio": round(retail_ratio, 2),
        "taker_buy_sell_1h": round(taker_1h, 3),
        "taker_buy_sell_4h": round(taker_4h, 3),
        "taker_buy_sell_24h": round(taker_24h, 3),
        "score": round(max(-25.0, min(25.0, score)), 1),
        "notes": notes,
    }


# ---------------------------------------------------------------- on-chain, macro & multi-source news

CHAIN_MAP = {
    "BNB": "BSC", "ETH": "Ethereum", "SOL": "Solana", "TRX": "Tron", "ARB": "Arbitrum",
    "OP": "Optimism", "SUI": "Sui", "AVAX": "Avalanche", "APT": "Aptos", "NEAR": "Near",
    "MATIC": "Polygon", "POL": "Polygon", "TON": "TON", "ADA": "Cardano", "INJ": "Injective",
    "SEI": "Sei", "DOT": "Polkadot", "ATOM": "CosmosHub", "HBAR": "Hedera", "FTM": "Fantom",
    "S": "Sonic", "ALGO": "Algorand", "ICP": "ICP", "FIL": "Filecoin", "XLM": "Stellar",
}
_MACRO_CACHE = {}


def fetch_global_macro_and_onchain(sym):
    """Fetch reputable macro & on-chain data from DefiLlama (TVL + Stablecoins), CoinGecko (BTC.D + Total Mcap),
    and Alternative.me (Fear & Greed Index)."""
    if "global" not in _MACRO_CACHE:
        with ThreadPoolExecutor(max_workers=4) as ex:
            f_fng = ex.submit(_json_or_none, "https://api.alternative.me/fng/?limit=2", 8)
            f_cg = ex.submit(_json_or_none, "https://api.coingecko.com/api/v3/global", 8)
            f_ch = ex.submit(_json_or_none, "https://api.llama.fi/v2/chains", 10)
            f_st = ex.submit(_json_or_none, "https://stablecoins.llama.fi/stablecoins?includePrices=false", 10)
            _MACRO_CACHE["global"] = (f_fng.result(), f_cg.result(), f_ch.result(), f_st.result())

    fng_doc, cg_doc, chains_doc, stables_doc = _MACRO_CACHE["global"]
    base = base_asset(sym)

    # 1. Fear & Greed Index
    fng_val, fng_lbl, fng_prev = 50, "Neutral", 50
    try:
        fd = (fng_doc or {}).get("data") or []
        if fd:
            fng_val = int(fd[0].get("value", 50))
            fng_lbl = fd[0].get("value_classification", "Neutral")
        if len(fd) > 1:
            fng_prev = int(fd[1].get("value", fng_val))
    except Exception:
        pass

    # 2. CoinGecko Global Market Cap & Dominance
    btc_dom, eth_dom, total_mcap_usd, mcap_24h_chg = 0.0, 0.0, 0.0, 0.0
    try:
        cgd = (cg_doc or {}).get("data") or {}
        mcp = cgd.get("market_cap_percentage") or {}
        btc_dom = round(float(mcp.get("btc") or 0.0), 2)
        eth_dom = round(float(mcp.get("eth") or 0.0), 2)
        total_mcap_usd = float((cgd.get("total_market_cap") or {}).get("usd") or 0.0)
        mcap_24h_chg = round(float(cgd.get("market_cap_change_percentage_24h_usd") or 0.0), 2)
    except Exception:
        pass

    # 3. DefiLlama Global Stablecoin Liquidity (USDT + USDC + Top Stablecoins)
    stable_total_usd = 0.0
    stable_24h_flow_usd = 0.0
    stable_7d_flow_usd = 0.0
    usdt_usd = 0.0
    usdc_usd = 0.0
    try:
        pegs = (stables_doc or {}).get("peggedAssets") or []
        for p in pegs[:15]:
            curr = float((p.get("circulating") or {}).get("peggedUSD") or 0.0)
            p_day = float((p.get("circulatingPrevDay") or {}).get("peggedUSD") or curr)
            p_wk = float((p.get("circulatingPrevWeek") or {}).get("peggedUSD") or curr)
            stable_total_usd += curr
            stable_24h_flow_usd += (curr - p_day)
            stable_7d_flow_usd += (curr - p_wk)
            if p.get("symbol") == "USDT":
                usdt_usd = curr
            elif p.get("symbol") == "USDC":
                usdc_usd = curr
    except Exception:
        pass

    # 4. DefiLlama Chain TVL for this token
    chain_name = CHAIN_MAP.get(base)
    chain_tvl_usd = None
    chain_rank = None
    try:
        if isinstance(chains_doc, list) and chains_doc:
            valid_chains = [c for c in chains_doc if float(c.get("tvl") or 0.0) > 0]
            valid_chains.sort(key=lambda c: -float(c.get("tvl") or 0.0))
            for idx, c in enumerate(valid_chains, 1):
                if (chain_name and c.get("name", "").lower() == chain_name.lower()) or (c.get("tokenSymbol") or "").upper() == base:
                    chain_name = c.get("name")
                    chain_tvl_usd = round(float(c.get("tvl") or 0.0), 2)
                    chain_rank = idx
                    break
    except Exception:
        pass

    # Macro & On-Chain Regime Score (-5 to +5 bonus)
    macro_bonus = 0.0
    macro_notes = []
    if stable_7d_flow_usd > 5e8:
        macro_bonus += 2.0
        macro_notes.append("DefiLlama Stablecoin Inflow: +%s minted over 7d (expanding on-chain buying power)" % fmt_usd(stable_7d_flow_usd))
    elif stable_7d_flow_usd < -5e8:
        macro_bonus -= 2.0
        macro_notes.append("DefiLlama Stablecoin Contraction: %s redeemed over 7d (tightening on-chain liquidity)" % fmt_usd(stable_7d_flow_usd, signed=True))

    if fng_val <= 25:
        macro_bonus += 2.0
        macro_notes.append("Extreme Fear (%d/100): Institutional contrarian accumulation zone" % fng_val)
    elif fng_val >= 78:
        macro_bonus -= 2.0
        macro_notes.append("Extreme Greed (%d/100): High retail euphoria / late-stage distribution risk" % fng_val)

    return {
        "fear_greed_value": fng_val,
        "fear_greed_label": fng_lbl,
        "fear_greed_prev": fng_prev,
        "total_mcap_usd": round(total_mcap_usd, 2),
        "mcap_24h_change_pct": mcap_24h_chg,
        "btc_dominance_pct": btc_dom,
        "eth_dominance_pct": eth_dom,
        "market_season": "BTC DOMINANCE SEASON (BTC.D %.1f%% > 56%% - Select High-Alpha Alts Only)" % btc_dom if btc_dom >= 56.0 else "ALTCOIN ROTATION WINDOW (BTC.D %.1f%%)" % btc_dom,
        "stablecoin_total_usd": round(stable_total_usd, 2),
        "usdt_supply_usd": round(usdt_usd, 2),
        "usdc_supply_usd": round(usdc_usd, 2),
        "stablecoin_24h_flow_usd": round(stable_24h_flow_usd, 2),
        "stablecoin_7d_flow_usd": round(stable_7d_flow_usd, 2),
        "chain_name": chain_name,
        "chain_tvl_usd": chain_tvl_usd,
        "chain_tvl_rank": chain_rank,
        "macro_bonus": round(macro_bonus, 1),
        "macro_notes": macro_notes,
    }


OMNI_RSS_FEEDS = (
    # 1. Official Federal Reserve (FOMC Press Releases & Fed Governor Speeches)
    ("Fed Press", "fed", "https://www.federalreserve.gov/feeds/press_all.xml"),
    ("Fed Speeches", "fed", "https://www.federalreserve.gov/feeds/speeches.xml"),
    # 2. Global Central Banks, Wall Street Investment Banks & SEC/Institutional Announcements
    ("Banks & Fed Flow", "bank", "https://news.google.com/rss/search?q=(%22Federal+Reserve%22+OR+FOMC+OR+Powell+OR+ECB+OR+JPMorgan+OR+BlackRock+OR+%22Goldman+Sachs%22+OR+%22Morgan+Stanley%22+OR+%22Standard+Chartered%22+OR+Citigroup+OR+Fidelity+OR+SEC)+crypto+OR+bitcoin+OR+rates+when:3d&hl=en-US&gl=US&ceid=US:en"),
    # 3. Crypto Twitter / X Whale Flow & Smart Money Trackers
    ("X/Twitter Whale Flow", "twitter", "https://news.google.com/rss/search?q=(%22Lookonchain%22+OR+%22Whale+Alert%22+OR+%22Wu+Blockchain%22+OR+%22Arkham%22+OR+%22PeckShield%22+OR+%22ZachXBT%22+OR+%22Arthur+Hayes%22+OR+%22Crypto+Twitter%22+OR+%22on+X%22)+crypto+OR+bitcoin+OR+whale+when:3d&hl=en-US&gl=US&ceid=US:en"),
    # 4. Financial & Crypto Magazines (Forbes, Fortune, Bloomberg, Reuters, Bitcoin Magazine, The Block, Blockworks, CryptoSlate, BeInCrypto)
    ("Forbes/Bloomberg/Fortune", "magazine", "https://news.google.com/rss/search?q=(site:forbes.com+OR+site:fortune.com+OR+site:bloomberg.com+OR+site:reuters.com)+crypto+OR+bitcoin+when:3d&hl=en-US&gl=US&ceid=US:en"),
    ("Bitcoin Magazine", "magazine", "https://bitcoinmagazine.com/.rss/full/"),
    ("The Block", "magazine", "https://www.theblock.co/rss.xml"),
    ("Blockworks", "magazine", "https://blockworks.co/feed"),
    ("CryptoSlate", "magazine", "https://cryptoslate.com/feed/"),
    ("BeInCrypto", "magazine", "https://beincrypto.com/feed/"),
    # 5. Core 24/7 Breaking Crypto Journalism
    ("CoinDesk", "news", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph", "news", "https://cointelegraph.com/rss"),
    ("Decrypt", "news", "https://decrypt.co/feed"),
)

BINANCE_CMS_URL = "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query?type=1&pageNo=1&pageSize=10"
COINGECKO_TRENDING_URL = "https://api.coingecko.com/api/v3/search/trending"


def _classify_headline(title):
    mb = [lbl for lbl, pat in BULL_PATTERNS if re.search(pat, title, re.I)]
    mr = [lbl for lbl, pat in BEAR_PATTERNS if re.search(pat, title, re.I)]
    if mb and not mr:
        return "BULLISH", mb
    if mr and not mb:
        return "BEARISH", mr
    if mb and mr:
        return "MIXED", mb + mr
    return "NEUTRAL", []


def headline_age_weight(pub, now_utc=None):
    """1.0 inside 24h, 0.6 inside 72h, 0.3 inside 7 days, 0 when older; 0.7 if the date is unknown."""
    if not pub:
        return 0.7
    try:
        dt = email.utils.parsedate_to_datetime(pub)
    except (TypeError, ValueError, IndexError):
        try:
            dt = datetime.datetime.fromisoformat(pub.replace("Z", "+00:00"))
        except ValueError:
            return 0.7
    if dt is None:
        return 0.7
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    now_utc = now_utc or datetime.datetime.now(datetime.timezone.utc)
    age_h = (now_utc - dt).total_seconds() / 3600.0
    if age_h <= 24:
        return 1.0
    if age_h <= 72:
        return 0.6
    if age_h <= 168:
        return 0.3
    return 0.0


def analyze_news(sym):
    """Aggregate 25+ omni-channel sources:
    - Official Federal Reserve Board Press Releases & Speeches (federalreserve.gov)
    - Global Central Banks & Wall St. Banks (JPMorgan, BlackRock, Goldman Sachs, Morgan Stanley, Standard Chartered, Citi, Fidelity, SEC)
    - Official Binance Exchange CMS Announcements (New Listings, Launchpool, Collateral, Delistings)
    - Crypto Twitter / X Flow (Lookonchain, Whale Alert, Arkham, Wu Blockchain, Arthur Hayes, token X chatter)
    - CoinGecko Global Trending Search & Social Heat
    - Financial & Crypto Magazines (Forbes, Fortune, Bloomberg, Reuters, Bitcoin Magazine, The Block, Blockworks, CryptoSlate, BeInCrypto)
    - Breaking News Desks (Yahoo Finance, CoinDesk, Cointelegraph, Decrypt)
    """
    base = base_asset(sym)
    tickers = [base + "-USD"]
    if base != "BTC":
        tickers.append("BTC-USD")
    headlines = []
    seen = set()

    def _parse_rss(xml_text, source_lbl, category="news"):
        out = []
        try:
            root = ET.fromstring(xml_text)
        except Exception:
            return out
        items = list(root.iter("item"))
        if not items:
            items = [e for e in root.iter() if e.tag.rsplit("}", 1)[-1] == "entry"]
        for item in items:
            title = ""
            pub = ""
            for c in item:
                t = c.tag.rsplit("}", 1)[-1]
                if t == "title" and not title:
                    title = (c.text or "").strip()
                elif t in ("pubDate", "published", "updated") and not pub:
                    pub = (c.text or "").strip()
            if title:
                out.append((source_lbl, category, title, pub))
        return out

    # 1. Fetch global omni-feeds once and cache in memory
    if "omni_feeds" not in _MACRO_CACHE:
        omni_items = []
        binance_cms = []
        trending_coins = []
        with ThreadPoolExecutor(max_workers=12) as ex:
            rss_futs = {ex.submit(_http_get, url, 7): (name, cat) for name, cat, url in OMNI_RSS_FEEDS}
            f_bnc = ex.submit(_json_or_none, BINANCE_CMS_URL, 7)
            f_trd = ex.submit(_json_or_none, COINGECKO_TRENDING_URL, 7)

            for f, (name, cat) in rss_futs.items():
                try:
                    omni_items.extend(_parse_rss(f.result(), name, cat)[:12])
                except Exception:
                    continue

            try:
                bdoc = f_bnc.result() or {}
                for cat_obj in (bdoc.get("data") or {}).get("catalogs") or []:
                    cat_name = cat_obj.get("catalogName") or "Binance Announcement"
                    for art in (cat_obj.get("articles") or [])[:5]:
                        t_art = (art.get("title") or "").strip()
                        if t_art:
                            binance_cms.append({"catalog": cat_name, "title": t_art})
            except Exception:
                pass

            try:
                tdoc = f_trd.result() or {}
                for idx, c_item in enumerate((tdoc.get("coins") or [])[:15], 1):
                    it = c_item.get("item") or {}
                    csym = (it.get("symbol") or "").upper()
                    if csym:
                        trending_coins.append({
                            "rank": idx,
                            "symbol": csym,
                            "name": it.get("name") or csym,
                            "market_cap_rank": it.get("market_cap_rank"),
                        })
            except Exception:
                pass

        _MACRO_CACHE["omni_feeds"] = {
            "rss": omni_items,
            "binance_cms": binance_cms,
            "trending": trending_coins,
        }

    # 2. Token-specific Yahoo RSS + Token-specific X/Twitter & Bank search
    tok_query_url = (
        "https://news.google.com/rss/search?q=("
        + urllib.parse.quote(base)
        + "+crypto)+(%22on+X%22+OR+whale+OR+Binance+OR+ETF+OR+institutional+OR+breakout)+when:7d&hl=en-US&gl=US&ceid=US:en"
    )
    with ThreadPoolExecutor(max_workers=3) as ex:
        f_y = {ex.submit(_http_get, YAHOO_RSS % ysym, 7): ysym for ysym in tickers}
        f_tx = ex.submit(_http_get, tok_query_url, 7)
        for fut_y, ysym in f_y.items():
            try:
                for src, cat, title, pub in _parse_rss(fut_y.result(), "Yahoo/" + ysym, "news")[:10]:
                    if title not in seen:
                        seen.add(title)
                        headlines.append({
                            "symbol": src, "category": cat, "title": title,
                            "pubDate": pub, "is_token": ysym.startswith(base + "-"),
                        })
            except Exception:
                continue
        try:
            for src, cat, title, pub in _parse_rss(f_tx.result(), "X/Social/" + base, "twitter")[:8]:
                if title not in seen:
                    seen.add(title)
                    headlines.append({
                        "symbol": src, "category": "twitter", "title": title,
                        "pubDate": pub, "is_token": True,
                    })
        except Exception:
            pass

    omni = _MACRO_CACHE["omni_feeds"]
    token_pat = (
        re.compile(r"\b(%s|binance|bnb chain|pancakeswap)\b" % re.escape(base), re.I)
        if base == "BNB"
        else re.compile(r"\b%s\b" % re.escape(base), re.I)
    )

    # 3. Categorize Fed, Banks, Binance Official, Twitter/X Flow, and Magazines
    fed_items = []
    bank_items = []
    twitter_items = []
    magazine_items = []
    binance_items = []

    for b_art in omni.get("binance_cms", []):
        t_art = b_art["title"]
        is_tok = bool(token_pat.search(t_art))
        sent, tags = _classify_headline(t_art)
        entry = {"source": "Binance (" + b_art["catalog"] + ")", "title": t_art, "sentiment": sent, "is_token": is_tok}
        binance_items.append(entry)
        if t_art not in seen:
            seen.add(t_art)
            headlines.append({
                "symbol": "Binance CMS", "category": "binance", "title": t_art,
                "pubDate": "", "is_token": is_tok,
            })

    for src, cat, title, pub in omni.get("rss", []):
        is_tok = bool(token_pat.search(title))
        sent, tags = _classify_headline(title)
        item_obj = {"source": src, "title": title, "sentiment": sent, "tags": tags, "is_token": is_tok}
        if cat == "fed" and sum(1 for x in fed_items if x["source"] == src) < 2:
            fed_items.append(item_obj)
        elif cat == "bank" and len(bank_items) < 6:
            bank_items.append(item_obj)
        elif cat == "twitter":
            if is_tok or sum(1 for x in twitter_items if x["source"] == src) < 3:
                twitter_items.insert(0 if is_tok else len(twitter_items), item_obj)
        elif cat == "magazine" and (is_tok or sum(1 for x in magazine_items if x["source"] == src) < 1):
            magazine_items.insert(0 if is_tok else len(magazine_items), item_obj)

        is_macro = cat in ("fed", "bank", "twitter") or bool(
            re.search(r"\b(bitcoin|btc|etf|sec|fed|fomc|powell|jpmorgan|blackrock|goldman|cpi|stablecoin|tether|binance|crypto|whale|lookonchain)\b", title, re.I)
        )
        if (is_tok or is_macro) and title not in seen:
            seen.add(title)
            headlines.append({"symbol": src, "category": cat, "title": title, "pubDate": pub, "is_token": is_tok})

    # Include token-specific Twitter items at top of twitter_items
    for h in headlines:
        if h.get("category") == "twitter" and h.get("is_token"):
            sent, tags = _classify_headline(h["title"])
            if not any(x["title"] == h["title"] for x in twitter_items):
                twitter_items.insert(0, {"source": h["symbol"], "title": h["title"], "sentiment": sent, "tags": tags, "is_token": True})

    # 4. Check CoinGecko Trending status for this token
    trending_list = omni.get("trending", [])
    trending_match = next((t for t in trending_list if t["symbol"] == base), None)

    # 5. Compute Omni-Intelligence Catalyst Score (-15 to +15)
    # One vote per headline (bull minus bear, mixed = 0), weighted by relevance and age,
    # then squashed with tanh so 90 generic headlines cannot pin the pillar at +15.
    bull_hits, bear_hits, event_hits = [], [], []
    raw = 0.0
    if trending_match:
        raw += 2.0
        bull_hits.append(("CoinGecko Trending", "social heat", "%s is Trending #%d Globally on CoinGecko Search" % (base, trending_match["rank"])))

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    for h in headlines[:90]:
        cat = h.get("category", "news")
        if cat == "binance" and not h.get("is_token"):
            continue  # exchange housekeeping about other coins (delistings, API notices) is not a catalyst here
        age_w = headline_age_weight(h.get("pubDate"), now_utc)
        if age_w <= 0:
            continue
        if re.search(EVENT_PATTERN, h["title"], re.I) and len(event_hits) < 4:
            event_hits.append((h["symbol"], "scheduled event risk", h["title"]))
        w = (1.6 if h.get("is_token") else (0.9 if cat in ("fed", "bank", "binance", "twitter") else 0.65)) * age_w
        matched_b = [lbl for lbl, pat in BULL_PATTERNS if re.search(pat, h["title"], re.I)]
        matched_r = [lbl for lbl, pat in BEAR_PATTERNS if re.search(pat, h["title"], re.I)]
        if matched_b and not matched_r:
            raw += 1.4 * w
            if len(bull_hits) < 6:
                bull_hits.append((h["symbol"], ", ".join(matched_b), h["title"]))
        elif matched_r and not matched_b:
            raw -= 1.4 * w
            if len(bear_hits) < 6:
                bear_hits.append((h["symbol"], ", ".join(matched_r), h["title"]))
    score = 15.0 * math.tanh(raw / 12.0)

    sources_list = [
        "Federal Reserve (Press & Speeches)",
        "Wall St. Banks (JPM/BlackRock/GS/MS)",
        "Binance Official CMS",
        "X/Twitter Whale Flow (Lookonchain/Arkham/Wu)",
        "CoinGecko Trending",
        "Forbes/Fortune/Bloomberg",
        "Bitcoin Magazine",
        "The Block",
        "Blockworks",
        "CryptoSlate",
        "BeInCrypto",
        "CoinDesk",
        "Cointelegraph",
        "Decrypt",
        "Yahoo Finance",
    ]

    return {
        "sources_checked": sources_list,
        "headlines_checked": len(headlines[:90]),
        "bullish_catalysts": bull_hits,
        "bearish_catalysts": bear_hits,
        "event_risk_headlines": event_hits,
        "fed_and_central_banks": fed_items[:5],
        "bank_and_institutional": bank_items[:5],
        "binance_official": binance_items[:5],
        "twitter_x_flow": twitter_items[:6],
        "magazines_and_press": magazine_items[:6],
        "trending_social": {
            "is_trending": bool(trending_match),
            "trending_rank": trending_match["rank"] if trending_match else None,
            "top_trending_coins": [t["symbol"] for t in trending_list[:10]],
        },
        "score": round(max(-15.0, min(15.0, score)), 1),
    }


# ---------------------------------------------------------------- simulation & forecast

def simulate_horizons(klines_1h, price, composite_score, seed=42):
    """5,000-path bootstrap + microstructure drift adjustment over 4h, 24h, and 7d (168h)."""
    rets = []
    for i in range(1, len(klines_1h)):
        p0, p1 = float(klines_1h[i - 1][4]), float(klines_1h[i][4])
        if p0 > 0:
            rets.append(math.log(p1 / p0))
    if len(rets) < 24:
        rets = [0.001, -0.001] * 24

    sigma_1h = statistics.pstdev(rets) or 0.003
    hist_drift_1h = statistics.mean(rets)
    # Blend historical drift with the microstructure/technical composite score (-100..+100)
    # At score = +50, add +0.12 * sigma_1h per hour; decaying over longer horizons as mean reversion kicks in.
    norm_score = max(-1.0, min(1.0, composite_score / 100.0))

    horizons = [
        ("4h", 4, 0.22),
        ("24h", 24, 0.12),
        ("7d", 168, 0.05),
    ]
    rng = random.Random(seed)
    out = {}
    for name, hours, alpha in horizons:
        adj_drift = 0.35 * hist_drift_1h + alpha * norm_score * sigma_1h
        finals = []
        up_count = 0
        for _ in range(5000):
            log_r = 0.0
            for _h in range(hours):
                r = rng.choice(rets) - hist_drift_1h + adj_drift
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
        out[name] = {
            "horizon": name,
            "hours": hours,
            "prob_up_pct": p_up,
            "prob_down_pct": p_down,
            "direction": "UP" if p_up >= 53.0 else ("DOWN" if p_up <= 47.0 else "SIDEWAYS / COIN FLIP"),
            "expected_price": round(med, 8),
            "expected_move_pct": round((med / price - 1.0) * 100.0, 2),
            "range_50_low": round(p25, 8),
            "range_50_high": round(p75, 8),
            "range_80_low": round(p10, 8),
            "range_80_high": round(p90, 8),
        }
    return out


# ---------------------------------------------------------------- full token forecast

def forecast_symbol(sym):
    bsym = to_binance(sym)
    with ThreadPoolExecutor(max_workers=8) as ex:
        f_t24 = ex.submit(spot_get, "/api/v3/ticker/24hr?symbol=%s" % bsym)
        f_k15 = ex.submit(spot_get, "/api/v3/klines?symbol=%s&interval=15m&limit=100" % bsym)
        f_k1h = ex.submit(spot_get, "/api/v3/klines?symbol=%s&interval=1h&limit=240" % bsym)
        f_k4h = ex.submit(spot_get, "/api/v3/klines?symbol=%s&interval=4h&limit=210" % bsym)
        f_k1d = ex.submit(spot_get, "/api/v3/klines?symbol=%s&interval=1d&limit=210" % bsym)
        f_dep = ex.submit(spot_get, "/api/v3/depth?symbol=%s&limit=5000" % bsym)
        f_btc = ex.submit(spot_get, "/api/v3/klines?symbol=BTCUSDT&interval=1d&limit=15") if bsym != "BTCUSDT" else None
        f_news = ex.submit(analyze_news, bsym)
        f_macro = ex.submit(fetch_global_macro_and_onchain, bsym)

        t24 = f_t24.result()
        k15 = f_k15.result()
        k1h = f_k1h.result()
        k4h = f_k4h.result()
        k1d = f_k1d.result()
        dep = f_dep.result()
        btc_1d = f_btc.result() if f_btc else k1d[-15:]
        news = f_news.result()
        macro_onchain = f_macro.result()

    price = float(t24["lastPrice"])
    chg_24h = float(t24["priceChangePercent"])
    vol_24h = float(t24["quoteVolume"])
    high_24h = float(t24["highPrice"])
    low_24h = float(t24["lowPrice"])

    # 1. Multi-timeframe technicals (-25 to +25 weighted)
    tf_15m = analyze_timeframe(k15, "15m")
    tf_1h = analyze_timeframe(k1h, "1h")
    tf_4h = analyze_timeframe(k4h, "4h")
    tf_1d = analyze_timeframe(k1d, "1d")
    tech_score = round(0.15 * tf_15m["score"] + 0.30 * tf_1h["score"] + 0.30 * tf_4h["score"] + 0.25 * tf_1d["score"], 1)

    # 2. Spot Order-Flow & Order Book Depth (-25 to +25)
    flow_1h = parse_klines_flow(k15, 4)
    flow_4h = parse_klines_flow(k1h, 4)
    flow_24h = parse_klines_flow(k1h, 24)
    flow_7d = parse_klines_flow(k4h, 42)
    ob = analyze_orderbook(dep, price)

    flow_score = 0.0
    # Taker buy % above/below 50%
    flow_score += max(-8.0, min(8.0, (flow_4h["buy_pct"] - 50.0) * 1.4))
    flow_score += max(-7.0, min(7.0, (flow_24h["buy_pct"] - 50.0) * 1.4))
    if ob:
        flow_score += max(-10.0, min(10.0, ob["imbalance_1pct"] * 0.22 + ob["imbalance_2pct"] * 0.12))
    flow_score = round(max(-25.0, min(25.0, flow_score)), 1)

    # 3. Perpetual Futures & Leverage (-25 to +25)
    fut = analyze_futures(bsym, price, k1h)
    fut_score = fut.get("score", 0.0) if fut.get("available") else 0.0

    # 4. Relative Strength vs BTC (-10 to +10)
    sym_7d = (float(k1d[-1][4]) / float(k1d[-8][4]) - 1.0) * 100.0 if len(k1d) >= 8 else chg_24h
    btc_24h = (float(btc_1d[-1][4]) / float(btc_1d[-2][4]) - 1.0) * 100.0 if len(btc_1d) >= 2 else 0.0
    btc_7d = (float(btc_1d[-1][4]) / float(btc_1d[-8][4]) - 1.0) * 100.0 if len(btc_1d) >= 8 else 0.0
    alpha_24h = round(chg_24h - btc_24h, 2) if bsym != "BTCUSDT" else 0.0
    alpha_7d = round(sym_7d - btc_7d, 2) if bsym != "BTCUSDT" else 0.0
    rel_score = 0.0
    if bsym != "BTCUSDT":
        rel_score = round(max(-10.0, min(10.0, alpha_24h * 1.2 + alpha_7d * 0.5)), 1)

    # 4.5. Institutional Volume Profile (POC/VAH/VAL), VWAP & Wyckoff/CVD Signals
    vp = compute_volume_profile(k1h, price, n_bars=168, bins=40)
    inst = detect_institutional_signals(k1h, k4h, price)

    # 5. Composite Score (-100 to +100)
    inst_and_macro_bonus = round(max(-10.0, min(10.0, inst["bonus_score"] + macro_onchain["macro_bonus"])), 1)
    raw_total = tech_score + flow_score + fut_score + rel_score + news["score"] + inst_and_macro_bonus
    composite = round(max(-100.0, min(100.0, raw_total)), 1)

    if composite >= 35:
        verdict = "STRONG BULLISH (High-Conviction Upside)"
    elif composite >= 12:
        verdict = "BULLISH (Leans Upward)"
    elif composite <= -35:
        verdict = "STRONG BEARISH (High-Conviction Downside)"
    elif composite <= -12:
        verdict = "BEARISH (Leans Downward)"
    else:
        verdict = "NEUTRAL / RANGE-BOUND (Mixed Signals - Wait for Breakout)"

    horizons = simulate_horizons(k1h, price, composite)
    kelly_plan = compute_kelly_trade_plan(price, horizons, vp, ob, tf_1h, composite)

    # Institutional Margin Execution Plan (Leverage tiering, safe liquidation buffer, financing carry, event risk)
    margin_exec = None
    if margin and kelly_plan:
        try:
            now_ms_val = int(time.time() * 1000)
            ev = margin.event_risk(now_ms_val)
            side_val = "long" if kelly_plan["bias"] in ("LONG", "WAIT / RANGE SCALP") else "short"
            f8h = (fut.get("consensus_funding_pct", 0.01) / 100.0) if fut and fut.get("available") else 0.0001
            margin_exec = margin.margin_plan(
                equity=5000.0,
                risk_usd=50.0,
                entry=price,
                stop=kelly_plan["stop_loss"],
                side=side_val,
                base=base_asset(bsym),
                funding_8h=f8h,
                hold_hours=24.0,
                venue="perp" if (fut and fut.get("available")) else "margin",
                event=ev,
            )
        except Exception:
            margin_exec = None
    kelly_plan["margin_plan"] = margin_exec

    # Key support & resistance levels
    atr_1h = tf_1h["atr14"]
    atr_1d = tf_1d["atr14"]
    supports = sorted({
        round(low_24h, 8),
        round(tf_4h["low_20"], 8),
        round(vp["val"], 8),
        round(vp["poc"], 8),
        round(ob["support_wall_price"], 8) if ob else round(price - atr_1h * 2, 8),
        round(price - atr_1d, 8),
    }, reverse=True)
    resistances = sorted({
        round(high_24h, 8),
        round(tf_4h["high_20"], 8),
        round(vp["vah"], 8),
        round(vp["poc"], 8),
        round(ob["resistance_wall_price"], 8) if ob else round(price + atr_1h * 2, 8),
        round(price + atr_1d, 8),
    })

    return {
        "symbol": bsym,
        "as_of": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "price": price,
        "change_24h_pct": chg_24h,
        "high_24h": high_24h,
        "low_24h": low_24h,
        "volume_24h_usd": round(vol_24h, 2),
        "composite_score": composite,
        "verdict": verdict,
        "pillar_scores": {
            "technicals_mtf": {"score": tech_score, "max": 25.0},
            "spot_orderflow_depth": {"score": flow_score, "max": 25.0},
            "futures_leverage": {"score": fut_score, "max": 25.0},
            "relative_strength_btc": {"score": rel_score, "max": 10.0},
            "news_catalysts": {"score": news["score"], "max": 15.0},
            "institutional_bonus": {"score": inst_and_macro_bonus, "max": 10.0},
        },
        "horizons": horizons,
        "volume_profile": vp,
        "institutional": inst,
        "macro_onchain": macro_onchain,
        "kelly_trade_plan": kelly_plan,
        "timeframes": {"15m": tf_15m, "1h": tf_1h, "4h": tf_4h, "1d": tf_1d},
        "spot_flow": {"1h": flow_1h, "4h": flow_4h, "24h": flow_24h, "7d": flow_7d, "orderbook": ob},
        "futures": fut,
        "relative_strength": {
            "return_7d_pct": round(sym_7d, 2),
            "btc_24h_pct": round(btc_24h, 2),
            "btc_7d_pct": round(btc_7d, 2),
            "alpha_24h_pct": alpha_24h,
            "alpha_7d_pct": alpha_7d,
        },
        "news": news,
        "levels": {
            "supports": [s for s in supports if s < price][:3],
            "resistances": [r for r in resistances if r > price][:3],
        },
    }


def render_report(d):
    lines = []
    lines.append("=" * 78)
    lines.append("%s DIRECTIONAL FORECAST & MICROSTRUCTURE REPORT  (%s)" % (d["symbol"], d["as_of"]))
    lines.append("=" * 78)
    lines.append("Spot Price: %s (%+.2f%% 24h) | 24h Range: %s - %s | 24h Vol: %s" % (
        fmt_p(d["price"]), d["change_24h_pct"], fmt_p(d["low_24h"]), fmt_p(d["high_24h"]), fmt_usd(d["volume_24h_usd"])))
    lines.append("VERDICT:    %s  [Composite Score: %+.1f / 100]" % (d["verdict"], d["composite_score"]))
    lines.append("")

    # 1. Probabilistic Forecast Table
    lines.append("1. PROBABILISTIC FUTURE ESTIMATION (5,000 Monte Carlo + Microstructure Drift)")
    lines.append("   %-8s  %-20s  %-10s  %-10s  %-18s  %s" % (
        "HORIZON", "CALL", "P(UP)", "P(DOWN)", "EXPECTED PRICE", "80% CONFIDENCE BAND"))
    for k in ("4h", "24h", "7d"):
        h = d["horizons"][k]
        lines.append("   %-8s  %-20s  %5.1f%%      %5.1f%%      %s (%+.2f%%)    %s to %s" % (
            h["horizon"], h["direction"], h["prob_up_pct"], h["prob_down_pct"],
            fmt_p(h["expected_price"]), h["expected_move_pct"],
            fmt_p(h["range_80_low"]), fmt_p(h["range_80_high"])))
    lines.append("")

    # 2. Pillar Breakdown
    ps = d["pillar_scores"]
    lines.append("2. DRIVER SCORE BREAKDOWN (Why it leans Up or Down)")
    lines.append("   - Multi-Timeframe Technicals : %+5.1f / 25" % ps["technicals_mtf"]["score"])
    lines.append("   - Spot Order-Flow & Depth    : %+5.1f / 25" % ps["spot_orderflow_depth"]["score"])
    lines.append("   - Futures & Whale Positioning: %+5.1f / 25" % ps["futures_leverage"]["score"])
    lines.append("   - Relative Strength vs BTC   : %+5.1f / 10" % ps["relative_strength_btc"]["score"])
    lines.append("   - News & Macro Catalysts     : %+5.1f / 15" % ps["news_catalysts"]["score"])
    lines.append("   - Institutional / Wyckoff    : %+5.1f / 10" % ps["institutional_bonus"]["score"])
    lines.append("")

    # 3. Cross-Exchange Perpetual Futures & Whale Positioning
    fut = d["futures"]
    lines.append("3. CROSS-EXCHANGE PERPETUAL FUTURES & LEVERAGE (Binance + Bybit + OKX + Coin-M)")
    if fut.get("available"):
        f_parts = ["Binance %+.4f%%" % fut["funding_rate_pct"]]
        if fut.get("bybit_funding_pct") is not None:
            f_parts.append("Bybit %+.4f%%" % fut["bybit_funding_pct"])
        if fut.get("okx_funding_pct") is not None:
            f_parts.append("OKX %+.4f%%" % fut["okx_funding_pct"])
        if fut.get("coinm_funding_pct") is not None:
            f_parts.append("Coin-M %+.4f%%" % fut["coinm_funding_pct"])
        lines.append("   Funding (8h) : %s | Consensus: %+.4f%% (%+.1f%% ann) | Basis: %+.4f%%" % (
            " | ".join(f_parts), fut.get("consensus_funding_pct", fut["funding_rate_pct"]),
            fut["annualized_funding_pct"], fut["basis_pct"]))
        lines.append("   Global OI    : %s Total (Binance %s | Bybit %s | OKX %s) [Binance 4h: %+.2f%%, 24h: %+.2f%%]" % (
            fmt_usd(fut.get("global_oi_usd", fut["open_interest_usd"])),
            fmt_usd(fut["open_interest_usd"]), fmt_usd(fut.get("bybit_oi_usd", 0.0)), fmt_usd(fut.get("okx_oi_usd", 0.0)),
            fut["oi_change_4h_pct"], fut["oi_change_24h_pct"]))
        lines.append("   OI Regime    : %s" % fut["oi_regime"])
        lines.append("   Positioning  : Top Whales %.1f%% Long (L/S %.2f) vs Retail Accounts %.1f%% Long (L/S %.2f)" % (
            fut["whale_long_pct"], fut["whale_ls_ratio"], fut["retail_long_pct"], fut["retail_ls_ratio"]))
        lines.append("   Futures Taker: 1h Buy/Sell Ratio %.2f | 4h Ratio %.2f | 24h Ratio %.2f (>1.0 = market buying)" % (
            fut["taker_buy_sell_1h"], fut["taker_buy_sell_4h"], fut["taker_buy_sell_24h"]))
        for n in fut.get("notes", []):
            lines.append("   * Signal     : %s" % n)
    else:
        lines.append("   %s" % fut.get("note", "No futures data"))
    lines.append("")

    # 4. Spot Order-Flow & Order Book Walls
    sf = d["spot_flow"]
    ob = sf.get("orderbook")
    lines.append("4. SPOT ORDER-FLOW (CVD) & ORDER-BOOK LIQUIDITY WALLS (api.binance.com)")
    lines.append("   Spot Taker Buy Share & Net CVD (Aggressive Market Buys minus Market Sells):")
    for win in ("1h", "4h", "24h", "7d"):
        w = sf[win]
        lines.append("     %-3s : Taker Buy %5.1f%% | Net CVD %10s (Vol %s)" % (
            win, w["buy_pct"], fmt_usd(w["cvd_usd"], signed=True), fmt_usd(w["total_usd"])))
    if ob:
        lines.append("   Order-Book Depth (+-1%%): Bids %s vs Asks %s (Imbalance %+.1f%%)" % (
            fmt_usd(ob["bid_1pct_usd"]), fmt_usd(ob["ask_1pct_usd"]), ob["imbalance_1pct"]))
        lines.append("   Order-Book Depth (+-2%%): Bids %s vs Asks %s (Imbalance %+.1f%%)" % (
            fmt_usd(ob["bid_2pct_usd"]), fmt_usd(ob["ask_2pct_usd"]), ob["imbalance_2pct"]))
        lines.append("   Largest Bid Support Wall : %s (%s) | Largest Ask Supply Wall: %s (%s)" % (
            fmt_p(ob["support_wall_price"]), fmt_usd(ob["support_wall_usd"]),
            fmt_p(ob["resistance_wall_price"]), fmt_usd(ob["resistance_wall_usd"])))
    lines.append("")

    # 5. Multi-Timeframe Technicals & Relative Strength
    lines.append("5. MULTI-TIMEFRAME TREND & RELATIVE STRENGTH VS BITCOIN")
    lines.append("   %-4s  %-8s  %-10s  %-10s  %-10s  %-6s  %-12s  %s" % (
        "TF", "REGIME", "EMA20", "EMA50", "SMA200", "RSI14", "MACD", "ATR(14)"))
    for tf in ("15m", "1h", "4h", "1d"):
        t = d["timeframes"][tf]
        macd_lbl = ("+rising" if t["macd_rising"] else "+fading") if t["macd_hist"] >= 0 else ("-falling" if not t["macd_rising"] else "-recovering")
        lines.append("   %-4s  %-8s  %-10s  %-10s  %-10s  %5.1f   %-12s  %s (%.2f%%)" % (
            tf, t["regime"], fmt_p(t["ema20"]), fmt_p(t["ema50"]), fmt_p(t["sma200"]),
            t["rsi14"], macd_lbl, fmt_p(t["atr14"]), t["atr_pct"]))
    rs = d["relative_strength"]
    if d["symbol"] != "BTCUSDT":
        lines.append("   Relative Strength vs BTC: 24h Alpha %+.2f%% | 7d Alpha %+.2f%% (%s 7d: %+.2f%% vs BTC 7d: %+.2f%%)" % (
            rs["alpha_24h_pct"], rs["alpha_7d_pct"], d["symbol"], rs["return_7d_pct"], rs["btc_7d_pct"]))
    lines.append("   Key Supports   : %s" % (", ".join(fmt_p(x) for x in d["levels"]["supports"]) or "-"))
    lines.append("   Key Resistances: %s" % (", ".join(fmt_p(x) for x in d["levels"]["resistances"]) or "-"))
    lines.append("")

    # 6. Institutional Volume Profile, VWAP & Wyckoff/CVD Signals
    vp = d.get("volume_profile") or {}
    inst = d.get("institutional") or {}
    lines.append("6. INSTITUTIONAL MARKET PROFILE (POC/VAH/VAL), VWAP & WYCKOFF SIGNALS")
    lines.append("   7d Volume Profile : VAL %s  |  POC (Fair Value Magnet) %s  |  VAH %s" % (
        fmt_p(vp.get("val")), fmt_p(vp.get("poc")), fmt_p(vp.get("vah"))))
    lines.append("   Auction State     : %s" % vp.get("state", "-"))
    lines.append("   Institutional VWAP: 24h VWAP %s (%+.2f%% dist) | 7d VWAP %s" % (
        fmt_p(vp.get("vwap_24h")), vp.get("vwap_24h_dist_pct", 0.0), fmt_p(vp.get("vwap_7d"))))
    lines.append("   Volatility Regime : %s (4h BB Width: %.2f%%)" % (
        inst.get("squeeze_4h", "-"), inst.get("bb_width_4h_pct", 0.0)))
    for sig in inst.get("signals", []):
        lines.append("   * Quant Signal    : %s" % sig)
    lines.append("")

    # 7. Global On-Chain TVL, Stablecoin Liquidity & Macro Sentiment
    mc = d.get("macro_onchain") or {}
    lines.append("7. ON-CHAIN TVL, STABLECOIN LIQUIDITY & MACRO REGIME (DefiLlama + CoinGecko + Alternative.me)")
    lines.append("   Fear & Greed Index: %d / 100 (%s, prev %d) | Total Crypto Mcap: %s (%+.2f%% 24h)" % (
        mc.get("fear_greed_value", 50), mc.get("fear_greed_label", "Neutral"), mc.get("fear_greed_prev", 50),
        fmt_usd(mc.get("total_mcap_usd", 0.0)), mc.get("mcap_24h_change_pct", 0.0)))
    lines.append("   Market Dominance  : BTC.D %.2f%% | ETH.D %.2f%% -> %s" % (
        mc.get("btc_dominance_pct", 0.0), mc.get("eth_dominance_pct", 0.0), mc.get("market_season", "-")))
    lines.append("   Global Stablecoins: %s (USDT %s | USDC %s) | 24h Mint/Burn: %s | 7d Flow: %s" % (
        fmt_usd(mc.get("stablecoin_total_usd", 0.0)), fmt_usd(mc.get("usdt_supply_usd", 0.0)),
        fmt_usd(mc.get("usdc_supply_usd", 0.0)), fmt_usd(mc.get("stablecoin_24h_flow_usd", 0.0), signed=True),
        fmt_usd(mc.get("stablecoin_7d_flow_usd", 0.0), signed=True)))
    if mc.get("chain_tvl_usd"):
        lines.append("   DefiLlama L1/L2   : %s Chain TVL %s (Global DeFi Rank #%s)" % (
            mc.get("chain_name"), fmt_usd(mc.get("chain_tvl_usd")), mc.get("chain_tvl_rank")))
    for mn in mc.get("macro_notes", []):
        lines.append("   * Macro Signal    : %s" % mn)
    lines.append("")

    # 8. God-Level Execution Playbook & Quarter-Kelly Sizing
    kp = d.get("kelly_trade_plan") or {}
    lines.append("8. INSTITUTIONAL EXECUTION PLAYBOOK & QUARTER-KELLY SIZING")
    lines.append("   Actionable Bias   : %s | Win Prob: %.1f%% | Reward:Risk: %.2fR | Math Edge: %+.2fR" % (
        kp.get("bias", "-"), kp.get("win_prob_pct", 50.0), kp.get("reward_risk_ratio", 1.5), kp.get("expected_value_R", 0.0)))
    lines.append("   Ideal Limit Entry : %s (or Spot %s) | Invalidation Stop: %s" % (
        fmt_p(kp.get("limit_pullback_entry")), fmt_p(kp.get("spot_entry")), fmt_p(kp.get("stop_loss"))))
    lines.append("   Profit Targets    : TP1 %s (Scale 50%%) | TP2 %s (Runner)" % (
        fmt_p(kp.get("tp1")), fmt_p(kp.get("tp2"))))
    lines.append("   Quarter-Kelly Risk: %.2f%% of total bankroll risked at Stop-Loss" % kp.get("quarter_kelly_risk_pct", 0.0))
    mp = kp.get("margin_plan") or {}
    if mp.get("ok"):
        lines.append("   Leveraged Margin  : Cap %.0fx (Liquidation Safe %.1fx) -> Suggested %.0fx %s | Margin: %s (%.1f%% eq)" % (
            mp.get("leverage_cap_tier", 1.0), mp.get("leverage_cap_liq_safe", 1.0),
            mp.get("leverage", 1.0), "Perp" if mp.get("venue") == "perp" else "Isolated Margin",
            fmt_usd(mp.get("margin_usd")), mp.get("margin_pct_equity", 0.0)))
        lines.append("   Liquidation Buffer: %s (%.2f%% dist = %.1fx stop distance) | Safe: YES" % (
            fmt_p(mp.get("liquidation_price")), mp.get("liq_distance_pct", 0.0), mp.get("liq_to_stop_ratio", 0.0)))
        lines.append("   Financing & Carry : 24h Carry %s (%+.3fR) | Event Regime: %s" % (
            fmt_usd(mp.get("carry_usd"), signed=True), mp.get("carry_r", 0.0), mp.get("event_level", "CLEAR")))
        if mp.get("event_reason") and mp.get("event_level") != "CLEAR":
            lines.append("   * Event Caution   : %s" % mp.get("event_reason"))
    lines.append("")

    # 9. Omni-Channel Intelligence: Fed, Central Banks, Wall St. Banks, Binance CMS, X/Twitter Flow & Magazines
    nw = d["news"]
    lines.append("9. 25-SOURCE OMNI-INTELLIGENCE: FED, BANKS, BINANCE CMS, X/TWITTER FLOW & MAGAZINES (%d items scanned)" % nw["headlines_checked"])
    trd = nw.get("trending_social") or {}
    if trd.get("is_trending"):
        lines.append("   [HOT SOCIAL TRENDING] %s is TRENDING #%d Globally on CoinGecko Search! (Top Trending: %s)" % (
            d["symbol"], trd.get("trending_rank"), ", ".join(trd.get("top_trending_coins") or [])))
    elif trd.get("top_trending_coins"):
        lines.append("   [Social Heat] Global Top Trending Coins Right Now: %s" % ", ".join(trd.get("top_trending_coins")[:8]))

    for sym_h, tag_h, title in nw.get("bullish_catalysts", []):
        lines.append("   [+] (%s: %s) %s" % (sym_h, tag_h, title))
    for sym_h, tag_h, title in nw.get("bearish_catalysts", []):
        lines.append("   [-] (%s: %s) %s" % (sym_h, tag_h, title))

    if nw.get("fed_and_central_banks"):
        lines.append("   --- Federal Reserve (FOMC) & Central Bank Announcements ---")
        for it in nw["fed_and_central_banks"][:3]:
            lines.append("     * [%s] %s" % (it["source"], it["title"]))
    if nw.get("bank_and_institutional"):
        lines.append("   --- Wall St. Banks (JPMorgan / BlackRock / GS / MS) & SEC Flow ---")
        for it in nw["bank_and_institutional"][:3]:
            lines.append("     * [%s] %s" % (it["source"], it["title"]))
    if nw.get("binance_official"):
        lines.append("   --- Official Binance Exchange Announcements (Listings / Collateral / Activities) ---")
        for it in nw["binance_official"][:3]:
            lines.append("     * [%s] %s" % (it["source"], it["title"]))
    if nw.get("twitter_x_flow"):
        lines.append("   --- Crypto Twitter / X Whale Flow (Lookonchain / Whale Alert / Arkham / CT) ---")
        for it in nw["twitter_x_flow"][:4]:
            lines.append("     * [%s] %s" % (it["source"], it["title"]))
    if nw.get("magazines_and_press"):
        lines.append("   --- Financial & Crypto Magazines (Forbes / Bloomberg / Bitcoin Mag / The Block / Blockworks) ---")
        for it in nw["magazines_and_press"][:4]:
            lines.append("     * [%s] %s" % (it["source"], it["title"]))
    lines.append("")
    lines.append("Note: Probabilistic estimation from live Binance Spot & Futures order flow + 25-source intelligence, not financial advice.")
    return "\n".join(lines)


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Crypto Directional Forecaster & Microstructure Analyst (trader-analyst).")
    ap.add_argument("symbols", nargs="*", default=["BNB"], help="Binance symbols to forecast, e.g. BNB BTC ETH SOL")
    ap.add_argument("--write", nargs="?", const="trader/forecast.json", metavar="PATH",
                    help="write forecast JSON (default trader/forecast.json)")
    ap.add_argument("--metrics", metavar="PATH", help="also write summary metrics to trader/metrics.json")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    results = []
    for s in a.symbols:
        try:
            results.append(forecast_symbol(s))
        except Exception as e:
            print("predict.py: error on %s: %s" % (s, e), file=sys.stderr)
    if not results:
        return 1

    if a.write:
        d = os.path.dirname(a.write)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.write, "w", encoding="utf-8") as fh:
            json.dump(results[0] if len(results) == 1 else results, fh, indent=2)
            fh.write("\n")
    if a.metrics:
        d = os.path.dirname(a.metrics)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.metrics, "w", encoding="utf-8") as fh:
            json.dump(results, fh, indent=2)
            fh.write("\n")

    if a.json:
        print(json.dumps(results[0] if len(results) == 1 else results, indent=2))
    else:
        for i, res in enumerate(results):
            if i:
                print("")
            print(render_report(res))
        if a.write:
            print("\nWrote forecast to %s." % a.write)
    return 0


if __name__ == "__main__":
    sys.exit(main())
