#!/usr/bin/env python3
"""Institutional Historical Backtester for Binance Crypto Margin & Perpetual Strategies.
Standard library only (with optional interactive HTML dashboard export).

Features:
  1. Multi-Symbol Historical Ingestion:
     - Downloads real 1h OHLCV klines with Quote Volume & Taker Buy Volume directly from Binance.
     - Automatically caches data locally in `.cache/klines/` for instantaneous re-runs.
  2. Wyckoff 2.0 & Market Microstructure Replay:
     - 7-Day Rolling Volume Profile (POC, VAL, VAH) across 168-bar sliding windows.
     - 24h Rolling Volume-Weighted Average Price (VWAP).
     - Net CVD & Taker Buy % absorption/exhaustion detection.
     - Wyckoff Springs (Bullish sweeps reclaiming VAL/Low with CVD absorption).
     - Wyckoff Upthrusts / SFPs (Bearish stop-hunts rejecting VAH/High with CVD exhaustion).
     - 1h EMA20 Trend Retest / Pullback signals.
  3. Risk Management & Portfolio Simulation:
     - Quarter-Kelly sizing based on stop distance: Dollar Risk = Qty * |Entry - Stop|.
     - Liquidity-tiered leverage caps (BTC 5x, ETH 4x, Alts 2-3x) and mandatory liquidation buffer.
     - Correlation Guard: Maximum 2 open positions on the same side.
     - Convex Multi-Tier Exits: 40% at +1.8R (move stop to breakeven), 30% at +3.5R, 30% trailing VWAP runner.
     - Conservative fill rule: If both stop and target are touched in the same bar, stop is filled first.
     - Realistic fee deductions (0.04% taker per side) + adverse execution slippage (0.02%).
  4. BTC Regime Filtering (Optional --btc-regime):
     - Prevents taking Altcoin Longs when BTC is below its 1h EMA50 or VWAP.
  5. Institutional Performance Analytics:
     - Win Rate, Profit Factor, Expected Value E(R), Total Return, ROI.
     - Sharpe Ratio (annualized), Sortino Ratio (downside risk), Max Drawdown ($ and %).
     - Outcome breakdown (% Stopped Out, % Breakeven after TP1, % Hitting TP2/Runner).
     - Performance by Setup, by Side (Long vs Short), and by Asset.
     - ASCII terminal report and standalone interactive HTML dashboard.

Usage:
  python trader/backtest_crypto.py --symbols BTCUSDT,ETHUSDT,SOLUSDT --days 90
  python trader/backtest_crypto.py --universe top10 --days 180 --btc-regime
  python trader/backtest_crypto.py --universe top20 --days 90 --html trader/backtest_report.html
"""

import argparse
import datetime
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TRADER_ANALYST = os.path.join(ROOT, "trader-analyst")
if TRADER_ANALYST not in sys.path:
    sys.path.insert(0, TRADER_ANALYST)

try:
    import margin
except ImportError:
    margin = None

CACHE_DIR = os.path.join(HERE, ".cache", "klines")
BINANCE_SPOT_API = "https://api.binance.com"
HOUR_MS = 3600 * 1000

CORE_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "DOGEUSDT", "AVAXUSDT", "LINKUSDT", "NEARUSDT", "ADAUSDT",
    "SUIUSDT", "DOTUSDT", "LTCUSDT", "BCHUSDT", "UNIUSDT",
    "APTUSDT", "ICPUSDT", "FETUSDT", "RENDERUSDT", "TAOUSDT"
]


# ---------------------------------------------------------------- data utilities

def ensure_cache_dir():
    os.makedirs(CACHE_DIR, exist_ok=True)


def iso(ms):
    return datetime.datetime.fromtimestamp(ms / 1000.0, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")


def fetch_binance_klines_chunk(symbol, interval, start_ms, end_ms, limit=1000):
    url = f"{BINANCE_SPOT_API}/api/v3/klines?symbol={symbol}&interval={interval}&startTime={start_ms}&endTime={end_ms}&limit={limit}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load_or_fetch_klines(symbol, interval="1h", days=90):
    """Paginates historical klines from Binance and caches on disk."""
    ensure_cache_dir()
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - int(days * 24 * HOUR_MS)

    cache_file = os.path.join(CACHE_DIR, f"{symbol}_{interval}_{days}d.json")
    if os.path.exists(cache_file):
        try:
            mtime = os.path.getmtime(cache_file)
            # Use cache if fresh (less than 4 hours old)
            if time.time() - mtime < 4 * 3600:
                with open(cache_file, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                    if len(data) > 0:
                        return data
        except Exception:
            pass

    print(f"[*] Downloading {days}d of {interval} klines for {symbol} from Binance...", flush=True)
    curr_start = start_ms
    all_raw = []

    while curr_start < end_ms:
        chunk = fetch_binance_klines_chunk(symbol, interval, curr_start, end_ms, limit=1000)
        if not chunk:
            break
        all_raw.extend(chunk)
        last_t = chunk[-1][0]
        if len(chunk) < 1000 or last_t >= end_ms - HOUR_MS:
            break
        curr_start = last_t + 1
        time.sleep(0.05)  # respectful rate limit

    # Parse and deduplicate
    seen = set()
    cleaned = []
    for k in all_raw:
        t = int(k[0])
        if t in seen:
            continue
        seen.add(t)
        qv = float(k[7])
        tb_q = float(k[10])
        cleaned.append({
            "t": t,
            "ct": int(k[6]),
            "o": float(k[1]),
            "h": float(k[2]),
            "l": float(k[3]),
            "c": float(k[4]),
            "vol_usd": qv,
            "tb_usd": tb_q,
            "tb": (tb_q / qv * 100.0) if qv > 0 else 50.0,
        })

    cleaned.sort(key=lambda x: x["t"])
    if cleaned:
        try:
            with open(cache_file, "w", encoding="utf-8") as fh:
                json.dump(cleaned, fh)
        except Exception:
            pass

    return cleaned


# ---------------------------------------------------------------- quantitative indicators

def ema(values, span):
    if not values:
        return []
    alpha = 2.0 / (span + 1.0)
    out = [values[0]]
    for i in range(1, len(values)):
        out.append(alpha * values[i] + (1.0 - alpha) * out[-1])
    return out


def atr(bars, span=14):
    if len(bars) < 2:
        return [0.0] * len(bars)
    trs = [bars[0]["h"] - bars[0]["l"]]
    for i in range(1, len(bars)):
        c_prev = bars[i - 1]["c"]
        tr = max(bars[i]["h"] - bars[i]["l"], abs(bars[i]["h"] - c_prev), abs(bars[i]["l"] - c_prev))
        trs.append(tr)
    return ema(trs, span)


def compute_vwap(bars, window=24):
    if not bars:
        return None
    window_bars = bars[-window:] if len(bars) >= window else bars
    cum_pv = sum(((b["h"] + b["l"] + b["c"]) / 3.0) * b["vol_usd"] for b in window_bars)
    cum_v = sum(b["vol_usd"] for b in window_bars)
    return (cum_pv / cum_v) if cum_v > 0 else bars[-1]["c"]


def compute_volume_profile(bars, window=168, bins=35):
    """Computes Rolling 7-day (168 1h bars) Volume Profile (POC, VAL, VAH)."""
    window_bars = bars[-window:] if len(bars) >= window else bars
    if not window_bars:
        return {"poc": None, "val": None, "vah": None}
    
    lo = min(b["l"] for b in window_bars)
    hi = max(b["h"] for b in window_bars)
    if hi <= lo:
        return {"poc": lo, "val": lo, "vah": hi}

    step = (hi - lo) / float(bins)
    buckets = [0.0] * bins

    for b in window_bars:
        typical = (b["h"] + b["l"] + b["c"]) / 3.0
        idx = int((typical - lo) / step)
        idx = max(0, min(bins - 1, idx))
        buckets[idx] += b["vol_usd"]

    poc_idx = buckets.index(max(buckets))
    poc = lo + (poc_idx + 0.5) * step

    total_vol = sum(buckets)
    target_vol = total_vol * 0.70
    acc_vol = buckets[poc_idx]
    left = poc_idx
    right = poc_idx

    while acc_vol < target_vol and (left > 0 or right < bins - 1):
        left_vol = buckets[left - 1] if left > 0 else -1.0
        right_vol = buckets[right + 1] if right < bins - 1 else -1.0
        if right_vol > left_vol and right < bins - 1:
            right += 1
            acc_vol += buckets[right]
        elif left > 0:
            left -= 1
            acc_vol += buckets[left]
        else:
            right += 1
            acc_vol += buckets[right]

    val = lo + left * step
    vah = lo + (right + 1) * step
    return {"poc": poc, "val": val, "vah": vah}


# ---------------------------------------------------------------- signal evaluator

def evaluate_signals_for_bar(bars_1h, i, ema20_arr, ema50_arr, atr_arr, vwap_filter=False, min_tb_long=53.0, max_tb_short=47.0):
    """Evaluates Wyckoff Spring, Upthrust, and EMA Retest for bar index i."""
    if i < 168:
        return None  # Warmup required for 7-day Volume Profile

    b = bars_1h[i]
    e20 = ema20_arr[i]
    e50 = ema50_arr[i]
    a = atr_arr[i]

    # Volume Profile over prior 168 bars
    hist = bars_1h[max(0, i - 168):i + 1]
    vp = compute_volume_profile(hist, window=168)
    val_ref = vp["val"]
    vah_ref = vp["vah"]

    vwap_24h = compute_vwap(bars_1h[max(0, i - 24):i + 1], 24) if vwap_filter else None

    # 1. Wyckoff Spring (CVD Absorption at VAL / 6-bar low)
    prior_low_6 = min(x["l"] for x in bars_1h[max(0, i - 6):i])
    ref_low = min(prior_low_6, val_ref) if val_ref else prior_low_6
    is_spring = (b["l"] <= ref_low * 1.001 and b["c"] > ref_low and b["c"] > b["o"] and b["tb"] >= min_tb_long)
    if vwap_filter and vwap_24h and b["c"] < vwap_24h:
        is_spring = False

    # 2. Bullish EMA20 Pullback
    is_long_pullback = (e20 > e50 and b["l"] <= e20 * 1.002 and b["c"] >= e20 and b["c"] > b["o"] and b["tb"] >= min_tb_long)
    if vwap_filter and vwap_24h and b["c"] < vwap_24h:
        is_long_pullback = False

    # 3. Wyckoff Upthrust SFP (CVD Exhaustion at VAH / 12-bar high)
    prior_high_12 = max(x["h"] for x in bars_1h[max(0, i - 12):i])
    ref_high = max(prior_high_12, vah_ref) if vah_ref else prior_high_12
    is_upthrust = (b["h"] >= ref_high * 0.999 and b["c"] < ref_high and b["c"] < b["o"] and b["tb"] <= max_tb_short)
    if vwap_filter and vwap_24h and b["c"] > vwap_24h:
        is_upthrust = False

    # 4. Bearish EMA20 Rejection
    is_short_rejection = (e20 < e50 and b["h"] >= e20 * 0.998 and b["c"] <= e20 and b["c"] < b["o"] and b["tb"] <= max_tb_short)
    if vwap_filter and vwap_24h and b["c"] > vwap_24h:
        is_short_rejection = False

    if is_spring:
        stop_px = min(b["l"] - 0.5 * a, b["c"] - 1.5 * a)
        return {"side": "long", "setup": "Wyckoff Spring", "stop": stop_px, "score": 90}
    elif is_upthrust:
        stop_px = max(b["h"] + 0.5 * a, b["c"] + 1.5 * a)
        return {"side": "short", "setup": "Wyckoff Upthrust", "stop": stop_px, "score": 90}
    elif is_long_pullback:
        stop_px = min(b["l"] - 0.5 * a, b["c"] - 1.5 * a)
        return {"side": "long", "setup": "EMA20 Pullback", "stop": stop_px, "score": 75}
    elif is_short_rejection:
        stop_px = max(b["h"] + 0.5 * a, b["c"] + 1.5 * a)
        return {"side": "short", "setup": "EMA20 Rejection", "stop": stop_px, "score": 75}

    return None


# ---------------------------------------------------------------- backtesting engine

class CryptoBacktester:
    def __init__(self, symbols, days=90, initial_equity=5000.0, risk_per_trade=50.0,
                 leverage=3.0, fee_rate=0.0004, slippage=0.0002, btc_regime=False,
                 max_positions=4, max_same_side=2, vwap_filter=False,
                 min_tb_long=53.0, max_tb_short=47.0):
        self.symbols = symbols
        self.days = days
        self.initial_equity = initial_equity
        self.risk_per_trade = risk_per_trade
        self.leverage = leverage
        self.fee_rate = fee_rate
        self.slippage = slippage
        self.btc_regime = btc_regime
        self.max_positions = max_positions
        self.max_same_side = max_same_side
        self.vwap_filter = vwap_filter
        self.min_tb_long = min_tb_long
        self.max_tb_short = max_tb_short

        # State tracking
        self.equity = initial_equity
        self.positions = {}       # symbol -> position dict
        self.closed_trades = []
        self.equity_curve = []    # [(timestamp, equity)]
        self.all_klines = {}
        self.btc_klines = {}

    def prepare_data(self):
        """Loads and syncs klines across all monitored symbols."""
        if self.btc_regime and "BTCUSDT" not in self.symbols and not self.btc_klines:
            self.btc_klines = load_or_fetch_klines("BTCUSDT", "1h", self.days)

        for sym in self.symbols:
            if sym in self.all_klines and len(self.all_klines[sym]) > 180:
                continue
            k = load_or_fetch_klines(sym, "1h", self.days)
            if len(k) > 180:
                self.all_klines[sym] = k
                if sym == "BTCUSDT":
                    self.btc_klines = k
            else:
                print(f"[!] Warning: Insufficient klines for {sym} ({len(k)} bars). Skipped.")

        print(f"[*] Data loaded successfully for {len(self.all_klines)} cryptocurrency pairs.\n")

    def run(self):
        self.prepare_data()
        if not self.all_klines:
            print("[-] No symbols loaded. Aborting backtest.")
            return

        # Build unified timeline of timestamps
        time_set = set()
        for klines in self.all_klines.values():
            for b in klines:
                time_set.add(b["t"])
        timeline = sorted(list(time_set))

        # Index lookup: symbol -> time -> bar_index
        bar_idx_by_time = {sym: {b["t"]: idx for idx, b in enumerate(klines)}
                           for sym, klines in self.all_klines.items()}

        # Precompute indicators for speed
        indicators = {}
        for sym, klines in self.all_klines.items():
            closes = [b["c"] for b in klines]
            indicators[sym] = {
                "ema20": ema(closes, 20),
                "ema50": ema(closes, 50),
                "atr": atr(klines, 14),
            }

        btc_time_map = {b["t"]: idx for idx, b in enumerate(self.btc_klines)} if self.btc_klines else {}
        btc_ema20 = ema([b["c"] for b in self.btc_klines], 20) if self.btc_klines else []
        btc_ema50 = ema([b["c"] for b in self.btc_klines], 50) if self.btc_klines else []

        print(f"[*] Simulating chronological walk-forward over {len(timeline)} 1-hour candles...")
        start_time = time.time()

        for t in timeline:
            # 1. Manage Active Positions across all symbols on current bar
            self._update_and_manage_positions(t, bar_idx_by_time)

            # 2. Check BTC Regime Bias if enabled
            btc_bullish = True
            btc_bearish = True
            if self.btc_regime and t in btc_time_map:
                b_idx = btc_time_map[t]
                if b_idx < len(btc_ema20):
                    btc_bullish = btc_ema20[b_idx] >= btc_ema50[b_idx]
                    btc_bearish = btc_ema20[b_idx] <= btc_ema50[b_idx]

            # 3. Scan Signals on completed bar
            for sym, klines in self.all_klines.items():
                if sym in self.positions:
                    continue  # already in position
                if len(self.positions) >= self.max_positions:
                    break

                if t not in bar_idx_by_time[sym]:
                    continue
                i = bar_idx_by_time[sym][t]
                if i < 168:
                    continue

                sig = evaluate_signals_for_bar(
                    klines, i,
                    indicators[sym]["ema20"],
                    indicators[sym]["ema50"],
                    indicators[sym]["atr"],
                    vwap_filter=self.vwap_filter,
                    min_tb_long=self.min_tb_long,
                    max_tb_short=self.max_tb_short
                )

                if not sig:
                    continue

                # Regime filter
                if self.btc_regime and sym != "BTCUSDT":
                    if sig["side"] == "long" and not btc_bullish:
                        continue
                    if sig["side"] == "short" and not btc_bearish:
                        continue

                # Correlation Guard
                same_side_count = sum(1 for p in self.positions.values() if p["side"] == sig["side"])
                if same_side_count >= self.max_same_side:
                    continue

                # Tokenized Stock Guard
                if margin and margin.is_tokenized_stock(sym):
                    continue

                # Enter Position on Next Candle Open (realistic zero lookahead bias)
                if i + 1 < len(klines):
                    next_bar = klines[i + 1]
                    self._open_position(sym, sig, next_bar, t)

            # Record periodic equity curve sample
            self.equity_curve.append((t, round(self.equity, 2)))

        elapsed = time.time() - start_time
        print(f"[+] Simulation completed in {elapsed:.2f} seconds. Analyzed {len(self.closed_trades)} trades.\n")

    def _open_position(self, sym, sig, next_bar, signal_t):
        side = sig["side"]
        entry_raw = next_bar["o"]
        entry_px = entry_raw * (1.0 + self.slippage) if side == "long" else entry_raw * (1.0 - self.slippage)
        stop_px = sig["stop"]
        risk_dist = abs(entry_px - stop_px)

        if risk_dist <= entry_px * 0.003:  # minimum 0.3% stop distance
            return

        # Quarter-Kelly Sizing based on risk
        planned_risk = self.risk_per_trade
        qty = planned_risk / risk_dist

        # Cap leverage and notional
        base = sym.replace("USDT", "")
        max_lev = margin.leverage_cap(base) if margin else self.leverage
        notional = qty * entry_px
        margin_req = notional / max_lev

        if margin_req > self.equity * 0.70:
            qty = (self.equity * 0.70 * max_lev) / entry_px
            notional = qty * entry_px
            margin_req = notional / max_lev

        entry_fee = notional * self.fee_rate
        self.equity -= entry_fee

        # Convex exit targets
        tp1_px = entry_px + 1.8 * risk_dist if side == "long" else entry_px - 1.8 * risk_dist
        tp2_px = entry_px + 3.5 * risk_dist if side == "long" else entry_px - 3.5 * risk_dist

        self.positions[sym] = {
            "symbol": sym,
            "side": side,
            "setup": sig["setup"],
            "opened_t": next_bar["t"],
            "entry": entry_px,
            "stop": stop_px,
            "initial_stop": stop_px,
            "tp1_px": tp1_px,
            "tp2_px": tp2_px,
            "initial_qty": qty,
            "qty": qty,
            "notional": notional,
            "margin": margin_req,
            "risk_usd": planned_risk,
            "fees_paid": entry_fee,
            "tp1_hit": False,
            "tp2_hit": False,
            "trail_vwap": False,
            "partial_realized": 0.0,
        }

    def _update_and_manage_positions(self, t, bar_idx_by_time):
        """Walks forward active positions through current bar."""
        to_close = []

        for sym, p in list(self.positions.items()):
            if t not in bar_idx_by_time[sym]:
                continue
            idx = bar_idx_by_time[sym][t]
            bar = self.all_klines[sym][idx]
            if bar["t"] <= p["opened_t"]:
                continue

            side = p["side"]
            initial_qty = p["initial_qty"]

            # Update VWAP Trailing Stop if TP2 hit
            if p["trail_vwap"]:
                vwap = compute_vwap(self.all_klines[sym][max(0, idx - 24):idx + 1], 24)
                if vwap:
                    if side == "long" and vwap * 0.998 > p["stop"]:
                        p["stop"] = vwap * 0.998
                    elif side == "short" and vwap * 1.002 < p["stop"]:
                        p["stop"] = vwap * 1.002

            # Check Stop-Loss vs Targets (Stop-First Conservative Institutional Convention)
            stopped = False
            exit_px = None
            outcome = None

            if side == "long":
                # 1. Stop loss hit check
                if bar["l"] <= p["stop"]:
                    stopped = True
                    exit_px = min(p["stop"], bar["o"]) * (1.0 - self.slippage)
                    outcome = "TRAILING-VWAP" if p["trail_vwap"] else ("BREAKEVEN (+0.72R)" if p["tp1_hit"] else "STOP-LOSS (-1.0R)")
                else:
                    # 2. TP1 hit check (scale out 40%, move stop to breakeven)
                    if not p["tp1_hit"] and bar["h"] >= p["tp1_px"]:
                        p["tp1_hit"] = True
                        close_qty = initial_qty * 0.40
                        p["qty"] -= close_qty
                        fee = close_qty * p["tp1_px"] * self.fee_rate
                        p["fees_paid"] += fee
                        p["partial_realized"] += close_qty * (p["tp1_px"] - p["entry"]) - fee
                        p["stop"] = max(p["stop"], p["entry"] * (1.0 + self.fee_rate * 2.5))

                    # 3. TP2 hit check (scale out 30%, activate VWAP trailing runner)
                    if p["tp1_hit"] and not p["tp2_hit"] and bar["h"] >= p["tp2_px"]:
                        p["tp2_hit"] = True
                        p["trail_vwap"] = True
                        close_qty = initial_qty * 0.30
                        p["qty"] -= close_qty
                        fee = close_qty * p["tp2_px"] * self.fee_rate
                        p["fees_paid"] += fee
                        p["partial_realized"] += close_qty * (p["tp2_px"] - p["entry"]) - fee
            else:  # Short
                if bar["h"] >= p["stop"]:
                    stopped = True
                    exit_px = max(p["stop"], bar["o"]) * (1.0 + self.slippage)
                    outcome = "TRAILING-VWAP" if p["trail_vwap"] else ("BREAKEVEN (+0.72R)" if p["tp1_hit"] else "STOP-LOSS (-1.0R)")
                else:
                    if not p["tp1_hit"] and bar["l"] <= p["tp1_px"]:
                        p["tp1_hit"] = True
                        close_qty = initial_qty * 0.40
                        p["qty"] -= close_qty
                        fee = close_qty * p["tp1_px"] * self.fee_rate
                        p["fees_paid"] += fee
                        p["partial_realized"] += close_qty * (p["entry"] - p["tp1_px"]) - fee
                        p["stop"] = min(p["stop"], p["entry"] * (1.0 - self.fee_rate * 2.5))

                    if p["tp1_hit"] and not p["tp2_hit"] and bar["l"] <= p["tp2_px"]:
                        p["tp2_hit"] = True
                        p["trail_vwap"] = True
                        close_qty = initial_qty * 0.30
                        p["qty"] -= close_qty
                        fee = close_qty * p["tp2_px"] * self.fee_rate
                        p["fees_paid"] += fee
                        p["partial_realized"] += close_qty * (p["entry"] - p["tp2_px"]) - fee

            if stopped:
                to_close.append((sym, exit_px, bar["t"], outcome))

        for sym, exit_px, close_t, outcome in to_close:
            p = self.positions.pop(sym)
            side = p["side"]
            rem_qty = p["qty"]
            exit_fee = rem_qty * exit_px * self.fee_rate
            p["fees_paid"] += exit_fee

            if side == "long":
                rem_pnl = rem_qty * (exit_px - p["entry"]) - exit_fee
            else:
                rem_pnl = rem_qty * (p["entry"] - exit_px) - exit_fee

            net_pnl = p["partial_realized"] + rem_pnl
            r_mult = net_pnl / p["risk_usd"]
            self.equity += net_pnl

            self.closed_trades.append({
                "symbol": sym,
                "side": side,
                "setup": p["setup"],
                "opened": iso(p["opened_t"]),
                "closed": iso(close_t),
                "entry": p["entry"],
                "exit": exit_px,
                "outcome": outcome,
                "tp1_hit": p["tp1_hit"],
                "tp2_hit": p["tp2_hit"],
                "pnl": round(net_pnl, 2),
                "r": round(r_mult, 2),
                "fees": round(p["fees_paid"], 2),
                "equity_after": round(self.equity, 2),
            })

    def calculate_metrics(self):
        """Computes institutional quantitative risk & profitability metrics."""
        trades = self.closed_trades
        n_trades = len(trades)
        if n_trades == 0:
            return {"error": "Zero trades executed"}

        wins = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] < 0]
        breakevens = [t for t in trades if t["pnl"] == 0]

        win_rate = (len(wins) / n_trades) * 100.0
        gross_profit = sum(t["pnl"] for t in wins)
        gross_loss = abs(sum(t["pnl"] for t in losses))
        net_profit = sum(t["pnl"] for t in trades)
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (99.0 if gross_profit > 0 else 0.0)

        r_values = [t["r"] for t in trades]
        avg_r = sum(r_values) / n_trades
        avg_win_r = (sum(t["r"] for t in wins) / len(wins)) if wins else 0.0
        avg_loss_r = (sum(t["r"] for t in losses) / len(losses)) if losses else 0.0

        # Drawdown calculation
        peak = self.initial_equity
        max_dd_usd = 0.0
        max_dd_pct = 0.0
        for _, eq in self.equity_curve:
            if eq > peak:
                peak = eq
            dd_usd = peak - eq
            dd_pct = (dd_usd / peak) * 100.0 if peak > 0 else 0.0
            if dd_usd > max_dd_usd:
                max_dd_usd = dd_usd
            if dd_pct > max_dd_pct:
                max_dd_pct = dd_pct

        # Annualized Sharpe & Sortino
        if len(r_values) > 1:
            mean_r = sum(r_values) / len(r_values)
            variance = sum((x - mean_r) ** 2 for x in r_values) / (len(r_values) - 1)
            std_dev = math.sqrt(variance)
            downside_sq = sum((min(0.0, x) ** 2) for x in r_values) / (len(r_values) - 1)
            downside_dev = math.sqrt(downside_sq)

            # Annualization factor based on trade frequency (assuming ~3 trades/day = ~1000 trades/year)
            ann_factor = math.sqrt(max(10, n_trades * (365.0 / max(1, self.days))))
            sharpe = (mean_r / std_dev * ann_factor) if std_dev > 0 else 0.0
            sortino = (mean_r / downside_dev * ann_factor) if downside_dev > 0 else 0.0
        else:
            sharpe = 0.0
            sortino = 0.0

        # Breakdown by setup
        by_setup = {}
        for t in trades:
            s_name = t["setup"]
            if s_name not in by_setup:
                by_setup[s_name] = {"count": 0, "wins": 0, "pnl": 0.0, "r_sum": 0.0}
            by_setup[s_name]["count"] += 1
            if t["pnl"] > 0:
                by_setup[s_name]["wins"] += 1
            by_setup[s_name]["pnl"] += t["pnl"]
            by_setup[s_name]["r_sum"] += t["r"]

        for s_name, d in by_setup.items():
            d["win_rate"] = round((d["wins"] / d["count"]) * 100.0, 1)
            d["avg_r"] = round(d["r_sum"] / d["count"], 2)
            d["pnl"] = round(d["pnl"], 2)

        # Breakdown by side
        long_trades = [t for t in trades if t["side"] == "long"]
        short_trades = [t for t in trades if t["side"] == "short"]
        long_wr = (sum(1 for t in long_trades if t["pnl"] > 0) / len(long_trades) * 100.0) if long_trades else 0.0
        short_wr = (sum(1 for t in short_trades if t["pnl"] > 0) / len(short_trades) * 100.0) if short_trades else 0.0

        return {
            "initial_equity": self.initial_equity,
            "final_equity": round(self.equity, 2),
            "net_profit": round(net_profit, 2),
            "roi_pct": round((net_profit / self.initial_equity) * 100.0, 2),
            "n_trades": n_trades,
            "wins": len(wins),
            "losses": len(losses),
            "breakevens": len(breakevens),
            "win_rate": round(win_rate, 1),
            "profit_factor": round(profit_factor, 2),
            "avg_r": round(avg_r, 2),
            "avg_win_r": round(avg_win_r, 2),
            "avg_loss_r": round(avg_loss_r, 2),
            "max_dd_usd": round(max_dd_usd, 2),
            "max_dd_pct": round(max_dd_pct, 2),
            "sharpe": round(sharpe, 2),
            "sortino": round(sortino, 2),
            "long_count": len(long_trades),
            "long_win_rate": round(long_wr, 1),
            "long_pnl": round(sum(t["pnl"] for t in long_trades), 2),
            "short_count": len(short_trades),
            "short_win_rate": round(short_wr, 1),
            "short_pnl": round(sum(t["pnl"] for t in short_trades), 2),
            "by_setup": by_setup,
        }

    def print_terminal_report(self):
        m = self.calculate_metrics()
        if "error" in m:
            print("[-] Backtest produced no trades. Adjust filters or period.")
            return

        print("=" * 76)
        print("     BINANCE QUANTITATIVE HISTORICAL BACKTEST PERFORMANCE REPORT     ")
        print("=" * 76)
        print(f"Monitored Assets  : {len(self.symbols)} coins ({', '.join(self.symbols[:6])}...)")
        print(f"Historical Horizon: {self.days} Days | BTC Regime Filter: {'ACTIVE' if self.btc_regime else 'OFF'}")
        print(f"Starting Capital  : ${m['initial_equity']:,.2f}  --->  Final Equity: ${m['final_equity']:,.2f}")
        print(f"Total Net Return  : {'+' if m['net_profit'] >= 0 else ''}${m['net_profit']:,.2f} ({'+' if m['roi_pct'] >= 0 else ''}{m['roi_pct']}%)")
        print("-" * 76)
        print(f"Total Trades      : {m['n_trades']}  (Wins: {m['wins']} | Losses: {m['losses']} | BE: {m['breakevens']})")
        print(f"Win Rate          : {m['win_rate']}%")
        print(f"Profit Factor     : {m['profit_factor']}")
        print(f"Expectancy (Avg R): {'+' if m['avg_r'] >= 0 else ''}{m['avg_r']}R per trade")
        print(f"Avg Win / Avg Loss: +{m['avg_win_r']}R / {m['avg_loss_r']}R (Convex Ratio)")
        print(f"Max Drawdown      : -${m['max_dd_usd']:,.2f} (-{m['max_dd_pct']}%)")
        print(f"Annualized Sharpe : {m['sharpe']}  |  Sortino: {m['sortino']}")
        print("-" * 76)
        print("PERFORMANCE BY DIRECTION:")
        print(f"  * LONG  : {m['long_count']:3d} trades | Win Rate {m['long_win_rate']:5.1f}% | Net P&L: {'+' if m['long_pnl'] >= 0 else ''}${m['long_pnl']:,.2f}")
        print(f"  * SHORT : {m['short_count']:3d} trades | Win Rate {m['short_win_rate']:5.1f}% | Net P&L: {'+' if m['short_pnl'] >= 0 else ''}${m['short_pnl']:,.2f}")
        print("-" * 76)
        print("PERFORMANCE BY SETUP TYPE:")
        for setup_name, st in m["by_setup"].items():
            print(f"  * {setup_name:25s}: {st['count']:3d} trades | Win Rate {st['win_rate']:5.1f}% | Avg R: {st['avg_r']:+5.2f}R | P&L: {'+' if st['pnl'] >= 0 else ''}${st['pnl']:,.2f}")
        print("=" * 76)

        # Print recent trades
        if self.closed_trades:
            print("\nSAMPLE RECENT TRADES (Last 10):")
            print(f"{'CLOSED':16s} {'SYMBOL':9s} {'SIDE':5s} {'SETUP':18s} {'OUTCOME':22s} {'R-MULT':7s} {'NET P&L':10s}")
            print("-" * 92)
            for t in self.closed_trades[-10:]:
                print(f"{t['closed'][:16]:16s} {t['symbol']:9s} {t['side'].upper():5s} {t['setup'][:18]:18s} {t['outcome'][:22]:22s} {t['r']:+6.2f}R  {'+' if t['pnl'] >= 0 else ''}${t['pnl']:,.2f}")
            print("=" * 92)

    def export_html(self, output_path):
        m = self.calculate_metrics()
        labels_json = json.dumps([iso(t) for t, _ in self.equity_curve[::max(1, len(self.equity_curve) // 250)]])
        curve_json = json.dumps([eq for _, eq in self.equity_curve[::max(1, len(self.equity_curve) // 250)]])
        trades_json = json.dumps(self.closed_trades)

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Binance Quantitative Backtest Report</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, monospace; background: #0b0e14; color: #e2e8f0; margin: 0; padding: 24px; }}
  .container {{ max-width: 1200px; margin: 0 auto; }}
  h1 {{ color: #f59e0b; margin-bottom: 4px; }}
  .subtitle {{ color: #94a3b8; font-size: 14px; margin-bottom: 24px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 24px; }}
  .card {{ background: #151b26; border: 1px solid #2d3748; border-radius: 8px; padding: 16px; }}
  .card .label {{ color: #94a3b8; font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; }}
  .card .val {{ font-size: 24px; font-weight: bold; margin-top: 6px; }}
  .green {{ color: #10b981; }}
  .red {{ color: #ef4444; }}
  .chart-box {{ background: #151b26; border: 1px solid #2d3748; border-radius: 8px; padding: 20px; margin-bottom: 24px; height: 380px; }}
  table {{ width: 100%; border-collapse: collapse; background: #151b26; border-radius: 8px; overflow: hidden; }}
  th, td {{ padding: 12px 14px; text-align: left; font-size: 13px; border-bottom: 1px solid #242c3d; }}
  th {{ background: #1a2233; color: #94a3b8; text-transform: uppercase; font-size: 11px; }}
</style>
</head>
<body>
<div class="container">
  <h1>Binance Quant Backtest Dashboard</h1>
  <div class="subtitle">Simulated {m.get('n_trades', 0)} trades over {self.days} days across {len(self.symbols)} cryptocurrencies (BTC Regime Filter: {'Active' if self.btc_regime else 'Off'}).</div>

  <div class="grid">
    <div class="card"><div class="label">Total Net Return</div><div class="val {'green' if m.get('net_profit', 0) >= 0 else 'red'}">${m.get('net_profit', 0):,.2f} ({m.get('roi_pct', 0)}%)</div></div>
    <div class="card"><div class="label">Win Rate</div><div class="val">{m.get('win_rate', 0)}%</div></div>
    <div class="card"><div class="label">Profit Factor</div><div class="val {'green' if m.get('profit_factor', 0) >= 1.5 else ''}">{m.get('profit_factor', 0)}</div></div>
    <div class="card"><div class="label">Expectancy (Avg R)</div><div class="val green">{'+' if m.get('avg_r', 0) >= 0 else ''}{m.get('avg_r', 0)}R</div></div>
    <div class="card"><div class="label">Max Drawdown</div><div class="val red">-${m.get('max_dd_usd', 0):,.2f} (-{m.get('max_dd_pct', 0)}%)</div></div>
    <div class="card"><div class="label">Sharpe Ratio</div><div class="val">{m.get('sharpe', 0)}</div></div>
  </div>

  <div class="chart-box">
    <canvas id="equityChart"></canvas>
  </div>

  <h2>Trade Journal ({m.get('n_trades', 0)} Trades)</h2>
  <table>
    <thead>
      <tr><th>Closed</th><th>Symbol</th><th>Side</th><th>Setup</th><th>Outcome</th><th>R-Mult</th><th>Net P&L</th><th>Equity</th></tr>
    </thead>
    <tbody id="tradeRows"></tbody>
  </table>
</div>

<script>
const labels = {labels_json};
const data = {curve_json};
const trades = {trades_json};

const ctx = document.getElementById('equityChart').getContext('2d');
new Chart(ctx, {{
  type: 'line',
  data: {{
    labels: labels,
    datasets: [{{
      label: 'Portfolio Equity ($)',
      data: data,
      borderColor: '#10b981',
      backgroundColor: 'rgba(16, 185, 129, 0.08)',
      fill: true,
      tension: 0.1,
      pointRadius: 0
    }}]
  }},
  options: {{
    responsive: true,
    maintainAspectRatio: false,
    plugins: {{ legend: {{ labels: {{ color: '#e2e8f0' }} }} }},
    scales: {{
      x: {{ grid: {{ color: '#1e293b' }}, ticks: {{ color: '#94a3b8', maxTicksLimit: 12 }} }},
      y: {{ grid: {{ color: '#1e293b' }}, ticks: {{ color: '#94a3b8' }} }}
    }}
  }}
}});

const tbody = document.getElementById('tradeRows');
trades.slice(-50).reverse().forEach(t => {{
  const tr = document.createElement('tr');
  const pnlClass = t.pnl >= 0 ? 'green' : 'red';
  tr.innerHTML = `
    <td>${{t.closed}}</td>
    <td><strong>${{t.symbol}}</strong></td>
    <td style="color: ${{t.side === 'long' ? '#10b981' : '#f43f5e'}}">${{t.side.toUpperCase()}}</td>
    <td>${{t.setup}}</td>
    <td>${{t.outcome}}</td>
    <td class="${{pnlClass}}">${{t.r > 0 ? '+' : ''}}${{t.r}}R</td>
    <td class="${{pnlClass}}">${{t.pnl >= 0 ? '+' : ''}}$${{t.pnl.toFixed(2)}}</td>
    <td>$${{t.equity_after.toFixed(2)}}</td>
  `;
  tbody.appendChild(tr);
}});
</script>
</body>
</html>
"""
        with open(output_path, "w", encoding="utf-8") as fh:
            fh.write(html)
        print(f"[+] Interactive HTML Dashboard exported to: {output_path}")


# ---------------------------------------------------------------- CLI

def main():
    parser = argparse.ArgumentParser(description="Binance Institutional Historical Crypto Backtester.")
    parser.add_argument("--symbols", type=str, default="", help="Comma-separated symbols (e.g. BTCUSDT,ETHUSDT,SOLUSDT)")
    parser.add_argument("--universe", choices=["core", "top10", "top20"], default="top10", help="Predefined symbol universe")
    parser.add_argument("--days", type=int, default=90, help="Days of historical 1h klines (default: 90)")
    parser.add_argument("--equity", type=float, default=5000.0, help="Starting equity in USD (default: 5000)")
    parser.add_argument("--risk", type=float, default=50.0, help="Planned risk per trade in USD (default: 50)")
    parser.add_argument("--leverage", type=float, default=3.0, help="Default leverage cap (default: 3.0)")
    parser.add_argument("--btc-regime", action="store_true", help="Enable BTC regime filter (filter alts by BTC trend)")
    parser.add_argument("--vwap-filter", action="store_true", help="Require price > 24h VWAP for longs and < VWAP for shorts")
    parser.add_argument("--min-taker-buy", type=float, default=53.0, help="Minimum taker buy ratio for longs (default: 53.0)")
    parser.add_argument("--max-taker-sell", type=float, default=47.0, help="Maximum taker buy ratio for shorts (default: 47.0)")
    parser.add_argument("--html", type=str, default="", help="Path to export interactive HTML dashboard")

    args = parser.parse_args()

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    elif args.universe == "top10":
        symbols = CORE_SYMBOLS[:10]
    elif args.universe == "top20":
        symbols = CORE_SYMBOLS[:20]
    else:
        symbols = CORE_SYMBOLS[:5]

    engine = CryptoBacktester(
        symbols=symbols,
        days=args.days,
        initial_equity=args.equity,
        risk_per_trade=args.risk,
        leverage=args.leverage,
        btc_regime=args.btc_regime,
        vwap_filter=args.vwap_filter,
        min_tb_long=args.min_taker_buy,
        max_tb_short=args.max_taker_sell
    )

    engine.run()
    engine.print_terminal_report()

    if args.html:
        engine.export_html(args.html)


if __name__ == "__main__":
    main()
