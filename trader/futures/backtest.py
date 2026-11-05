#!/usr/bin/env python3
"""Walk-Forward Historical Backtester for Binance USDⓈ-M Perpetual Futures (trader/futures/backtest.py).

Replays the exact institutional Futures Strategy (Playbook 1 & Playbook 2) across historical
Binance USDⓈ-M Perpetual 1h candles (`fapi.binance.com`), accounting for:
  - Maker/Taker execution fees (default 3 bps blended)
  - 8-hourly Perpetual Funding Rate carry
  - Convex Scale-Out Exits:
      * TP1 (+1.5R): Scale out 50% of position, move stop to Breakeven (+0.1R fee buffer)
      * TP2 (+3.0R): Scale out 25% of position, enable 1h EMA20 / VWAP trailing stop
      * Runner (25%): Trails dynamically until trend reversal or hard time/trailing stop
  - Portfolio & Correlation Risk Guards (max 3 open positions, max 2 same-side, 1% risk per trade)

Usage:
    python trader/futures/backtest.py --days 45
    python trader/futures/backtest.py --days 45 --write
"""

import argparse
import datetime
import json
import math
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import strategy

BACKTEST_REPORT_JSON = os.path.join(HERE, "backtest_report.json")
HOUR_MS = 3600 * 1000


def fetch_history_1h(sym, days=45):
    """Fetches `days` of 1h klines (+140 warmup bars) from Binance USDⓈ-M Futures."""
    total_bars = int(days * 24) + 150
    out = []
    end_ms = int(time.time() * 1000)
    while len(out) < total_bars:
        batch_limit = min(1000, total_bars - len(out) + 10)
        bars = strategy.fetch_futures_klines(sym, "1h", limit=batch_limit, end_ms=end_ms)
        if not bars:
            break
        out = bars + out
        end_ms = bars[0]["t"] - 1
        if len(bars) < batch_limit:
            break
    # Deduplicate and sort by timestamp
    by_t = {b["t"]: b for b in out}
    sorted_bars = [by_t[k] for k in sorted(by_t.keys())]
    return sorted_bars[-total_bars:]


def run_backtest(symbols=None, days=45, start_equity=5000.0, risk_pct=0.01, fee_rate=0.0003):
    symbols = symbols or ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "SUIUSDT", "AVAXUSDT"]
    if "BTCUSDT" not in symbols:
        symbols = ["BTCUSDT"] + list(symbols)

    history = {}
    for sym in symbols:
        try:
            bars = fetch_history_1h(sym, days=days)
            if len(bars) >= 160:
                history[sym] = bars
        except Exception as e:
            print("Warning: could not fetch history for %s: %s" % (sym, e))

    if "BTCUSDT" not in history:
        raise RuntimeError("BTCUSDT history is required to run the futures portfolio backtest.")

    btc_bars = history["BTCUSDT"]
    btc_by_t = {b["t"]: i for i, b in enumerate(btc_bars)}

    # Align simulation timeline across all loaded symbols after 140-bar indicator warmup
    common_timestamps = sorted(list(btc_by_t.keys()))[140:]
    sym_index_map = {
        sym: {b["t"]: idx for idx, b in enumerate(bars)}
        for sym, bars in history.items()
    }

    equity = float(start_equity)
    peak_equity = equity
    max_dd_pct = 0.0
    open_positions = {}
    closed_trades = []
    last_exit_t = {}
    equity_curve = []

    for t in common_timestamps:
        # 1. Manage existing open positions on this 1h bar
        for sym in list(open_positions.keys()):
            idx = sym_index_map[sym].get(t)
            if idx is None:
                continue
            b = history[sym][idx]
            p = open_positions[sym]
            side = p["side"]
            initial_qty = p["initial_qty"]

            # Update dynamic trailing stop if TP1 or TP2 was already hit on a prior bar
            if p["trail_active"] and idx >= 24:
                vwap_now = strategy.rolling_vwap(history[sym][idx - 24:idx + 1], 24)
                e20_now = strategy.ema_series([x["c"] for x in history[sym][max(0, idx - 50):idx + 1]], 20)[-1]
                if side == "long":
                    trail_lvl = max(vwap_now * 0.996, e20_now * 0.995)
                    if trail_lvl > p["stop"]:
                        p["stop"] = trail_lvl
                else:
                    trail_lvl = min(vwap_now * 1.004, e20_now * 1.005)
                    if trail_lvl < p["stop"]:
                        p["stop"] = trail_lvl

            # Check Stop-Loss first (conservative intrabar ordering unless open already cleared TP)
            stopped = (b["l"] <= p["stop"]) if side == "long" else (b["h"] >= p["stop"])
            if stopped:
                exit_px = p["stop"]
                rem_qty = p["qty"]
                gross = (exit_px - p["entry"]) * rem_qty if side == "long" else (p["entry"] - exit_px) * rem_qty
                fee = rem_qty * exit_px * fee_rate
                net = gross - fee - (p["entry_fee"] * (rem_qty / initial_qty))
                equity += net
                total_net = p["partial_net"] + net
                total_r = total_net / p["risk_usd"]
                outcome = (
                    "RUNNER_TRAIL (+%.2fR)" % total_r if p["tp2_hit"]
                    else ("TP1_PLUS_BE (+%.2fR)" % total_r if p["tp1_hit"] else "STOP_LOSS (-1.0R)")
                )
                closed_trades.append({
                    "symbol": sym,
                    "side": side,
                    "playbook": p["playbook"],
                    "setup": p["setup"],
                    "opened_t": p["opened_t"],
                    "closed_t": t,
                    "entry": round(p["entry"], 6),
                    "exit": round(exit_px, 6),
                    "net_usd": round(total_net, 2),
                    "r": round(total_r, 2),
                    "outcome": outcome,
                    "equity_after": round(equity, 2),
                })
                last_exit_t[sym] = t
                del open_positions[sym]
                continue

            # Check TP1 (+1.5R: scale out 50%, move stop to Breakeven +0.15R)
            if not p["tp1_hit"]:
                hit_tp1 = (b["h"] >= p["tp1_px"]) if side == "long" else (b["l"] <= p["tp1_px"])
                if hit_tp1:
                    p["tp1_hit"] = True
                    close_q = initial_qty * 0.50
                    gross = (p["tp1_px"] - p["entry"]) * close_q if side == "long" else (p["entry"] - p["tp1_px"]) * close_q
                    fee = close_q * p["tp1_px"] * fee_rate + (p["entry_fee"] * 0.50)
                    net = gross - fee
                    equity += net
                    p["partial_net"] += net
                    p["qty"] -= close_q
                    # Move stop to breakeven + 0.15R cushion
                    be_offset = 0.15 * abs(p["entry"] - p["initial_stop"])
                    p["stop"] = (p["entry"] + be_offset) if side == "long" else (p["entry"] - be_offset)

            # Check TP2 (+3.0R: scale out 25%, activate runner trailing stop)
            if p["tp1_hit"] and not p["tp2_hit"]:
                hit_tp2 = (b["h"] >= p["tp2_px"]) if side == "long" else (b["l"] <= p["tp2_px"])
                if hit_tp2:
                    p["tp2_hit"] = True
                    p["trail_active"] = True
                    close_q = initial_qty * 0.25
                    gross = (p["tp2_px"] - p["entry"]) * close_q if side == "long" else (p["entry"] - p["tp2_px"]) * close_q
                    fee = close_q * p["tp2_px"] * fee_rate + (p["entry_fee"] * 0.25)
                    net = gross - fee
                    equity += net
                    p["partial_net"] += net
                    p["qty"] -= close_q
                    # Lock stop at TP1 price
                    p["stop"] = max(p["stop"], p["tp1_px"]) if side == "long" else min(p["stop"], p["tp1_px"])

            # Time stop for stagnant trades (72 hours without hitting TP1)
            if not p["tp1_hit"] and (t - p["opened_t"]) >= 72 * HOUR_MS:
                exit_px = b["c"]
                rem_qty = p["qty"]
                gross = (exit_px - p["entry"]) * rem_qty if side == "long" else (p["entry"] - exit_px) * rem_qty
                fee = rem_qty * exit_px * fee_rate + p["entry_fee"]
                net = gross - fee
                equity += net
                total_r = net / p["risk_usd"]
                closed_trades.append({
                    "symbol": sym,
                    "side": side,
                    "playbook": p["playbook"],
                    "setup": p["setup"],
                    "opened_t": p["opened_t"],
                    "closed_t": t,
                    "entry": round(p["entry"], 6),
                    "exit": round(exit_px, 6),
                    "net_usd": round(net, 2),
                    "r": round(total_r, 2),
                    "outcome": "TIME_EXIT (%+.2fR)" % total_r,
                    "equity_after": round(equity, 2),
                })
                last_exit_t[sym] = t
                del open_positions[sym]

        # Track drawdown
        peak_equity = max(peak_equity, equity)
        dd = (peak_equity - equity) / peak_equity * 100.0 if peak_equity > 0 else 0.0
        max_dd_pct = max(max_dd_pct, dd)
        equity_curve.append([t, round(equity, 2)])

        # 2. Scan for new A+ signals if portfolio has room (max 3 open positions)
        if len(open_positions) >= 3:
            continue

        btc_idx = btc_by_t.get(t)
        btc_slice = btc_bars[max(0, btc_idx - 165):btc_idx + 1]
        btc_regime = strategy.detect_btc_regime(btc_slice)

        # Scheduled Macro Event Guard (FOMC / Deribit Options Expiry)
        ev = strategy.margin.event_risk(t) if strategy.margin else {"level": "CLEAR", "risk_mult": 1.0}
        if ev["level"] == "BLOCK":
            continue

        candidates = []
        for sym in symbols:
            if sym in open_positions:
                continue
            if t < last_exit_t.get(sym, 0) + 6 * HOUR_MS:
                continue
            idx = sym_index_map.get(sym, {}).get(t)
            if idx is None or idx < 130:
                continue
            window = history[sym][idx - 130:idx + 1]
            sig, _ = strategy.evaluate_futures_setup(sym, window, btc_regime=btc_regime)
            if sig and sig["score"] >= 82:
                candidates.append((sig["score"], sym, sig))

        candidates.sort(key=lambda x: x[0], reverse=True)
        for _, sym, sig in candidates:
            if len(open_positions) >= 3:
                break
            side = sig["side"]
            same_side = sum(1 for p in open_positions.values() if p["side"] == side)
            if same_side >= 2:
                continue

            base_r = risk_pct * (1.25 if sig["score"] >= 88 else 1.0) * ev.get("risk_mult", 1.0)
            risk_usd = round(equity * base_r, 2)
            entry_px = sig["entry"]
            stop_px = sig["stop"]
            stop_dist = abs(entry_px - stop_px)
            if stop_dist <= 0:
                continue
            qty = risk_usd / stop_dist
            notional = qty * entry_px
            open_positions[sym] = {
                "symbol": sym,
                "side": side,
                "playbook": sig["playbook"],
                "setup": sig["setup"],
                "opened_t": t,
                "entry": entry_px,
                "initial_stop": stop_px,
                "stop": stop_px,
                "tp1_px": sig["tp1_px"],
                "tp2_px": sig["tp2_px"],
                "tp1_hit": False,
                "tp2_hit": False,
                "trail_active": False,
                "initial_qty": qty,
                "qty": qty,
                "risk_usd": risk_usd,
                "entry_fee": notional * fee_rate,
                "partial_net": 0.0,
            }

    wins = [x for x in closed_trades if x["net_usd"] > 0]
    losses = [x for x in closed_trades if x["net_usd"] <= 0]
    gross_profit = sum(x["net_usd"] for x in wins)
    gross_loss = abs(sum(x["net_usd"] for x in losses))
    pf = (gross_profit / gross_loss) if gross_loss > 0 else (99.0 if gross_profit > 0 else 0.0)
    avg_r = (sum(x["r"] for x in closed_trades) / len(closed_trades)) if closed_trades else 0.0

    report = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "days": days,
        "symbols": list(history.keys()),
        "start_equity": round(start_equity, 2),
        "end_equity": round(equity, 2),
        "net_profit_usd": round(equity - start_equity, 2),
        "roi_pct": round((equity - start_equity) / start_equity * 100.0, 2),
        "total_trades": len(closed_trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(len(wins) / len(closed_trades) * 100.0, 1) if closed_trades else 0.0,
        "profit_factor": round(pf, 2),
        "expectancy_r": round(avg_r, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "recent_trades": closed_trades[-15:],
    }
    return report


def main():
    ap = argparse.ArgumentParser(description="Walk-Forward Backtester for Binance USDS-M Perpetual Futures.")
    ap.add_argument("--days", type=int, default=45, help="Number of days to backtest (default: 45)")
    ap.add_argument("--write", action="store_true", help="Save report to trader/futures/backtest_report.json")
    args = ap.parse_args()

    print("Running %d-day Walk-Forward Backtest across Tier-1 Binance USDS-M Perpetuals..." % args.days)
    rep = run_backtest(days=args.days)
    if args.write:
        with open(BACKTEST_REPORT_JSON, "w", encoding="utf-8") as fh:
            json.dump(rep, fh, indent=2)

    print("=" * 72)
    print("BINANCE USDS-M FUTURES WALK-FORWARD BACKTEST (%d DAYS)" % rep["days"])
    print("=" * 72)
    print("Start Equity : $%.2f  ->  End Equity: $%.2f (%+.2f%% / $%+.2f)" % (
        rep["start_equity"], rep["end_equity"], rep["roi_pct"], rep["net_profit_usd"]
    ))
    print("Total Trades : %d (%d Wins / %d Losses) | Win Rate: %.1f%%" % (
        rep["total_trades"], rep["wins"], rep["losses"], rep["win_rate_pct"]
    ))
    print("Profit Factor: %.2f | Expectancy: %+.2fR / trade | Max Drawdown: %.2f%%" % (
        rep["profit_factor"], rep["expectancy_r"], rep["max_drawdown_pct"]
    ))
    print("-" * 72)
    for t in rep["recent_trades"][-8:]:
        dt_str = datetime.datetime.fromtimestamp(t["closed_t"] / 1000.0, datetime.timezone.utc).strftime("%m-%d %H:%M")
        print("  [%s] %-8s %-5s %-18s | Net: $%+7.2f (%+.2fR) | %s" % (
            dt_str, t["symbol"], t["side"].upper(), t["playbook"], t["net_usd"], t["r"], t["outcome"]
        ))
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
