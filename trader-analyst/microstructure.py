#!/usr/bin/env python3
"""Market Microstructure, VPIN (Volume-Synchronized Probability of Toxicity),
Wyckoff 2.0 Order-Flow Divergence, and Volatility-Adaptive Kelly Sizing.

Canonical Quantitative Foundations:
  1. Marcos López de Prado, David Easley, Maureen O'Hara (2011, 2012):
     - "The Volume Clock: Insights into the High Frequency Trading"
     - "Flow Toxicity and Liquidity in a High-Frequency World" (VPIN framework)
     - Slices order flow into equal volume buckets on a Volume Clock to measure
       informed trader arrival rates and order-flow toxicity.
  2. Rubén Villahermosa (Wyckoff 2.0) & Jim Dalton (Auction Market Theory):
     - Wyckoff Springs (Bullish Liquidity Sweeps of swing low / VAL)
     - Wyckoff Upthrusts (Bearish Stop-Hunts / SFPs of swing high / VAH)
     - Multi-window Cumulative Volume Delta (CVD) absorption and exhaustion divergences.
  3. Edward Thorp (Fortune's Formula) & John Carter (TTM Squeeze):
     - Volatility-adaptive fractional Kelly sizing scaling by TTM Squeeze coiling,
       ATR expansion shock ratios, and VPIN order-flow toxicity penalties.

Pure functions (Python 3.8+, standard library only, zero heavy dependencies).
"""

import math
import statistics
from typing import Any, Dict, List, Optional, Tuple, Union


# --------------------------------------------------------------------------- Normal CDF & BVC helpers

def norm_cdf(x: float) -> float:
    """Standard normal cumulative distribution function using math.erf."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def classify_trade_flow(price_change: float, price_sigma: float, volume: float) -> Tuple[float, float]:
    """Bulk Volume Classification (BVC) decomposition of volume into buy and sell.
    
    If price_sigma > 0, decomposes volume using normal CDF of standardized price change.
    Returns (buy_volume, sell_volume).
    """
    if volume <= 0:
        return 0.0, 0.0
    if price_sigma <= 1e-12:
        # Default 50/50 if no price variation
        if price_change > 0:
            return volume, 0.0
        elif price_change < 0:
            return 0.0, volume
        return volume * 0.5, volume * 0.5

    z = price_change / price_sigma
    # Bound z between -4.0 and 4.0 to avoid floating issues
    z = max(-4.0, min(4.0, z))
    buy_ratio = norm_cdf(z)
    buy_vol = volume * buy_ratio
    sell_vol = volume * (1.0 - buy_ratio)
    return buy_vol, sell_vol


# --------------------------------------------------------------------------- VPIN Engine

def compute_vpin(
    records: List[Union[Dict[str, Any], List[Any]]],
    num_buckets: int = 20,
    bucket_volume: Optional[float] = None,
    use_bvc: bool = False,
) -> Dict[str, Any]:
    """Compute Marcos López de Prado's Volume-Synchronized Probability of Toxicity (VPIN).

    Accepts:
      records: List of kline lists [open_time, open, high, low, close, volume, close_time,
               quote_vol, trades, taker_buy_base, taker_buy_quote, ...]
               OR list of trade dicts: [{"price": float, "qty": float, "is_buyer_maker": bool}, ...]
               OR dicts with {"price", "volume", "buy_volume"}.
      num_buckets: Number of volume buckets in the rolling window (typically 20 or 50).
      bucket_volume: If specified, target volume per bucket; otherwise computed as
                     total_volume / num_buckets.
      use_bvc: If True, uses Bulk Volume Classification (BVC) via normal CDF rather
               than raw taker buy/sell volume.

    Returns dict with VPIN metrics, toxicity regime, and informed arrival estimate.
    """
    if not records or len(records) < 2:
        return {
            "vpin": 0.0,
            "vpin_pct": 0.0,
            "regime": "INSUFFICIENT_DATA",
            "toxicity_status": "NEUTRAL",
            "num_buckets": 0,
            "bucket_volume": 0.0,
            "bucket_imbalances": [],
            "avg_buy_ratio": 0.5,
            "informed_arrival_estimate": 0.0,
            "adverse_selection_risk": "LOW",
            "warning": None,
        }

    # Normalize records into standardized flow items:
    # [{"price": float, "volume": float, "buy_vol": float, "sell_vol": float}]
    items: List[Dict[str, float]] = []
    prices: List[float] = []

    for r in records:
        if isinstance(r, (list, tuple)) and len(r) >= 6:
            # Kline format
            try:
                p_close = float(r[4])
                p_open = float(r[1])
                vol = float(r[5])
                taker_buy = float(r[9]) if len(r) > 9 else (vol * 0.5)
                sell_vol = max(0.0, vol - taker_buy)
                prices.append(p_close)
                items.append({
                    "price": p_close,
                    "price_delta": p_close - p_open,
                    "volume": vol,
                    "buy_vol": taker_buy,
                    "sell_vol": sell_vol,
                })
            except (ValueError, TypeError, IndexError):
                continue
        elif isinstance(r, dict):
            p = float(r.get("price", r.get("close", 0.0)))
            vol = float(r.get("qty", r.get("volume", 0.0)))
            if "buy_vol" in r:
                b_vol = float(r["buy_vol"])
                s_vol = float(r.get("sell_vol", max(0.0, vol - b_vol)))
            elif "is_buyer_maker" in r:
                # If buyer is maker, seller is aggressive taker -> sell volume
                if r["is_buyer_maker"]:
                    s_vol, b_vol = vol, 0.0
                else:
                    b_vol, s_vol = vol, 0.0
            else:
                b_vol = float(r.get("taker_buy", vol * 0.5))
                s_vol = max(0.0, vol - b_vol)
            prices.append(p)
            items.append({
                "price": p,
                "price_delta": 0.0,
                "volume": vol,
                "buy_vol": b_vol,
                "sell_vol": s_vol,
            })

    total_vol = sum(it["volume"] for it in items)
    if total_vol <= 0 or not items:
        return {
            "vpin": 0.0,
            "vpin_pct": 0.0,
            "regime": "INSUFFICIENT_DATA",
            "toxicity_status": "NEUTRAL",
            "num_buckets": 0,
            "bucket_volume": 0.0,
            "bucket_imbalances": [],
            "avg_buy_ratio": 0.5,
            "informed_arrival_estimate": 0.0,
            "adverse_selection_risk": "LOW",
            "warning": None,
        }

    # BVC decomposition if requested
    if use_bvc and len(prices) >= 2:
        price_diffs = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
        sigma = statistics.pstdev(price_diffs) if len(price_diffs) >= 2 else 0.0
        for i, it in enumerate(items):
            delta = price_diffs[i - 1] if i > 0 else it["price_delta"]
            b_vol, s_vol = classify_trade_flow(delta, sigma, it["volume"])
            it["buy_vol"] = b_vol
            it["sell_vol"] = s_vol

    # Target bucket volume V
    if bucket_volume is None or bucket_volume <= 0:
        bucket_vol = total_vol / float(max(1, num_buckets))
    else:
        bucket_vol = float(bucket_volume)

    if bucket_vol <= 0:
        bucket_vol = total_vol

    # Slicing volume into equal volume buckets on the Volume Clock
    buckets: List[Dict[str, float]] = []
    curr_b_vol = 0.0
    curr_s_vol = 0.0
    curr_filled = 0.0

    for it in items:
        rem_vol = it["volume"]
        b_ratio = (it["buy_vol"] / it["volume"]) if it["volume"] > 0 else 0.5
        s_ratio = (it["sell_vol"] / it["volume"]) if it["volume"] > 0 else 0.5

        while rem_vol > 1e-9:
            needed = bucket_vol - curr_filled
            if rem_vol <= needed:
                curr_filled += rem_vol
                curr_b_vol += rem_vol * b_ratio
                curr_s_vol += rem_vol * s_ratio
                rem_vol = 0.0
                # If bucket exactly full
                if abs(curr_filled - bucket_vol) < 1e-8:
                    buckets.append({
                        "buy_vol": curr_b_vol,
                        "sell_vol": curr_s_vol,
                        "imbalance": abs(curr_b_vol - curr_s_vol),
                        "total_vol": curr_filled,
                    })
                    curr_b_vol = 0.0
                    curr_s_vol = 0.0
                    curr_filled = 0.0
            else:
                # Slice: fill current bucket to V, then start next bucket
                curr_filled += needed
                curr_b_vol += needed * b_ratio
                curr_s_vol += needed * s_ratio
                rem_vol -= needed
                buckets.append({
                    "buy_vol": curr_b_vol,
                    "sell_vol": curr_s_vol,
                    "imbalance": abs(curr_b_vol - curr_s_vol),
                    "total_vol": curr_filled,
                })
                curr_b_vol = 0.0
                curr_s_vol = 0.0
                curr_filled = 0.0

    # If the last bucket has meaningful volume, include it
    if curr_filled > (bucket_vol * 0.25):
        buckets.append({
            "buy_vol": curr_b_vol,
            "sell_vol": curr_s_vol,
            "imbalance": abs(curr_b_vol - curr_s_vol) * (bucket_vol / max(curr_filled, 1e-6)),
            "total_vol": bucket_vol,
        })

    if not buckets:
        # Fallback to single aggregated bucket
        total_b = sum(it["buy_vol"] for it in items)
        total_s = sum(it["sell_vol"] for it in items)
        imb = abs(total_b - total_s)
        vpin_raw = imb / max(total_vol, 1e-6)
        buckets = [{
            "buy_vol": total_b,
            "sell_vol": total_s,
            "imbalance": imb,
            "total_vol": total_vol,
        }]
    else:
        # Evaluate over the last N buckets (up to num_buckets)
        eval_buckets = buckets[-num_buckets:]
        sum_imbalances = sum(b["imbalance"] for b in eval_buckets)
        sum_total_vol = sum(b["total_vol"] for b in eval_buckets)
        vpin_raw = sum_imbalances / max(sum_total_vol, 1e-6)

    # VPIN is bounded between 0.0 and 1.0
    vpin_val = max(0.0, min(1.0, vpin_raw))
    vpin_pct = round(vpin_val * 100.0, 2)

    # Toxicity Regime Classification & Adverse Selection Risk
    if vpin_val < 0.25:
        regime = "LOW_TOXICITY"
        status = "UNINFORMED_FLOW_DOMINANT"
        adverse_risk = "LOW"
        warning = None
    elif vpin_val < 0.45:
        regime = "NORMAL_FLOW"
        status = "STANDARD_LIQUIDITY_PROVISION"
        adverse_risk = "MODERATE"
        warning = None
    elif vpin_val < 0.60:
        regime = "ELEVATED_TOXICITY"
        status = "INFORMED_FLOW_ACTIVE"
        adverse_risk = "HIGH"
        warning = "Informed market participants active. Expect volatility expansion or aggressive sweeps."
    else:
        regime = "TOXIC_SURGE"
        status = "SEVERE_ADVERSE_SELECTION"
        adverse_risk = "CRITICAL"
        warning = "Order book toxicity alert (VPIN > 60%). Passive limit orders risk immediate adverse fill."

    # Average buy ratio across buckets
    total_eval_b = sum(b["buy_vol"] for b in buckets[-num_buckets:])
    total_eval_vol = sum(b["total_vol"] for b in buckets[-num_buckets:])
    avg_buy_ratio = round(total_eval_b / max(total_eval_vol, 1e-6), 4)

    return {
        "vpin": round(vpin_val, 4),
        "vpin_pct": vpin_pct,
        "regime": regime,
        "toxicity_status": status,
        "num_buckets": len(buckets[-num_buckets:]),
        "bucket_volume": round(bucket_vol, 4),
        "bucket_imbalances": [round(b["imbalance"], 2) for b in buckets[-num_buckets:]],
        "avg_buy_ratio": avg_buy_ratio,
        "informed_arrival_estimate": round(vpin_val, 4),
        "adverse_selection_risk": adverse_risk,
        "warning": warning,
    }


# --------------------------------------------------------------------------- Wyckoff 2.0 & CVD Divergence

def detect_wyckoff_cvd_divergence(
    klines_1h: List[List[Any]],
    klines_15m: Optional[List[List[Any]]] = None,
    vp: Optional[Dict[str, float]] = None,
    current_price: Optional[float] = None,
) -> Dict[str, Any]:
    """Algorithmic Wyckoff 2.0 Liquidity Sweep and CVD Divergence Detector.

    Identifies:
      1. Wyckoff Springs: Sweep below prior swing low or 7d VAL followed by rapid reclaim
         and positive taker buy absorption.
      2. Wyckoff Upthrusts (SFPs): Sweep above prior swing high or 7d VAH followed by
         rejection back inside range and negative taker distribution.
      3. CVD Absorption / Exhaustion Divergences across rolling windows.
    """
    signals: List[str] = []
    detected_patterns: List[Dict[str, Any]] = []
    bonus_score = 0.0

    if not klines_1h or len(klines_1h) < 16:
        return {
            "signals": signals,
            "patterns": detected_patterns,
            "bonus_score": 0.0,
            "dominant_regime": "NEUTRAL",
            "cvd_slope_ratio": 1.0,
        }

    closes = [float(k[4]) for k in klines_1h]
    highs = [float(k[2]) for k in klines_1h]
    lows = [float(k[3]) for k in klines_1h]
    vols = [float(k[5]) for k in klines_1h]
    taker_buys = [float(k[9]) if len(k) > 9 else (vols[i] * 0.5) for i, k in enumerate(klines_1h)]

    price = current_price if current_price is not None else closes[-1]

    # Calculate Cumulative Volume Delta (CVD) series
    deltas = [taker_buys[i] - (vols[i] - taker_buys[i]) for i in range(len(klines_1h))]
    cvd_series = []
    c = 0.0
    for d in deltas:
        c += d
        cvd_series.append(c)

    # 1. Wyckoff Spring (Bullish Liquidity Sweep & SFP)
    # Check swing low over prior 24 bars (excluding the most recent 3 bars)
    if len(klines_1h) >= 24:
        lookback = min(36, len(klines_1h))
        prior_low = min(lows[-lookback:-3])
        recent_low = min(lows[-3:])

        val_level = vp.get("val") if vp else None
        target_support = val_level if (val_level and val_level > 0 and abs(val_level - prior_low) / prior_low < 0.03) else prior_low

        # Spring condition: recent low swept below target_support, but current price reclaimed above
        if recent_low < target_support and price > target_support:
            sweep_depth_pct = round((target_support - recent_low) / target_support * 100.0, 2)
            recent_taker_buys = sum(taker_buys[-3:])
            recent_total_vol = sum(vols[-3:])
            buy_pct = (recent_taker_buys / recent_total_vol * 100.0) if recent_total_vol > 0 else 50.0

            pat = {
                "pattern": "WYCKOFF_SPRING",
                "type": "BULLISH_LIQUIDITY_SWEEP",
                "reference_level": round(target_support, 4),
                "sweep_low": round(recent_low, 4),
                "reclaim_price": round(price, 4),
                "sweep_depth_pct": sweep_depth_pct,
                "absorbed_taker_buy_pct": round(buy_pct, 1),
                "confidence_score": 88 if buy_pct >= 51.0 else 72,
            }
            detected_patterns.append(pat)
            signals.append(
                "WYCKOFF SPRING (Bullish Liquidity Sweep): Swept swing low/VAL $%.2f (wicked to $%.2f, -%.2f%%) and rapidly reclaimed $%.2f with %.1f%% taker absorption"
                % (target_support, recent_low, sweep_depth_pct, price, buy_pct)
            )
            bonus_score += 6.0

    # 2. Wyckoff Upthrust / SFP (Bearish Stop-Hunt)
    if len(klines_1h) >= 24:
        lookback = min(36, len(klines_1h))
        prior_high = max(highs[-lookback:-3])
        recent_high = max(highs[-3:])

        vah_level = vp.get("vah") if vp else None
        target_resistance = vah_level if (vah_level and vah_level > 0 and abs(vah_level - prior_high) / prior_high < 0.03) else prior_high

        # Upthrust condition: recent high swept above resistance, but current price rejected below
        if recent_high > target_resistance and price < target_resistance:
            sweep_height_pct = round((recent_high - target_resistance) / target_resistance * 100.0, 2)
            recent_taker_buys = sum(taker_buys[-3:])
            recent_total_vol = sum(vols[-3:])
            buy_pct = (recent_taker_buys / recent_total_vol * 100.0) if recent_total_vol > 0 else 50.0

            pat = {
                "pattern": "WYCKOFF_UPTHRUST",
                "type": "BEARISH_STOP_HUNT_SFP",
                "reference_level": round(target_resistance, 4),
                "sweep_high": round(recent_high, 4),
                "rejection_price": round(price, 4),
                "sweep_height_pct": sweep_height_pct,
                "absorbed_taker_buy_pct": round(buy_pct, 1),
                "confidence_score": 88 if buy_pct <= 49.0 else 72,
            }
            detected_patterns.append(pat)
            signals.append(
                "WYCKOFF UPTHRUST / SFP (Bearish Stop-Hunt): Swept swing high/VAH $%.2f (spiked to $%.2f, +%.2f%%) and rejected back inside range to $%.2f (exhaustion buy %.1f%%)"
                % (target_resistance, recent_high, sweep_height_pct, price, buy_pct)
            )
            bonus_score -= 6.0

    # 3. Multi-Window CVD Absorption vs Exhaustion Divergence
    # Compare first 8 bars vs last 8 bars in 16-bar window
    w1_prices, w2_prices = closes[-16:-8], closes[-8:]
    w1_cvd, w2_cvd = cvd_series[-16:-8], cvd_series[-8:]

    p_change_pct = (w2_prices[-1] - w1_prices[-1]) / w1_prices[-1] * 100.0
    cvd_change = w2_cvd[-1] - w1_cvd[-1]

    # CVD Absorption (Bullish): Price fell but CVD rose
    if p_change_pct <= -0.4 and cvd_change > 0:
        pat = {
            "pattern": "BULLISH_CVD_ABSORPTION_DIVERGENCE",
            "type": "ACCUMULATION",
            "price_change_pct": round(p_change_pct, 2),
            "cvd_change": round(cvd_change, 2),
            "confidence_score": 80,
        }
        detected_patterns.append(pat)
        signals.append(
            "BULLISH CVD ABSORPTION DIVERGENCE: Price drifted lower (%.2f%%) while net CVD expanded (+%.1f units), confirming passive limit accumulation"
            % (p_change_pct, cvd_change)
        )
        bonus_score += 4.0

    # CVD Exhaustion (Bearish): Price rose but CVD fell
    elif p_change_pct >= 0.4 and cvd_change < 0:
        pat = {
            "pattern": "BEARISH_CVD_EXHAUSTION_DIVERGENCE",
            "type": "DISTRIBUTION",
            "price_change_pct": round(p_change_pct, 2),
            "cvd_change": round(cvd_change, 2),
            "confidence_score": 80,
        }
        detected_patterns.append(pat)
        signals.append(
            "BEARISH CVD EXHAUSTION DIVERGENCE: Price pressed higher (+%.2f%%) while net CVD declined (%+.1f units), signaling passive limit distribution"
            % (p_change_pct, cvd_change)
        )
        bonus_score -= 4.0

    dominant_regime = "NEUTRAL"
    if bonus_score >= 4.0:
        dominant_regime = "BULLISH_ACCUMULATION"
    elif bonus_score <= -4.0:
        dominant_regime = "BEARISH_DISTRIBUTION"

    return {
        "signals": signals,
        "patterns": detected_patterns,
        "bonus_score": round(max(-10.0, min(10.0, bonus_score)), 1),
        "dominant_regime": dominant_regime,
        "net_cvd_16h": round(cvd_change, 2),
    }


# --------------------------------------------------------------------------- Volatility-Adaptive Kelly Engine

def compute_adaptive_kelly(
    win_prob: float,
    reward_risk: float,
    atr_1h: float,
    atr_baseline: float,
    bb_width_pct: float,
    is_ttm_squeeze: bool,
    vpin: float,
    bias: str = "LONG",
    capital_usdt: float = 10000.0,
    max_loss_cap: float = 150.0,
) -> Dict[str, Any]:
    """Compute Volatility-Adaptive Fractional Kelly Position Sizing.

    Adjusts standard Quarter-Kelly based on:
      1. TTM Squeeze Coiling: Explosive breakout multiplier (1.15x - 1.20x) or mean-reversion discount (0.70x).
      2. ATR Expansion Shock Ratio: Inversely dampens risk when candle volatility spikes (1 / sqrt(R_atr)).
      3. VPIN Toxicity Adjustment: Slashes risk (0.50x) when fighting toxic informed flow, or ensures disciplined sizing.
    """
    # 1. Base Full-Kelly & Quarter-Kelly
    # Kelly Formula: f* = p - (1 - p) / b
    b = max(0.5, float(reward_risk))
    p = max(0.01, min(0.99, float(win_prob)))
    full_kelly = max(0.0, p - (1.0 - p) / b)
    quarter_kelly = full_kelly * 0.25

    # Base quarter Kelly percentage (capped between 0.0% and 5.0%)
    base_quarter_pct = round(min(5.0, quarter_kelly * 100.0), 2)

    # 2. TTM Squeeze Multiplier (kappa_squeeze)
    if is_ttm_squeeze:
        if bias in ("LONG", "SHORT / HEDGE"):
            kappa_squeeze = 1.20
            squeeze_regime = "TTM_SQUEEZE_COILING (Directional Breakout Fuel)"
        else:
            kappa_squeeze = 1.05
            squeeze_regime = "TTM_SQUEEZE_RANGE"
    elif bb_width_pct < 3.5:
        kappa_squeeze = 1.15
        squeeze_regime = "TIGHT_COMPRESSION_COILING"
    elif bb_width_pct > 12.0:
        kappa_squeeze = 0.70
        squeeze_regime = "HYPER_EXPANSION_MEAN_REVERSION_RISK"
    else:
        kappa_squeeze = 1.00
        squeeze_regime = "NORMAL_VOLATILITY"

    # 3. ATR Expansion Shock Multiplier (kappa_atr)
    atr_base = max(atr_baseline, 1e-6)
    atr_ratio = max(0.1, atr_1h / atr_base)

    if atr_ratio > 1.25:
        # Volatility shock: Candle range is expanding rapidly; dampen sizing to prevent dollar blowout
        kappa_atr = max(0.50, min(1.0, 1.0 / math.sqrt(atr_ratio)))
        atr_regime = "EXPANDING_VOLATILITY_DAMPENED"
    elif atr_ratio < 0.80:
        # Subdued quiet range
        kappa_atr = 1.10
        atr_regime = "COMPRESSED_ATR_EXPANSION_POTENTIAL"
    else:
        kappa_atr = 1.00
        atr_regime = "BALANCED_ATR"

    # 4. VPIN Order-Flow Toxicity Multiplier (kappa_vpin)
    vpin_val = max(0.0, min(1.0, float(vpin)))
    if vpin_val >= 0.60:
        # High toxicity: If trade bias matches directional momentum, allow disciplined entry;
        # if trade is range/mean-reversion or counter-trend, slash Kelly in half
        if bias in ("LONG", "SHORT / HEDGE"):
            kappa_vpin = 1.00
            vpin_regime = "TOXIC_SURGE_ALIGNMENT"
        else:
            kappa_vpin = 0.50
            vpin_regime = "TOXIC_SURGE_COUNTER_TREND_PENALTY"
    elif vpin_val < 0.25:
        kappa_vpin = 1.00
        vpin_regime = "BENIGN_NOISE_LIQUIDITY"
    else:
        kappa_vpin = 1.00
        vpin_regime = "NORMAL_TOXICITY"

    # 5. Composite Volatility-Adaptive Kelly
    composite_multiplier = round(kappa_squeeze * kappa_atr * kappa_vpin, 3)
    adaptive_kelly_pct = round(max(0.25, min(5.0, base_quarter_pct * composite_multiplier)), 2)

    # Dollar sizing bounded by max loss cap and account capital
    calculated_risk_usd = round(min(capital_usdt * (adaptive_kelly_pct / 100.0), max_loss_cap), 2)

    rationale_parts = []
    if kappa_squeeze != 1.0:
        rationale_parts.append("Squeeze multiplier %.2fx (%s)" % (kappa_squeeze, squeeze_regime))
    if kappa_atr != 1.0:
        rationale_parts.append("ATR multiplier %.2fx (%s)" % (kappa_atr, atr_regime))
    if kappa_vpin != 1.0:
        rationale_parts.append("VPIN toxicity multiplier %.2fx (%s)" % (kappa_vpin, vpin_regime))

    rationale_str = "; ".join(rationale_parts) if rationale_parts else "Standard Quarter-Kelly in balanced volatility regime"

    return {
        "base_full_kelly": round(full_kelly, 4),
        "base_quarter_kelly_pct": base_quarter_pct,
        "adaptive_kelly_pct": adaptive_kelly_pct,
        "composite_multiplier": composite_multiplier,
        "squeeze_multiplier": kappa_squeeze,
        "atr_multiplier": round(kappa_atr, 3),
        "vpin_multiplier": kappa_vpin,
        "atr_ratio": round(atr_ratio, 2),
        "volatility_regime": squeeze_regime,
        "calculated_risk_usd": calculated_risk_usd,
        "sizing_rationale": rationale_str,
    }
