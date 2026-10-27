#!/usr/bin/env python3
"""Cloud Runner for GitHub Actions & GitHub Web (Python 3.8+, standard library only).

Enables continuous, unattended execution of the Institutional Binance Trader on GitHub
infrastructure, with automatic state persistence, GitHub Pages deployment, and
step summary reporting.

Usage:
    python trader/live/cloud_runner.py --mode once --build-site _site
    python trader/live/cloud_runner.py --mode continuous --duration 15 --build-site _site
    python trader/live/cloud_runner.py --mode predict --symbols BTC ETH SOL BNB
"""

import argparse
import datetime
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
ANALYST_DIR = os.path.join(ROOT, "trader-analyst")
if HERE not in sys.path:
    sys.path.insert(0, HERE)
if ANALYST_DIR not in sys.path:
    sys.path.insert(0, ANALYST_DIR)

import paper_trader  # noqa: E402

STATE_FILE = os.path.join(ROOT, "trader", "live", "state.json")
DASHBOARD_FILE = os.path.join(ROOT, "trader", "live", "dashboard.html")
APP_HTML_FILE = os.path.join(ROOT, "app", "index.html")
TRADES_FILE = os.path.join(ROOT, "trader", "live", "trades.jsonl")
NEWS_FILE = os.path.join(ROOT, "trader", "live", "news_feed.json")
FORECAST_FILE = os.path.join(ROOT, "trader", "forecast.json")


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def usd(val, signed=False):
    f = float(val or 0.0)
    sign = ("+" if f > 0 else "-" if f < 0 else "") if signed else ("-" if f < 0 else "")
    return "%s$%s" % (sign, "{:,.2f}".format(abs(f)))


def run_cycle():
    """Run a single end-to-end trading and position management cycle."""
    print("[%s] Starting cloud trading cycle..." % now_iso(), flush=True)
    s = paper_trader.load_state()
    paper_trader.cycle(s)
    paper_trader.save(s)
    paper_trader.write_dashboard(s)
    print("[%s] Cycle complete. Equity: %s | Active positions: %d" % (
        now_iso(), usd(s.get("start_equity", 5000.0) + s.get("realized", 0.0)), len(s.get("positions", {}))
    ), flush=True)
    return s


def run_continuous(duration_minutes=15, interval_seconds=60):
    """Run recurring cycles continuously for the specified duration (minutes)."""
    end_time = time.time() + (duration_minutes * 60)
    print("[%s] Starting continuous cloud runner for %d minutes (interval %ds)..." % (
        now_iso(), duration_minutes, interval_seconds
    ), flush=True)
    iteration = 0
    last_state = None
    while time.time() < end_time:
        iteration += 1
        print("\n--- Iteration %d (%s remaining) ---" % (
            iteration, datetime.timedelta(seconds=int(max(0, end_time - time.time())))
        ), flush=True)
        try:
            last_state = run_cycle()
        except Exception as e:
            print("[%s] Warning in cycle iteration %d: %s" % (now_iso(), iteration, e), file=sys.stderr)
        
        sleep_secs = min(interval_seconds, max(1, int(end_time - time.time())))
        if time.time() + sleep_secs >= end_time:
            break
        time.sleep(sleep_secs)
    print("[%s] Continuous runner finished %d iterations." % (now_iso(), iteration), flush=True)
    return last_state


def run_predictions(symbols=None):
    """Run directional forecasts for top liquid symbols."""
    syms = symbols or ["BTC", "ETH", "SOL", "BNB"]
    print("[%s] Generating microstructure forecasts for: %s..." % (now_iso(), ", ".join(syms)), flush=True)
    try:
        import predict
        d = predict.forecast_tokens(syms, write_path=FORECAST_FILE)
        print("[%s] Forecast generated successfully. %d tokens analyzed." % (now_iso(), len(d.get("tokens", []))), flush=True)
        return d
    except Exception as e:
        print("[%s] Warning: could not run predict.py: %s" % (now_iso(), e), file=sys.stderr)
        return None


def build_static_site(dest_dir="_site"):
    """Prepare static site directory for instant deployment to GitHub Pages."""
    os.makedirs(dest_dir, exist_ok=True)
    print("[%s] Building GitHub Pages static site in '%s'..." % (now_iso(), dest_dir), flush=True)

    # 1. Main Interactive Simulation Terminal -> index.html & app.html
    if os.path.isfile(APP_HTML_FILE):
        shutil.copyfile(APP_HTML_FILE, os.path.join(dest_dir, "index.html"))
        shutil.copyfile(APP_HTML_FILE, os.path.join(dest_dir, "app.html"))
    elif os.path.isfile(DASHBOARD_FILE):
        shutil.copyfile(DASHBOARD_FILE, os.path.join(dest_dir, "index.html"))

    # 2. Automated 24/7 Cloud Bot Dashboard -> dashboard.html
    if os.path.isfile(DASHBOARD_FILE):
        shutil.copyfile(DASHBOARD_FILE, os.path.join(dest_dir, "dashboard.html"))

    # 3. JSON data feeds for dynamic client queries
    for src in (STATE_FILE, TRADES_FILE, NEWS_FILE, FORECAST_FILE):
        if os.path.isfile(src):
            shutil.copyfile(src, os.path.join(dest_dir, os.path.basename(src)))

    # 4. Create .nojekyll so GitHub Pages doesn't ignore files
    with open(os.path.join(dest_dir, ".nojekyll"), "w", encoding="utf-8") as fh:
        fh.write("")

    print("[%s] GitHub Pages static assets assembled successfully." % now_iso(), flush=True)


def write_github_step_summary(summary_file=None):
    """Write an institutional summary table to GitHub Actions Step Summary."""
    target = summary_file or os.environ.get("GITHUB_STEP_SUMMARY")
    if not target:
        return
    if not os.path.isfile(STATE_FILE):
        return

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            s = json.load(fh)
    except Exception:
        return

    realized_eq = float(s.get("start_equity", 5000.0)) + float(s.get("realized", 0.0))
    positions = s.get("positions", {})
    closed = s.get("closed", [])
    wins = sum(1 for t in closed if float(t.get("net", 0.0)) > 0)
    win_rate = (wins / len(closed) * 100.0) if closed else 0.0
    total_pnl = float(s.get("realized", 0.0))

    unrealized_sum = 0.0
    for p in positions.values():
        m = float(s.get("marks", {}).get(p.get("symbol", ""), p.get("entry", 0.0)))
        qty = float(p.get("qty", 0.0))
        entry = float(p.get("entry", 0.0))
        if p.get("side") == "long":
            unrealized_sum += (m - entry) * qty
        else:
            unrealized_sum += (entry - m) * qty

    lines = [
        "## 🚀 Institutional Binance Crypto Trader -- Live Execution Summary",
        "",
        "> **Run Timestamp**: `%s` | **Mode**: Cloud 24/7 Automated Engine" % now_iso(),
        "",
        "### 📊 Portfolio Metrics",
        "",
        "| Metric | Current Value | Target / Benchmark |",
        "| :--- | :--- | :--- |",
        "| **Account Realized Equity** | `%s` | Starting: $5,000.00 |" % usd(realized_eq),
        "| **Net Realized PnL** | `%s` | Risk Budget: $50/trade |" % usd(total_pnl, signed=True),
        "| **Unrealized PnL** | `%s` | Mark-to-Market |" % usd(unrealized_sum, signed=True),
        "| **Total Closed Trades** | `%d` (Win Rate: `%.1f%%`) | Profit Factor Target: >2.0 |" % (len(closed), win_rate),
        "| **Active Positions** | `%d / 4 max` | Max Gross Leverage: 2.0x |" % len(positions),
        "",
    ]

    if positions:
        lines.extend([
            "### 🎯 Open Margin Positions (Convex Multi-Tier Exits)",
            "",
            "| ID | Symbol | Side | Leverage | Entry | Mark Price | Unrealized PnL | Stop-Loss | TP1 (40%) | TP2 (30%) | Liq Price |",
            "| :-: | :--- | :-: | :-: | :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
        ])
        for p in positions.values():
            sym = p.get("symbol", "")
            m = float(s.get("marks", {}).get(sym, p.get("entry", 0.0)))
            entry = float(p.get("entry", 0.0))
            qty = float(p.get("qty", 0.0))
            pnl = (m - entry) * qty if p.get("side") == "long" else (entry - m) * qty
            lev = float(p.get("leverage", 1.0))
            lines.append(
                "| #%d | **%s** | `%s` | `%.0fx` | `%s` | `%s` | **%s** | `%s` | `%s` | `%s` | `%s` |" % (
                    p.get("id", 0), sym, str(p.get("side", "")).upper(), lev,
                    "{:,.4f}".format(entry), "{:,.4f}".format(m),
                    usd(pnl, signed=True),
                    "{:,.4f}".format(float(p.get("stop", 0.0))),
                    "{:,.4f}".format(float(p.get("tp1_px", 0.0))),
                    "{:,.4f}".format(float(p.get("tp2_px", 0.0))),
                    "{:,.4f}".format(float(p.get("liq_px", 0.0))),
                )
            )
        lines.append("")

    funding_arb = s.get("funding_arb", [])
    if funding_arb:
        lines.extend([
            "### ⚡ Top Delta-Neutral Cash-and-Carry Funding Arbitrage",
            "",
            "| Symbol | 8h Funding Rate | Annualized APR | Spot-Perp Basis | Strategy |",
            "| :--- | :-: | :-: | :-: | :--- |",
        ])
        for fo in funding_arb[:5]:
            lines.append(
                "| **%s** | `%.4f%%` | **%+.1f%%** | `%.2f%%` | %s |" % (
                    fo.get("symbol", ""),
                    float(fo.get("rate_8h", 0.0)),
                    float(fo.get("apr_pct", 0.0)),
                    float(fo.get("basis_pct", 0.0)),
                    fo.get("strategy", ""),
                )
            )
        lines.append("")

    lines.extend([
        "---",
        "🌐 **Live Web Dashboard**: Visit your deployed GitHub Pages URL to monitor the interactive terminal, live charts, and real-time order-book metrics.",
    ])

    with open(target, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print("[%s] GitHub Step Summary successfully written." % now_iso(), flush=True)


def main():
    parser = argparse.ArgumentParser(description="Cloud Runner for Institutional Binance Crypto Trader")
    parser.add_argument("--mode", choices=["once", "continuous", "predict", "status"], default="once",
                        help="Execution mode (default: once)")
    parser.add_argument("--duration", type=int, default=15,
                        help="Duration in minutes for continuous mode (default: 15)")
    parser.add_argument("--interval", type=int, default=60,
                        help="Interval in seconds between cycles in continuous mode (default: 60)")
    parser.add_argument("--symbols", nargs="+", default=["BTC", "ETH", "SOL", "BNB"],
                        help="Symbols for prediction mode (default: BTC ETH SOL BNB)")
    parser.add_argument("--build-site", type=str, default="",
                        help="Output directory to build GitHub Pages static site (e.g. _site)")
    parser.add_argument("--summary", action="store_true",
                        help="Write markdown summary to GITHUB_STEP_SUMMARY")
    args = parser.parse_args()

    if args.mode == "once":
        run_cycle()
    elif args.mode == "continuous":
        run_continuous(duration_minutes=args.duration, interval_seconds=args.interval)
    elif args.mode == "predict":
        run_predictions(symbols=args.symbols)
    elif args.mode == "status":
        s = paper_trader.load_state()
        paper_trader.print_status(s)

    # Optional predictions refresh
    if args.mode in ("once", "continuous"):
        try:
            run_predictions(symbols=["BTC", "ETH", "SOL", "BNB"])
        except Exception:
            pass

    # Build static site if requested
    if args.build_site:
        build_static_site(dest_dir=args.build_site)

    # Write summary if requested
    if args.summary:
        write_github_step_summary()


if __name__ == "__main__":
    main()
