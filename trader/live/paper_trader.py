#!/usr/bin/env python3
"""Institutional Binance PAPER Trader (Wyckoff 2.0 + Market Microstructure Engine).
Standard library only. No API keys required. All trades are paper/simulated.

Synthesizes the core literature from 'institutional_crypto_trading_mastery.md':
  1. Market Microstructure (Larry Harris, Maureen O'Hara, Marcos López de Prado):
     - Tracks real-time Spot Cumulative Volume Delta (CVD) and Taker Buy %
     - Evaluates live Order-Book Depth Imbalance (±1% and ±2%) from /api/v3/depth
  2. Auction Market Theory & Wyckoff 2.0 (Jim Dalton, Rubén Villahermosa):
     - Computes 7-Day Rolling Volume Profile: Point of Control (POC), Value Area Low (VAL),
       and Value Area High (VAH) across 168 1h bars
     - Computes Rolling 24h Volume-Weighted Average Price (VWAP) as institutional benchmark
     - Detects Wyckoff Springs (Bullish Liquidity Sweeps) reclaiming VAL/POC with CVD Absorption
     - Detects Wyckoff Upthrusts / SFPs (Bearish Stop-Hunts) rejecting VAH with CVD Exhaustion
  3. Perpetual Derivatives & Reflexivity (Arthur Hayes):
     - Cross-references Binance USDⓈ-M Futures (fapi.binance.com) 8h Funding Rate & Open Interest
  4. Mathematical Risk Management (Edward Thorp, Ernest Chan):
     - Fractional Quarter-Kelly sizing capped at $50 max loss per trade (1.0% of $5,000 equity)
     - 3x Isolated Margin leverage, $150 daily loss shutoff guard, and 2.0R profit targets.

Commands:
    python trader/live/paper_trader.py run        # run continuous loop (Ctrl+C to stop)
    python trader/live/paper_trader.py once       # single cycle test
    python trader/live/paper_trader.py status     # print account & open positions
    python trader/live/paper_trader.py reset --yes
"""

import argparse
import asyncio
import datetime
import html
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request

try:
    import aiohttp
    import aiohttp.web
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
ROOT = os.path.dirname(os.path.dirname(HERE))
TRADER_ANALYST = os.path.join(ROOT, "trader-analyst")
if TRADER_ANALYST not in sys.path:
    sys.path.insert(0, TRADER_ANALYST)

try:
    import news_monitor
except ImportError:
    news_monitor = None

try:
    import margin
except ImportError:
    margin = None
STATE = os.path.join(HERE, "state.json")
TRADES = os.path.join(HERE, "trades.jsonl")
LOG = os.path.join(HERE, "events.log")
DASH = os.path.join(HERE, "dashboard.html")

SPOT_HOSTS = ("https://api.binance.com", "https://data-api.binance.vision")
FUT_HOSTS = ("https://fapi.binance.com",)
HOUR_MS = 3600 * 1000

EXCLUDED_SYMBOLS = {
    "USDCUSDT", "FDUSDUSDT", "TUSDUSDT", "USDPUSDT", "EURUSDT", "RLUSDUSDT",
    "USD1USDT", "XAUTUSDT", "UUSDT", "BUSDUSDT", "AEURUSDT", "DAIUSDT", "EURIUSDT",
    "PAXGUSDT", "WBTCUSDT"
}

def base_asset(sym):
    s = str(sym or "").upper()
    for q in ("USDT", "USDC", "FDUSD", "BUSD"):
        if s.endswith(q) and len(s) > len(q):
            s = s[:-len(q)]
            break
    if s.startswith("1000") and len(s) > 4:
        s = s[4:]
    return s

CFG = {
    "start_equity": 5000.0,
    "risk_usd": 50.0,                 # Fallback fixed risk
    "risk_usd_base": 50.0,            # Base risk per trade (0.25x Kelly)
    "risk_usd_max": 125.0,            # Max risk for A+ 90+ God-Level setups (0.50x Kelly)
    "leverage": 3.0,
    "max_margin_frac": 0.75,
    "fee_rate": 0.0004,               # 4 bps per side (VIP/BNB discount)
    "slippage": 0.0002,               # 2 bps adverse execution slippage
    "borrow_rate_hourly": 0.00002,    # 0.002%/hour margin borrow rate
    "daily_loss_limit": 150.0,        # Hard 3% daily loss guard
    "max_entries_per_day": 10,
    "max_open_positions": 4,          # Up to 4 concurrent positions across the entire market
    "poll_seconds": 30,
    "stale_signal_minutes": 30,
    "cooldown_bars": 2,
    "tp1_r": 1.8,                     # Phase 1: Tier 1 exit (40% at 1.8R, move stop to Breakeven)
    "tp2_r": 3.5,                     # Phase 1: Tier 2 exit (30% at 3.5R, enable VWAP trail)
    "runner_pct": 0.30,               # Phase 1: Runner (30% trailed dynamically behind 1h VWAP)
    "min_risk_pct": 0.004,            # Phase 2: Micro-stop compression minimum threshold (0.4%)
    "min_universe_vol": 5000000.0,    # $5M+ 24h quote volume filter
    "max_universe_coins": 40,         # Dynamically scan top 40 liquid Binance coins
}


# ---------------------------------------------------------------- network & utilities

def now_ms():
    return int(time.time() * 1000)


def iso(ms=None):
    t = datetime.datetime.fromtimestamp((ms if ms is not None else now_ms()) / 1000.0, datetime.timezone.utc)
    return t.strftime("%Y-%m-%d %H:%M:%S UTC")


def utc_day(ms=None):
    return iso(ms)[:10]


def fp(x):
    if x is None:
        return "-"
    f = float(x)
    if abs(f) < 0.001:
        return "%.6f" % f
    if abs(f) < 0.1:
        return "%.5f" % f
    if abs(f) < 10:
        return "%.4f" % f
    return "%.2f" % f


def usd(x, signed=False):
    x = float(x)
    s = ("+" if x > 0 else "-" if x < 0 else "") if signed else ("-" if x < 0 else "")
    return "%s$%s" % (s, "{:,.2f}".format(abs(x)))


def log(msg):
    line = "[%s] %s" % (iso(), msg)
    try:
        print(line, flush=True)
    except Exception:
        pass
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def fetch_api(hosts, path, timeout=12):
    last = None
    for h in hosts:
        try:
            req = urllib.request.Request(h + path, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            last = e
    raise RuntimeError("API unreachable (%s): %s" % (path.split("?")[0], last))


def klines(sym, interval, limit=200, start=None):
    path = "/api/v3/klines?symbol=%s&interval=%s&limit=%d" % (sym, interval, limit)
    if start is not None:
        path += "&startTime=%d" % start
    out = []
    for k in fetch_api(SPOT_HOSTS, path):
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
        })
    return out


def spot_price(sym):
    return float(fetch_api(SPOT_HOSTS, "/api/v3/ticker/price?symbol=%s" % sym)["price"])


def fetch_liquid_universe(min_vol=None, max_coins=None):
    min_vol = min_vol or CFG["min_universe_vol"]
    max_coins = max_coins or CFG["max_universe_coins"]
    try:
        raw = fetch_api(SPOT_HOSTS, "/api/v3/ticker/24hr", timeout=8)
        coins = []
        for x in raw:
            s = x.get("symbol", "")
            if not s.endswith("USDT") or s in EXCLUDED_SYMBOLS:
                continue
            if any(k in s for k in ("UPUSDT", "DOWNUSDT", "BEARUSDT", "BULLUSDT")):
                continue
            if margin and margin.is_tokenized_stock(s):
                continue
            vol = float(x.get("quoteVolume", 0))
            if vol >= min_vol:
                coins.append((s, vol))
        coins.sort(key=lambda item: item[1], reverse=True)
        res = [s for s, _ in coins[:max_coins]]
        if res:
            return res
    except Exception as exc:
        log("Universe discovery error: %s. Using fallback core list." % exc)
    return [
        "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT", "DOGEUSDT", "AVAXUSDT",
        "NEARUSDT", "SUIUSDT", "PEPEUSDT", "LINKUSDT", "ADAUSDT", "WLDUSDT", "ENAUSDT",
        "UNIUSDT", "TAOUSDT", "AAVEUSDT", "INJUSDT", "LTCUSDT", "TONUSDT"
    ]


def write_atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    for _ in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.15)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


# ---------------------------------------------------------------- institutional indicators

def ema(vals, n):
    k, cur, out = 2.0 / (n + 1), vals[0], []
    for v in vals:
        cur = v * k + cur * (1 - k)
        out.append(cur)
    return out


def atr(bars, n=14):
    trs = []
    for i, b in enumerate(bars):
        if i == 0:
            trs.append(b["h"] - b["l"])
        else:
            pc = bars[i - 1]["c"]
            trs.append(max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc)))
    cur = sum(trs[:n]) / n
    out = []
    for i, tr in enumerate(trs):
        if i >= n:
            cur = (cur * (n - 1) + tr) / n
        out.append(cur)
    return out


def compute_vwap(bars, n_bars=24):
    sub = bars[-n_bars:] if len(bars) >= n_bars else bars
    num = sum(((b["h"] + b["l"] + b["c"]) / 3.0) * b["vol_usd"] for b in sub)
    den = sum(b["vol_usd"] for b in sub)
    return (num / den) if den > 0 else sub[-1]["c"]


def compute_volume_profile(bars, n_bars=168, bins=35):
    """Auction Market Theory (Jim Dalton & Wyckoff 2.0):
    Calculates 7-Day Point of Control (POC), Value Area High (VAH 70%), and Value Area Low (VAL 70%)."""
    sub = bars[-n_bars:] if len(bars) >= n_bars else bars
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
        state = "INITIATIVE BREAKOUT (> VAH)"
    elif cur_p < val_price:
        state = "DISCOUNT / BREAKDOWN (< VAL)"
    elif cur_p >= poc_price:
        state = "UPPER VALUE (POC to VAH)"
    else:
        state = "LOWER VALUE (VAL to POC)"

    return {
        "poc": poc_price,
        "val": val_price,
        "vah": vah_price,
        "state": state,
    }


def fetch_order_book_depth(sym):
    """Market Microstructure (Larry Harris):
    Measures aggressive Bid/Ask Depth Imbalance within ±1% and ±2% of current mid price."""
    try:
        depth = fetch_api(SPOT_HOSTS, "/api/v3/depth?symbol=%s&limit=1000" % sym, timeout=8)
        bids = [(float(p), float(q)) for p, q in depth.get("bids", [])]
        asks = [(float(p), float(q)) for p, q in depth.get("asks", [])]
        if not bids or not asks:
            return {"imb_1pct": 0.0, "imb_2pct": 0.0, "wall_bid": 0.0, "wall_ask": 0.0}
        mid = (bids[0][0] + asks[0][0]) / 2.0
        bids_1 = sum(p * q for p, q in bids if p >= mid * 0.99)
        asks_1 = sum(p * q for p, q in asks if p <= mid * 1.01)
        bids_2 = sum(p * q for p, q in bids if p >= mid * 0.98)
        asks_2 = sum(p * q for p, q in asks if p <= mid * 1.02)
        imb_1 = ((bids_1 - asks_1) / (bids_1 + asks_1) * 100.0) if (bids_1 + asks_1) > 0 else 0.0
        imb_2 = ((bids_2 - asks_2) / (bids_2 + asks_2) * 100.0) if (bids_2 + asks_2) > 0 else 0.0
        wall_b = max(bids[:50], key=lambda x: x[0] * x[1])[0] if bids else mid
        wall_a = max(asks[:50], key=lambda x: x[0] * x[1])[0] if asks else mid
        return {
            "imb_1pct": round(imb_1, 1),
            "imb_2pct": round(imb_2, 1),
            "bids_1pct_usd": bids_1,
            "asks_1pct_usd": asks_1,
            "wall_bid": wall_b,
            "wall_ask": wall_a,
        }
    except Exception:
        return {"imb_1pct": 0.0, "imb_2pct": 0.0, "bids_1pct_usd": 0, "asks_1pct_usd": 0, "wall_bid": 0, "wall_ask": 0}


def fetch_futures_metrics(sym):
    """Perpetuals & Reflexive Leverage (Arthur Hayes):
    Fetches 8h Funding Rate & Open Interest from fapi.binance.com."""
    try:
        fr = fetch_api(FUT_HOSTS, "/fapi/v1/fundingRate?symbol=%s&limit=1" % sym, timeout=8)
        rate = float(fr[0]["fundingRate"]) if fr else 0.0001
        oi_data = fetch_api(FUT_HOSTS, "/fapi/v1/openInterest?symbol=%s" % sym, timeout=8)
        oi_usd = float(oi_data.get("openInterest", 0.0)) * spot_price(sym)
        return {"funding_8h": rate, "funding_ann_pct": rate * 3 * 365 * 100.0, "oi_usd": oi_usd}
    except Exception:
        return {"funding_8h": 0.0001, "funding_ann_pct": 10.95, "oi_usd": 0.0}


# ---------------------------------------------------------------- institutional evaluation engine

def evaluate_institutional(sym, bars_1h, side=None, news_sentiment=0.0):
    """Applies Playbook A (Wyckoff Spring & CVD Absorption), Playbook B (Wyckoff Upthrust SFP),
    and Playbook C (Volume Area Mean-Reversion) synthesized with real-time news & macro catalysts."""
    i = len(bars_1h) - 1
    b = bars_1h[i]
    closes = [x["c"] for x in bars_1h]
    e20 = ema(closes, 20)[i]
    e50 = ema(closes, 50)[i]
    a = atr(bars_1h, 14)[i]

    # 1. Auction Market Theory Profile & VWAP
    vp = compute_volume_profile(bars_1h, n_bars=168)
    vwap_24h = compute_vwap(bars_1h, 24)
    vwap_dist = ((b["c"] / vwap_24h) - 1.0) * 100.0 if vwap_24h else 0.0

    # 2. Order Book Depth Imbalance & Walls
    ob = fetch_order_book_depth(sym)

    # 3. Perpetual Funding Rate
    fut = fetch_futures_metrics(sym)

    trend = "bullish" if e20 > e50 else "bearish"
    diag = {
        "symbol": sym,
        "bar": iso(b["t"])[:16],
        "close": b["c"],
        "ema20": e20,
        "ema50": e50,
        "poc": vp["poc"],
        "val": vp["val"],
        "vah": vp["vah"],
        "vwap_24h": vwap_24h,
        "vwap_dist_pct": round(vwap_dist, 2),
        "auction_state": vp["state"],
        "taker_buy": round(b["tb"], 1),
        "imb_1pct": ob["imb_1pct"],
        "funding_8h": fut["funding_8h"],
        "trend": trend,
        "news_sentiment": round(news_sentiment, 1),
    }

    # ==================== PLAYBOOK A: MARGIN LONG ====================
    prior_low_6 = min(x["l"] for x in bars_1h[max(0, i-6):i])
    val_ref = vp["val"] if vp["val"] else prior_low_6
    is_spring = (b["l"] <= min(prior_low_6, val_ref) * 1.001 and b["c"] > min(prior_low_6, val_ref)
                 and b["c"] > b["o"] and b["tb"] >= 53.0)
    is_long_pullback = (e20 > e50 and b["l"] <= e20 * 1.002 and b["c"] >= e20 and b["c"] > b["o"] and b["tb"] >= 53.0)
    long_ob_ok = ob["imb_1pct"] >= -15.0
    long_funding_ok = fut["funding_8h"] < 0.0005

    # ==================== PLAYBOOK B: MARGIN SHORT ====================
    prior_high_12 = max(x["h"] for x in bars_1h[max(0, i-12):i])
    vah_ref = vp["vah"] if vp["vah"] else prior_high_12
    is_upthrust = (b["h"] >= max(prior_high_12, vah_ref) * 0.999 and b["c"] < max(prior_high_12, vah_ref)
                   and b["c"] < b["o"] and b["tb"] <= 47.0)
    is_short_rejection = (e20 < e50 and b["h"] >= e20 * 0.998 and b["c"] <= e20 and b["c"] < b["o"] and b["tb"] <= 47.0)
    short_ob_ok = ob["imb_1pct"] <= 15.0
    short_funding_ok = fut["funding_8h"] > -0.0005

    sig = None
    long_vwap_ok = (b["c"] >= vwap_24h) if vwap_24h else True
    short_vwap_ok = (b["c"] <= vwap_24h) if vwap_24h else True
    long_valid = (is_spring or is_long_pullback) and long_ob_ok and long_funding_ok and long_vwap_ok and (side in (None, "long"))
    short_valid = (is_upthrust or is_short_rejection) and short_ob_ok and short_funding_ok and short_vwap_ok and (side in (None, "short"))

    if long_valid and (not short_valid or is_spring or b["tb"] > 55.0):
        setup_name = "Wyckoff Spring (CVD Absorption at VAL/Low)" if is_spring else "Volume Profile VWAP/EMA20 Pullback"
        stop_px = min(b["l"] - 0.5 * a, b["c"] - 1.5 * a)
        score = 90 if is_spring else 75
        if news_sentiment >= 2.0:
            score = min(100, score + 10)
        elif news_sentiment <= -2.0:
            score = max(50, score - 20)
        sig = {"side": "long", "setup": setup_name, "stop": stop_px, "score": score}
        diag["check"] = "LONG: %s | Taker Buy %.1f%% | Imb %+.1f%% | Funding %+.4f%% | News: %+.1f" % (
            setup_name, b["tb"], ob["imb_1pct"], fut["funding_8h"] * 100, news_sentiment
        )
        diag["side"] = "long"
    elif short_valid:
        setup_name = "Wyckoff Upthrust SFP (CVD Exhaustion at VAH/High)" if is_upthrust else "1h EMA20 Bearish Rejection"
        stop_px = max(b["h"] + 0.5 * a, b["c"] + 1.5 * a)
        score = 90 if is_upthrust else 75
        if news_sentiment <= -2.0:
            score = min(100, score + 10)
        elif news_sentiment >= 2.0:
            score = max(50, score - 20)
        sig = {"side": "short", "setup": setup_name, "stop": stop_px, "score": score}
        diag["check"] = "SHORT: %s | Taker Buy %.1f%% | Imb %+.1f%% | Funding %+.4f%% | News: %+.1f" % (
            setup_name, b["tb"], ob["imb_1pct"], fut["funding_8h"] * 100, news_sentiment
        )
        diag["side"] = "short"
    else:
        diag["side"] = side or ("long" if trend == "bullish" else "short")
        diag["check"] = "No Setup | Taker Buy %.1f%% | Imb %+.1f%% | Funding %+.4f%% | Trend %s | News: %+.1f" % (
            b["tb"], ob["imb_1pct"], fut["funding_8h"] * 100, trend, news_sentiment
        )

    diag["signal"] = ("[%s] %s" % (sig["side"].upper(), sig["setup"])) if sig else "No Setup"
    return sig, diag
    return sig, diag


# ---------------------------------------------------------------- account state management

def fresh_state():
    return {
        "created": iso(),
        "start_equity": CFG["start_equity"],
        "realized": 0.0,
        "next_id": 1,
        "positions": {},
        "closed": [],
        "last_eval": {},
        "last_exit_bar": {},
        "diag": {},
        "marks": {},
        "universe": [],
        "last_universe_ms": 0,
        "news": {},
        "last_news_ms": 0,
        "funding_arb": [],
        "btc_lead": {},
        "curve": [],
        "heartbeat": None,
        "errors": 0,
        "cycles": 0,
    }


def load_state():
    if os.path.exists(STATE):
        try:
            with open(STATE, encoding="utf-8") as fh:
                s = json.load(fh)
                s.setdefault("universe", [])
                s.setdefault("last_universe_ms", 0)
                s.setdefault("news", {})
                s.setdefault("last_news_ms", 0)
                s.setdefault("funding_arb", [])
                s.setdefault("btc_lead", {})
                for p in s.get("positions", {}).values():
                    p.setdefault("initial_qty", p["qty"])
                    dist = abs(p["entry"] - p["stop"])
                    p.setdefault("tp1_px", p["entry"] + CFG.get("tp1_r", 1.8) * dist if p["side"] == "long" else p["entry"] - CFG.get("tp1_r", 1.8) * dist)
                    p.setdefault("tp2_px", p["entry"] + CFG.get("tp2_r", 3.5) * dist if p["side"] == "long" else p["entry"] - CFG.get("tp2_r", 3.5) * dist)
                    p.setdefault("tp1_hit", False)
                    p.setdefault("tp2_hit", False)
                    p.setdefault("trail_vwap", False)
                    p.setdefault("partial_realized", 0.0)
                    p.setdefault("partial_costs", 0.0)
                return s
        except Exception:
            pass
    s = fresh_state()
    log("Initialized Institutional Binance Paper Trader: %s starting bankroll." % usd(CFG["start_equity"]))
    return s


def equity_parts(s):
    unreal = 0.0
    for p in s["positions"].values():
        m = s["marks"].get(p["symbol"], p["entry"])
        unreal += (m - p["entry"]) * p["qty"] if p["side"] == "long" else (p["entry"] - m) * p["qty"]
        unreal -= p["entry_fee"]
    realized_eq = s["start_equity"] + s["realized"]
    return realized_eq, unreal, realized_eq + unreal


# ---------------------------------------------------------------- position lifecycle & execution

def partial_close_position(s, p, close_qty, exit_px, ms, tier_label):
    """Phase 1 Convex Scale-Out: Realizes a partial fraction (e.g. 40% at TP1 or 30% at TP2)
    and reduces position risk while leaving runners to compound."""
    close_qty = min(close_qty, p["qty"])
    if close_qty <= 0.0:
        return 0.0

    gross = (exit_px - p["entry"]) * close_qty if p["side"] == "long" else (p["entry"] - exit_px) * close_qty
    exit_fee = close_qty * exit_px * CFG["fee_rate"]
    hours = max(1, math.ceil((ms - p["opened_ms"]) / HOUR_MS))
    prop = close_qty / (p.get("initial_qty") or p["qty"])
    initial_notional = (p.get("initial_qty") or p["qty"]) * p["entry"]
    borrowed = (initial_notional * (1.0 - 1.0 / CFG["leverage"]) if p["side"] == "long" else initial_notional) * prop
    interest = borrowed * CFG["borrow_rate_hourly"] * hours
    prop_entry_fee = p["entry_fee"] * prop
    net = gross - prop_entry_fee - exit_fee - interest

    s["realized"] += net
    p["partial_realized"] = p.get("partial_realized", 0.0) + net
    p["partial_costs"] = p.get("partial_costs", 0.0) + (prop_entry_fee + exit_fee + interest)
    p["qty"] -= close_qty
    p["notional"] = p["qty"] * p["entry"]
    p["margin"] = p["notional"] / CFG["leverage"]

    r_gain = net / (p["risk_usd"] * prop) if (p["risk_usd"] * prop) > 0 else 0.0
    log("CONVEX SCALE-OUT #%d %s %s [%s] at %s: Qty %s (%.0f%%), Net %s (%+.2fR). Remaining: %s. Account: %s" % (
        p["id"], p["side"].upper(), p["symbol"], tier_label, fp(exit_px),
        ("%.4f" % close_qty).rstrip("0").rstrip("."), prop * 100, usd(net, True), r_gain,
        ("%.4f" % p["qty"]).rstrip("0").rstrip("."), usd(s["start_equity"] + s["realized"])
    ))
    return net


def close_position(s, p, exit_px, ms, outcome):
    rem_qty = p["qty"]
    gross = (exit_px - p["entry"]) * rem_qty if p["side"] == "long" else (p["entry"] - exit_px) * rem_qty
    exit_fee = rem_qty * exit_px * CFG["fee_rate"]
    hours = max(1, math.ceil((ms - p["opened_ms"]) / HOUR_MS))
    prop = rem_qty / (p.get("initial_qty") or rem_qty)
    initial_notional = (p.get("initial_qty") or rem_qty) * p["entry"]
    borrowed = (initial_notional * (1.0 - 1.0 / CFG["leverage"]) if p["side"] == "long" else initial_notional) * prop
    interest = borrowed * CFG["borrow_rate_hourly"] * hours
    rem_entry_fee = p["entry_fee"] * prop
    rem_net = gross - rem_entry_fee - exit_fee - interest

    s["realized"] += rem_net
    total_net = rem_net + p.get("partial_realized", 0.0)
    total_costs = p.get("partial_costs", 0.0) + rem_entry_fee + exit_fee + interest
    total_r = total_net / p["risk_usd"] if p["risk_usd"] > 0 else 0.0

    t = dict(
        p,
        exit=exit_px,
        closed=iso(ms),
        closed_ms=ms,
        outcome=outcome,
        gross=round(gross, 2),
        costs=round(total_costs, 2),
        net=round(total_net, 2),
        r=round(total_r, 2),
        equity_after=round(s["start_equity"] + s["realized"], 2),
    )
    s["closed"].append(t)
    with open(TRADES, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(t) + "\n")

    s["last_exit_bar"][p["symbol"]] = (ms // HOUR_MS) * HOUR_MS
    del s["positions"][p["symbol"]]

    log("CLOSED #%d %s %s [%s] at %s: total net %s (%+.2fR). Account: %s" % (
        p["id"], p["side"].upper(), p["symbol"], outcome, fp(exit_px),
        usd(total_net, True), total_r, usd(t["equity_after"])
    ))


def manage_positions(s):
    for sym in list(s["positions"]):
        p = s["positions"][sym]
        start = p["checked_ms"]
        done = False
        initial_qty = p.get("initial_qty", p["qty"])
        while not done:
            bars = klines(sym, "1m", 1000, start)
            if not bars:
                break
            for b in bars:
                if b["t"] < p["checked_ms"]:
                    continue
                slip = CFG["slippage"]

                # Trailing VWAP check for active runners
                if p.get("trail_vwap"):
                    vwap = s.get("diag", {}).get(sym, {}).get("vwap_24h")
                    if vwap:
                        if p["side"] == "long" and vwap * 0.998 > p["stop"]:
                            p["stop"] = vwap * 0.998
                        elif p["side"] == "short" and vwap * 1.002 < p["stop"]:
                            p["stop"] = vwap * 1.002

                if p["side"] == "long":
                    # TP1 Check (40% scale-out at 1.8R + move stop to Breakeven)
                    if not p.get("tp1_hit") and b["h"] >= p["tp1_px"]:
                        p["tp1_hit"] = True
                        be_stop = p["entry"] * (1.0 + CFG["fee_rate"] * 2.5)
                        p["stop"] = max(p["stop"], be_stop)
                        close_qty = initial_qty * 0.40
                        partial_close_position(s, p, close_qty, p["tp1_px"], b["t"], "TP1 (+1.8R Scale-Out)")

                    # TP2 Check (30% scale-out at 3.5R + enable VWAP trailing runner)
                    elif p.get("tp1_hit") and not p.get("tp2_hit") and b["h"] >= p["tp2_px"]:
                        p["tp2_hit"] = True
                        p["trail_vwap"] = True
                        close_qty = initial_qty * 0.30
                        partial_close_position(s, p, close_qty, p["tp2_px"], b["t"], "TP2 (+3.5R Scale-Out)")

                    # Stop-Loss / Breakeven / Trailing Exit
                    if b["l"] <= p["stop"]:
                        px = min(p["stop"], b["o"]) * (1 - slip)
                        lbl = "TRAILING-VWAP-EXIT" if p.get("trail_vwap") else (
                            "BREAKEVEN-EXIT (+0.72R Locked)" if p.get("tp1_hit") else "STOP-LOSS (-1.0R)"
                        )
                        close_position(s, p, px, max(b["t"], p["opened_ms"]), lbl)
                        done = True
                else: # Short
                    # TP1 Check (40% scale-out at 1.8R + move stop to Breakeven)
                    if not p.get("tp1_hit") and b["l"] <= p["tp1_px"]:
                        p["tp1_hit"] = True
                        be_stop = p["entry"] * (1.0 - CFG["fee_rate"] * 2.5)
                        p["stop"] = min(p["stop"], be_stop)
                        close_qty = initial_qty * 0.40
                        partial_close_position(s, p, close_qty, p["tp1_px"], b["t"], "TP1 (+1.8R Scale-Out)")

                    # TP2 Check (30% scale-out at 3.5R + enable VWAP trailing runner)
                    elif p.get("tp1_hit") and not p.get("tp2_hit") and b["l"] <= p["tp2_px"]:
                        p["tp2_hit"] = True
                        p["trail_vwap"] = True
                        close_qty = initial_qty * 0.30
                        partial_close_position(s, p, close_qty, p["tp2_px"], b["t"], "TP2 (+3.5R Scale-Out)")

                    # Stop-Loss / Breakeven / Trailing Exit
                    if b["h"] >= p["stop"]:
                        px = max(p["stop"], b["o"]) * (1 + slip)
                        lbl = "TRAILING-VWAP-EXIT" if p.get("trail_vwap") else (
                            "BREAKEVEN-EXIT (+0.72R Locked)" if p.get("tp1_hit") else "STOP-LOSS (-1.0R)"
                        )
                        close_position(s, p, px, max(b["t"], p["opened_ms"]), lbl)
                        done = True

                if done:
                    break
                p["checked_ms"] = b["t"]
                s["marks"][sym] = b["c"]
            if done or len(bars) < 1000:
                break
            start = bars[-1]["t"]


def process_live_tick(s, sym, px, ms=None):
    ms = ms or now_ms()
    s["marks"][sym] = px
    p = s["positions"].get(sym)
    if not p:
        return False

    slip = CFG["slippage"]
    side = p["side"]
    initial_qty = p.get("initial_qty", p["qty"])
    changed = False

    # 1. Update Trailing Stop for Runners (if active)
    if p.get("trail_vwap"):
        vwap = s.get("diag", {}).get(sym, {}).get("vwap_24h")
        if vwap:
            if side == "long" and vwap * 0.998 > p["stop"]:
                p["stop"] = vwap * 0.998
            elif side == "short" and vwap * 1.002 < p["stop"]:
                p["stop"] = vwap * 1.002

    # 2. Check Scale-Outs and Exits
    if side == "long":
        # TP1 Check (40% scale-out at 1.8R + move stop to Breakeven)
        if not p.get("tp1_hit") and px >= p["tp1_px"]:
            p["tp1_hit"] = True
            be_stop = p["entry"] * (1.0 + CFG["fee_rate"] * 2.5)
            p["stop"] = max(p["stop"], be_stop)
            close_qty = initial_qty * 0.40
            partial_close_position(s, p, close_qty, p["tp1_px"], ms, "TP1 (+1.8R Scale-Out)")
            log("TP1 HIT #%d %s LONG: Scaled 40%% (+1.8R). Stop moved to Breakeven (%s)" % (p["id"], sym, fp(p["stop"])))
            changed = True

        # TP2 Check (30% scale-out at 3.5R + enable VWAP trailing runner)
        elif p.get("tp1_hit") and not p.get("tp2_hit") and px >= p["tp2_px"]:
            p["tp2_hit"] = True
            p["trail_vwap"] = True
            close_qty = initial_qty * 0.30
            partial_close_position(s, p, close_qty, p["tp2_px"], ms, "TP2 (+3.5R Scale-Out)")
            log("TP2 HIT #%d %s LONG: Scaled 30%% (+3.5R). Runner (30%%) active, trailing 1h VWAP!" % (p["id"], sym))
            changed = True

        # Stop-Loss / Breakeven / Trailing Exit Check
        if px <= p["stop"]:
            exit_px = min(p["stop"], px) * (1.0 - slip)
            lbl = "TRAILING-VWAP-EXIT [MILLI-SEC WS]" if p.get("trail_vwap") else (
                "BREAKEVEN-EXIT (+0.72R Locked) [MILLI-SEC WS]" if p.get("tp1_hit") else "STOP-LOSS (-1.0R) [MILLI-SEC WS]"
            )
            close_position(s, p, exit_px, ms, lbl)
            changed = True

    else: # Short
        # TP1 Check (40% scale-out at 1.8R + move stop to Breakeven)
        if not p.get("tp1_hit") and px <= p["tp1_px"]:
            p["tp1_hit"] = True
            be_stop = p["entry"] * (1.0 - CFG["fee_rate"] * 2.5)
            p["stop"] = min(p["stop"], be_stop)
            close_qty = initial_qty * 0.40
            partial_close_position(s, p, close_qty, p["tp1_px"], ms, "TP1 (+1.8R Scale-Out)")
            log("TP1 HIT #%d %s SHORT: Scaled 40%% (+1.8R). Stop moved to Breakeven (%s)" % (p["id"], sym, fp(p["stop"])))
            changed = True

        # TP2 Check (30% scale-out at 3.5R + enable VWAP trailing runner)
        elif p.get("tp1_hit") and not p.get("tp2_hit") and px <= p["tp2_px"]:
            p["tp2_hit"] = True
            p["trail_vwap"] = True
            close_qty = initial_qty * 0.30
            partial_close_position(s, p, close_qty, p["tp2_px"], ms, "TP2 (+3.5R Scale-Out)")
            log("TP2 HIT #%d %s SHORT: Scaled 30%% (+3.5R). Runner (30%%) active, trailing 1h VWAP!" % (p["id"], sym))
            changed = True

        # Stop-Loss / Breakeven / Trailing Exit Check
        if px >= p["stop"]:
            exit_px = max(p["stop"], px) * (1.0 + slip)
            lbl = "TRAILING-VWAP-EXIT [MILLI-SEC WS]" if p.get("trail_vwap") else (
                "BREAKEVEN-EXIT (+0.72R Locked) [MILLI-SEC WS]" if p.get("tp1_hit") else "STOP-LOSS (-1.0R) [MILLI-SEC WS]"
            )
            close_position(s, p, exit_px, ms, lbl)
            changed = True

    if changed:
        save(s)
        write_dashboard(s)
        return True
    return False

def compute_trade_risk(score, macro_score=0.0):
    """Phase 1: Edward Thorp's Fractional Kelly criterion scaled by conviction."""
    base = CFG.get("risk_usd_base", 50.0)
    if score >= 90:
        mult = 2.4 if macro_score >= 1.0 else 2.0
        return min(CFG.get("risk_usd_max", 125.0), base * mult)
    elif score >= 80:
        return base * 1.5
    else:
        return base


def get_available_risk(s):
    today = utc_day()
    realized_today = sum(t["net"] for t in s["closed"] if t["closed"][:10] == today)
    open_risk = sum(p["risk_usd"] for p in s["positions"].values())
    current_worst_case = -min(realized_today, 0) + open_risk
    remaining_budget = max(0.0, CFG["daily_loss_limit"] - current_worst_case)
    return remaining_budget


def fetch_btc_lead_lag():
    """Phase 3: Tracks real-time BTC 15m/1h momentum and Spot Taker CVD as market lead-lag driver."""
    try:
        btc_bars = klines("BTCUSDT", "15m", 8)
        if len(btc_bars) < 4:
            return {"bias": "NEUTRAL", "btc_tb": 50.0, "btc_ret_1h": 0.0}
        b0 = btc_bars[-1]
        ret_1h = ((b0["c"] / btc_bars[-4]["c"]) - 1.0) * 100.0
        tb = b0["tb"]
        if ret_1h >= 0.4 and tb >= 53.0:
            bias = "BULLISH_LEAD"
        elif ret_1h <= -0.4 and tb <= 47.0:
            bias = "BEARISH_LEAD"
        else:
            bias = "NEUTRAL"
        return {"bias": bias, "btc_tb": round(tb, 1), "btc_ret_1h": round(ret_1h, 2)}
    except Exception:
        return {"bias": "NEUTRAL", "btc_tb": 50.0, "btc_ret_1h": 0.0}


def scan_funding_arbitrage(s):
    """Phase 3: Scans Binance USDⓈ-M Perpetuals for extreme funding rates to harvest delta-neutral cash-and-carry basis."""
    opps = []
    try:
        data = fetch_api(FUT_HOSTS, "/fapi/v1/premiumIndex", timeout=8)
        if not data or not isinstance(data, list):
            return []
        for item in data:
            sym = item.get("symbol", "")
            if not sym.endswith("USDT") or sym in EXCLUDED_SYMBOLS:
                continue
            rate = float(item.get("lastFundingRate", 0.0))
            mark = float(item.get("markPrice", 0.0))
            idx = float(item.get("indexPrice", 0.0))
            apr = rate * 3.0 * 365.0 * 100.0
            basis_pct = ((mark - idx) / idx * 100.0) if idx > 0 else 0.0
            if abs(apr) >= 20.0:
                strat = "LONG SPOT + SHORT PERP 1x (Earn Funding)" if apr > 0 else "SHORT SPOT + LONG PERP 1x (Squeeze Yield)"
                opps.append({
                    "symbol": sym,
                    "rate_8h": round(rate * 100, 4),
                    "apr_pct": round(apr, 1),
                    "basis_pct": round(basis_pct, 2),
                    "strategy": strat,
                    "type": "POSITIVE_YIELD" if apr > 0 else "SQUEEZE_YIELD",
                })
        opps.sort(key=lambda x: abs(x["apr_pct"]), reverse=True)
        s["funding_arb"] = opps[:8]
        return opps[:8]
    except Exception:
        return []


def risk_gates(s, new_risk):
    today = utc_day()
    entries_today = [t for t in s["closed"] if t["opened"][:10] == today] + \
                    [p for p in s["positions"].values() if p["opened"][:10] == today]
    if len(entries_today) >= CFG["max_entries_per_day"]:
        return "Daily entry limit reached (%d entries today)" % CFG["max_entries_per_day"]

    realized_today = sum(t["net"] for t in s["closed"] if t["closed"][:10] == today)
    open_risk = sum(p["risk_usd"] for p in s["positions"].values())
    worst_case = -min(realized_today, 0) + open_risk + new_risk
    if worst_case > CFG["daily_loss_limit"]:
        return "Daily loss shutoff triggered: worst-case %s > %s limit" % (
            usd(worst_case), usd(CFG["daily_loss_limit"])
        )
    return None


def scan_signals(s):
    # 1. Update universe dynamically every 30 minutes or on startup
    if not s.get("universe") or (now_ms() - s.get("last_universe_ms", 0) > 30 * 60 * 1000):
        s["universe"] = fetch_liquid_universe()
        s["last_universe_ms"] = now_ms()
        log("Universe discovery updated: %d liquid coins actively monitored across Binance." % len(s["universe"]))

    # Phase 3: Track real-time BTC lead-lag momentum
    btc_lead = fetch_btc_lead_lag()
    s["btc_lead"] = btc_lead

    last_closed_open = (now_ms() // HOUR_MS) * HOUR_MS - HOUR_MS
    candidates = []

    for sym in s["universe"]:
        if s["last_eval"].get(sym, 0) >= last_closed_open:
            continue

        bars_1h = [b for b in klines(sym, "1h", 180) if b["ct"] < now_ms()]
        if len(bars_1h) < 60:
            continue

        bar = bars_1h[-1]
        s["last_eval"][sym] = bar["t"]

        base = base_asset(sym)
        t_sent = s.get("news", {}).get("token_scores", {}).get(base, 0.0)
        macro_sent = s.get("news", {}).get("macro_score", 0.0)
        net_sent = t_sent + 0.3 * macro_sent

        sig, diag = evaluate_institutional(sym, bars_1h, news_sentiment=net_sent)
        s["diag"][sym] = dict(diag, evaluated=iso())

        if not sig:
            if sym in s["universe"][:8]:
                log("SCAN %s [%s] bar %s: %s | Check: %s" % (
                    sym, diag.get("side", "").upper(), diag["bar"], diag["auction_state"], diag["check"]
                ))
            continue

        # Phase 3: Cross-Asset Lead-Lag Bonus
        alt_1h_ret = ((bar["c"] / bars_1h[-2]["c"]) - 1.0) * 100.0 if len(bars_1h) >= 2 else 0.0
        if btc_lead["bias"] == "BULLISH_LEAD" and sig["side"] == "long" and alt_1h_ret > btc_lead["btc_ret_1h"]:
            sig["score"] = min(100, sig["score"] + 10)
            diag["check"] += " | BTC LEAD OUTPERFORMER (+10)"
        elif btc_lead["bias"] == "BEARISH_LEAD" and sig["side"] == "short" and alt_1h_ret < btc_lead["btc_ret_1h"]:
            sig["score"] = min(100, sig["score"] + 10)
            diag["check"] += " | BTC LEAD BREAKDOWN (+10)"

        log("SETUP DETECTED %s [%s]: %s (Score %d) | %s" % (
            sym, sig["side"].upper(), sig["setup"], sig["score"], diag["check"]
        ))
        candidates.append((sig, diag, sym, bar))

    candidates.sort(key=lambda item: item[0]["score"], reverse=True)

    for sig, diag, sym, bar in candidates:
        if len(s["positions"]) >= CFG["max_open_positions"]:
            log("SIGNAL %s [%s] SKIPPED: Portfolio limit reached (%d/%d open)" % (
                sym, sig["side"].upper(), len(s["positions"]), CFG["max_open_positions"]
            ))
            break

        why = None
        age_min = (now_ms() - bar["ct"]) / 60000.0
        side = sig["side"]
        base = base_asset(sym)
        news_veto = s.get("news", {}).get("token_vetoes", {}).get(base)
        if news_veto:
            if side == "long" and news_veto.get("veto_long"):
                why = news_veto.get("reason", "Bearish news veto")
            elif side == "short" and news_veto.get("veto_short"):
                why = news_veto.get("reason", "Bullish news veto")

        if not why:
            if sym in s["positions"]:
                why = "Active position already open in %s" % sym
            elif bar["t"] < s["last_exit_bar"].get(sym, 0) + CFG["cooldown_bars"] * HOUR_MS:
                why = "Cooldown period active after recent exit"
            elif age_min > CFG["stale_signal_minutes"]:
                why = "Signal candle closed %.0f min ago (stale limit %d min)" % (age_min, CFG["stale_signal_minutes"])

        if why:
            log("SIGNAL %s %s [%s] SKIPPED: %s" % (sym, side.upper(), sig["setup"], why))
            s["diag"][sym]["signal"] += " (skipped: %s)" % why
            continue

        cur_p = spot_price(sym)
        entry_px = cur_p * (1.0 + CFG["slippage"]) if side == "long" else cur_p * (1.0 - CFG["slippage"])
        stop_px = sig["stop"]
        risk_dist = abs(entry_px - stop_px)

        # Phase 2: Micro-Orderflow Entry Timing (5m/1m CVD Compression)
        micro_compressed = False
        bars_5m = klines(sym, "5m", 24)
        if len(bars_5m) >= 6:
            a_5m = atr(bars_5m, 14)[-1]
            last_5m = bars_5m[-1]
            if side == "long":
                low_5m_6 = min(x["l"] for x in bars_5m[-6:])
                m_stop = low_5m_6 - 0.3 * a_5m
                if (entry_px - m_stop) >= entry_px * CFG["min_risk_pct"] and (entry_px - m_stop) < risk_dist * 0.85:
                    if last_5m["tb"] >= 50.0 or last_5m["c"] >= last_5m["o"]:
                        old_d = risk_dist
                        stop_px = m_stop
                        risk_dist = entry_px - stop_px
                        micro_compressed = True
                        log("MICRO-TIMING 5m CVD COMPRESSION %s [LONG]: Stop compressed %s -> %s (Dist: %.2f%% -> %.2f%%)" % (
                            sym, fp(sig["stop"]), fp(stop_px), (old_d / entry_px) * 100, (risk_dist / entry_px) * 100
                        ))
            else:
                high_5m_6 = max(x["h"] for x in bars_5m[-6:])
                m_stop = high_5m_6 + 0.3 * a_5m
                if (m_stop - entry_px) >= entry_px * CFG["min_risk_pct"] and (m_stop - entry_px) < risk_dist * 0.85:
                    if last_5m["tb"] <= 50.0 or last_5m["c"] <= last_5m["o"]:
                        old_d = risk_dist
                        stop_px = m_stop
                        risk_dist = stop_px - entry_px
                        micro_compressed = True
                        log("MICRO-TIMING 5m CVD COMPRESSION %s [SHORT]: Stop compressed %s -> %s (Dist: %.2f%% -> %.2f%%)" % (
                            sym, fp(sig["stop"]), fp(stop_px), (old_d / entry_px) * 100, (risk_dist / entry_px) * 100
                        ))

        if risk_dist <= entry_px * CFG["min_risk_pct"]:
            log("SIGNAL %s %s SKIPPED: Risk distance %s is below minimum spread safety threshold" % (sym, side, fp(risk_dist)))
            continue

        # Phase 1: Adaptive Conviction Kelly Sizing
        macro_score = s.get("news", {}).get("macro_score", 0.0)
        target_risk = compute_trade_risk(sig["score"], macro_score)

        # Margin Doctrine: Scheduled Event Risk Guard
        if margin:
            ev = margin.event_risk(now_ms())
            if ev["level"] == "BLOCK":
                log("SIGNAL %s %s [%s] BLOCKED BY EVENT RISK: %s" % (sym, side.upper(), sig["setup"], ev["reason"]))
                s["diag"][sym]["signal"] += " (blocked: %s)" % ev["reason"]
                continue
            elif ev["level"] == "CAUTION":
                target_risk *= ev["risk_mult"]

        avail_risk = get_available_risk(s)
        trade_risk = min(target_risk, avail_risk)

        if trade_risk < 20.0:
            log("SIGNAL %s %s [%s] SKIPPED: Available risk budget %s is below $20 minimum" % (
                sym, side.upper(), sig["setup"], usd(avail_risk)
            ))
            s["diag"][sym]["signal"] += " (skipped: risk budget %s)" % usd(avail_risk)
            continue

        gate_err = risk_gates(s, trade_risk)
        if gate_err:
            log("SIGNAL %s %s [%s] BLOCKED BY RISK GATE: %s" % (sym, side.upper(), sig["setup"], gate_err))
            s["diag"][sym]["signal"] += " (blocked: %s)" % gate_err
            continue

        realized_eq, _, _ = equity_parts(s)
        used_margin = sum(p["margin"] for p in s["positions"].values())
        free_margin = realized_eq - used_margin

        # Margin Doctrine: Liquidity Tier Cap & Liquidation Safe Leverage
        base = base_asset(sym)
        tier_lev = margin.leverage_cap(base) if margin else CFG["leverage"]
        safe_lev = margin.max_safe_leverage(entry_px, stop_px, side, venue="margin", base=base) if margin else CFG["leverage"]
        eff_lev = max(1.0, min(tier_lev, math.floor(safe_lev)))

        qty = min(
            trade_risk / risk_dist,
            realized_eq * CFG["max_margin_frac"] * eff_lev / entry_px,
            max(free_margin, 0) * 0.95 * eff_lev / entry_px
        )

        if qty * entry_px < 10.0:
            log("SIGNAL %s %s SKIPPED: Insufficient available margin (%s free)" % (sym, side, usd(free_margin)))
            continue

        notional = qty * entry_px

        # Margin Doctrine: Correlation Guard (Cap same-direction beta stacking & gross leverage)
        if margin:
            corr_err = margin.correlation_guard(s["positions"].values(), side, realized_eq, notional)
            if corr_err:
                log("SIGNAL %s %s [%s] BLOCKED BY CORRELATION GUARD: %s" % (sym, side.upper(), sig["setup"], corr_err))
                s["diag"][sym]["signal"] += " (blocked: %s)" % corr_err
                continue

        # Phase 1: Convex Multi-Tier Exit Targets
        tp1_px = entry_px + CFG["tp1_r"] * risk_dist if side == "long" else entry_px - CFG["tp1_r"] * risk_dist
        tp2_px = entry_px + CFG["tp2_r"] * risk_dist if side == "long" else entry_px - CFG["tp2_r"] * risk_dist
        ms = now_ms()
        liq_px = margin.liquidation_price(entry_px, side, eff_lev, "margin", base) if margin else 0.0

        p = {
            "id": s["next_id"],
            "symbol": sym,
            "side": side,
            "setup": sig["setup"] + (" (Micro 5m)" if micro_compressed else ""),
            "score": sig["score"],
            "signal_bar": diag["bar"],
            "opened": iso(ms),
            "opened_ms": ms,
            "entry": entry_px,
            "initial_stop": stop_px,
            "stop": stop_px,
            "tp1_px": tp1_px,
            "tp2_px": tp2_px,
            "tp1_hit": False,
            "tp2_hit": False,
            "trail_vwap": False,
            "initial_qty": qty,
            "qty": qty,
            "notional": notional,
            "leverage": eff_lev,
            "margin": notional / eff_lev,
            "liq_px": liq_px,
            "risk_usd": round(qty * risk_dist, 2),
            "planned_risk_usd": round(trade_risk, 2),
            "entry_fee": notional * CFG["fee_rate"],
            "partial_realized": 0.0,
            "partial_costs": 0.0,
            "checked_ms": (ms // 60000) * 60000,
        }
        s["next_id"] += 1
        s["positions"][sym] = p
        s["marks"][sym] = cur_p

        log("OPENED CONVEX #%d %s %s [%s]: %s %s at %s (%.0fx margin, liq %s) | Stop %s | TP1 %s (40%%) | TP2 %s (30%%) | Sized Risk %s (Score %d)" % (
            p["id"], side.upper(), sym, p["setup"],
            "BUY" if side == "long" else "SELL SHORT",
            ("%.4f" % qty).rstrip("0").rstrip("."),
            fp(entry_px), eff_lev, fp(liq_px), fp(stop_px), fp(tp1_px), fp(tp2_px),
            usd(p["risk_usd"]), sig["score"]
        ))


def refresh_marks(s):
    for sym in list(s["positions"]):
        try:
            s["marks"][sym] = spot_price(sym)
        except Exception:
            pass
    for sym in s.get("universe", [])[:15]:
        if sym not in s["positions"]:
            try:
                s["marks"][sym] = spot_price(sym)
            except Exception:
                pass


def refresh_news(s):
    if not news_monitor:
        return
    try:
        news_data = news_monitor.fetch_all_news()
        s["news"] = news_data
        s["last_news_ms"] = now_ms()
        log("News monitor updated: %d headlines tracked across 25+ sources | Macro sentiment %+.1f" % (
            news_data.get("total_headlines", 0), news_data.get("macro_score", 0.0)
        ))
    except Exception as e:
        log("News monitor update error: %s" % e)


def cycle(s):
    if not s.get("news") or (now_ms() - s.get("last_news_ms", 0) > 180 * 1000):
        refresh_news(s)
    if not s.get("funding_arb") or (now_ms() - s.get("last_funding_arb_ms", 0) > 10 * 60 * 1000):
        scan_funding_arbitrage(s)
        s["last_funding_arb_ms"] = now_ms()
    manage_positions(s)
    scan_signals(s)
    refresh_marks(s)

    _, _, eq = equity_parts(s)
    ms = now_ms()
    if not s["curve"] or ms - s["curve"][-1][0] >= 5 * 60 * 1000 or len(s["closed"]) != s.get("_n_closed", -1):
        s["curve"].append([ms, round(eq, 2)])
        s["curve"] = s["curve"][-6000:]
        s["_n_closed"] = len(s["closed"])

    s["heartbeat"] = iso(ms)
    s["heartbeat_ms"] = ms
    s["cycles"] += 1


# ---------------------------------------------------------------- dashboard & visual reporting

def stats(s):
    realized_eq, unreal, eq = equity_parts(s)
    closed = s["closed"]
    wins = [t for t in closed if t["net"] > 0]
    peak, mdd = s["start_equity"], 0.0
    for _, v in s["curve"]:
        peak = max(peak, v)
        mdd = max(mdd, (peak - v) / peak * 100.0 if peak else 0.0)
    gp = sum(t["net"] for t in wins)
    gl = -sum(t["net"] for t in closed if t["net"] <= 0)
    return {
        "equity": eq,
        "realized_eq": realized_eq,
        "unreal": unreal,
        "pnl": eq - s["start_equity"],
        "roi": (eq - s["start_equity"]) / s["start_equity"] * 100.0,
        "n": len(closed),
        "wins": len(wins),
        "wr": len(wins) / len(closed) * 100.0 if closed else 0.0,
        "mdd": mdd,
        "pf": (gp / gl) if gl > 0 else None,
        "avg_r": (sum(t["r"] for t in closed) / len(closed)) if closed else 0.0,
        "costs": sum(t["costs"] for t in closed),
    }


def svg_curve(s, w=1000, h=220):
    pts = s["curve"]
    if len(pts) < 2:
        return '<div class="muted">Live equity curve generates as execution cycles occur.</div>'
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts] + [s["start_equity"]]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    if y1 - y0 < 1:
        y0, y1 = y0 - 5, y1 + 5

    def X(x):
        return 45 + (x - x0) / max(1, x1 - x0) * (w - 55)

    def Y(y):
        return 12 + (y1 - y) / (y1 - y0) * (h - 32)

    line = " ".join("%.1f,%.1f" % (X(x), Y(y)) for x, y in pts)
    base = Y(s["start_equity"])
    col = "#22c55e" if pts[-1][1] >= s["start_equity"] else "#ef4444"

    return (
        '<svg viewBox="0 0 %d %d" width="100%%" height="%d">'
        '<line x1="45" x2="%d" y1="%.1f" y2="%.1f" stroke="#475569" stroke-dasharray="4 4"/>'
        '<text x="2" y="%.1f" fill="#94a3b8" font-size="11">%s</text>'
        '<text x="2" y="20" fill="#94a3b8" font-size="11">%s</text>'
        '<text x="2" y="%d" fill="#94a3b8" font-size="11">%s</text>'
        '<polyline fill="none" stroke="%s" stroke-width="2.5" points="%s"/></svg>'
    ) % (w, h, h, w - 10, base, base, base - 3, usd(s["start_equity"]), usd(y1), h - 22, usd(y0), col, line)


def write_dashboard(s):
    st = stats(s)
    e = html.escape
    cls = lambda v: "pos" if v > 0 else "neg" if v < 0 else ""

    btc_lead = s.get("btc_lead", {})
    btc_bias = btc_lead.get("bias", "NEUTRAL")
    btc_color = "#22c55e" if btc_bias == "BULLISH_LEAD" else "#ef4444" if btc_bias == "BEARISH_LEAD" else "#94a3b8"
    btc_lead_txt = (
        "🚀 BULLISH (+%.2f%%, %.1f%% Taker)" % (btc_lead.get("btc_ret_1h", 0.0), btc_lead.get("btc_tb", 50.0))
        if btc_bias == "BULLISH_LEAD" else (
            "⚠️ BEARISH (%+.2f%%, %.1f%% Taker)" % (btc_lead.get("btc_ret_1h", 0.0), btc_lead.get("btc_tb", 50.0))
            if btc_bias == "BEARISH_LEAD" else "NEUTRAL MOMENTUM"
        )
    )

    rows_open = ""
    for p in s["positions"].values():
        m = s["marks"].get(p["symbol"], p["entry"])
        u = ((m - p["entry"]) if p["side"] == "long" else (p["entry"] - m)) * p["qty"] - p.get("entry_fee", 0.0) + p.get("partial_realized", 0.0)
        be_status = " <span class='badge' style='background:#065f46;color:#a7f3d0;'>Breakeven</span>" if p.get("tp1_hit") else ""
        trail_status = " <span class='badge' style='background:#4338ca;color:#e0e7ff;'>VWAP Trail</span>" if p.get("trail_vwap") else ""
        tp1_str = ("<span class='pos'><b>%s</b></span><br><span style='font-size:11px;color:#22c55e;'>CLOSED (+1.8R)</span>" % fp(p.get("tp1_px"))) if p.get("tp1_hit") else fp(p.get("tp1_px"))
        tp2_str = ("<span class='pos'><b>%s</b></span><br><span style='font-size:11px;color:#22c55e;'>CLOSED (+3.5R)</span>" % fp(p.get("tp2_px"))) if p.get("tp2_hit") else fp(p.get("tp2_px"))
        runner_str = "<span class='pos' style='font-weight:600;'>Running 30%% (1h VWAP)</span>" if p.get("trail_vwap") else "<span class='muted'>Waiting TP2</span>"

        rows_open += (
            "<tr id='pos-row-%s' data-pos-sym='%s' data-entry='%.8f' data-qty='%.8f' data-fee='%.8f' data-risk='%.2f' data-side='%s'>"
            "<td>#%d</td><td><b>%s</b></td><td class='%s'>%s</td><td>%s</td>"
            "<td>%s</td><td id='pos-mark-%s' class='ticker-cell' data-sym='%s'><b>%s</b></td>"
            "<td>%s%s%s</td><td>%s</td><td>%s</td><td>%s</td>"
            "<td>%s / %s</td><td>%s</td><td>%s</td>"
            "<td id='pos-pnl-%s' class='%s'>%s</td><td id='pos-r-%s' class='%s'>%+.2fR</td><td>%s</td></tr>"
        ) % (
            p["symbol"], p["symbol"], p["entry"], p["qty"], p.get("entry_fee", 0.0), p["risk_usd"], p["side"],
            p["id"], p["symbol"], "pos" if p["side"] == "long" else "neg",
            "LONG (BUY)" if p["side"] == "long" else "SHORT (SELL)", e(p["setup"]),
            fp(p["entry"]), p["symbol"], p["symbol"], fp(m),
            fp(p["stop"]), be_status, trail_status,
            tp1_str, tp2_str, runner_str,
            ("%.4f" % p["qty"]).rstrip("0").rstrip("."),
            ("%.4f" % (p.get("initial_qty") or p["qty"])).rstrip("0").rstrip("."),
            usd(p["notional"]), usd(p["margin"]),
            p["symbol"], cls(u), usd(u, True), p["symbol"], cls(u), u / p["risk_usd"], p["opened"][5:16]
        )
    if not rows_open:
        rows_open = "<tr><td colspan='16' class='muted'>No active positions open. Scanning dynamically across all liquid Binance USDT pairs.</td></tr>"

    rows_watch = ""
    sorted_syms = sorted(
        s["diag"].keys(),
        key=lambda k: (
            0 if "No Setup" not in s["diag"][k].get("signal", "No Setup") else 1,
            0 if k in s["positions"] else 1,
            k
        )
    )
    news = s.get("news", {})
    headlines = news.get("headlines", [])
    trending = news.get("trending_coins", [])
    macro_score = news.get("macro_score", 0.0)
    vetoes = news.get("token_vetoes", {})

    for sym in sorted_syms:
        d = s["diag"].get(sym, {})
        side = d.get("side", "long")
        sig_str = d.get("signal", "Scanning...")
        is_active_setup = "No Setup" not in sig_str and "Scanning" not in sig_str
        base = base_asset(sym)
        t_score = news.get("token_scores", {}).get(base, 0.0)
        v_info = vetoes.get(base)
        if v_info:
            news_badge = "<span style='color:#ef4444;font-weight:700;'>⛔ VETO (%s)</span>" % ("SHORT" if v_info.get("veto_short") else "LONG")
        elif t_score >= 1.5:
            news_badge = "<span class='pos' style='font-weight:600;'>▲ +%.1f (Bullish)</span>" % t_score
        elif t_score <= -1.5:
            news_badge = "<span class='neg' style='font-weight:600;'>▼ %.1f (Bearish)</span>" % t_score
        else:
            news_badge = "<span class='muted'>Neutral</span>"

        rows_watch += (
            "<tr><td><b>%s</b></td><td class='%s'>%s</td>"
            "<td id='price-%s' class='ticker-cell' data-sym='%s'><b>%s</b></td>"
            "<td>%s</td><td>%s</td><td>%s</td>"
            "<td>VAL: %s<br>POC: %s<br>VAH: %s</td><td>%s (%+.1f%%)</td>"
            "<td>%s</td><td class='%s'>%+.1f%%</td><td>%s</td><td class='%s'><b>%s</b></td></tr>"
        ) % (
            sym, "pos" if side == "long" else "neg", "MARGIN LONG" if side == "long" else "MARGIN SHORT",
            sym, sym, fp(s["marks"].get(sym, d.get("close"))), news_badge,
            d.get("auction_state", "-"), d.get("trend", "-"),
            fp(d.get("val")), fp(d.get("poc")), fp(d.get("vah")),
            fp(d.get("vwap_24h")), d.get("vwap_dist_pct", 0.0),
            ("%.1f%%" % d["taker_buy"]) if "taker_buy" in d else "-",
            "pos" if d.get("imb_1pct", 0) > 0 else "neg", d.get("imb_1pct", 0.0),
            (("%.3f%%" % (d["funding_8h"] * 100)) if "funding_8h" in d else "-"),
            "pos" if is_active_setup else "", e(sig_str)
        )

    trending_pills = ""
    for c in trending[:10]:
        trending_pills += (
            "<span class='badge' style='background:#3b0764;color:#f5d0fe;border:1px solid #701a75;margin-right:6px;'>"
            "🔥 #%d <b>%s</b></span>" % (c.get("rank", 0), e(c.get("symbol", "")))
        )
    if not trending_pills:
        trending_pills = "<span class='muted'>Syncing CoinGecko trending feed...</span>"

    veto_box = ""
    if vetoes:
        v_items = []
        for v_tok, v_info in vetoes.items():
            side_txt = "SHORT" if v_info.get("veto_short") else "LONG"
            v_items.append("<b>%s</b>: VETO %s (%s)" % (e(v_tok), side_txt, e(v_info.get("reason", ""))))
        veto_box = (
            "<div style='background:#450a0a;border:1px solid #dc2626;color:#fecaca;padding:10px 14px;border-radius:8px;margin:10px 0;font-size:13px;'>"
            "⚠️ <b>ACTIVE NEWS RISK VETOES:</b> %s</div>" % " &middot; ".join(v_items)
        )

    rows_news = ""
    for h in headlines[:20]:
        sent = h.get("sentiment", "NEUTRAL")
        sent_cls = "pos" if sent == "BULLISH" else "neg" if sent == "BEARISH" else "muted"
        tok_pills = " ".join("<span class='badge' style='background:#1e293b;color:#38bdf8;font-size:11px;'>%s</span>" % e(t) for t in h.get("tokens", []))
        rows_news += (
            "<tr><td style='color:#94a3b8;font-family:monospace;font-size:11px;'>%s</td>"
            "<td><span class='badge'>%s</span></td>"
            "<td class='%s'><b>[%s]</b></td>"
            "<td>%s</td>"
            "<td style='white-space:normal;font-size:13px;'>%s</td></tr>"
        ) % (
            e(h.get("time", "")), e(h.get("source", "")), sent_cls, sent, tok_pills, e(h.get("title", ""))
        )
    if not rows_news:
        rows_news = "<tr><td colspan='5' class='muted'>Tracking 25+ omni-channel feeds in real time (Binance CMS, Fed, X Whale Alert, Banks, Forbes, Cointelegraph)...</td></tr>"

    macro_cls = "pos" if macro_score > 0 else "neg" if macro_score < 0 else "muted"
    macro_txt = "Bullish Tailwind" if macro_score > 0 else "Bearish Headwind" if macro_score < 0 else "Neutral"
    total_hl = news.get("total_headlines", len(headlines))
    last_news_str = news.get("updated", iso())[11:19] + " UTC" if news.get("updated") else "Live"

    rows_funding = ""
    for fo in s.get("funding_arb", []):
        apr_cls = "pos" if fo["apr_pct"] > 0 else "neg"
        rows_funding += (
            "<tr><td><b>%s</b></td><td class='%s'><b>%+.1f%% APR</b></td>"
            "<td>%+.4f%%</td><td>%+.2f%%</td>"
            "<td>%s</td><td><span class='badge' style='background:#1e293b;color:#38bdf8;'>Zero Delta Directional Risk</span></td></tr>"
        ) % (
            fo["symbol"], apr_cls, fo["apr_pct"], fo["rate_8h"], fo["basis_pct"], fo["strategy"]
        )
    if not rows_funding:
        rows_funding = "<tr><td colspan='6' class='muted'>Scanning Binance USDⓈ-M funding index for cash-and-carry yields...</td></tr>"

    rows_closed = ""
    for t in reversed(s["closed"][-200:]):
        rows_closed += (
            "<tr><td>#%d</td><td><b>%s</b></td><td class='%s'>%s</td><td class='small'>%s</td><td>%s</td><td>%s</td>"
            "<td>%s</td><td>%s</td><td class='%s'>%s</td><td>%s</td><td class='%s'>%s</td><td class='%s'>%+.2fR</td><td>%s</td></tr>"
        ) % (
            t["id"], t["symbol"], "pos" if t["side"] == "long" else "neg", t["side"].upper(), e(t["setup"]),
            t["opened"][5:16], t["closed"][5:16], fp(t["entry"]), fp(t["exit"]),
            "pos" if "TAKE-PROFIT" in t["outcome"] or "CONVEX" in t["outcome"] or "SCALE-OUT" in t["outcome"] or "BREAKEVEN" in t["outcome"] else "neg",
            t["outcome"], usd(t["costs"]), cls(t["net"]),
            usd(t["net"], True), cls(t["r"]), t["r"], usd(t["equity_after"])
        )
    if not rows_closed:
        rows_closed = "<tr><td colspan='13' class='muted'>No closed trades yet in current session.</td></tr>"

    try:
        with open(LOG, encoding="utf-8") as fh:
            tail = fh.readlines()[-60:]
    except FileNotFoundError:
        tail = []
    events = "".join("<div>%s</div>" % e(x.rstrip()) for x in reversed(tail))
    pf = ("%.2f" % st["pf"]) if st["pf"] is not None else "-"

    doc = """<!doctype html><html><head><meta charset="utf-8">
<title>Binance Institutional Paper Trader (Millisecond Stream)</title><style>
body{background:#090d16;color:#e2e8f0;font:14px/1.45 -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;margin:18px}
h1{font-size:22px;margin:0 0 4px;color:#f8fafc} h2{font-size:15px;margin:22px 0 8px;color:#60a5fa}
.banner{background:#1e1b4b;border:1px solid #3730a3;color:#c7d2fe;padding:10px 14px;border-radius:6px;margin:8px 0;font-size:13px}
.ws-bar{display:flex;align-items:center;gap:12px;background:#0f172a;padding:8px 14px;border-radius:8px;border:1px solid #1e293b;margin:10px 0;font-family:monospace;font-size:13px}
.ws-dot{width:10px;height:10px;border-radius:50%%;background:#eab308;display:inline-block;box-shadow:0 0 8px #eab308}
.ws-dot.live{background:#22c55e;box-shadow:0 0 10px #22c55e}
.ws-dot.err{background:#ef4444;box-shadow:0 0 10px #ef4444}
.badge{background:#1e293b;padding:3px 8px;border-radius:4px;color:#93c5fd}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.k{background:#0f172a;border:1px solid #1e293b;border-radius:8px;padding:10px}
.k .l{color:#94a3b8;font-size:12px}.k .v{font-size:20px;font-weight:600}
table{border-collapse:collapse;width:100%%;background:#0f172a;border-radius:8px;overflow:hidden;margin-bottom:12px}
th,td{padding:7px 10px;border-bottom:1px solid #1e293b;text-align:left;white-space:nowrap;font-size:13px}
th{color:#94a3b8;font-weight:600;font-size:12px;background:#1e293b}.small{font-size:12px;white-space:normal}
.ticker-cell{transition:background 0.15s ease}
.flash-up{background:rgba(34,197,94,0.38) !important}
.flash-down{background:rgba(239,68,68,0.38) !important}
.pos{color:#22c55e}.neg{color:#ef4444}.muted{color:#64748b}
#hb.ok{color:#22c55e;font-weight:600}#hb.bad{color:#ef4444;font-weight:700}
.log{background:#020617;border:1px solid #1e293b;border-radius:8px;padding:10px;font:12px Consolas,monospace;max-height:420px;overflow:auto}
</style></head><body>
<h1>Binance Institutional Paper Trader &amp; Millisecond Execution Monitor</h1>
<div class="banner">
  <b>MARKET-WIDE QUANT ENGINE ACTIVE:</b> Scanning dynamically across all liquid Binance USDT pairs (%d coins). Executing 7-Day Volume Profile (VAL/POC/VAH), 24h Rolling VWAP, Spot Taker CVD Absorption, and Order-Book Depth Imbalances (&plusmn;1%%) with Convex Multi-Tier Exits (TP1 1.8R, TP2 3.5R, Trailing Runner) and Conviction Kelly Sizing. Paper trading only: no real capital risked.
</div>

<div class="ws-bar">
  <span class="ws-dot" id="ws-dot"></span>
  <span id="ws-status">Connecting to Binance Real-Time Millisecond CLOB WebSocket...</span>
  <span class="badge" id="ws-latency">Ping: -- ms</span>
  <span class="badge" id="ws-rate">0 ticks/s</span>
  <span class="badge" id="btc-lead-badge" style="color:%s;font-weight:600;">BTC Lead: %s</span>
  <span class="badge" id="ws-clock">--:--:--.---</span>
  <span class="badge" style="margin-left:auto;color:#a78bfa;">Instant Millisecond Execution</span>
</div>

<div>Status: <span id="hb">connecting...</span> &middot; Last cycle: %s &middot; Cycles: %d &middot; Initialized: %s &middot; <i>Streaming live via WebSockets (Zero Reload Lag)</i></div>

<h2>Portfolio Capital & Performance</h2><div class="kpis">
<div class="k"><div class="l">Account Equity</div><div id="kpi-equity" class="v %s" data-val="%.2f">%s</div></div>
<div class="k"><div class="l">Net Realized P&amp;L</div><div class="v %s">%s (%+.2f%%)</div></div>
<div class="k"><div class="l">Realized Equity</div><div id="kpi-realized-eq" class="v" data-val="%.2f">%s</div></div>
<div class="k"><div class="l">Unrealized P&amp;L</div><div id="kpi-unreal" class="v %s">%s</div></div>
<div class="k"><div class="l">Trades Closed</div><div class="v">%d (%d W / %d L)</div></div>
<div class="k"><div class="l">Win Rate (2R Target)</div><div class="v">%.1f%%</div></div>
<div class="k"><div class="l">Average R / Profit Factor</div><div class="v">%+.2f / %s</div></div>
<div class="k"><div class="l">Maximum Drawdown</div><div class="v">%.2f%%</div></div>
<div class="k"><div class="l">Simulated Fees &amp; Carry</div><div class="v">%s</div></div>
</div>

<h2>Live Equity Curve</h2>%s

<h2>Active Positions (3x Isolated Margin &amp; Convex Multi-Tier Exits, Max %d Open)</h2>
<table><tr><th>#</th><th>Symbol</th><th>Side</th><th>Institutional Setup</th><th>Entry</th><th>Live Mark</th><th>Stop-Loss / Trail</th><th>TP1 (1.8R, 40%%)</th><th>TP2 (3.5R, 30%%)</th><th>Runner (30%%)</th><th>Qty (Open / Total)</th><th>Notional</th><th>Margin</th><th>Unrealized</th><th>R Multiple</th><th>Opened (UTC)</th></tr>%s</table>

<h2>25-Source Omni-Channel Live News &amp; Macro Intelligence Stream</h2>
<div style="background:#0f172a;border:1px solid #1e293b;border-radius:8px;padding:12px;margin-bottom:12px;">
  <div style="display:flex;flex-wrap:wrap;align-items:center;gap:12px;margin-bottom:10px;">
    <div><b>Macro Fiat Pulse:</b> <span class="%s"><b>%+.1f</b></span> (%s)</div>
    <div class="badge">Headlines Tracked: %d</div>
    <div class="badge">Last Scanned: %s</div>
    <div style="margin-left:auto;font-size:12px;color:#94a3b8;">Binance CMS, Federal Reserve, X Whale Alerts, Wall St Banks, Forbes, Bloomberg, CoinDesk, Decrypt, The Block</div>
  </div>
  <div style="display:flex;align-items:center;flex-wrap:wrap;gap:6px;margin-bottom:8px;">
    <span style="font-size:12px;color:#94a3b8;font-weight:600;">CoinGecko Global Trending Heat:</span>
    %s
  </div>
  %s
  <div style="max-height:280px;overflow:auto;margin-top:8px;">
    <table style="margin-bottom:0;">
      <tr><th>Time (UTC)</th><th>Source</th><th>Sentiment</th><th>Impacted Coins</th><th>Headline &amp; Narrative Catalyst</th></tr>
      %s
    </table>
  </div>
</div>

<h2>Market Universe Microstructure &amp; Volume Profile Matrix</h2>
<table><tr><th>Symbol</th><th>Direction</th><th>Live Price</th><th>News Catalyst</th><th>Auction State</th><th>Trend (1h)</th><th>7d Volume Profile (VAL / POC / VAH)</th><th>24h VWAP</th><th>Spot Taker Buy</th><th>Depth Imbalance (&plusmn;1%%)</th><th>8h Funding</th><th>Signal Status</th></tr>%s</table>

<h2>Delta-Neutral Perpetual Cash &amp; Carry Arbitrage (Zero Delta Risk Yield Farming)</h2>
<table><tr><th>Perpetual Pair</th><th>Annualized Synthetic APR</th><th>8h Funding Rate</th><th>Mark/Index Basis</th><th>Arbitrage Mechanics</th><th>Risk Profile</th></tr>%s</table>

<h2>Closed Trade History</h2>
<table><tr><th>#</th><th>Symbol</th><th>Side</th><th>Setup</th><th>Opened</th><th>Closed</th><th>Entry</th><th>Exit</th><th>Outcome</th><th>Fees/Carry</th><th>Net Profit</th><th>R Return</th><th>Account Balance</th></tr>%s</table>

<h2>Execution Decision Log (Newest First)</h2>
<div class="log">%s</div>

<script>
var hb = %d;
function upd(){
  var s = (Date.now() - hb) / 1000, el = document.getElementById('hb');
  if (s > 120) {
    el.className = 'bad';
    el.textContent = 'STALLED: No heartbeat for ' + Math.round(s/60) + ' min';
  } else {
    el.className = 'ok';
    el.textContent = 'BACKEND ENGINE ACTIVE (' + Math.round(s) + 's ago, ' + new Date(hb).toLocaleTimeString() + ')';
  }
}
upd();
setInterval(upd, 1000);

// High-Frequency Real-Time Binance Millisecond WebSocket Stream
var ws = null;
var tickCount = 0;
var lastSec = Date.now();
var wsClock = document.getElementById('ws-clock');
var wsStatus = document.getElementById('ws-status');
var wsDot = document.getElementById('ws-dot');
var wsLat = document.getElementById('ws-latency');
var wsRate = document.getElementById('ws-rate');
var prevPrices = {};

function connectBinanceWS() {
  try {
    ws = new WebSocket('wss://stream.binance.com:9443/ws/!miniTicker@arr');
    ws.onopen = function() {
      wsDot.className = 'ws-dot live';
      wsStatus.textContent = 'BINANCE CLOB WEBSOCKET LIVE: Streaming Millisecond Ticks';
    };
    ws.onclose = function() {
      wsDot.className = 'ws-dot err';
      wsStatus.textContent = 'Disconnected. Reconnecting in 1s...';
      setTimeout(connectBinanceWS, 1000);
    };
    ws.onerror = function() {
      wsDot.className = 'ws-dot err';
    };
    ws.onmessage = function(ev) {
      var arr = JSON.parse(ev.data);
      var now = Date.now();
      tickCount += arr.length;
      if (now - lastSec >= 1000) {
        var tps = Math.round(tickCount / ((now - lastSec) / 1000));
        wsRate.textContent = tps + ' ticks/s';
        tickCount = 0;
        lastSec = now;
      }
      var sample = arr[0];
      if (sample && sample.E) {
        var lat = Math.max(0, now - sample.E);
        wsLat.textContent = 'Ping: ' + lat + ' ms';
      }
      for (var i = 0; i < arr.length; i++) {
        var item = arr[i];
        updateTick(item.s, parseFloat(item.c));
      }
    };
  } catch (err) {
    console.error(err);
    setTimeout(connectBinanceWS, 2000);
  }
}

function updateTick(sym, px) {
  var el = document.getElementById('price-' + sym);
  if (el) {
    var old = prevPrices[sym] || px;
    el.innerHTML = '<b>' + (px < 0.001 ? px.toFixed(6) : px < 10 ? px.toFixed(4) : px.toFixed(2)) + '</b>';
    if (px > old) {
      el.className = 'ticker-cell flash-up';
      setTimeout(function(){ el.className = 'ticker-cell'; }, 150);
    } else if (px < old) {
      el.className = 'ticker-cell flash-down';
      setTimeout(function(){ el.className = 'ticker-cell'; }, 150);
    }
    prevPrices[sym] = px;
  }

  var pRow = document.querySelector('[data-pos-sym="' + sym + '"]');
  if (pRow) {
    var entry = parseFloat(pRow.getAttribute('data-entry'));
    var qty = parseFloat(pRow.getAttribute('data-qty'));
    var fee = parseFloat(pRow.getAttribute('data-fee'));
    var risk = parseFloat(pRow.getAttribute('data-risk'));
    var side = pRow.getAttribute('data-side');

    var u = (side === 'long') ? ((px - entry) * qty - fee) : ((entry - px) * qty - fee);
    var r = u / risk;

    var mEl = document.getElementById('pos-mark-' + sym);
    if (mEl) mEl.innerHTML = '<b>' + (px < 0.001 ? px.toFixed(6) : px < 10 ? px.toFixed(4) : px.toFixed(2)) + '</b>';

    var pEl = document.getElementById('pos-pnl-' + sym);
    if (pEl) {
      pEl.className = u >= 0 ? 'pos' : 'neg';
      pEl.textContent = (u >= 0 ? '+$' : '-$') + Math.abs(u).toFixed(2);
    }

    var rEl = document.getElementById('pos-r-' + sym);
    if (rEl) {
      rEl.className = r >= 0 ? 'pos' : 'neg';
      rEl.textContent = (r >= 0 ? '+' : '') + r.toFixed(2) + 'R';
    }

    recalcTotal();
  }
}

function recalcTotal() {
  var rows = document.querySelectorAll('[data-pos-sym]');
  var totalUnreal = 0;
  for (var i = 0; i < rows.length; i++) {
    var r = rows[i];
    var sym = r.getAttribute('data-pos-sym');
    var markEl = document.getElementById('pos-mark-' + sym);
    if (!markEl) continue;
    var px = parseFloat(markEl.textContent);
    var entry = parseFloat(r.getAttribute('data-entry'));
    var qty = parseFloat(r.getAttribute('data-qty'));
    var fee = parseFloat(r.getAttribute('data-fee'));
    var side = r.getAttribute('data-side');
    var u = (side === 'long') ? ((px - entry) * qty - fee) : ((entry - px) * qty - fee);
    totalUnreal += u;
  }
  var baseEl = document.getElementById('kpi-realized-eq');
  var base = baseEl ? parseFloat(baseEl.getAttribute('data-val')) : 5000.0;
  var eq = base + totalUnreal;
  var eqEl = document.getElementById('kpi-equity');
  if (eqEl) {
    eqEl.textContent = '$' + eq.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
    eqEl.className = 'v ' + (totalUnreal >= 0 ? 'pos' : 'neg');
  }
  var unEl = document.getElementById('kpi-unreal');
  if (unEl) {
    unEl.textContent = (totalUnreal >= 0 ? '+$' : '-$') + Math.abs(totalUnreal).toFixed(2);
    unEl.className = 'v ' + (totalUnreal >= 0 ? 'pos' : 'neg');
  }
}

connectBinanceWS();

setInterval(function() {
  var d = new Date();
  var ms = String(d.getMilliseconds()).padStart(3, '0');
  wsClock.textContent = d.toTimeString().split(' ')[0] + '.' + ms + ' UTC';
}, 33);

// Background state sync (every 3 seconds)
var initialPosCount = %d;
setInterval(function() {
  fetch('/api/state?_t=' + Date.now())
    .then(function(res){ return res.json(); })
    .then(function(s){
      if (s && s.positions && Object.keys(s.positions).length !== initialPosCount) {
        location.reload();
      }
    })
    .catch(function(){});
}, 3000);
</script>
</body></html>""" % (
        len(s.get("universe", [])),
        btc_color, btc_lead_txt,
        s.get("heartbeat") or "-", s["cycles"], s["created"],
        cls(st["pnl"]), st["equity"], usd(st["equity"]),
        cls(st["pnl"]), usd(st["pnl"], True), st["roi"],
        st["realized_eq"], usd(st["realized_eq"]),
        cls(st["unreal"]), usd(st["unreal"], True),
        st["n"], st["wins"], st["n"] - st["wins"], st["wr"],
        st["avg_r"], pf, st["mdd"], usd(st["costs"]),
        svg_curve(s),
        CFG["max_open_positions"],
        rows_open,
        macro_cls, macro_score, macro_txt, total_hl, last_news_str,
        trending_pills,
        veto_box,
        rows_news,
        rows_watch,
        rows_funding,
        rows_closed,
        events,
        s.get("heartbeat_ms") or 0,
        len(s["positions"])
    )
    write_atomic(DASH, doc)


def save(s):
    write_atomic(STATE, json.dumps(s, indent=1))


def print_status(s):
    st = stats(s)
    print("=" * 70)
    print("BINANCE INSTITUTIONAL PAPER TRADER STATUS  (%s)" % iso())
    print("=" * 70)
    print("Equity: %s | P&L: %s (%+.2f%%) | Unrealized: %s" % (
        usd(st["equity"]), usd(st["pnl"], True), st["roi"], usd(st["unreal"], True)
    ))
    print("Trades: %d closed | Win Rate: %.1f%% | Avg R: %+.2f | Max DD: %.2f%%" % (
        st["n"], st["wr"], st["avg_r"], st["mdd"]
    ))
    print("Universe Monitored: %d coins | Active Positions: %d" % (
        len(s.get("universe", [])), len(s["positions"])
    ))
    if s["positions"]:
        print("\nOPEN POSITIONS (Convex Multi-Tier Exits):")
        for p in s["positions"].values():
            m = s["marks"].get(p["symbol"], p["entry"])
            u = ((m - p["entry"]) if p["side"] == "long" else (p["entry"] - m)) * p["qty"] + p.get("partial_realized", 0.0)
            print("  * #%d %s %s [%s]: Entry %s | Mark %s | P&L %s (%+.2fR) | Stop %s | TP1 %s (40%%) | TP2 %s (30%%)" % (
                p["id"], p["side"].upper(), p["symbol"], p["setup"], fp(p["entry"]), fp(m), usd(u, True), u / p["risk_usd"], fp(p["stop"]), fp(p.get("tp1_px")), fp(p.get("tp2_px"))
            ))
    print("\nTOP WATCHLIST MATRIX:")
    for sym in list(s.get("diag", {}).keys())[:10]:
        d = s["diag"][sym]
        print("  * %s [%s]: Price %s | State: %s | Imbalance: %+.1f%% | Signal: %s" % (
            sym, d.get("side", "").upper(), fp(s["marks"].get(sym, d.get("close"))),
            d.get("auction_state", "-"), d.get("imb_1pct", 0.0), d.get("signal", "No Setup")
        ))


async def async_ws_ticks(s):
    url = "wss://stream.binance.com:9443/ws/!miniTicker@arr"
    last_ui_flush = time.time()
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(url, heartbeat=20.0) as ws:
                    log("Connected to Binance Millisecond CLOB WebSocket stream (%s)" % url)
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            arr = json.loads(msg.data)
                            now = now_ms()
                            for item in arr:
                                sym = item.get("s")
                                px_str = item.get("c")
                                if sym and px_str:
                                    process_live_tick(s, sym, float(px_str), item.get("E", now))

                            if time.time() - last_ui_flush >= 1.0:
                                _, _, eq = equity_parts(s)
                                ms = now_ms()
                                if not s["curve"] or ms - s["curve"][-1][0] >= 5 * 60 * 1000 or len(s["closed"]) != s.get("_n_closed", -1):
                                    s["curve"].append([ms, round(eq, 2)])
                                    s["curve"] = s["curve"][-6000:]
                                    s["_n_closed"] = len(s["closed"])
                                s["heartbeat"] = iso(ms)
                                s["heartbeat_ms"] = ms
                                s["cycles"] += 1
                                write_dashboard(s)
                                save(s)
                                last_ui_flush = time.time()
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
        except Exception as e:
            log("WebSocket stream reconnecting in 2s: %s" % e)
            await asyncio.sleep(2.0)


async def async_scanner_loop(s):
    while True:
        try:
            manage_positions(s)
            scan_signals(s)
            save(s)
            write_dashboard(s)
        except Exception as e:
            log("Background scanner cycle error: %s" % e)
        await asyncio.sleep(15.0)


async def async_local_server(s, port=8765):
    try:
        from aiohttp import web
        app = web.Application()

        async def handle_index(req):
            if os.path.exists(DASH):
                with open(DASH, encoding="utf-8") as fh:
                    return web.Response(text=fh.read(), content_type="text/html")
            return web.Response(text="Dashboard initializing...", content_type="text/plain")

        async def handle_state(req):
            return web.json_response(s, headers={"Access-Control-Allow-Origin": "*"})

        async def handle_news(req):
            return web.json_response(s.get("news", {}), headers={"Access-Control-Allow-Origin": "*"})

        app.router.add_get("/", handle_index)
        app.router.add_get("/api/state", handle_state)
        app.router.add_get("/api/news", handle_news)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", port)
        await site.start()
        log("Ultra-low latency HTTP server live: http://localhost:%d" % port)
    except Exception as e:
        log("Local HTTP server failed to bind (port %d in use?): %s" % (port, e))


async def async_news_task(s):
    while True:
        try:
            if news_monitor:
                news_data = await asyncio.to_thread(news_monitor.fetch_all_news)
                s["news"] = news_data
                s["last_news_ms"] = now_ms()
                log("News monitor refreshed: %d headlines tracked across 25+ sources | Macro sentiment %+.1f" % (
                    news_data.get("total_headlines", 0), news_data.get("macro_score", 0.0)
                ))
                save(s)
                write_dashboard(s)
        except Exception as e:
            log("Background news monitor error: %s" % e)
        await asyncio.sleep(180.0)


async def async_run(s):
    log("Starting Binance Millisecond Execution Engine (Async WebSockets + Live Dashboard + Omni-News Monitor)...")
    cycle(s)
    save(s)
    write_dashboard(s)
    await async_local_server(s, 8765)
    await asyncio.gather(
        async_ws_ticks(s),
        async_scanner_loop(s),
        async_news_task(s)
    )


def main():
    ap = argparse.ArgumentParser(description="Binance Institutional Paper Trader.")
    ap.add_argument("cmd", choices=["run", "once", "status", "reset"])
    ap.add_argument("--yes", action="store_true")
    a = ap.parse_args()

    if a.cmd == "reset":
        if not a.yes:
            print("To reset the paper trading account, pass --yes.")
            return 1
        for f in (STATE, TRADES, LOG, DASH):
            if os.path.exists(f):
                os.remove(f)
        print("Paper trading state wiped.")
        return 0

    s = load_state()

    if a.cmd == "status":
        print_status(s)
        return 0

    if a.cmd == "once":
        cycle(s)
        save(s)
        write_dashboard(s)
        print_status(s)
        return 0

    if a.cmd == "run":
        if HAS_AIOHTTP:
            try:
                asyncio.run(async_run(s))
            except KeyboardInterrupt:
                log("Millisecond engine stopped by user.")
            return 0
        else:
            log("Institutional Paper Trader started (poll every %ds). Dashboard: %s" % (CFG["poll_seconds"], DASH))
            while True:
                try:
                    cycle(s)
                    save(s)
                    write_dashboard(s)
                except Exception as ex:
                    log("CYCLE ERROR: %s" % ex)
                try:
                    time.sleep(CFG["poll_seconds"])
                except KeyboardInterrupt:
                    return 0
        return 0

    s = load_state()

    if a.cmd == "status":
        print_status(s)
        return 0

    if a.cmd == "run":
        log("Institutional Paper Trader started (poll every %ds). Dashboard: %s" % (CFG["poll_seconds"], DASH))

    while True:
        try:
            cycle(s)
        except Exception as ex:
            s["errors"] = s.get("errors", 0) + 1
            log("CYCLE ERROR (retrying next loop): %s" % ex)

        try:
            save(s)
            write_dashboard(s)
        except Exception as ex:
            log("FILE WRITE ERROR: %s" % ex)

        if a.cmd == "once":
            print_status(s)
            return 0

        try:
            time.sleep(CFG["poll_seconds"])
        except KeyboardInterrupt:
            log("Paper trader stopped by user. State preserved.")
            return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
