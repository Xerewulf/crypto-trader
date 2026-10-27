#!/usr/bin/env python3
"""Margin-trading doctrine for Binance crypto (standard library only, Python 3.8+).

Pure functions shared by predict.py (the forecaster) and trader/live/paper_trader.py
(the paper margin engine). Nothing here touches the network or places an order.

What a professional margin desk checks before every leveraged crypto trade:

  1. Size by the STOP, never by the leverage. Dollar risk = qty x |entry - stop|.
     Leverage only decides how much collateral is locked up and where the
     liquidation price sits. Pick the LOWEST leverage that fits the margin budget.
  2. Liquidation must sit far beyond the stop (default: at least 2x the stop
     distance), so a wick, a mark/last divergence or slippage cannot liquidate
     the position before the stop fills.
  3. Leverage tiers by liquidity: BTC 5x, ETH 4x, large caps 3x, everything else 2x.
  4. Event risk: no new leveraged entries from 2h before to 1h after an FOMC
     decision; half size and half leverage from 24h before to 6h after. Half size
     around the monthly Deribit options expiry (last Friday, 08:00 UTC).
  5. Carry: positive funding (perps) or borrow interest (spot margin) is a cost
     to the side that pays it. Express it in R so a slow trade's bleed is visible.
  6. Correlation: crypto is one trade most days. Cap same-direction positions and
     gross leverage across the book, and do not fight the BTC regime with alts.
"""

import datetime
import math

# --------------------------------------------------------------------------- calendar

# FOMC statement days (the second day of each two-day meeting), Federal Reserve 2026 calendar.
# The statement is released at 2:00 p.m. New York time.
FOMC_DECISION_DAYS = (
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
)

FOMC_BLOCK_PRE_H = 2.0      # no new entries this many hours before the statement
FOMC_BLOCK_POST_H = 1.0     # ...until this many hours after it
FOMC_CAUTION_PRE_H = 24.0   # half risk / half leverage window starts
FOMC_CAUTION_POST_H = 6.0   # ...and ends
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


def monthly_expiry_ms(year, month):
    """Deribit monthly options expiry: last Friday of the month, 08:00 UTC."""
    d = _nth_weekday(year, month, 4, -1)
    dt = datetime.datetime(d.year, d.month, d.day, 8, 0, tzinfo=datetime.timezone.utc)
    return int(dt.timestamp() * 1000)


def upcoming_events(now_ms, horizon_h=24 * 8, extra=None):
    """Known scheduled volatility events within the horizon (plus any `extra` list of
    {"name", "time_ms", "kind"} dicts from the user's config), sorted by time."""
    out = []
    lo = now_ms - 12 * HOUR_MS
    hi = now_ms + int(horizon_h * HOUR_MS)
    for d in FOMC_DECISION_DAYS:
        t = fomc_decision_ms(d)
        if lo <= t <= hi:
            out.append({"name": "FOMC rate decision", "time_ms": t, "kind": "fomc"})
    now = datetime.datetime.fromtimestamp(now_ms / 1000.0, datetime.timezone.utc)
    for k in range(0, 2):
        y, m = now.year + (now.month + k - 1) // 12, (now.month + k - 1) % 12 + 1
        t = monthly_expiry_ms(y, m)
        if lo <= t <= hi:
            out.append({"name": "Deribit monthly options expiry", "time_ms": t, "kind": "expiry"})
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

    level: "BLOCK" (no new leveraged entries), "CAUTION" (half risk, half leverage) or "CLEAR".
    """
    best = {"level": "CLEAR", "risk_mult": 1.0, "lev_mult": 1.0, "event": None, "hours_to": None,
            "reason": "No scheduled macro or expiry event inside the risk window"}
    rank = {"CLEAR": 0, "CAUTION": 1, "BLOCK": 2}
    for e in upcoming_events(now_ms, extra=extra):
        h = (e["time_ms"] - now_ms) / float(HOUR_MS)
        level = "CLEAR"
        if e["kind"] == "fomc":
            if -FOMC_BLOCK_POST_H <= h <= FOMC_BLOCK_PRE_H:
                level = "BLOCK"
            elif -FOMC_CAUTION_POST_H <= h <= FOMC_CAUTION_PRE_H:
                level = "CAUTION"
        elif -EXPIRY_CAUTION_POST_H <= h <= EXPIRY_CAUTION_PRE_H:
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


# --------------------------------------------------------------------------- leverage & liquidation

MAJOR_LEVERAGE_CAPS = {"BTC": 5.0, "ETH": 4.0}
LARGE_CAPS = {"SOL", "BNB", "XRP"}
DEFAULT_ALT_CAP = 2.0

# Binance isolated spot margin liquidates at margin level ~1.1 (assets / debt).
MARGIN_LIQ_LEVEL = 1.10
# Typical USDⓈ-M maintenance margin rate for the first notional bracket.
PERP_MMR = {"BTC": 0.004, "ETH": 0.005}
PERP_MMR_DEFAULT = 0.01


def leverage_cap(base):
    b = str(base or "").upper()
    if b in MAJOR_LEVERAGE_CAPS:
        return MAJOR_LEVERAGE_CAPS[b]
    if b in LARGE_CAPS:
        return 3.0
    return DEFAULT_ALT_CAP


def liquidation_price(entry, side, leverage, venue="perp", base=None):
    """Approximate isolated liquidation price (ignores fees and accrued interest).

    perp   : long  = entry * (1 - 1/L + mmr), short = entry * (1 + 1/L - mmr)
    margin : Binance isolated spot margin, liquidation at margin level 1.1
             long  = entry * (1 - 1/L) * 1.1, short = entry * (1 + 1/L) / 1.1
    """
    lev = max(1.0, float(leverage))
    if venue == "margin":
        if side == "long":
            if lev <= 1.0:
                return 0.0
            return entry * (1.0 - 1.0 / lev) * MARGIN_LIQ_LEVEL
        return entry * (1.0 + 1.0 / lev) / MARGIN_LIQ_LEVEL
    mmr = PERP_MMR.get(str(base or "").upper(), PERP_MMR_DEFAULT)
    if side == "long":
        return max(0.0, entry * (1.0 - 1.0 / lev + mmr))
    return entry * (1.0 + 1.0 / lev - mmr)


def max_safe_leverage(entry, stop, side, venue="perp", base=None, buffer_mult=2.0):
    """Highest leverage whose liquidation sits at least `buffer_mult` x the stop distance away."""
    d = abs(entry - stop) / entry if entry else 0.0
    if d <= 0:
        return 1.0
    kd = buffer_mult * d
    if venue == "margin":
        m = MARGIN_LIQ_LEVEL
        denom = (1.0 - (1.0 - kd) / m) if side == "long" else (m * (1.0 + kd) - 1.0)
    else:
        mmr = PERP_MMR.get(str(base or "").upper(), PERP_MMR_DEFAULT)
        denom = kd + mmr
    if denom <= 0:
        return 125.0
    return max(1.0, 1.0 / denom)


def stop_is_liquidation_safe(entry, stop, side, leverage, venue="perp", base=None, buffer_mult=2.0):
    liq = liquidation_price(entry, side, leverage, venue, base)
    stop_d = abs(entry - stop)
    liq_d = abs(entry - liq)
    wrong_side = (side == "long" and stop >= entry) or (side == "short" and stop <= entry)
    ok = (not wrong_side) and liq_d >= buffer_mult * stop_d
    return ok, liq


def margin_plan(equity, risk_usd, entry, stop, side, base, funding_8h=0.0, hold_hours=24.0,
                venue="perp", event=None, fee_rate=0.0004, borrow_rate_hourly=0.00002,
                max_margin_frac=0.5, buffer_mult=2.0):
    """Turn a stop-defined trade into a complete leveraged execution plan.

    The dollar risk is fixed by `risk_usd`; leverage is chosen as the lowest whole
    number that keeps the locked margin under `max_margin_frac` of equity, capped by the
    asset's tier, by liquidation safety, and by the event regime.
    """
    side = "long" if str(side).lower().startswith("l") else "short"
    stop_d = abs(entry - stop)
    if entry <= 0 or stop_d <= 0 or equity <= 0 or risk_usd <= 0:
        return {"ok": False, "reason": "entry, stop, equity and risk must all be positive"}
    if (side == "long" and stop >= entry) or (side == "short" and stop <= entry):
        return {"ok": False, "reason": "stop is on the wrong side of the entry for a %s" % side}

    ev = event or {"level": "CLEAR", "risk_mult": 1.0, "lev_mult": 1.0, "reason": ""}
    risk_mult = ev.get("risk_mult", 1.0)
    if risk_mult <= 0:
        return {"ok": False, "reason": "event block: %s" % ev.get("reason", "")}
    risk = risk_usd * risk_mult

    qty = risk / stop_d
    notional = qty * entry
    tier = leverage_cap(base)
    safe = max_safe_leverage(entry, stop, side, venue, base, buffer_mult)
    cap = max(1.0, min(tier, safe) * (ev.get("lev_mult", 1.0) or 1.0))
    cap = max(1.0, math.floor(cap))
    needed = notional / (equity * max_margin_frac)
    lev = max(1.0, min(cap, float(math.ceil(needed))))
    notes = []
    if needed > cap:
        # Margin budget cannot carry the full size at a safe leverage: shrink the size, not the stop.
        notional = equity * max_margin_frac * cap
        qty = notional / entry
        risk = qty * stop_d
        notes.append("size cut to fit %.0fx and %.0f%% of equity as margin" % (cap, max_margin_frac * 100))
    margin = notional / lev
    liq = liquidation_price(entry, side, lev, venue, base)
    liq_d_pct = abs(entry - liq) / entry * 100.0

    if venue == "perp":
        # Positive funding: longs pay shorts. Paid every 8h on notional.
        carry = notional * funding_8h * (hold_hours / 8.0) * (1.0 if side == "long" else -1.0)
    else:
        borrowed = notional * (1.0 - 1.0 / lev) if side == "long" else notional
        carry = borrowed * borrow_rate_hourly * hold_hours
    fees = notional * fee_rate * 2.0
    return {
        "ok": True,
        "venue": venue,
        "side": side,
        "risk_usd": round(risk, 2),
        "qty": qty,
        "notional_usd": round(notional, 2),
        "leverage": lev,
        "leverage_cap_tier": tier,
        "leverage_cap_liq_safe": round(safe, 1),
        "margin_usd": round(margin, 2),
        "margin_pct_equity": round(margin / equity * 100.0, 1),
        "liquidation_price": liq,
        "liq_distance_pct": round(liq_d_pct, 2),
        "stop_distance_pct": round(stop_d / entry * 100.0, 2),
        "liq_to_stop_ratio": round(abs(entry - liq) / stop_d, 1),
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
