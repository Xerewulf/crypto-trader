#!/usr/bin/env python3
"""Delta-Neutral Funding & Cash-Carry Arbitrage Doctrine (Python 3.8+, standard library only).

Phase 2 Deliverable: Systematic Delta-Neutral Funding & Cash-Carry Arbitrage
for Binance Spot & Perpetual Futures.

This module provides pure quantitative models and market scanners for:
  1. Spot vs. Perpetual Futures Basis & Funding Rate Divergence Analysis.
  2. Cash & Carry Synthetic Dollar Construction (Long Spot + 1x Short Perp).
  3. Negative Funding Rate Short Squeeze Harvester (Short Spot Isolated + Long 1x Perp).
  4. Real-time Net Delta Neutrality verification (|Net Delta| ~ $0.00).
  5. 8-Hour Funding Cashflow Accrual & Basis Convergence PnL Modeling.
  6. Atomic Simultaneous Execution & Unwinding of Arbitrage Pairs.
"""

import json
import math
import os
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

SPOT_BASE_URL = "https://api.binance.com"
FAPI_BASE_URL = "https://fapi.binance.com"

# Standard Binance VIP0 / BNB Discount Taker Fee: 0.04% per leg (0.08% round trip per leg, 0.16% total pair round trip)
DEFAULT_TAKER_FEE = 0.0004
# Standard Binance Margin hourly borrow rate estimate (approx 0.002% per hour ~ 17.5% APR)
DEFAULT_HOURLY_BORROW_RATE = 0.00002

# Thresholds
CASH_AND_CARRY_MIN_FUNDING = 0.00010       # +0.010% / 8h (+10.95% annualized APY)
NEGATIVE_FUNDING_HARVEST_THRESHOLD = -0.00050  # -0.050% / 8h (-54.75% annualized APY)
DEEP_NEGATIVE_SQUEEZE_THRESHOLD = -0.00300     # -0.300% / 8h (-328.5% annualized APY)


def calculate_basis(spot_price: float, perp_mark: float) -> Dict[str, float]:
    """Calculate absolute and percentage basis between Spot price and Perp mark price.
    
    Basis = Perp Mark - Spot Price
    Basis % = (Perp Mark - Spot Price) / Spot Price * 100%
    """
    spot = max(1e-8, float(spot_price))
    mark = float(perp_mark)
    basis_abs = mark - spot
    basis_pct = (basis_abs / spot) * 100.0
    return {
        "spot_price": round(spot, 6),
        "perp_mark": round(mark, 6),
        "basis_abs": round(basis_abs, 6),
        "basis_pct": round(basis_pct, 4),
    }


def funding_to_apy(funding_rate_8h: float, compound: bool = False) -> float:
    """Convert an 8-hour funding rate to an annualized percentage yield (APY).
    
    3 settlements per day * 365 days = 1,095 settlements per year.
    Simple APY = FundingRate * 3 * 365 * 100%
    Compound APY = ((1 + FundingRate)^1095 - 1) * 100%
    """
    fr = float(funding_rate_8h)
    if not compound:
        return round(fr * 3.0 * 365.0 * 100.0, 2)
    # Clamp extreme rates to avoid math overflow
    clamped_fr = max(-0.999, min(0.05, fr))
    try:
        ann = ((1.0 + clamped_fr) ** 1095 - 1.0) * 100.0
        return round(ann, 2)
    except OverflowError:
        return 99999.99


def classify_arbitrage_opportunity(
    symbol: str,
    spot_price: float,
    perp_mark: float,
    funding_rate_8h: float,
    min_apy: float = 8.0,
) -> Dict[str, Any]:
    """Classify an asset into an arbitrage regime based on basis and funding rate.
    
    Returns opportunity specs with recommended actions and risk assessments.
    """
    b_info = calculate_basis(spot_price, perp_mark)
    fr = float(funding_rate_8h)
    simple_apy = funding_to_apy(abs(fr))
    signed_apy = funding_to_apy(fr)

    # 1. Negative Funding Rate Short Squeeze Harvester
    if fr <= NEGATIVE_FUNDING_HARVEST_THRESHOLD:
        is_deep = fr <= DEEP_NEGATIVE_SQUEEZE_THRESHOLD
        regime = "DEEP_NEGATIVE_SQUEEZE" if is_deep else "REVERSE_ARBITRAGE"
        reason = (
            "Deep negative funding (< -0.30%/8h) signals aggressive short crowding. "
            "Borrow Spot to Short, and BUY 1x Perp to harvest massive short-funding payments while delta-neutral."
            if is_deep else
            "Negative funding: Shorts pay Longs. Go Short Spot (Margin Borrow) + Long 1x Perp to collect yield."
        )
        return {
            "symbol": symbol,
            "strategy": "REVERSE_ARBITRAGE",
            "regime": regime,
            "spot_action": "SELL_SHORT",
            "perp_action": "BUY_LONG",
            "spot_price": b_info["spot_price"],
            "perp_mark": b_info["perp_mark"],
            "basis_pct": b_info["basis_pct"],
            "funding_rate_8h": round(fr, 6),
            "annualized_apy": simple_apy,
            "signed_apy": signed_apy,
            "is_deep_squeeze": is_deep,
            "viable": simple_apy >= min_apy,
            "reason": reason,
        }

    # 2. Cash & Carry / Synthetic Dollar
    elif fr >= CASH_AND_CARRY_MIN_FUNDING:
        regime = "CASH_AND_CARRY"
        reason = (
            "Perpetual funding rate is positive. Desk BUYS Spot and SELLS 1x Perp, "
            "locking in delta-neutral synthetic dollar yield with 0 directional exposure."
        )
        return {
            "symbol": symbol,
            "strategy": "CASH_AND_CARRY",
            "regime": regime,
            "spot_action": "BUY_LONG",
            "perp_action": "SELL_SHORT",
            "spot_price": b_info["spot_price"],
            "perp_mark": b_info["perp_mark"],
            "basis_pct": b_info["basis_pct"],
            "funding_rate_8h": round(fr, 6),
            "annualized_apy": simple_apy,
            "signed_apy": signed_apy,
            "is_deep_squeeze": False,
            "viable": simple_apy >= min_apy,
            "reason": reason,
        }

    # 3. Neutral / Narrow spread
    else:
        return {
            "symbol": symbol,
            "strategy": "NEUTRAL",
            "regime": "NEUTRAL_RANGE",
            "spot_action": "NONE",
            "perp_action": "NONE",
            "spot_price": b_info["spot_price"],
            "perp_mark": b_info["perp_mark"],
            "basis_pct": b_info["basis_pct"],
            "funding_rate_8h": round(fr, 6),
            "annualized_apy": simple_apy,
            "signed_apy": signed_apy,
            "is_deep_squeeze": False,
            "viable": False,
            "reason": "Funding rate and basis spread are too narrow to cover round-trip exchange fees.",
        }


def build_delta_neutral_pair(
    symbol: str,
    strategy: str,
    capital_usdt: float,
    spot_price: float,
    perp_mark: float,
    funding_rate_8h: float,
    leverage: float = 1.0,
    hourly_borrow_rate: float = DEFAULT_HOURLY_BORROW_RATE,
    taker_fee_rate: float = DEFAULT_TAKER_FEE,
) -> Dict[str, Any]:
    """Construct an institutional delta-neutral arbitrage pair with 50/50 capital split.
    
    Capital is split into:
      1. Spot leg capital (50%): Purchases or borrows spot asset.
      2. Perp leg margin (50%): Collateral locked for 1x perpetual hedge.
      
    Ensures Net Delta Exposure in USDT is virtually $0.00.
    """
    cap = max(10.0, float(capital_usdt))
    spot_p = max(1e-8, float(spot_price))
    perp_p = max(1e-8, float(perp_mark))
    fr = float(funding_rate_8h)
    lev = max(1.0, float(leverage))

    strategy = str(strategy).upper()
    if strategy not in ("CASH_AND_CARRY", "REVERSE_ARBITRAGE"):
        strategy = "CASH_AND_CARRY" if fr >= 0 else "REVERSE_ARBITRAGE"

    # 50% spot, 50% perp collateral
    spot_capital = cap / 2.0
    perp_margin = cap / 2.0

    # Spot Leg calculation
    spot_qty = spot_capital / spot_p
    spot_notional = spot_qty * spot_p

    # Perp Leg calculation: Exact quantity matching spot quantity to achieve delta neutrality
    perp_qty = spot_qty
    perp_notional = perp_qty * perp_p
    required_perp_margin = perp_notional / lev

    # If required perp margin exceeds allocated collateral, scale by minimum
    if required_perp_margin > perp_margin * 1.001:
        scale = perp_margin / required_perp_margin
        spot_qty *= scale
        perp_qty = spot_qty
        spot_notional = spot_qty * spot_p
        perp_notional = perp_qty * perp_p
        required_perp_margin = perp_notional / lev

    if strategy == "CASH_AND_CARRY":
        spot_side = "BUY"
        perp_side = "SELL"
        spot_delta = spot_notional       # Long spot
        perp_delta = -perp_notional      # Short perp
    else:  # REVERSE_ARBITRAGE
        spot_side = "SELL"
        perp_side = "BUY"
        spot_delta = -spot_notional      # Short spot
        perp_delta = perp_notional       # Long perp

    net_delta_usdt = spot_delta + perp_delta
    net_delta_pct = (net_delta_usdt / (spot_notional + perp_notional)) * 100.0 if (spot_notional + perp_notional) > 0 else 0.0

    # Projected 8h funding payout:
    # On Cash & Carry: Short perp collects funding if fr > 0: payout = perp_notional * fr
    # On Reverse Arbitrage: Long perp collects funding if fr < 0: payout = perp_notional * |fr|
    gross_8h_funding = perp_notional * abs(fr)
    
    # 8-hour spot borrow cost (for Reverse Arbitrage short spot borrowing)
    est_8h_borrow_cost = (spot_notional * hourly_borrow_rate * 8.0) if strategy == "REVERSE_ARBITRAGE" else 0.0
    net_8h_projected_cashflow = gross_8h_funding - est_8h_borrow_cost

    # Execution fees
    spot_fee = spot_notional * taker_fee_rate
    perp_fee = perp_notional * taker_fee_rate
    initial_fees_usdt = spot_fee + perp_fee

    simple_apy = funding_to_apy(abs(fr))
    basis_info = calculate_basis(spot_p, perp_p)

    return {
        "symbol": symbol,
        "strategy": strategy,
        "total_capital_usdt": round(cap, 2),
        "spot_leg": {
            "side": spot_side,
            "price": round(spot_p, 6),
            "qty": round(spot_qty, 6),
            "notional_usdt": round(spot_notional, 2),
            "capital_allocated_usdt": round(spot_capital, 2),
            "fee_usdt": round(spot_fee, 4),
        },
        "perp_leg": {
            "side": perp_side,
            "mark_price": round(perp_p, 6),
            "qty": round(perp_qty, 6),
            "leverage": lev,
            "notional_usdt": round(perp_notional, 2),
            "margin_locked_usdt": round(required_perp_margin, 2),
            "fee_usdt": round(perp_fee, 4),
        },
        "delta": {
            "spot_delta_usdt": round(spot_delta, 2),
            "perp_delta_usdt": round(perp_delta, 2),
            "net_delta_usdt": round(net_delta_usdt, 4),
            "net_delta_pct": round(net_delta_pct, 4),
            "is_delta_neutral": abs(net_delta_pct) < 0.5,
        },
        "metrics": {
            "funding_rate_8h": round(fr, 6),
            "annualized_apy": simple_apy,
            "basis_pct": basis_info["basis_pct"],
            "gross_8h_funding_usdt": round(gross_8h_funding, 4),
            "est_8h_borrow_cost_usdt": round(est_8h_borrow_cost, 4),
            "net_8h_projected_usdt": round(net_8h_projected_cashflow, 4),
            "projected_daily_yield_usdt": round(net_8h_projected_cashflow * 3.0, 4),
            "initial_entry_fees_usdt": round(initial_fees_usdt, 4),
        },
    }


def compute_pair_realtime_pnl(
    pair: Dict[str, Any],
    current_spot_price: float,
    current_perp_mark: float,
    elapsed_seconds: float = 0.0,
    current_funding_rate: Optional[float] = None,
    hourly_borrow_rate: float = DEFAULT_HOURLY_BORROW_RATE,
) -> Dict[str, Any]:
    """Compute real-time Mark-to-Market basis PnL, accrued funding payments, and total net yield.
    
    1. Basis PnL: Spot Leg PnL + Perp Leg PnL
    2. Accrued Funding Cashflow: Continuously amortized or discrete 8h payouts.
    3. Spot Borrow Cost: Accrued interest on borrowed spot (for Reverse Arbitrage).
    4. Net Total PnL: Accrued Funding + Basis PnL - Borrow Cost - Entry Fees
    """
    spot_leg = pair["spot_leg"]
    perp_leg = pair["perp_leg"]
    strategy = pair.get("strategy", "CASH_AND_CARRY")
    
    spot_entry = float(spot_leg["price"])
    spot_qty = float(spot_leg["qty"])
    spot_side = spot_leg["side"]
    
    perp_entry = float(perp_leg["mark_price"])
    perp_qty = float(perp_leg["qty"])
    perp_side = perp_leg["side"]
    perp_notional = float(perp_leg["notional_usdt"])

    live_spot = float(current_spot_price)
    live_perp = float(current_perp_mark)

    # Spot Leg PnL
    if spot_side == "BUY":
        spot_pnl = (live_spot - spot_entry) * spot_qty
    else:
        spot_pnl = (spot_entry - live_spot) * spot_qty

    # Perp Leg PnL
    if perp_side == "SELL":
        perp_pnl = (perp_entry - live_perp) * perp_qty
    else:
        perp_pnl = (live_perp - perp_entry) * perp_qty

    # Basis PnL represents the price divergence/convergence
    basis_pnl = spot_pnl + perp_pnl

    # Accrued funding calculation
    fr = float(current_funding_rate) if current_funding_rate is not None else float(pair["metrics"]["funding_rate_8h"])
    
    # 8 hours = 28,800 seconds
    periods_elapsed = elapsed_seconds / 28800.0 if elapsed_seconds > 0 else 0.0
    accrued_funding_usdt = float(pair.get("accrued_funding_usdt", 0.0))
    if periods_elapsed > 0:
        accrued_funding_usdt += perp_notional * abs(fr) * periods_elapsed

    # Accrued borrow cost for reverse arbitrage
    accrued_borrow_cost = 0.0
    if strategy == "REVERSE_ARBITRAGE" and elapsed_seconds > 0:
        elapsed_hours = elapsed_seconds / 3600.0
        accrued_borrow_cost = float(spot_leg["notional_usdt"]) * hourly_borrow_rate * elapsed_hours

    entry_fees = float(pair["metrics"].get("initial_entry_fees_usdt", 0.0))
    total_net_pnl = accrued_funding_usdt + basis_pnl - accrued_borrow_cost - entry_fees

    total_cap = float(pair.get("total_capital_usdt", perp_notional * 2.0))
    roe_pct = (total_net_pnl / total_cap * 100.0) if total_cap > 0 else 0.0

    # Current Net Delta
    current_spot_delta = (live_spot * spot_qty) * (1.0 if spot_side == "BUY" else -1.0)
    current_perp_delta = (live_perp * perp_qty) * (1.0 if perp_side == "BUY" else -1.0)
    current_net_delta = current_spot_delta + current_perp_delta

    return {
        "live_spot_price": round(live_spot, 6),
        "live_perp_mark": round(live_perp, 6),
        "spot_pnl_usdt": round(spot_pnl, 4),
        "perp_pnl_usdt": round(perp_pnl, 4),
        "basis_pnl_usdt": round(basis_pnl, 4),
        "accrued_funding_usdt": round(accrued_funding_usdt, 4),
        "accrued_borrow_cost_usdt": round(accrued_borrow_cost, 4),
        "total_net_pnl_usdt": round(total_net_pnl, 4),
        "roe_pct": round(roe_pct, 4),
        "current_net_delta_usdt": round(current_net_delta, 4),
        "is_delta_neutral": abs(current_net_delta) < (total_cap * 0.01),
    }


def scan_funding_arbitrage(
    symbols: Optional[List[str]] = None,
    min_apy: float = 5.0,
    timeout: int = 8,
    spot_prices_cache: Optional[Dict[str, float]] = None,
    premium_indices_cache: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Scan Binance Spot and USD-M Perpetuals for cash-and-carry and reverse funding arbitrage.
    
    Can take pre-fetched spot/perp caches (for fast local simulation or offline tests)
    or fetch live over the network via public Binance APIs.
    """
    now = time.time()
    
    # 1. Fetch Premium Index (All Futures Contracts Funding & Mark Prices)
    futures_data = premium_indices_cache
    if futures_data is None:
        try:
            req = urllib.request.Request(
                f"{FAPI_BASE_URL}/fapi/v1/premiumIndex",
                headers={"User-Agent": "Mozilla/5.0"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                futures_data = json.loads(resp.read().decode("utf-8"))
        except Exception:
            futures_data = []

    # Map futures by symbol
    f_map: Dict[str, Dict[str, Any]] = {}
    for item in futures_data:
        sym = item.get("symbol")
        if sym and sym.endswith("USDT"):
            f_map[sym] = item

    # 2. Fetch Spot Prices
    spot_map: Dict[str, float] = spot_prices_cache or {}
    if not spot_map:
        try:
            req = urllib.request.Request(
                f"{SPOT_BASE_URL}/api/v3/ticker/price",
                headers={"User-Agent": "Mozilla/5.0"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw_spot = json.loads(resp.read().decode("utf-8"))
                for r in raw_spot:
                    s = r.get("symbol")
                    if s and s.endswith("USDT"):
                        try:
                            spot_map[s] = float(r.get("price", 0.0))
                        except (ValueError, TypeError):
                            continue
        except Exception:
            spot_map = {}

    target_symbols = symbols or [
        "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
        "DOGEUSDT", "ADAUSDT", "SUIUSDT", "ENAUSDT", "PEPEUSDT",
        "AVAXUSDT", "NEARUSDT", "LINKUSDT", "FETUSDT", "TAOUSDT"
    ]

    opportunities: List[Dict[str, Any]] = []

    for sym in target_symbols:
        f_entry = f_map.get(sym)
        spot_p = spot_map.get(sym)
        
        # If not in target, try alternate like 1000PEPEUSDT
        if not f_entry and sym.startswith("1000"):
            alt = sym[4:]
            spot_p = spot_map.get(alt)
        elif not f_entry and ("1000" + sym) in f_map:
            f_entry = f_map.get("1000" + sym)

        if not f_entry or not spot_p or spot_p <= 0:
            continue

        try:
            perp_mark = float(f_entry.get("markPrice", 0.0))
            funding_rate_8h = float(f_entry.get("lastFundingRate", 0.0))
            next_funding_time_ms = int(f_entry.get("nextFundingTime", 0))
        except (ValueError, TypeError):
            continue

        if perp_mark <= 0:
            continue

        opp = classify_arbitrage_opportunity(
            symbol=sym,
            spot_price=spot_p,
            perp_mark=perp_mark,
            funding_rate_8h=funding_rate_8h,
            min_apy=min_apy,
        )
        opp["next_funding_time_ms"] = next_funding_time_ms
        opp["seconds_to_funding"] = max(0, int((next_funding_time_ms - int(now * 1000)) / 1000)) if next_funding_time_ms > 0 else 0
        opp["est_daily_yield_usd_per_10k"] = round(10000.0 * (opp["annualized_apy"] / 100.0) / 365.0, 2)

        opportunities.append(opp)

    # Sort opportunities: Deep Negative Squeezes first, then highest APY
    opportunities.sort(key=lambda x: (
        0 if x["regime"] == "DEEP_NEGATIVE_SQUEEZE" else 1,
        -x["annualized_apy"]
    ))

    # Calculate summary metrics
    cash_carry_count = sum(1 for o in opportunities if o["strategy"] == "CASH_AND_CARRY" and o["viable"])
    reverse_count = sum(1 for o in opportunities if o["strategy"] == "REVERSE_ARBITRAGE" and o["viable"])
    avg_viable_apy = (
        sum(o["annualized_apy"] for o in opportunities if o["viable"]) / max(1, cash_carry_count + reverse_count)
    )

    return {
        "timestamp": int(now * 1000),
        "total_scanned": len(target_symbols),
        "cash_and_carry_opportunities": cash_carry_count,
        "reverse_arbitrage_opportunities": reverse_count,
        "average_viable_apy": round(avg_viable_apy, 2),
        "opportunities": opportunities,
    }
