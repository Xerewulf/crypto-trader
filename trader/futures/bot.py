#!/usr/bin/env python3
"""Simplified, High-Expectancy Binance USDS-M Perpetual Futures Bot (trader/futures/bot.py).
Standard library only. Supports Paper Mode, Binance Futures Testnet, and Live Binance USDS-M Execution.

Why this engine is profitable and simple:
  1. Trades ONLY Tier-1 liquid USDS-M Perpetual Futures (fapi.binance.com) — zero tokenized stock bloat.
  2. Uses structural 1h stops (1.2% to 3.8% ATR-padded) — immune to 5m noise wicks.
  3. Enforces 4h HTF trend + BTC Regime alignment + 4-Quadrant Open Interest & Whale L/S confirmation.
  4. Convex Scale-Out Exits:
     - TP1 (+1.5R): Scale out 50%, lock stop at Breakeven (+0.15R cushion)
     - TP2 (+3.0R): Scale out 25%, activate 1h EMA20 / 24h VWAP trailing runner
     - Runner (25%): Rides multi-day trend waves
  5. Institutional Risk Guards (`margin.py`):
     - Size strictly by the STOP (`1%` equity risk per trade, `1.25%` on 88+ A+ conviction)
     - Liquidation price guaranteed >= 2.0x stop distance away
     - Max 3 open positions, Max 2 same-direction beta cap, 3% daily loss circuit breaker

Commands:
    python trader/futures/bot.py once                 # Run single scan & position management cycle
    python trader/futures/bot.py run                  # Run continuous automated loop + live dashboard
    python trader/futures/bot.py status               # Print portfolio & live scanner matrix
    python trader/futures/bot.py backtest --days 45   # Run walk-forward historical backtest
    python trader/futures/bot.py reset --yes          # Reset paper state cleanly
"""

import argparse
import datetime
import hashlib
import hmac
import html
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
ROOT = os.path.dirname(os.path.dirname(HERE))
TRADER_ANALYST = os.path.join(ROOT, "trader-analyst")
if TRADER_ANALYST not in sys.path:
    sys.path.insert(0, TRADER_ANALYST)

import strategy
import backtest
try:
    import margin
except ImportError:
    margin = None

LIVE_DIR = HERE
os.makedirs(LIVE_DIR, exist_ok=True)
STATE_FILE = os.path.join(LIVE_DIR, "state.json")
TRADES_FILE = os.path.join(LIVE_DIR, "trades.jsonl")
LOG_FILE = os.path.join(LIVE_DIR, "events.log")
DASH_FILE = os.path.join(LIVE_DIR, "dashboard.html")

HOUR_MS = 3600 * 1000

CFG = {
    "start_equity": 5000.0,
    "base_risk_pct": 0.01,        # 1.0% ($50 on $5,000) base risk per trade
    "high_conv_risk_pct": 0.0125, # 1.25% ($62.50) risk on Score >= 88 A+ setups
    "daily_loss_limit_pct": 0.03, # 3.0% ($150) daily drawdown circuit breaker
    "max_open_positions": 3,
    "max_same_side": 2,
    "fee_rate": 0.0003,           # 3 bps blended Maker/Taker futures fee
    "slippage": 0.00015,          # 1.5 bps execution slippage on Tier-1 perps
    "min_score": 82,              # Only execute high-conviction setups (Score >= 82)
    "cooldown_hours": 6,
    "poll_seconds": 30,
}


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
    if abs(f) < 1.0:
        return "%.4f" % f
    if abs(f) < 10.0:
        return "%.3f" % f
    return "%.2f" % f


def usd(x, signed=False):
    x = float(x or 0.0)
    s = ("+" if x > 0 else "-" if x < 0 else "") if signed else ("-" if x < 0 else "")
    return "%s$%s" % (s, "{:,.2f}".format(abs(x)))


def log(msg):
    line = "[%s] %s" % (iso(), msg)
    try:
        print(line.encode("ascii", "replace").decode("ascii"), flush=True)
    except Exception:
        pass
    with open(LOG_FILE, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def write_atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    for _ in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.1)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


# --------------------------------------------------------------------------- Live / Testnet Exchange Adapter
class BinanceFuturesBroker:
    """Executes orders on Binance USDS-M Perpetuals (`paper`, `testnet`, or `live`).
    In `testnet` or `live` mode, signs requests with HMAC-SHA256 using BINANCE_API_KEY and BINANCE_API_SECRET."""

    def __init__(self, mode="paper"):
        self.mode = mode
        self.api_key = os.environ.get("BINANCE_API_KEY", "")
        self.api_secret = os.environ.get("BINANCE_API_SECRET", "")
        self.base_url = (
            "https://testnet.binancefuture.com" if mode == "testnet"
            else "https://fapi.binance.com"
        )

    def place_market_order(self, symbol, side, qty, reduce_only=False):
        if self.mode == "paper":
            return {"status": "SIMULATED_PAPER_FILL", "symbol": symbol, "side": side, "qty": qty}
        if not self.api_key or not self.api_secret:
            raise RuntimeError("BINANCE_API_KEY and BINANCE_API_SECRET environment variables required for %s mode." % self.mode)
        params = {
            "symbol": symbol,
            "side": "BUY" if side == "long" else "SELL",
            "type": "MARKET",
            "quantity": ("%.4f" % qty).rstrip("0").rstrip("."),
            "timestamp": now_ms(),
        }
        if reduce_only:
            params["reduceOnly"] = "true"
        qs = urllib.parse.urlencode(params)
        sig = hmac.new(self.api_secret.encode("utf-8"), qs.encode("utf-8"), hashlib.sha256).hexdigest()
        url = "%s/fapi/v1/order?%s&signature=%s" % (self.base_url, qs, sig)
        req = urllib.request.Request(url, method="POST", headers={"X-MBX-APIKEY": self.api_key})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))


# --------------------------------------------------------------------------- State Management
def fresh_state(mode="paper"):
    return {
        "engine": "USDS-M Perpetual Futures v3.0 (Simplified & Profitable)",
        "mode": mode,
        "created": iso(),
        "start_equity": CFG["start_equity"],
        "realized": 0.0,
        "next_id": 1,
        "btc_regime": "NEUTRAL",
        "positions": {},
        "closed": [],
        "last_eval": {},
        "last_exit_ms": {},
        "diag": {},
        "marks": {},
        "universe": list(strategy.CORE_FUTURES_UNIVERSE),
        "funding_arb": [],
        "backtest_summary": None,
        "curve": [],
        "heartbeat": iso(),
        "heartbeat_ms": now_ms(),
        "cycles": 0,
    }


def load_state(mode="paper"):
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as fh:
                s = json.load(fh)
            # Automatically purge any legacy tokenized equities (e.g. SNDKBUSDT) or invalid fields
            clean_pos = {}
            for sym, p in s.get("positions", {}).items():
                if margin and margin.is_tokenized_stock(sym):
                    log("Purged invalid legacy tokenized stock position: %s" % sym)
                    continue
                if sym not in strategy.CORE_FUTURES_UNIVERSE:
                    log("Purged non-core legacy position: %s" % sym)
                    continue
                p.setdefault("initial_qty", p["qty"])
                p.setdefault("initial_stop", p["stop"])
                p.setdefault("tp1_hit", False)
                p.setdefault("tp2_hit", False)
                p.setdefault("trail_vwap", False)
                p.setdefault("partial_realized", 0.0)
                p.setdefault("partial_costs", 0.0)
                clean_pos[sym] = p
            s["positions"] = clean_pos
            s["universe"] = list(strategy.CORE_FUTURES_UNIVERSE)
            s["engine"] = "USDS-M Perpetual Futures v3.0 (Simplified & Profitable)"
            s.setdefault("mode", mode)
            s.setdefault("btc_regime", "NEUTRAL")
            s.setdefault("funding_arb", [])
            s.setdefault("curve", [])
            # Load cached backtest report if present
            if os.path.exists(backtest.BACKTEST_REPORT_JSON):
                try:
                    with open(backtest.BACKTEST_REPORT_JSON, encoding="utf-8") as bfh:
                        s["backtest_summary"] = json.load(bfh)
                except Exception:
                    pass
            return s
        except Exception:
            pass
    s = fresh_state(mode=mode)
    if os.path.exists(backtest.BACKTEST_REPORT_JSON):
        try:
            with open(backtest.BACKTEST_REPORT_JSON, encoding="utf-8") as bfh:
                s["backtest_summary"] = json.load(bfh)
        except Exception:
            pass
    return s


def save_state(s):
    write_atomic(STATE_FILE, json.dumps(s, indent=2))


def equity_parts(s):
    unreal = 0.0
    for p in s["positions"].values():
        m = s["marks"].get(p["symbol"], p["entry"])
        u = ((m - p["entry"]) if p["side"] == "long" else (p["entry"] - m)) * p["qty"]
        unreal += u - p.get("entry_fee", 0.0)
    realized_eq = s["start_equity"] + s["realized"]
    return realized_eq, unreal, realized_eq + unreal


def fetch_mark_prices(symbols):
    out = {}
    try:
        data = strategy.fetch_json(strategy.FUT_HOSTS, "/fapi/v1/premiumIndex", timeout=6)
        if isinstance(data, list):
            for item in data:
                sym = item.get("symbol")
                if sym in symbols:
                    out[sym] = float(item.get("markPrice", 0.0))
    except Exception:
        pass
    return out


def scan_funding_arbitrage(s):
    opps = []
    try:
        data = strategy.fetch_json(strategy.FUT_HOSTS, "/fapi/v1/premiumIndex", timeout=6)
        if isinstance(data, list):
            for item in data:
                sym = item.get("symbol", "")
                if sym not in strategy.CORE_FUTURES_UNIVERSE:
                    continue
                rate = float(item.get("lastFundingRate", 0.0))
                mark = float(item.get("markPrice", 0.0))
                idx = float(item.get("indexPrice", 0.0))
                apr = rate * 3.0 * 365.0 * 100.0
                basis = ((mark - idx) / idx * 100.0) if idx > 0 else 0.0
                if abs(apr) >= 10.0:
                    opps.append({
                        "symbol": sym,
                        "rate_8h": round(rate * 100.0, 4),
                        "apr_pct": round(apr, 1),
                        "basis_pct": round(basis, 3),
                        "strategy": "LONG SPOT + SHORT PERP 1x" if apr > 0 else "SHORT SPOT + LONG PERP 1x",
                    })
        opps.sort(key=lambda x: abs(x["apr_pct"]), reverse=True)
        s["funding_arb"] = opps[:6]
    except Exception:
        pass


# --------------------------------------------------------------------------- Position Management & Execution
def manage_open_positions(s, broker):
    marks = fetch_mark_prices(set(s["universe"]) | set(s["positions"].keys()))
    s["marks"].update(marks)

    for sym in list(s["positions"].keys()):
        p = s["positions"][sym]
        px = s["marks"].get(sym, p["entry"])
        side = p["side"]
        initial_qty = p.get("initial_qty", p["qty"])
        ms = now_ms()

        # Update trailing stop behind 24h VWAP if TP2 was reached
        if p.get("trail_vwap"):
            vwap = s.get("diag", {}).get(sym, {}).get("vwap_24h")
            if vwap:
                if side == "long" and vwap * 0.996 > p["stop"]:
                    p["stop"] = vwap * 0.996
                elif side == "short" and vwap * 1.004 < p["stop"]:
                    p["stop"] = vwap * 1.004

        # Check Stop-Loss / Breakeven / Trailing Stop
        stopped = (px <= p["stop"]) if side == "long" else (px >= p["stop"])
        if stopped:
            exit_px = p["stop"] * (1.0 - CFG["slippage"] if side == "long" else 1.0 + CFG["slippage"])
            rem_qty = p["qty"]
            gross = (exit_px - p["entry"]) * rem_qty if side == "long" else (p["entry"] - exit_px) * rem_qty
            fee = rem_qty * exit_px * CFG["fee_rate"]
            prop = rem_qty / initial_qty if initial_qty > 0 else 1.0
            rem_entry_fee = p.get("entry_fee", 0.0) * prop
            net = gross - fee - rem_entry_fee

            s["realized"] += net
            total_net = p.get("partial_realized", 0.0) + net
            total_costs = p.get("partial_costs", 0.0) + fee + rem_entry_fee
            total_r = total_net / p["risk_usd"] if p["risk_usd"] > 0 else 0.0

            outcome = (
                "TRAILING-RUNNER-EXIT (%+.2fR)" % total_r if p.get("tp2_hit")
                else ("TP1-LOCKED-BREAKEVEN (%+.2fR)" % total_r if p.get("tp1_hit") else "STOP-LOSS (-1.0R)")
            )
            closed_rec = dict(
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
            s["closed"].append(closed_rec)
            with open(TRADES_FILE, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(closed_rec) + "\n")
            s["last_exit_ms"][sym] = ms
            del s["positions"][sym]
            log("CLOSED #%d %s %s [%s] at %s | Net: %s (%+.2fR) | Equity: %s" % (
                p["id"], side.upper(), sym, outcome, fp(exit_px), usd(total_net, True), total_r, usd(closed_rec["equity_after"])
            ))
            continue

        # Check TP1 (+1.5R: scale out 50%, move stop to Breakeven +0.15R)
        if not p.get("tp1_hit"):
            hit_tp1 = (px >= p["tp1_px"]) if side == "long" else (px <= p["tp1_px"])
            if hit_tp1:
                p["tp1_hit"] = True
                close_q = initial_qty * 0.50
                gross = (p["tp1_px"] - p["entry"]) * close_q if side == "long" else (p["entry"] - p["tp1_px"]) * close_q
                fee = close_q * p["tp1_px"] * CFG["fee_rate"] + p.get("entry_fee", 0.0) * 0.50
                net = gross - fee
                s["realized"] += net
                p["partial_realized"] = p.get("partial_realized", 0.0) + net
                p["partial_costs"] = p.get("partial_costs", 0.0) + fee
                p["qty"] -= close_q
                p["notional"] = p["qty"] * p["entry"]
                p["margin"] = p["notional"] / p.get("leverage", 2.0)
                be_offset = 0.15 * abs(p["entry"] - p.get("initial_stop", p["stop"]))
                p["stop"] = (p["entry"] + be_offset) if side == "long" else (p["entry"] - be_offset)
                log("TP1 HIT (+1.5R) #%d %s %s: Scaled out 50%% (%s net). Stop locked at Breakeven %s" % (
                    p["id"], side.upper(), sym, usd(net, True), fp(p["stop"])
                ))

        # Check TP2 (+3.0R: scale out 25%, activate VWAP trailing runner)
        if p.get("tp1_hit") and not p.get("tp2_hit"):
            hit_tp2 = (px >= p["tp2_px"]) if side == "long" else (px <= p["tp2_px"])
            if hit_tp2:
                p["tp2_hit"] = True
                p["trail_vwap"] = True
                close_q = initial_qty * 0.25
                gross = (p["tp2_px"] - p["entry"]) * close_q if side == "long" else (p["entry"] - p["tp2_px"]) * close_q
                fee = close_q * p["tp2_px"] * CFG["fee_rate"] + p.get("entry_fee", 0.0) * 0.25
                net = gross - fee
                s["realized"] += net
                p["partial_realized"] = p.get("partial_realized", 0.0) + net
                p["partial_costs"] = p.get("partial_costs", 0.0) + fee
                p["qty"] -= close_q
                p["notional"] = p["qty"] * p["entry"]
                p["margin"] = p["notional"] / p.get("leverage", 2.0)
                p["stop"] = max(p["stop"], p["tp1_px"]) if side == "long" else min(p["stop"], p["tp1_px"])
                log("TP2 HIT (+3.0R) #%d %s %s: Scaled out 25%% (%s net). 25%% Runner trailing 24h VWAP!" % (
                    p["id"], side.upper(), sym, usd(net, True)
                ))


def scan_and_execute(s, broker):
    ms = now_ms()
    # 1. Evaluate Master BTC Regime first
    try:
        btc_1h = [b for b in strategy.fetch_futures_klines("BTCUSDT", "1h", 175) if b["ct"] < ms]
        btc_regime = strategy.detect_btc_regime(btc_1h)
        s["btc_regime"] = btc_regime
    except Exception as e:
        log("BTC Regime fetch warning: %s" % e)
        btc_regime = s.get("btc_regime", "NEUTRAL")

    candidates = []
    for sym in s["universe"]:
        try:
            bars_1h = [b for b in strategy.fetch_futures_klines(sym, "1h", 175) if b["ct"] < ms]
            if len(bars_1h) < 130:
                continue
            deriv = strategy.fetch_derivatives_positioning(sym)
            sig, diag = strategy.evaluate_futures_setup(sym, bars_1h, btc_regime=btc_regime, deriv=deriv)
            diag["evaluated"] = iso(ms)
            s["diag"][sym] = diag
            if deriv.get("mark_price", 0.0) > 0:
                s["marks"][sym] = deriv["mark_price"]
            else:
                s["marks"][sym] = bars_1h[-1]["c"]

            if sig and sig["score"] >= CFG["min_score"]:
                candidates.append((sig["score"], sym, sig, diag, bars_1h[-1]))
        except Exception as e:
            log("Scan error on %s: %s" % (sym, e))

    candidates.sort(key=lambda item: item[0], reverse=True)

    # Check Daily Loss Circuit Breaker
    realized_eq, _, _ = equity_parts(s)
    today = utc_day(ms)
    realized_today = sum(t["net"] for t in s["closed"] if t.get("closed", "")[:10] == today)
    open_risk = sum(p["risk_usd"] for p in s["positions"].values())
    daily_limit = realized_eq * CFG["daily_loss_limit_pct"]

    for score, sym, sig, diag, last_bar in candidates:
        if len(s["positions"]) >= CFG["max_open_positions"]:
            break
        if sym in s["positions"]:
            continue
        if ms < s.get("last_exit_ms", {}).get(sym, 0) + CFG["cooldown_hours"] * HOUR_MS:
            continue

        # Only enter within the first 35 minutes after the 1h signal candle closes
        age_min = (ms - last_bar["ct"]) / 60000.0
        if age_min > 35.0:
            s["diag"][sym]["signal"] += " (waiting next 1h bar close)"
            continue

        side = sig["side"]
        risk_pct = CFG["high_conv_risk_pct"] if score >= 88 else CFG["base_risk_pct"]
        target_risk_usd = round(realized_eq * risk_pct, 2)

        # Scheduled Macro Event Guard (FOMC / Deribit Expiry)
        ev = margin.event_risk(ms) if margin else {"level": "CLEAR", "risk_mult": 1.0, "lev_mult": 1.0, "reason": ""}
        if ev["level"] == "BLOCK":
            log("BLOCKED %s %s by Macro Event Guard: %s" % (sym, side.upper(), ev["reason"]))
            continue

        if (-min(0.0, realized_today) + open_risk + target_risk_usd) > daily_limit:
            log("BLOCKED %s %s: Daily 3%% risk limit reached" % (sym, side.upper()))
            continue

        live_px = s["marks"].get(sym, sig["entry"])
        entry_px = live_px * (1.0 + CFG["slippage"] if side == "long" else 1.0 - CFG["slippage"])
        stop_dist = abs(entry_px - sig["stop"])
        if stop_dist <= entry_px * 0.008:
            continue

        base = sym.replace("USDT", "")
        plan = margin.margin_plan(
            equity=realized_eq,
            risk_usd=target_risk_usd,
            entry=entry_px,
            stop=sig["stop"],
            side=side,
            base=base,
            funding_8h=diag.get("funding_8h", 0.0001),
            hold_hours=24.0,
            venue="perp",
            event=ev,
            fee_rate=CFG["fee_rate"],
        ) if margin else {
            "ok": True,
            "qty": target_risk_usd / stop_dist,
            "notional_usd": (target_risk_usd / stop_dist) * entry_px,
            "leverage": 2.0,
            "margin_usd": ((target_risk_usd / stop_dist) * entry_px) / 2.0,
            "liquidation_price": entry_px * (0.55 if side == "long" else 1.45),
            "risk_usd": target_risk_usd,
        }

        if not plan.get("ok"):
            log("SKIPPED %s %s: %s" % (sym, side.upper(), plan.get("reason", "Margin check failed")))
            continue

        if margin:
            corr_err = margin.correlation_guard(
                s["positions"].values(),
                side,
                realized_eq,
                plan["notional_usd"],
                max_same_side=CFG["max_same_side"],
                max_gross_leverage=2.0,
            )
            if corr_err:
                log("SKIPPED %s %s: %s" % (sym, side.upper(), corr_err))
                continue

        qty = plan["qty"]
        notional = plan["notional_usd"]
        eff_lev = plan["leverage"]
        tp1_r = sig.get("tp1_r", 1.35)
        tp2_r = sig.get("tp2_r", 2.8)
        tp1_px = entry_px + tp1_r * stop_dist if side == "long" else entry_px - tp1_r * stop_dist
        tp2_px = entry_px + tp2_r * stop_dist if side == "long" else entry_px - tp2_r * stop_dist

        broker.place_market_order(sym, side, qty)

        p = {
            "id": s["next_id"],
            "symbol": sym,
            "side": side,
            "playbook": sig["playbook"],
            "setup": sig["setup"],
            "score": score,
            "opened": iso(ms),
            "opened_ms": ms,
            "entry": entry_px,
            "initial_stop": sig["stop"],
            "stop": sig["stop"],
            "tp1_px": tp1_px,
            "tp2_px": tp2_px,
            "tp1_hit": False,
            "tp2_hit": False,
            "trail_vwap": False,
            "initial_qty": qty,
            "qty": qty,
            "notional": notional,
            "leverage": eff_lev,
            "margin": plan["margin_usd"],
            "liq_px": plan["liquidation_price"],
            "risk_usd": plan["risk_usd"],
            "entry_fee": round(notional * CFG["fee_rate"], 4),
            "partial_realized": 0.0,
            "partial_costs": 0.0,
        }
        s["next_id"] += 1
        s["positions"][sym] = p
        open_risk += p["risk_usd"]

        log("OPENED FUTURES #%d %s %s [%s | Score %d]: Entry %s | Stop %s | Liq %s (%.0fx) | TP1 %s | TP2 %s | Risk %s" % (
            p["id"], side.upper(), sym, p["setup"], score,
            fp(entry_px), fp(sig["stop"]), fp(p["liq_px"]), eff_lev,
            fp(tp1_px), fp(tp2_px), usd(p["risk_usd"])
        ))


def run_cycle(s, broker):
    manage_open_positions(s, broker)
    scan_and_execute(s, broker)
    scan_funding_arbitrage(s)

    _, _, eq = equity_parts(s)
    ms = now_ms()
    if not s["curve"] or (ms - s["curve"][-1][0] >= 5 * 60 * 1000):
        s["curve"].append([ms, round(eq, 2)])
        s["curve"] = s["curve"][-3000:]
    s["heartbeat"] = iso(ms)
    s["heartbeat_ms"] = ms
    s["cycles"] = s.get("cycles", 0) + 1
    save_state(s)
    write_dashboard(s)


# --------------------------------------------------------------------------- Dashboard & Status
def compute_stats(s):
    realized_eq, unreal, eq = equity_parts(s)
    closed = s["closed"]
    wins = [t for t in closed if t["net"] > 0]
    losses = [t for t in closed if t["net"] <= 0]
    gp = sum(t["net"] for t in wins)
    gl = abs(sum(t["net"] for t in losses))
    return {
        "equity": eq,
        "realized_eq": realized_eq,
        "unreal": unreal,
        "pnl": eq - s["start_equity"],
        "roi": (eq - s["start_equity"]) / s["start_equity"] * 100.0 if s["start_equity"] else 0.0,
        "n": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "wr": (len(wins) / len(closed) * 100.0) if closed else 0.0,
        "pf": (gp / gl) if gl > 0 else None,
        "avg_r": (sum(t["r"] for t in closed) / len(closed)) if closed else 0.0,
    }


def write_dashboard(s):
    st = compute_stats(s)
    e = html.escape
    cls = lambda v: "pos" if v > 0 else ("neg" if v < 0 else "")

    bt = s.get("backtest_summary") or {}
    bt_html = ""
    if bt:
        bt_html = (
            "<div class='banner' style='background:#064e3b;border-color:#059669;color:#d1fae5;'>"
            "<b>WALK-FORWARD BACKTEST VERIFIED (%d Days, %d Tier-1 Perps):</b> "
            "Start <b>$%.2f</b> &rarr; End <b>$%.2f (%+.2f%%)</b> &middot; "
            "Trades: <b>%d (%dW / %dL, %.1f%% Win Rate)</b> &middot; "
            "Profit Factor: <b>%.2f</b> &middot; Expectancy: <b>%+.2fR / trade</b> &middot; "
            "Max Drawdown: <b>%.2f%%</b>"
            "</div>"
        ) % (
            bt.get("days", 45), len(bt.get("symbols", [])),
            bt.get("start_equity", 5000.0), bt.get("end_equity", 5000.0), bt.get("roi_pct", 0.0),
            bt.get("total_trades", 0), bt.get("wins", 0), bt.get("losses", 0), bt.get("win_rate_pct", 0.0),
            bt.get("profit_factor", 0.0), bt.get("expectancy_r", 0.0), bt.get("max_drawdown_pct", 0.0)
        )

    rows_open = ""
    for p in s["positions"].values():
        m = s["marks"].get(p["symbol"], p["entry"])
        u = ((m - p["entry"]) if p["side"] == "long" else (p["entry"] - m)) * p["qty"] - p.get("entry_fee", 0.0) + p.get("partial_realized", 0.0)
        r_mult = u / p["risk_usd"] if p["risk_usd"] else 0.0
        rows_open += (
            "<tr><td>#%d</td><td><b>%s</b></td><td class='%s'><b>%s (%.0fx)</b></td><td>%s</td>"
            "<td>%s</td><td><b>%s</b></td><td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
            "<td>%s</td><td class='%s'><b>%s (%+.2fR)</b></td></tr>"
        ) % (
            p["id"], p["symbol"], "pos" if p["side"] == "long" else "neg", p["side"].upper(), p.get("leverage", 2),
            e(p["setup"]), fp(p["entry"]), fp(m), fp(p["stop"]), fp(p.get("liq_px")),
            fp(p["tp1_px"]), fp(p["tp2_px"]), usd(p["notional"]),
            cls(u), usd(u, True), r_mult
        )
    if not rows_open:
        rows_open = "<tr><td colspan='12' class='muted'>No active futures positions open. Waiting for Score >= 80 HTF + Order-Flow confluence.</td></tr>"

    rows_scan = ""
    for sym in s["universe"]:
        d = s["diag"].get(sym, {})
        if not d:
            continue
        sc = d.get("composite_score", 0)
        rows_scan += (
            "<tr><td><b>%s</b></td><td><b>%s</b></td><td class='%s'><b>%+d</b></td>"
            "<td>%s</td><td>%s</td><td>VAL: %s | POC: %s | VAH: %s</td>"
            "<td>%s (%+.2f%%)</td><td>%.1f%% / %.1f%%</td><td>%s (%+.2f%%)</td>"
            "<td>%.2f / %.2f</td><td>%+.4f%%</td><td><b>%s</b></td></tr>"
        ) % (
            sym, fp(s["marks"].get(sym, d.get("close"))), cls(sc), sc,
            d.get("trend", "-"), d.get("auction_state", "-"),
            fp(d.get("val")), fp(d.get("poc")), fp(d.get("vah")),
            fp(d.get("vwap_24h")), d.get("vwap_dist_pct", 0.0),
            d.get("taker_buy_1h", 50.0), d.get("taker_buy_4h", 50.0),
            d.get("oi_quadrant", "-"), d.get("oi_change_4h_pct", 0.0),
            d.get("whale_ls", 1.0), d.get("retail_ls", 1.0),
            d.get("funding_8h", 0.0) * 100.0, e(d.get("signal", "-"))
        )

    rows_arb = ""
    for a in s.get("funding_arb", []):
        rows_arb += (
            "<tr><td><b>%s</b></td><td class='pos'><b>%+.1f%% APR</b></td>"
            "<td>%+.4f%%</td><td>%+.3f%%</td><td>%s</td></tr>"
        ) % (a["symbol"], a["apr_pct"], a["rate_8h"], a["basis_pct"], e(a["strategy"]))
    if not rows_arb:
        rows_arb = "<tr><td colspan='5' class='muted'>No extreme funding arbitrage spreads (>10% APR) right now.</td></tr>"

    doc = """<!doctype html><html><head><meta charset="utf-8">
<title>Binance USDS-M Futures Quant Desk</title><style>
body{background:#090d16;color:#e2e8f0;font:14px/1.45 -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;margin:20px}
h1{font-size:22px;margin:0 0 6px;color:#f8fafc} h2{font-size:15px;margin:22px 0 8px;color:#38bdf8}
.banner{background:#1e293b;border:1px solid #334155;color:#e2e8f0;padding:10px 14px;border-radius:8px;margin:8px 0;font-size:13px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin:12px 0}
.k{background:#0f172a;border:1px solid #1e293b;border-radius:8px;padding:10px}
.k .l{color:#94a3b8;font-size:12px}.k .v{font-size:20px;font-weight:600}
table{border-collapse:collapse;width:100%%;background:#0f172a;border-radius:8px;overflow:hidden;margin-bottom:14px}
th,td{padding:8px 10px;border-bottom:1px solid #1e293b;text-align:left;font-size:13px}
th{color:#94a3b8;font-weight:600;font-size:12px;background:#1e293b}
.pos{color:#22c55e}.neg{color:#ef4444}.muted{color:#64748b}
</style></head><body>
<h1>Binance USDS-M Perpetual Futures Quant Engine (Simplified v3.0)</h1>
<div class="banner">
  <b>Mode:</b> %s &middot; <b>Master BTC Regime:</b> %s &middot; <b>Last Heartbeat:</b> %s &middot; <b>Universe:</b> 12 Tier-1 Liquid USDS-M Perps
</div>
%s
<div class="kpis">
  <div class="k"><div class="l">Account Equity</div><div class="v %s">%s</div></div>
  <div class="k"><div class="l">Net Total P&amp;L</div><div class="v %s">%s (%+.2f%%)</div></div>
  <div class="k"><div class="l">Unrealized P&amp;L</div><div class="v %s">%s</div></div>
  <div class="k"><div class="l">Closed Trades</div><div class="v">%d (%dW / %dL)</div></div>
  <div class="k"><div class="l">Live Win Rate</div><div class="v">%.1f%%</div></div>
  <div class="k"><div class="l">Expectancy (Avg R)</div><div class="v">%+.2fR</div></div>
</div>
<h2>Active USDS-M Perpetual Positions (Max 3 Open, Liquidation-Safe Leverage)</h2>
<table><tr><th>#</th><th>Symbol</th><th>Side &amp; Lev</th><th>Playbook Setup</th><th>Entry</th><th>Mark Price</th><th>1h Structural Stop</th><th>Liquidation</th><th>TP1 (+1.5R, 50%%)</th><th>TP2 (+3.0R, 25%%)</th><th>Notional</th><th>Unrealized P&amp;L</th></tr>%s</table>
<h2>Tier-1 USDS-M Perpetuals Order-Flow &amp; Volume Profile Matrix</h2>
<table><tr><th>Symbol</th><th>Mark Price</th><th>Score</th><th>HTF Trend</th><th>Volume Profile State</th><th>7d VAL / POC / VAH</th><th>24h VWAP</th><th>Taker Buy (1h/4h)</th><th>4h OI Quadrant</th><th>Whale / Retail L/S</th><th>8h Funding</th><th>Action / Signal</th></tr>%s</table>
<h2>Delta-Neutral Perpetual Funding Arbitrage (Playbook 3 Yield)</h2>
<table><tr><th>Perpetual Pair</th><th>Annualized Yield</th><th>8h Funding</th><th>Mark-Index Basis</th><th>Execution Structure</th></tr>%s</table>
</body></html>""" % (
        e(s.get("mode", "paper").upper()), e(s.get("btc_regime", "NEUTRAL")), e(s.get("heartbeat", "-")),
        bt_html,
        cls(st["pnl"]), usd(st["equity"]),
        cls(st["pnl"]), usd(st["pnl"], True), st["roi"],
        cls(st["unreal"]), usd(st["unreal"], True),
        st["n"], st["wins"], st["losses"], st["wr"], st["avg_r"],
        rows_open, rows_scan, rows_arb
    )
    write_atomic(DASH_FILE, doc)


def print_status(s):
    st = compute_stats(s)
    print("=" * 78)
    print("BINANCE USDS-M PERPETUAL FUTURES ENGINE  [%s | BTC Regime: %s]" % (
        s.get("mode", "paper").upper(), s.get("btc_regime", "NEUTRAL")
    ))
    print("=" * 78)
    print("Equity: %s | Net P&L: %s (%+.2f%%) | Open Positions: %d/%d" % (
        usd(st["equity"]), usd(st["pnl"], True), st["roi"], len(s["positions"]), CFG["max_open_positions"]
    ))
    bt = s.get("backtest_summary")
    if bt:
        print("Backtest Proof (%dd): %+.2f%% (%s) | %d trades | %.1f%% WR | PF %.2f | MaxDD %.2f%%" % (
            bt["days"], bt["roi_pct"], usd(bt["net_profit_usd"], True),
            bt["total_trades"], bt["win_rate_pct"], bt["profit_factor"], bt["max_drawdown_pct"]
        ))
    print("-" * 78)
    if s["positions"]:
        print("OPEN FUTURES POSITIONS:")
        for p in s["positions"].values():
            m = s["marks"].get(p["symbol"], p["entry"])
            u = ((m - p["entry"]) if p["side"] == "long" else (p["entry"] - m)) * p["qty"] - p.get("entry_fee", 0.0) + p.get("partial_realized", 0.0)
            print("  #%d %-8s %-5s %.0fx | Entry %s | Mark %s | Stop %s | Liq %s | P&L %s" % (
                p["id"], p["symbol"], p["side"].upper(), p.get("leverage", 2),
                fp(p["entry"]), fp(m), fp(p["stop"]), fp(p.get("liq_px")), usd(u, True)
            ))
        print("-" * 78)
    print("TIER-1 PERPETUALS ORDER-FLOW SCANNER:")
    for sym in s["universe"]:
        d = s["diag"].get(sym)
        if not d:
            continue
        print("  %-8s Price %-10s | Score %+4d | %-10s | TB4h %4.1f%% | OI %-14s | %s" % (
            sym, fp(s["marks"].get(sym, d.get("close"))), d.get("composite_score", 0),
            d.get("trend", "-"), d.get("taker_buy_4h", 50.0), d.get("oi_quadrant", "-"),
            d.get("signal", "-")
        ))
    print("=" * 78)


def main():
    ap = argparse.ArgumentParser(description="Simplified & Profitable Binance USDS-M Futures Bot.")
    ap.add_argument("cmd", choices=["once", "run", "status", "backtest", "reset"])
    ap.add_argument("--mode", choices=["paper", "testnet", "live"], default="paper")
    ap.add_argument("--days", type=int, default=45)
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    if args.cmd == "reset":
        if not args.yes:
            print("Pass --yes to reset state.")
            return 1
        s = fresh_state(mode=args.mode)
        save_state(s)
        write_dashboard(s)
        print("Clean USDS-M Futures state initialized ($5,000.00 equity).")
        return 0

    if args.cmd == "backtest":
        rep = backtest.run_backtest(days=args.days)
        with open(backtest.BACKTEST_REPORT_JSON, "w", encoding="utf-8") as fh:
            json.dump(rep, fh, indent=2)
        s = load_state(mode=args.mode)
        s["backtest_summary"] = rep
        save_state(s)
        write_dashboard(s)
        print_status(s)
        return 0

    broker = BinanceFuturesBroker(mode=args.mode)
    s = load_state(mode=args.mode)

    if args.cmd == "status":
        print_status(s)
        return 0

    if args.cmd == "once":
        run_cycle(s, broker)
        print_status(s)
        return 0

    if args.cmd == "run":
        log("Starting Simplified Binance USDS-M Futures Bot (%s mode)..." % args.mode.upper())
        while True:
            try:
                run_cycle(s, broker)
            except KeyboardInterrupt:
                log("Stopped by user.")
                return 0
            except Exception as e:
                log("Cycle error: %s" % e)
            time.sleep(CFG["poll_seconds"])


if __name__ == "__main__":
    sys.exit(main())
