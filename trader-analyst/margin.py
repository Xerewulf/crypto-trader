#!/usr/bin/env python3
"""Margin-trading doctrine for Binance crypto (standard library only, Python 3.8+).

Pure functions shared by predict.py (the forecaster) and trader/live/paper_trader.py
(the paper margin engine). Nothing here touches the network or places an order.

What an institutional crypto margin desk checks before and during every leveraged trade:

  1. Size by the STOP, never by the leverage. Dollar risk = qty x |entry - stop|.
     Leverage only decides how much collateral is locked up and where the
     liquidation price sits. Pick the LOWEST leverage that fits the margin budget.
  2. Mandatory Liquidation Safety Buffer: Liquidation must sit far beyond the stop
     (at least 2.0x the stop distance), so a wick, mark/last divergence, or slippage
     cannot liquidate the position before the stop fills.
  3. Liquidity-tiered maximum leverage: BTC 5x, ETH 4x, large caps (SOL, BNB, XRP) 3x,
     and all other altcoins capped at 2x.
  4. Tiered Maintenance Margin Rates (MMR) & Deductions (CUM):
     Maintenance margin scales with position notional brackets.
     MM = (Notional x MMR) - CUM.
  5. Dual-Price Mechanism (Mark Price vs. Last Price):
     Liquidations on Binance and institutional exchanges trigger on MARK PRICE to
     neutralize local order-book illiquidity and stop-hunting wicks.
  6. Cumulative Carry Drag (Funding & Borrow Interest):
     Positive perpetual funding or hourly spot margin borrow interest erodes collateral.
     Desk models carry-adjusted liquidation prices over the expected holding duration.
  7. Position Health & Real-Time Margin Alerts:
     Tracks Spot Margin Level and Futures Margin Ratio with proactive thresholds:
     HEALTHY (>=1.50), MARGIN CALL warning (<=1.30), CRITICAL (<=1.15), LIQUIDATED (<=1.10).
  8. Auto-Deleveraging (ADL) Priority:
     High leverage and high profit place traders in high ADL quantiles. Lower leverage
     (1x-5x) protects institutional capital in Quantile 1 (safest priority).
  9. Macro & Scheduled Volatility Event Guards:
     FOMC rate decisions: BLOCK entries 2h before to 1h after; CAUTION (half risk, half lev)
     24h before to 6h after. CPI and NFP releases: BLOCK 1h before to 30m after.
     Monthly and quarterly Deribit options expiries: CAUTION 12h before to 2h after.
 10. Book-level correlation and gross leverage caps:
     Crypto is one trade under stress (correlations spike >0.90). Cap same-side positions
     at max 2, gross book leverage at 2.0x equity, and exclude tokenized equities (bStocks).
"""

import datetime
import math

# --------------------------------------------------------------------------- calendar

# FOMC statement days (second day of two-day meetings), Federal Reserve 2026 calendar.
# The statement is released at 2:00 p.m. New York time.
FOMC_DECISION_DAYS = (
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
)

# US Bureau of Labor Statistics (BLS) CPI (Consumer Price Index) 2026 release dates.
# The report is released at 8:30 a.m. New York time.
CPI_DECISION_DAYS = (
    "2026-01-14", "2026-02-11", "2026-03-11", "2026-04-14",
    "2026-05-13", "2026-06-10", "2026-07-15", "2026-08-12",
    "2026-09-15", "2026-10-14", "2026-11-12", "2026-12-11",
)

FOMC_BLOCK_PRE_H = 2.0      # no new entries this many hours before statement
FOMC_BLOCK_POST_H = 1.0     # ...until this many hours after it
FOMC_CAUTION_PRE_H = 24.0   # half risk / half leverage window starts
FOMC_CAUTION_POST_H = 6.0   # ...and ends

CPI_BLOCK_PRE_H = 1.0       # no new entries 1h before CPI release
CPI_BLOCK_POST_H = 0.5      # ...until 30m after CPI release
CPI_CAUTION_PRE_H = 12.0
CPI_CAUTION_POST_H = 2.0

EXPIRY_CAUTION_PRE_H = 12.0
EXPIRY_CAUTION_POST_H = 2.0

HOUR_MS = 3600 * 1000


def _nth_weekday(year, month, weekday, n):
    """n-th weekday (Mon=0) of a month; n=-1 for the last one."""
    if n > 0:
        d = datetime.date(year, month, 1)
        d += datetime.timedelta(days=(weekday - d.weekday()) % 7)
        return d + datetime.timedelta(weeks=n - 1)
    nxt = datetime.date(year + (month == 12), month % 12 + 1, 1)
    d = nxt - datetime.timedelta(days=1)
    return d - datetime.timedelta(days=(d.weekday() - weekday) % 7)


def us_dst(day):
    """True when New York is on daylight time (2nd Sunday of March to 1st Sunday of November)."""
    start = _nth_weekday(day.year, 3, 6, 2)
    end = _nth_weekday(day.year, 11, 6, 1)
    return start <= day < end


def fomc_decision_ms(day_str):
    """UTC epoch ms of the 2:00 p.m. New York FOMC statement on that day."""
    day = datetime.date.fromisoformat(day_str)
    hour_utc = 18 if us_dst(day) else 19
    dt = datetime.datetime(day.year, day.month, day.day, hour_utc, 0, tzinfo=datetime.timezone.utc)
    return int(dt.timestamp() * 1000)


def cpi_decision_ms(day_str):
    """UTC epoch ms of the 8:30 a.m. New York CPI report on that day."""
    day = datetime.date.fromisoformat(day_str)
    hour_utc = 12 if us_dst(day) else 13
    dt = datetime.datetime(day.year, day.month, day.day, hour_utc, 30, tzinfo=datetime.timezone.utc)
    return int(dt.timestamp() * 1000)


def nfp_decision_ms(year, month):
    """UTC epoch ms of US Non-Farm Payrolls (first Friday of the month, 8:30 a.m. New York)."""
    d = _nth_weekday(year, month, 4, 1)
    hour_utc = 12 if us_dst(d) else 13
    dt = datetime.datetime(d.year, d.month, d.day, hour_utc, 30, tzinfo=datetime.timezone.utc)
    return int(dt.timestamp() * 1000)


def monthly_expiry_ms(year, month):
    """Deribit monthly crypto options expiry: last Friday of the month, 08:00 UTC."""
    d = _nth_weekday(year, month, 4, -1)
    dt = datetime.datetime(d.year, d.month, d.day, 8, 0, tzinfo=datetime.timezone.utc)
    return int(dt.timestamp() * 1000)


def quarterly_expiry_ms(year, quarter):
    """Deribit quarterly crypto options expiry: last Friday of March, June, September, December, 08:00 UTC."""
    month = quarter * 3
    return monthly_expiry_ms(year, month)


def upcoming_events(now_ms, horizon_h=24 * 8, extra=None):
    """Known scheduled volatility events within the horizon, sorted by time."""
    out = []
    lo = now_ms - 12 * HOUR_MS
    hi = now_ms + int(horizon_h * HOUR_MS)
    for d in FOMC_DECISION_DAYS:
        t = fomc_decision_ms(d)
        if lo <= t <= hi:
            out.append({"name": "FOMC rate decision", "time_ms": t, "kind": "fomc"})
    for d in CPI_DECISION_DAYS:
        t = cpi_decision_ms(d)
        if lo <= t <= hi:
            out.append({"name": "US CPI inflation report", "time_ms": t, "kind": "cpi"})
    now = datetime.datetime.fromtimestamp(now_ms / 1000.0, datetime.timezone.utc)
    for k in range(0, 2):
        y, m = now.year + (now.month + k - 1) // 12, (now.month + k - 1) % 12 + 1
        t_exp = monthly_expiry_ms(y, m)
        if lo <= t_exp <= hi:
            kind_exp = "quarterly_expiry" if m in (3, 6, 9, 12) else "expiry"
            name_exp = "Deribit quarterly options expiry" if m in (3, 6, 9, 12) else "Deribit monthly options expiry"
            out.append({"name": name_exp, "time_ms": t_exp, "kind": kind_exp})
        t_nfp = nfp_decision_ms(y, m)
        if lo <= t_nfp <= hi:
            out.append({"name": "US Non-Farm Payrolls (NFP)", "time_ms": t_nfp, "kind": "nfp"})
    for e in extra or []:
        try:
            t = int(e["time_ms"])
        except (KeyError, TypeError, ValueError):
            continue
        if lo <= t <= hi:
            out.append({"name": str(e.get("name", "Scheduled event")), "time_ms": t, "kind": str(e.get("kind", "fomc"))})
    out.sort(key=lambda e: e["time_ms"])
    return out


def event_risk(now_ms, extra=None):
    """Return the strictest event regime right now.

    level: "BLOCK" (no new leveraged entries), "CAUTION" (half risk, half leverage), or "CLEAR".
    """
    best = {"level": "CLEAR", "risk_mult": 1.0, "lev_mult": 1.0, "event": None, "hours_to": None,
            "reason": "No scheduled macro or expiry event inside the risk window"}
    rank = {"CLEAR": 0, "CAUTION": 1, "BLOCK": 2}
    for e in upcoming_events(now_ms, extra=extra):
        h = (e["time_ms"] - now_ms) / float(HOUR_MS)
        level = "CLEAR"
        k = e["kind"]
        if k == "fomc":
            if -FOMC_BLOCK_POST_H <= h <= FOMC_BLOCK_PRE_H:
                level = "BLOCK"
            elif -FOMC_CAUTION_POST_H <= h <= FOMC_CAUTION_PRE_H:
                level = "CAUTION"
        elif k in ("cpi", "nfp"):
            if -CPI_BLOCK_POST_H <= h <= CPI_BLOCK_PRE_H:
                level = "BLOCK"
            elif -CPI_CAUTION_POST_H <= h <= CPI_CAUTION_PRE_H:
                level = "CAUTION"
        elif k in ("expiry", "quarterly_expiry"):
            if -EXPIRY_CAUTION_POST_H <= h <= EXPIRY_CAUTION_PRE_H:
                level = "CAUTION"
        if rank[level] > rank[best["level"]]:
            when = "in %.1fh" % h if h >= 0 else "%.1fh ago" % -h
            best = {
                "level": level,
                "risk_mult": 0.0 if level == "BLOCK" else 0.5,
                "lev_mult": 0.0 if level == "BLOCK" else 0.5,
                "event": e["name"],
                "hours_to": round(h, 1),
                "reason": "%s %s: %s" % (
                    e["name"], when,
                    "no new leveraged entries" if level == "BLOCK" else "half size, half leverage"),
            }
    return best


# --------------------------------------------------------------------------- leverage tiers & brackets

MAJOR_LEVERAGE_CAPS = {"BTC": 5.0, "ETH": 4.0}
LARGE_CAPS = {"SOL", "BNB", "XRP"}
DEFAULT_ALT_CAP = 2.0

# Binance isolated spot margin thresholds (assets / debt)
MARGIN_LIQ_LEVEL = 1.10
MARGIN_CALL_LEVEL = 1.30
CRITICAL_MARGIN_LEVEL = 1.15

# Typical USD(S)-M maintenance margin rates for initial notional bracket
PERP_MMR = {"BTC": 0.004, "ETH": 0.005}
PERP_MMR_DEFAULT = 0.01

# Institutional Tiered Margin Brackets (Binance USDS-M Perpetual Architecture)
# Form: notional up to max_notional has maintenance margin rate (mmr) and deduction (cum).
# Maintenance Margin = (Notional x MMR) - CUM
BINANCE_PERP_TIERS = {
    "BTC": [
        {"max_notional": 50000.0, "mmr": 0.004, "cum": 0.0, "max_lev": 125.0},
        {"max_notional": 250000.0, "mmr": 0.005, "cum": 50.0, "max_lev": 100.0},
        {"max_notional": 1000000.0, "mmr": 0.010, "cum": 1300.0, "max_lev": 50.0},
        {"max_notional": 5000000.0, "mmr": 0.025, "cum": 16300.0, "max_lev": 20.0},
        {"max_notional": float("inf"), "mmr": 0.050, "cum": 141300.0, "max_lev": 10.0},
    ],
    "ETH": [
        {"max_notional": 40000.0, "mmr": 0.005, "cum": 0.0, "max_lev": 100.0},
        {"max_notional": 200000.0, "mmr": 0.010, "cum": 200.0, "max_lev": 50.0},
        {"max_notional": 1000000.0, "mmr": 0.020, "cum": 2200.0, "max_lev": 25.0},
        {"max_notional": float("inf"), "mmr": 0.050, "cum": 32200.0, "max_lev": 10.0},
    ],
    "LARGE": [  # SOL, BNB, XRP
        {"max_notional": 10000.0, "mmr": 0.010, "cum": 0.0, "max_lev": 50.0},
        {"max_notional": 50000.0, "mmr": 0.025, "cum": 150.0, "max_lev": 20.0},
        {"max_notional": 250000.0, "mmr": 0.050, "cum": 1400.0, "max_lev": 10.0},
        {"max_notional": float("inf"), "mmr": 0.100, "cum": 13900.0, "max_lev": 5.0},
    ],
    "DEFAULT": [  # Altcoins
        {"max_notional": 5000.0, "mmr": 0.020, "cum": 0.0, "max_lev": 20.0},
        {"max_notional": 25000.0, "mmr": 0.050, "cum": 150.0, "max_lev": 10.0},
        {"max_notional": 100000.0, "mmr": 0.100, "cum": 1400.0, "max_lev": 5.0},
        {"max_notional": float("inf"), "mmr": 0.150, "cum": 6400.0, "max_lev": 3.0},
    ],
}

# Liquidation clearance fee rates charged by exchange to replenish Insurance Fund
LIQUIDATION_CLEARANCE_FEE_RATES = {
    "BTC": 0.010,
    "ETH": 0.010,
    "LARGE": 0.015,
    "DEFAULT": 0.020,
}


def get_tier_bracket(base, notional=0.0):
    """Return the applicable Binance maintenance margin bracket for the notional size."""
    b = str(base or "").upper()
    key = b if b in ("BTC", "ETH") else ("LARGE" if b in LARGE_CAPS else "DEFAULT")
    tiers = BINANCE_PERP_TIERS[key]
    val = max(0.0, float(notional or 0.0))
    for t in tiers:
        if val <= t["max_notional"]:
            return dict(t)
    return dict(tiers[-1])


def maintenance_margin(notional, base=None):
    """Exact maintenance margin required by Binance for a given notional position size."""
    brk = get_tier_bracket(base, notional)
    return max(0.0, notional * brk["mmr"] - brk["cum"])


def bankruptcy_price(entry, side, leverage):
    """The price where position equity reaches exactly zero.

    Liquidation triggers before bankruptcy so the exchange can close out the position
    without burdening the Insurance Fund or triggering Auto-Deleveraging (ADL).
    """
    lev = max(1.0, float(leverage))
    side_str = "long" if str(side).lower().startswith("l") else "short"
    if side_str == "long":
        return max(0.0, entry * (1.0 - 1.0 / lev))
    return entry * (1.0 + 1.0 / lev)


def liquidation_clearance_fee(notional, base=None):
    """Estimated liquidation clearance fee assessed by Binance into the Futures Insurance Fund."""
    b = str(base or "").upper()
    key = b if b in ("BTC", "ETH") else ("LARGE" if b in LARGE_CAPS else "DEFAULT")
    rate = LIQUIDATION_CLEARANCE_FEE_RATES[key]
    return notional * rate


def leverage_cap(base):
    """Institutional liquidity-tiered maximum leverage cap."""
    b = str(base or "").upper()
    if b in MAJOR_LEVERAGE_CAPS:
        return MAJOR_LEVERAGE_CAPS[b]
    if b in LARGE_CAPS:
        return 3.0
    return DEFAULT_ALT_CAP


def liquidation_price(entry, side, leverage, venue="perp", base=None):
    """Isolated liquidation price (standard baseline).

    perp   : long  = entry * (1 - 1/L + mmr), short = entry * (1 + 1/L - mmr)
    margin : Binance isolated spot margin, liquidation at margin level 1.1
             long  = entry * (1 - 1/L) * 1.1, short = entry * (1 + 1/L) / 1.1
    """
    lev = max(1.0, float(leverage))
    side_str = "long" if str(side).lower().startswith("l") else "short"
    if venue == "margin":
        if side_str == "long":
            if lev <= 1.0:
                return 0.0
            return entry * (1.0 - 1.0 / lev) * MARGIN_LIQ_LEVEL
        return entry * (1.0 + 1.0 / lev) / MARGIN_LIQ_LEVEL
    mmr = PERP_MMR.get(str(base or "").upper(), PERP_MMR_DEFAULT)
    if side_str == "long":
        return max(0.0, entry * (1.0 - 1.0 / lev + mmr))
    return entry * (1.0 + 1.0 / lev - mmr)


def max_safe_leverage(entry, stop, side, venue="perp", base=None, buffer_mult=2.0):
    """Highest leverage whose liquidation sits at least `buffer_mult` x the stop distance away."""
    d = abs(entry - stop) / entry if entry else 0.0
    if d <= 0:
        return 1.0
    kd = buffer_mult * d
    side_str = "long" if str(side).lower().startswith("l") else "short"
    if venue == "margin":
        m = MARGIN_LIQ_LEVEL
        denom = (1.0 - (1.0 - kd) / m) if side_str == "long" else (m * (1.0 + kd) - 1.0)
    else:
        mmr = PERP_MMR.get(str(base or "").upper(), PERP_MMR_DEFAULT)
        denom = kd + mmr
    if denom <= 0:
        return 125.0
    return max(1.0, 1.0 / denom)


def stop_is_liquidation_safe(entry, stop, side, leverage, venue="perp", base=None, buffer_mult=2.0):
    """Verify that the stop-loss is placed safely before the liquidation threshold."""
    liq = liquidation_price(entry, side, leverage, venue, base)
    stop_d = abs(entry - stop)
    liq_d = abs(entry - liq)
    side_str = "long" if str(side).lower().startswith("l") else "short"
    wrong_side = (side_str == "long" and stop >= entry) or (side_str == "short" and stop <= entry)
    ok = (not wrong_side) and liq_d >= buffer_mult * stop_d
    return ok, liq


# --------------------------------------------------------------------------- dual-price mechanism & carry drag

def mark_last_basis(last_price, mark_price):
    """Evaluate basis between Spot/Last Price and Futures Mark Price.

    Binance liquidations trigger strictly on Mark Price to protect against local
    order-book wick manipulation. If basis diverges significantly (>0.5%), volatility
    risk is elevated.
    """
    if not (last_price and mark_price) or mark_price <= 0:
        return {"basis_usd": 0.0, "basis_pct": 0.0, "risk_level": "NORMAL", "safe": True}
    diff = last_price - mark_price
    pct = (diff / mark_price) * 100.0
    abs_pct = abs(pct)
    if abs_pct <= 0.3:
        lvl = "NORMAL"
    elif abs_pct <= 0.7:
        lvl = "ELEVATED_BASIS_RISK"
    else:
        lvl = "EXTREME_DISLOCATION"
    return {
        "basis_usd": round(diff, 4),
        "basis_pct": round(pct, 3),
        "risk_level": lvl,
        "safe": abs_pct <= 0.7,
    }


def carry_adjusted_liquidation_price(entry, side, leverage, hold_hours=24.0, funding_8h=0.0,
                                     borrow_rate_hourly=0.00002, venue="perp", base=None):
    """Dynamic liquidation price after accounting for cumulative carry erosion on isolated collateral.

    When holding a position under positive perp funding (longs paying shorts) or spot margin borrow
    interest, collateral is deducted over time, causing the liquidation price to drift closer to entry.
    """
    lev = max(1.0, float(leverage))
    side_str = "long" if str(side).lower().startswith("l") else "short"
    base_liq = liquidation_price(entry, side_str, lev, venue, base)
    if hold_hours <= 0:
        return base_liq

    # Initial isolated collateral per unit = entry / lev
    init_collateral = entry / lev
    if venue == "perp":
        carry_unit = entry * funding_8h * (hold_hours / 8.0) * (1.0 if side_str == "long" else -1.0)
    else:
        borrowed = entry * (1.0 - 1.0 / lev) if side_str == "long" else entry
        carry_unit = borrowed * borrow_rate_hourly * hold_hours

    rem_collateral = max(0.01 * init_collateral, init_collateral - carry_unit)
    eff_lev = max(1.0, entry / rem_collateral)
    return liquidation_price(entry, side_str, eff_lev, venue, base)


def position_health(entry, current_price, side, leverage, venue="perp", base=None, stop_price=None, accrued_interest=0.0):
    """Real-time account/position margin health and margin call warnings.

    Evaluates the remaining liquidation buffer ratio:
      HEALTHY: Safety buffer > 50% intact (maintain position)
      MARGIN_CALL: Safety buffer between 20% and 50% (prompt to reduce debt or add collateral)
      CRITICAL: Safety buffer between 0% and 20% (imminent liquidation)
      LIQUIDATED: Liquidation price breached (exchange engine takes over)
    """
    lev = max(1.0, float(leverage))
    side_str = "long" if str(side).lower().startswith("l") else "short"
    liq = liquidation_price(entry, side_str, lev, venue, base)
    cur_p = max(0.0000001, float(current_price))

    # Spot margin level: Total Asset Value / (Debt + Accrued Interest)
    if side_str == "long":
        borrowed_per_unit = entry * (1.0 - 1.0 / lev)
        debt = borrowed_per_unit + accrued_interest
        margin_level = (cur_p / debt) if debt > 0 else 999.0
        liq_breached = cur_p <= liq
        buffer_total = entry - liq
        buffer_rem = cur_p - liq
    else:
        borrowed_per_unit = 1.0
        debt_value = cur_p * borrowed_per_unit + accrued_interest
        asset_value = entry * (1.0 + 1.0 / lev)
        margin_level = (asset_value / debt_value) if debt_value > 0 else 999.0
        liq_breached = cur_p >= liq
        buffer_total = liq - entry
        buffer_rem = liq - cur_p

    # Futures Margin Ratio (%)
    mmr = PERP_MMR.get(str(base or "").upper(), PERP_MMR_DEFAULT)
    collateral_unit = entry / lev
    unreal_pnl = (cur_p - entry) if side_str == "long" else (entry - cur_p)
    margin_bal = max(0.00001, collateral_unit + unreal_pnl)
    margin_ratio_pct = (cur_p * mmr / margin_bal) * 100.0

    safety_ratio = (buffer_rem / buffer_total) if buffer_total > 0 else 0.0

    if liq_breached or safety_ratio <= 0.0 or margin_ratio_pct >= 100.0:
        status = "LIQUIDATED"
        rec = "Position liquidated by exchange engine. Clearance fee assessed to Insurance Fund."
    elif safety_ratio <= 0.20 or margin_ratio_pct >= 85.0:
        status = "CRITICAL"
        rec = "Critical margin risk: immediate emergency reduction or limit de-risk required."
    elif safety_ratio <= 0.50 or margin_ratio_pct >= 65.0:
        status = "MARGIN_CALL"
        rec = "Margin call warning: deleverage position by 50% or deposit additional collateral."
    else:
        status = "HEALTHY"
        rec = "Margin health optimal; maintain stop-loss and active target structure."

    dist_liq_pct = abs(cur_p - liq) / cur_p * 100.0
    buffer_to_stop = None
    if stop_price:
        stop_d = abs(cur_p - stop_price)
        liq_d = abs(cur_p - liq)
        buffer_to_stop = round(liq_d / stop_d, 2) if stop_d > 0 else 0.0

    return {
        "status": status,
        "margin_level": round(margin_level, 3),
        "margin_ratio_pct": round(margin_ratio_pct, 1),
        "safety_ratio": round(safety_ratio, 3),
        "liquidation_price": liq,
        "distance_to_liq_pct": round(dist_liq_pct, 2),
        "buffer_to_stop_ratio": buffer_to_stop,
        "recommendation": rec,
    }


def adl_priority_quantile(leverage, pnl_pct):
    """Institutional Auto-Deleveraging (ADL) ranking quantile (1 = lowest risk, 5 = highest).

    Binance and Bybit use ADL to force-close opposing positions when the Insurance Fund
    cannot cover liquidations. High leverage combined with high profit places traders
    in Quantiles 4-5. Capping leverage at institutional tiers keeps traders in Quantile 1-2.
    """
    lev = max(1.0, float(leverage))
    ranking_metric = max(0.0, float(pnl_pct)) * lev
    if ranking_metric < 0.20:
        quantile = 1
        hazard = "LOW"
    elif ranking_metric < 0.60:
        quantile = 2
        hazard = "LOW_MODERATE"
    elif ranking_metric < 1.20:
        quantile = 3
        hazard = "MODERATE"
    elif ranking_metric < 2.50:
        quantile = 4
        hazard = "HIGH"
    else:
        quantile = 5
        hazard = "EXTREME_ADL_RISK"
    return {
        "quantile": quantile,
        "hazard": hazard,
        "note": "Low institutional leverage (<=5x) keeps positions in safest ADL priority buckets.",
    }


def validate_margin_mode(venue="perp", mode="isolated"):
    """Enforce the institutional rule: directional crypto trades must use Isolated Margin.

    Cross margin exposes the entire wallet balance to catastrophic contagion if an illiquid
    altcoin gaps over the weekend. Isolated margin strictly confines loss to the allocated bucket.
    """
    is_iso = str(mode or "").strip().lower() == "isolated"
    return {
        "mode": "isolated" if is_iso else "cross",
        "institutional_approved": is_iso,
        "note": "Isolated margin eliminates portfolio contagion risk during black-swan liquidations." if is_iso else
                "WARNING: Cross margin pools collateral across all positions and risks complete account wipeout.",
    }


# --------------------------------------------------------------------------- execution planner

def margin_plan(equity, risk_usd, entry, stop, side, base, funding_8h=0.0, hold_hours=24.0,
                venue="perp", event=None, fee_rate=0.0004, borrow_rate_hourly=0.00002,
                max_margin_frac=0.5, buffer_mult=2.0):
    """Turn a stop-defined trade into a complete leveraged execution plan.

    The dollar risk is fixed by `risk_usd`; leverage is chosen as the lowest whole
    number that keeps the locked margin under `max_margin_frac` of equity, capped by the
    asset's tier, by liquidation safety, and by the event regime.
    """
    side_str = "long" if str(side).lower().startswith("l") else "short"
    stop_d = abs(entry - stop)
    if entry <= 0 or stop_d <= 0 or equity <= 0 or risk_usd <= 0:
        return {"ok": False, "reason": "entry, stop, equity and risk must all be positive"}
    if (side_str == "long" and stop >= entry) or (side_str == "short" and stop <= entry):
        return {"ok": False, "reason": "stop is on the wrong side of the entry for a %s" % side_str}

    ev = event or {"level": "CLEAR", "risk_mult": 1.0, "lev_mult": 1.0, "reason": ""}
    risk_mult = ev.get("risk_mult", 1.0)
    if risk_mult <= 0:
        return {"ok": False, "reason": "event block: %s" % ev.get("reason", "")}
    risk = risk_usd * risk_mult

    qty = risk / stop_d
    notional = qty * entry
    tier = leverage_cap(base)
    safe = max_safe_leverage(entry, stop, side_str, venue, base, buffer_mult)
    cap = max(1.0, min(tier, safe) * (ev.get("lev_mult", 1.0) or 1.0))
    cap = max(1.0, math.floor(cap))
    needed = notional / (equity * max_margin_frac)
    lev = max(1.0, min(cap, float(math.ceil(needed))))
    notes = []
    if needed > cap:
        # Margin budget cannot carry full size at safe leverage: shrink size, not stop.
        notional = equity * max_margin_frac * cap
        qty = notional / entry
        risk = qty * stop_d
        notes.append("size cut to fit %.0fx and %.0f%% of equity as margin" % (cap, max_margin_frac * 100))
    margin_locked = notional / lev
    liq = liquidation_price(entry, side_str, lev, venue, base)
    liq_d_pct = abs(entry - liq) / entry * 100.0

    # Institutional Tiered Maintenance Margin & Clearance Fees
    brk = get_tier_bracket(base, notional)
    mm_usd = maintenance_margin(notional, base)
    liq_fee_usd = liquidation_clearance_fee(notional, base)
    bankrupt_px = bankruptcy_price(entry, side_str, lev)

    # Dynamic carry decay on liquidation threshold
    carry_liq = carry_adjusted_liquidation_price(
        entry, side_str, lev, hold_hours, funding_8h, borrow_rate_hourly, venue, base
    )

    if venue == "perp":
        # Positive funding: longs pay shorts. Paid every 8h on notional.
        carry = notional * funding_8h * (hold_hours / 8.0) * (1.0 if side_str == "long" else -1.0)
    else:
        borrowed = notional * (1.0 - 1.0 / lev) if side_str == "long" else notional
        carry = borrowed * borrow_rate_hourly * hold_hours
    fees = notional * fee_rate * 2.0

    # Margin call warning trigger price (when margin level hits 1.30 on spot margin)
    if venue == "margin":
        margin_call_px = entry * (1.0 - 1.0 / lev) * MARGIN_CALL_LEVEL if side_str == "long" else entry * (1.0 + 1.0 / lev) / MARGIN_CALL_LEVEL
    else:
        margin_call_px = entry * (1.0 - (1.0 / lev) * 0.70) if side_str == "long" else entry * (1.0 + (1.0 / lev) * 0.70)

    adl = adl_priority_quantile(lev, 0.0)

    return {
        "ok": True,
        "venue": venue,
        "side": side_str,
        "risk_usd": round(risk, 2),
        "qty": qty,
        "notional_usd": round(notional, 2),
        "leverage": lev,
        "leverage_cap_tier": tier,
        "leverage_cap_liq_safe": round(safe, 1),
        "margin_usd": round(margin_locked, 2),
        "margin_pct_equity": round(margin_locked / equity * 100.0, 1),
        "liquidation_price": liq,
        "liq_distance_pct": round(liq_d_pct, 2),
        "stop_distance_pct": round(stop_d / entry * 100.0, 2),
        "liq_to_stop_ratio": round(abs(entry - liq) / stop_d, 1),
        "bankruptcy_price": round(bankrupt_px, 4),
        "maintenance_margin_usd": round(mm_usd, 2),
        "mmr": brk["mmr"],
        "cum_deduction": brk["cum"],
        "liquidation_clearance_fee_usd": round(liq_fee_usd, 2),
        "carry_adjusted_liq_price": round(carry_liq, 4),
        "margin_call_price": round(margin_call_px, 4),
        "adl_risk_quantile": adl["quantile"],
        "carry_usd": round(carry, 2),
        "carry_r": round(carry / risk, 3) if risk else 0.0,
        "fees_usd": round(fees, 2),
        "fees_r": round(fees / risk, 3) if risk else 0.0,
        "hold_hours": hold_hours,
        "event_level": ev.get("level", "CLEAR"),
        "event_reason": ev.get("reason", ""),
        "notes": notes,
    }


# --------------------------------------------------------------------------- book-level guards

def classify_regime(close, ema20, ema50, val=None, vah=None):
    """BTC (market) regime from 4h trend and 7d value area."""
    if not (close and ema20 and ema50):
        return "NEUTRAL"
    up = ema20 > ema50 and close > ema50
    down = ema20 < ema50 and close < ema50
    if down and (val is None or close < val * 1.002):
        return "BEAR"
    if up and (vah is None or close > (val or 0)):
        return "BULL"
    return "NEUTRAL"


def correlation_guard(positions, side, equity, new_notional, max_same_side=2, max_gross_leverage=2.0):
    """Block a new position that would stack the same crypto beta or over-lever the book.

    `positions` is an iterable of dicts with "side" and "notional".
    """
    same = sum(1 for p in positions if p.get("side") == side)
    if same >= max_same_side:
        return "Correlation cap: already %d open %s positions (crypto trades as one asset under stress)" % (same, side)
    gross = sum(abs(float(p.get("notional", 0.0))) for p in positions) + abs(new_notional)
    if equity > 0 and gross / equity > max_gross_leverage:
        return "Gross leverage cap: book would be %.2fx equity (limit %.1fx)" % (gross / equity, max_gross_leverage)
    return None


# Bases that end in "B" but are crypto, not Binance bStocks tokenized equities.
CRYPTO_B_BASES = {"BNB", "DGB", "TRB", "CKB", "SHIB", "ARB", "BB", "YB", "MUB", "LAB", "BOB"}


def is_tokenized_stock(symbol):
    """Binance bStocks (e.g. SNDKBUSDT, NVDAB, SPYB) follow US equity hours and gap
    over weekends; a 24/7 crypto margin engine should not trade them."""
    s = str(symbol or "").upper()
    for q in ("USDT", "USDC", "FDUSD"):
        if s.endswith(q) and len(s) > len(q):
            s = s[:-len(q)]
            break
    if s.startswith("1000") and len(s) > 4:
        s = s[4:]
    return len(s) >= 3 and s.endswith("B") and s not in CRYPTO_B_BASES
