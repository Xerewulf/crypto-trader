#!/usr/bin/env python3
"""Local iPhone 13 Crypto Paper-Trading Simulator & Margin Terminal Server.
Runs strictly on your local network (LAN / localhost) with simulated virtual USDT/BNB
and real-time live Binance Spot & Perpetual Futures data. Standard library only.

Features:
- Full Isolated Crypto Margin Trading Simulation (LONG & SHORT positions)
- 1x, 2x, 3x, 5x, 10x Isolated Leverage with automatic liquidation price calculation
- Dual-Currency Denomination: Everything computed & displayed in both USDT and BNB (Binance Coin)
- Simulation Engine Controls: START & STOP simulation engine toggle
- Real-time Mark-to-Market PnL, Stop-Loss (SL), Take-Profit (TP), and Liquidation auto-triggers
- 1-Click Buy, Sell, Close Partial/Full, and Position Reversal

Usage:
    python app/server.py --port 8765
"""

import argparse
import datetime
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import socket
import sys
import asyncio
import threading
import time

try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    aiohttp = None
    HAS_AIOHTTP = False
import urllib.parse
import urllib.request

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.dirname(os.path.abspath(__file__))
ANALYST_DIR = os.path.join(ROOT_DIR, "trader-analyst")
if ANALYST_DIR not in sys.path:
    sys.path.insert(0, ANALYST_DIR)

import math  # noqa: E402
import statistics  # noqa: E402
import predict  # noqa: E402
try:
    import margin  # noqa: E402
except ImportError:
    margin = None
try:
    import arbitrage  # noqa: E402
except ImportError:
    arbitrage = None
try:
    import microstructure  # noqa: E402
except ImportError:
    microstructure = None
try:
    import sor  # noqa: E402
except ImportError:
    sor = None

WALLET_PATH = os.path.join(ROOT_DIR, "trader", "sim_wallet.json")
FORECAST_PATH = os.path.join(ROOT_DIR, "trader", "forecast.json")
WALLET_LOCK = threading.Lock()
PRICE_CACHE = {}
PRICE_CACHE_TS = 0.0
FUTURES_PREMIUM_CACHE = []
FUTURES_PREMIUM_CACHE_TS = 0.0
FORECAST_CACHE = {}
WS_STATS = {
    "connected": False,
    "ticks_received": 0,
    "rate_per_sec": 0.0,
    "last_tick_iso": None,
    "stream": "wss://stream.binance.com:9443/ws/!miniTicker@arr",
    "mode": "WebSocket (Live 0ms Relay)",
}

DEFAULT_WATCHLIST = [
    "BNBUSDT", "BTCUSDT", "ETHUSDT", "SOLUSDT",
    "PHAUSDT", "PEPEUSDT", "ENAUSDT", "ERAUSDT",
    "DOGEUSDT", "SUIUSDT", "XRPUSDT", "ADAUSDT",
]

FEE_RATE = 0.0004  # 0.04% standard Binance Futures/Margin taker fee with BNB discount


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_lan_ip():
    """Detect local Wi-Fi / LAN IPv4 address so iPhone 13 on the same Wi-Fi can connect."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def calc_liquidation_price(entry, side, leverage, mmr=0.01):
    """Calculate isolated margin liquidation price."""
    entry = float(entry)
    leverage = max(1.0, float(leverage))
    side = "LONG" if str(side).upper() in ("BUY", "LONG") else "SHORT"
    if leverage <= 1.0:
        return 0.0 if side == "LONG" else round(entry * 2.0, 6)
    buffer_frac = (1.0 - mmr) / leverage
    if side == "LONG":
        return max(0.0, round(entry * (1.0 - buffer_frac), 6))
    else:
        return round(entry * (1.0 + buffer_frac), 6)


def default_wallet(starting_cash=10000.0):
    return {
        "starting_cash": round(float(starting_cash), 2),
        "cash_usdt": round(float(starting_cash), 2),
        "realized_pnl_usdt": 0.0,
        "total_trades": 0,
        "winning_trades": 0,
        "losing_trades": 0,
        "positions": {},       # symbol -> {symbol, side, qty, avg_entry, leverage, margin_locked_usdt, notional_usdt, liquidation_price, stop_loss, take_profit, opened_at}
        "arbitrage_pairs": {}, # pair_id -> {pair_id, symbol, strategy, spot_leg, perp_leg, delta, metrics, ...}
        "open_orders": [],     # [{id, symbol, side, type, leverage, limit_price, usdt_amount, margin_usdt, stop_loss, take_profit, created_at}]
        "history": [],         # [{id, time, symbol, side, action, qty, price, leverage, margin_usdt, usdt_total, fee_usdt, pnl_usdt, pnl_pct, reason}]
        "simulation_running": True,
        "updated_at": now_iso(),
    }


def load_wallet():
    os.makedirs(os.path.dirname(WALLET_PATH), exist_ok=True)
    if not os.path.exists(WALLET_PATH):
        w = default_wallet(10000.0)
        save_wallet(w)
        return w
    try:
        with open(WALLET_PATH, "r", encoding="utf-8") as fh:
            w = json.load(fh)
        if not isinstance(w, dict) or "cash_usdt" not in w:
            w = default_wallet(10000.0)
        w.setdefault("positions", {})
        w.setdefault("arbitrage_pairs", {})
        w.setdefault("open_orders", [])
        w.setdefault("history", [])
        w.setdefault("simulation_running", True)
        return w
    except Exception:
        w = default_wallet(10000.0)
        save_wallet(w)
        return w


def save_wallet(w):
    w["updated_at"] = now_iso()
    os.makedirs(os.path.dirname(WALLET_PATH), exist_ok=True)
    tmp = WALLET_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(w, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, WALLET_PATH)


def fetch_live_tickers(symbols=None):
    """Fetch live 24h ticker stats from Binance Spot with 2-second memory cache."""
    global PRICE_CACHE, PRICE_CACHE_TS
    now = time.time()
    if now - PRICE_CACHE_TS > 2.0 or not PRICE_CACHE:
        try:
            raw = predict.spot_get("/api/v3/ticker/24hr")
            if isinstance(raw, list):
                PRICE_CACHE = {
                    r["symbol"]: {
                        "symbol": r["symbol"],
                        "price": float(r.get("lastPrice") or 0.0),
                        "change_24h_pct": float(r.get("priceChangePercent") or 0.0),
                        "high_24h": float(r.get("highPrice") or 0.0),
                        "low_24h": float(r.get("lowPrice") or 0.0),
                        "volume_24h_usd": float(r.get("quoteVolume") or 0.0),
                        "bid": float(r.get("bidPrice") or r.get("lastPrice") or 0.0),
                        "ask": float(r.get("askPrice") or r.get("lastPrice") or 0.0),
                    }
                    for r in raw if "symbol" in r and float(r.get("lastPrice") or 0.0) > 0
                }
                PRICE_CACHE_TS = now
        except Exception:
            pass
    if not symbols:
        return PRICE_CACHE
    out = {}
    for s in symbols:
        bs = predict.to_binance(s)
        if bs in PRICE_CACHE:
            out[bs] = PRICE_CACHE[bs]
        else:
            try:
                r = predict.spot_get("/api/v3/ticker/24hr?symbol=%s" % bs)
                info = {
                    "symbol": bs,
                    "price": float(r.get("lastPrice") or 0.0),
                    "change_24h_pct": float(r.get("priceChangePercent") or 0.0),
                    "high_24h": float(r.get("highPrice") or 0.0),
                    "low_24h": float(r.get("lowPrice") or 0.0),
                    "volume_24h_usd": float(r.get("quoteVolume") or 0.0),
                    "bid": float(r.get("bidPrice") or r.get("lastPrice") or 0.0),
                    "ask": float(r.get("askPrice") or r.get("lastPrice") or 0.0),
                }
                PRICE_CACHE[bs] = info
                out[bs] = info
            except Exception:
                continue
    return out


def _open_margin_position_locked(w, bsym, side, exec_price, notional_usdt=None, margin_usdt=None, leverage=3.0, stop_loss=None, take_profit=None, reason="Market Order"):
    """Open or add to an isolated margin position (LONG or SHORT) with leverage."""
    side = "LONG" if str(side).upper() in ("BUY", "LONG") else "SHORT"
    leverage = max(1.0, min(10.0, float(leverage or 3.0)))
    exec_price = float(exec_price)
    if exec_price <= 0:
        raise ValueError("Invalid execution price.")

    if notional_usdt is not None and float(notional_usdt) > 0:
        notional_usdt = float(notional_usdt)
        margin_req = notional_usdt / leverage
    elif margin_usdt is not None and float(margin_usdt) > 0:
        margin_req = float(margin_usdt)
        notional_usdt = margin_req * leverage
    else:
        raise ValueError("Must provide order amount greater than $0.")

    fee_usdt = round(notional_usdt * FEE_RATE, 4)
    total_cost = margin_req + fee_usdt
    if total_cost > w["cash_usdt"] + 1e-4:
        raise ValueError("Insufficient virtual cash ($%.2f free margin available, tried $%.2f)." % (w["cash_usdt"], total_cost))

    # Institutional Risk Doctrine Guards
    base = predict.base_asset(bsym) if hasattr(predict, "base_asset") else bsym.replace("USDT", "")
    if margin and hasattr(margin, "is_tokenized_stock") and margin.is_tokenized_stock(bsym):
        raise ValueError("Tokenized equity %s is excluded from 24/7 crypto margin trading." % bsym)

    tier_cap = margin.leverage_cap(base) if margin and hasattr(margin, "leverage_cap") else 3.0
    leverage = max(1.0, min(float(leverage), tier_cap))

    if margin and hasattr(margin, "event_risk"):
        now_ms = int(time.time() * 1000)
        ev = margin.event_risk(now_ms)
        if ev.get("level") == "BLOCK":
            raise ValueError("Macro Risk Guard: %s" % ev.get("reason", "Event Block Active"))
        if ev.get("level") == "CAUTION":
            leverage = max(1.0, min(leverage, math.floor(tier_cap * 0.5)))

    if stop_loss is not None and float(stop_loss) > 0:
        sl_val = float(stop_loss)
        if (side == "LONG" and sl_val >= exec_price) or (side == "SHORT" and sl_val <= exec_price):
            raise ValueError("Stop-loss (%s) is on the wrong side for a %s position." % (predict.fmt_p(sl_val), side))

    qty = notional_usdt / exec_price
    liq_p = calc_liquidation_price(exec_price, side, leverage)

    pos = w["positions"].get(bsym)
    if pos:
        cur_side = pos.get("side", "LONG")
        if cur_side == side:
            old_qty = float(pos["qty"])
            old_notional = float(pos.get("notional_usdt") or (old_qty * float(pos["avg_entry"])))
            old_margin = float(pos.get("margin_locked_usdt") or (old_notional / float(pos.get("leverage", 1.0))))
            new_qty = old_qty + qty
            new_notional = old_notional + notional_usdt
            new_margin = old_margin + margin_req
            pos["qty"] = new_qty
            pos["avg_entry"] = new_notional / new_qty
            pos["notional_usdt"] = round(new_notional, 4)
            pos["margin_locked_usdt"] = round(new_margin, 4)
            pos["leverage"] = leverage
            pos["liquidation_price"] = calc_liquidation_price(pos["avg_entry"], side, leverage)
            if stop_loss is not None and float(stop_loss) > 0:
                pos["stop_loss"] = float(stop_loss)
            if take_profit is not None and float(take_profit) > 0:
                pos["take_profit"] = float(take_profit)
        else:
            # Opposite side: close current position first, then open remainder
            _close_margin_position_locked(w, bsym, exec_price, close_pct=100.0, reason="Reversed to %s" % side)
            return _open_margin_position_locked(w, bsym, side, exec_price, notional_usdt=notional_usdt, leverage=leverage, stop_loss=stop_loss, take_profit=take_profit, reason=reason)
    else:
        w["positions"][bsym] = {
            "symbol": bsym,
            "side": side,
            "qty": qty,
            "avg_entry": exec_price,
            "leverage": leverage,
            "notional_usdt": round(notional_usdt, 4),
            "margin_locked_usdt": round(margin_req, 4),
            "liquidation_price": liq_p,
            "stop_loss": float(stop_loss) if stop_loss and float(stop_loss) > 0 else None,
            "take_profit": float(take_profit) if take_profit and float(take_profit) > 0 else None,
            "opened_at": now_iso(),
        }

    w["cash_usdt"] = round(max(0.0, w["cash_usdt"] - total_cost), 4)
    trade_rec = {
        "id": "T%d" % int(time.time() * 1000),
        "time": now_iso(),
        "symbol": bsym,
        "side": side,
        "action": "OPEN %s %dx" % (side, int(leverage)),
        "qty": qty,
        "price": exec_price,
        "leverage": leverage,
        "margin_usdt": round(margin_req, 2),
        "usdt_total": round(notional_usdt, 2),
        "fee_usdt": fee_usdt,
        "pnl_usdt": None,
        "pnl_pct": None,
        "reason": reason,
    }
    w["history"].insert(0, trade_rec)
    w["history"] = w["history"][:100]
    return trade_rec


def _close_margin_position_locked(w, bsym, exec_price, close_pct=100.0, reason="Market Close"):
    """Close part or all of an open isolated margin position."""
    pos = w["positions"].get(bsym)
    if not pos or float(pos.get("qty", 0)) <= 0:
        raise ValueError("No open position in %s to close." % bsym)

    side = pos.get("side", "LONG")
    total_qty = float(pos["qty"])
    total_margin = float(pos.get("margin_locked_usdt") or pos.get("invested_usdt", 0.0))
    avg_entry = float(pos["avg_entry"])
    leverage = float(pos.get("leverage", 1.0))

    frac = min(1.0, max(0.001, float(close_pct) / 100.0))
    close_qty = total_qty * frac
    close_margin = total_margin * frac
    close_notional = close_qty * exec_price
    fee_usdt = round(close_notional * FEE_RATE, 4)

    if side == "LONG":
        gross_pnl = (exec_price - avg_entry) * close_qty
    else:  # SHORT
        gross_pnl = (avg_entry - exec_price) * close_qty

    net_pnl = round(gross_pnl - fee_usdt, 4)
    roi_pct = round((net_pnl / close_margin) * 100.0, 2) if close_margin > 0 else 0.0

    returned_cash = max(0.0, close_margin + net_pnl)
    w["cash_usdt"] = round(w["cash_usdt"] + returned_cash, 4)
    w["realized_pnl_usdt"] = round(w.get("realized_pnl_usdt", 0.0) + net_pnl, 4)
    w["total_trades"] = int(w.get("total_trades", 0)) + 1
    if net_pnl >= 0:
        w["winning_trades"] = int(w.get("winning_trades", 0)) + 1
    else:
        w["losing_trades"] = int(w.get("losing_trades", 0)) + 1

    rem_qty = total_qty - close_qty
    if frac >= 0.999 or rem_qty <= total_qty * 1e-4 or rem_qty * exec_price < 0.10:
        del w["positions"][bsym]
    else:
        pos["qty"] = rem_qty
        pos["margin_locked_usdt"] = round(total_margin - close_margin, 4)
        pos["notional_usdt"] = round(rem_qty * avg_entry, 4)

    trade_rec = {
        "id": "T%d" % int(time.time() * 1000),
        "time": now_iso(),
        "symbol": bsym,
        "side": side,
        "action": "CLOSE %s (%d%%)" % (side, int(round(frac * 100))),
        "qty": close_qty,
        "price": exec_price,
        "leverage": leverage,
        "margin_usdt": round(close_margin, 2),
        "usdt_total": round(close_notional, 2),
        "fee_usdt": fee_usdt,
        "pnl_usdt": round(net_pnl, 2),
        "pnl_pct": roi_pct,
        "reason": reason,
    }
    w["history"].insert(0, trade_rec)
    w["history"] = w["history"][:100]
    return trade_rec


def _exec_buy_locked(w, bsym, exec_price, usdt_amount, stop_loss=None, take_profit=None, reason="Market Buy"):
    """Backward compatibility wrapper."""
    return _open_margin_position_locked(w, bsym, "LONG", exec_price, notional_usdt=usdt_amount, leverage=1.0, stop_loss=stop_loss, take_profit=take_profit, reason=reason)


def _exec_sell_locked(w, bsym, exec_price, sell_qty=None, sell_pct=None, reason="Market Sell"):
    """Backward compatibility wrapper."""
    pct = float(sell_pct or 100.0)
    return _close_margin_position_locked(w, bsym, exec_price, close_pct=pct, reason=reason)


def fetch_live_futures_premium_index():
    """Fetch all Binance Perpetual Futures mark prices and 8h funding rates with memory cache."""
    global FUTURES_PREMIUM_CACHE, FUTURES_PREMIUM_CACHE_TS
    now = time.time()
    if now - FUTURES_PREMIUM_CACHE_TS > 4.0 or not FUTURES_PREMIUM_CACHE:
        try:
            req = urllib.request.Request(
                "https://fapi.binance.com/fapi/v1/premiumIndex",
                headers={"User-Agent": "Mozilla/5.0"}
            )
            with urllib.request.urlopen(req, timeout=3.5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if isinstance(data, list) and data:
                    FUTURES_PREMIUM_CACHE = data
                    FUTURES_PREMIUM_CACHE_TS = now
        except Exception:
            pass
    if not FUTURES_PREMIUM_CACHE:
        # High-fidelity offline simulation fallback
        now_ms = int(now * 1000)
        next_8h = now_ms + 14400000
        FUTURES_PREMIUM_CACHE = [
            {"symbol": "BNBUSDT", "markPrice": "740.10", "lastFundingRate": "0.00020", "nextFundingTime": next_8h},
            {"symbol": "BTCUSDT", "markPrice": "90050.00", "lastFundingRate": "0.00010", "nextFundingTime": next_8h},
            {"symbol": "ETHUSDT", "markPrice": "2502.50", "lastFundingRate": "0.00015", "nextFundingTime": next_8h},
            {"symbol": "SOLUSDT", "markPrice": "180.20", "lastFundingRate": "0.00025", "nextFundingTime": next_8h},
            {"symbol": "ENAUSDT", "markPrice": "0.4520", "lastFundingRate": "0.00045", "nextFundingTime": next_8h},
            {"symbol": "PEPEUSDT", "markPrice": "0.0000105", "lastFundingRate": "-0.00350", "nextFundingTime": next_8h},
            {"symbol": "SUIUSDT", "markPrice": "2.1050", "lastFundingRate": "-0.00080", "nextFundingTime": next_8h},
            {"symbol": "DOGEUSDT", "markPrice": "0.1450", "lastFundingRate": "0.00010", "nextFundingTime": next_8h},
            {"symbol": "XRPUSDT", "markPrice": "0.5420", "lastFundingRate": "0.00012", "nextFundingTime": next_8h},
        ]
    return FUTURES_PREMIUM_CACHE


def _open_arbitrage_pair_locked(w, symbol, strategy="CASH_AND_CARRY", capital_usdt=2000.0, spot_price=None, perp_mark=None, funding_rate=None, reason="Delta-Neutral Execution"):
    """Open a synthetic delta-neutral arbitrage pair (Spot leg + 1x Perp hedge leg)."""
    if not arbitrage:
        raise ValueError("Arbitrage module not available.")
    if sor:
        cb = sor.evaluate_circuit_breakers(w)
        if not cb.get("allowed_to_trade", True):
            reasons = "; ".join(cb.get("reasons", ["Circuit breaker active"]))
            raise ValueError("CIRCUIT BREAKER BLOCKED ARBITRAGE: %s" % reasons)
    bsym = predict.to_binance(symbol)
    capital_usdt = float(capital_usdt)
    if capital_usdt <= 0:
        raise ValueError("Capital must be greater than $0.")

    if spot_price is None or float(spot_price) <= 0:
        tk = fetch_live_tickers([bsym]).get(bsym) or {}
        spot_price = float(tk.get("price") or 0.0)
    if spot_price <= 0:
        spot_price = 739.95 if bsym == "BNBUSDT" else 100.0

    if perp_mark is None or funding_rate is None:
        p_list = fetch_live_futures_premium_index()
        f_match = next((item for item in p_list if item.get("symbol") == bsym or item.get("symbol") == "1000" + bsym), None)
        if f_match:
            perp_mark = float(f_match.get("markPrice") or spot_price)
            funding_rate = float(f_match.get("lastFundingRate") or 0.0001)
        else:
            perp_mark = spot_price
            funding_rate = 0.0002 if strategy == "CASH_AND_CARRY" else -0.0010

    pair = arbitrage.build_delta_neutral_pair(
        symbol=bsym,
        strategy=strategy,
        capital_usdt=capital_usdt,
        spot_price=spot_price,
        perp_mark=perp_mark,
        funding_rate_8h=funding_rate,
        leverage=1.0,
        taker_fee_rate=FEE_RATE,
    )

    total_cost = pair["total_capital_usdt"] + pair["metrics"]["initial_entry_fees_usdt"]
    if total_cost > w["cash_usdt"] + 1e-4:
        raise ValueError("Insufficient virtual cash ($%.2f available, tried $%.2f for arbitrage)." % (w["cash_usdt"], total_cost))

    pair_id = "ARB-%s-%d" % (bsym, int(time.time() * 1000))
    pair["id"] = pair_id
    pair["opened_at"] = now_iso()
    pair["last_accrual_ts"] = time.time()
    pair["accrued_funding_usdt"] = 0.0

    w.setdefault("arbitrage_pairs", {})
    w["arbitrage_pairs"][pair_id] = pair
    w["cash_usdt"] = round(max(0.0, w["cash_usdt"] - total_cost), 4)

    trade_rec = {
        "id": "T%d" % int(time.time() * 1000),
        "time": now_iso(),
        "symbol": bsym,
        "side": "DELTA-NEUTRAL",
        "action": "OPEN ARB %s" % pair["strategy"],
        "qty": pair["spot_leg"]["qty"],
        "price": spot_price,
        "leverage": 1.0,
        "margin_usdt": pair["total_capital_usdt"],
        "usdt_total": pair["total_capital_usdt"],
        "fee_usdt": pair["metrics"]["initial_entry_fees_usdt"],
        "pnl_usdt": None,
        "pnl_pct": None,
        "reason": "%s (%+.2f%% APY)" % (pair["strategy"], pair["metrics"]["annualized_apy"]),
    }
    w["history"].insert(0, trade_rec)
    w["history"] = w["history"][:200]
    return pair


def _close_arbitrage_pair_locked(w, pair_id, spot_price=None, perp_mark=None, reason="Manual Unwind"):
    """Simultaneously close both legs of an active delta-neutral arbitrage pair."""
    if not arbitrage:
        raise ValueError("Arbitrage module not available.")
    w.setdefault("arbitrage_pairs", {})
    pair = w["arbitrage_pairs"].get(pair_id)
    if not pair:
        raise ValueError("Arbitrage pair %s not found." % pair_id)

    bsym = pair["symbol"]
    if spot_price is None or float(spot_price) <= 0:
        tk = fetch_live_tickers([bsym]).get(bsym) or {}
        spot_price = float(tk.get("price") or pair["spot_leg"]["price"])
    if perp_mark is None or float(perp_mark) <= 0:
        p_list = fetch_live_futures_premium_index()
        f_match = next((item for item in p_list if item.get("symbol") == bsym or item.get("symbol") == "1000" + bsym), None)
        perp_mark = float(f_match.get("markPrice") or spot_price) if f_match else spot_price

    now_t = time.time()
    dt = max(0.0, now_t - float(pair.get("last_accrual_ts", now_t)))
    pnl_info = arbitrage.compute_pair_realtime_pnl(
        pair,
        current_spot_price=spot_price,
        current_perp_mark=perp_mark,
        elapsed_seconds=dt,
    )

    exit_fees = round((float(pair["spot_leg"]["qty"]) * spot_price + float(pair["perp_leg"]["qty"]) * perp_mark) * FEE_RATE, 4)
    net_realized_pnl = round(pnl_info["total_net_pnl_usdt"] - exit_fees, 4)
    total_capital = float(pair["total_capital_usdt"])
    cash_returned = max(0.0, total_capital + net_realized_pnl)

    w["cash_usdt"] = round(w["cash_usdt"] + cash_returned, 4)
    w["realized_pnl_usdt"] = round(float(w.get("realized_pnl_usdt", 0.0)) + net_realized_pnl, 2)
    w["total_trades"] = int(w.get("total_trades", 0)) + 1
    if net_realized_pnl >= 0:
        w["winning_trades"] = int(w.get("winning_trades", 0)) + 1
    else:
        w["losing_trades"] = int(w.get("losing_trades", 0)) + 1

    del w["arbitrage_pairs"][pair_id]

    trade_rec = {
        "id": "T%d" % int(time.time() * 1000),
        "time": now_iso(),
        "symbol": bsym,
        "side": "DELTA-NEUTRAL",
        "action": "CLOSE ARB %s" % pair["strategy"],
        "qty": pair["spot_leg"]["qty"],
        "price": spot_price,
        "leverage": 1.0,
        "margin_usdt": total_capital,
        "usdt_total": round(cash_returned, 2),
        "fee_usdt": exit_fees,
        "pnl_usdt": net_realized_pnl,
        "pnl_pct": round(net_realized_pnl / total_capital * 100.0, 2) if total_capital > 0 else 0.0,
        "reason": reason,
    }
    w["history"].insert(0, trade_rec)
    w["history"] = w["history"][:200]
    return {
        "pair_id": pair_id,
        "symbol": bsym,
        "strategy": pair["strategy"],
        "realized_pnl_usdt": net_realized_pnl,
        "accrued_funding_usdt": pnl_info["accrued_funding_usdt"],
        "basis_pnl_usdt": pnl_info["basis_pnl_usdt"],
        "cash_returned": cash_returned,
    }


def check_triggers_and_enrich(extra_symbol=None):
    """Evaluate resting Limit Orders + Stop-Loss / Take-Profit / Liquidation triggers against live Binance prices,
    and return the enriched portfolio state with dual USDT and Binance Coin (BNB) values."""
    with WALLET_LOCK:
        w = load_wallet()
        if extra_symbol:
            bs = predict.to_binance(extra_symbol)
            if bs and bs not in DEFAULT_WATCHLIST:
                DEFAULT_WATCHLIST.insert(0, bs)
        needed = set(DEFAULT_WATCHLIST) | set(w["positions"].keys()) | {o["symbol"] for o in w["open_orders"]} | {"BNBUSDT"}
    tickers = fetch_live_tickers(needed)

    bnb_ticker = tickers.get("BNBUSDT") or {}
    bnb_price = float(bnb_ticker.get("price") or 735.0)
    if bnb_price <= 0:
        bnb_price = 735.0
    bnb_change_24h_pct = float(bnb_ticker.get("change_24h_pct") or 0.0)

    sim_running = w.get("simulation_running", True)

    with WALLET_LOCK:
        w = load_wallet()
        changed = False

        if sim_running:
            # 1. Check resting Limit Orders
            remaining_orders = []
            for o in w["open_orders"]:
                bsym = o["symbol"]
                tk = tickers.get(bsym)
                if not tk:
                    remaining_orders.append(o)
                    continue
                live_p = tk["price"]
                lim_p = float(o["limit_price"])
                side = str(o["side"]).upper()
                lev = float(o.get("leverage") or 3.0)

                if side in ("BUY", "LONG") and live_p <= lim_p:
                    try:
                        _open_margin_position_locked(
                            w, bsym, "LONG", lim_p,
                            notional_usdt=o.get("usdt_amount"),
                            leverage=lev,
                            stop_loss=o.get("stop_loss"),
                            take_profit=o.get("take_profit"),
                            reason="Limit Buy Filled @ %s" % predict.fmt_p(lim_p),
                        )
                        changed = True
                    except Exception:
                        remaining_orders.append(o)
                elif side in ("SELL", "SHORT") and live_p >= lim_p:
                    try:
                        if bsym in w["positions"] and w["positions"][bsym].get("side") == "LONG":
                            _close_margin_position_locked(w, bsym, lim_p, close_pct=o.get("sell_pct", 100.0), reason="Limit Sell Filled @ %s" % predict.fmt_p(lim_p))
                        else:
                            _open_margin_position_locked(
                                w, bsym, "SHORT", lim_p,
                                notional_usdt=o.get("usdt_amount"),
                                leverage=lev,
                                stop_loss=o.get("stop_loss"),
                                take_profit=o.get("take_profit"),
                                reason="Limit Short Filled @ %s" % predict.fmt_p(lim_p),
                            )
                        changed = True
                    except Exception:
                        remaining_orders.append(o)
                else:
                    remaining_orders.append(o)
            if len(remaining_orders) != len(w["open_orders"]):
                w["open_orders"] = remaining_orders
                changed = True

            # 2. Check Liquidation, Stop-Loss & Take-Profit on Open Margin Positions
            for bsym, pos in list(w["positions"].items()):
                tk = tickers.get(bsym)
                if not tk:
                    continue
                live_p = tk["price"]
                side = pos.get("side", "LONG")
                liq_p = float(pos.get("liquidation_price") or 0.0)
                sl = pos.get("stop_loss")
                tp = pos.get("take_profit")

                # Liquidation Check (Priority 1)
                if side == "LONG" and liq_p > 0 and live_p <= liq_p:
                    _close_margin_position_locked(w, bsym, liq_p, close_pct=100.0, reason="⚠️ LIQUIDATION TRIGGERED @ %s" % predict.fmt_p(liq_p))
                    changed = True
                    continue
                elif side == "SHORT" and liq_p > 0 and live_p >= liq_p:
                    _close_margin_position_locked(w, bsym, liq_p, close_pct=100.0, reason="⚠️ LIQUIDATION TRIGGERED @ %s" % predict.fmt_p(liq_p))
                    changed = True
                    continue

                # Stop-Loss & Take-Profit Check
                if side == "LONG":
                    if sl and live_p <= float(sl):
                        _close_margin_position_locked(w, bsym, live_p, close_pct=100.0, reason="STOP-LOSS Triggered @ %s" % predict.fmt_p(live_p))
                        changed = True
                    elif tp and live_p >= float(tp):
                        _close_margin_position_locked(w, bsym, live_p, close_pct=100.0, reason="TAKE-PROFIT Hit @ %s" % predict.fmt_p(live_p))
                        changed = True
                else:  # SHORT
                    if sl and live_p >= float(sl):
                        _close_margin_position_locked(w, bsym, live_p, close_pct=100.0, reason="STOP-LOSS Triggered @ %s" % predict.fmt_p(live_p))
                        changed = True
                    elif tp and live_p <= float(tp):
                        _close_margin_position_locked(w, bsym, live_p, close_pct=100.0, reason="TAKE-PROFIT Hit @ %s" % predict.fmt_p(live_p))
                        changed = True

            if changed:
                save_wallet(w)

        # 3. Compute live mark-to-market equity & dual currency values
        positions_out = []
        total_margin_locked = 0.0
        total_unrealized_pnl = 0.0
        total_notional = 0.0

        for bsym, pos in w["positions"].items():
            tk = tickers.get(bsym) or {"price": pos["avg_entry"], "change_24h_pct": 0.0}
            live_p = float(tk["price"])
            qty = float(pos["qty"])
            side = pos.get("side", "LONG")
            leverage = float(pos.get("leverage", 3.0))
            margin_locked = float(pos.get("margin_locked_usdt") or pos.get("invested_usdt", 0.0))
            notional = qty * live_p

            if side == "LONG":
                upnl = (live_p - float(pos["avg_entry"])) * qty
            else:
                upnl = (float(pos["avg_entry"]) - live_p) * qty

            upnl_pct = (upnl / margin_locked * 100.0) if margin_locked > 0 else 0.0
            total_margin_locked += margin_locked
            total_unrealized_pnl += upnl
            total_notional += notional

            liq_p = float(pos.get("liquidation_price") or 0.0)
            if liq_p > 0 and live_p > 0:
                liq_dist_pct = abs(live_p - liq_p) / live_p * 100.0
            else:
                liq_dist_pct = 0.0

            is_positive = upnl > 0.0
            is_negative = upnl < 0.0

            # R-Multiple and setup
            sl = pos.get("stop_loss")
            stop_dist = abs(float(pos["avg_entry"]) - float(sl)) if sl else None
            r_mult = round(upnl / (qty * stop_dist), 2) if (stop_dist and stop_dist > 0) else None

            positions_out.append({
                "symbol": bsym,
                "side": side,
                "leverage": leverage,
                "qty": qty,
                "avg_entry": float(pos["avg_entry"]),
                "live_price": live_p,
                "liquidation_price": liq_p,
                "liquidation_dist_pct": round(liq_dist_pct, 2),
                "notional_usdt": round(notional, 2),
                "notional_bnb": round(notional / bnb_price, 4),
                "margin_locked_usdt": round(margin_locked, 2),
                "margin_locked_bnb": round(margin_locked / bnb_price, 4),
                "invested_usdt": round(margin_locked, 2),
                "unrealized_pnl_usdt": round(upnl, 2),
                "unrealized_pnl_bnb": round(upnl / bnb_price, 4),
                "unrealized_pnl_pct": round(upnl_pct, 2),
                "r_multiple": r_mult,
                "setup": pos.get("setup", "Institutional Isolated Margin"),
                "is_positive": is_positive,
                "is_negative": is_negative,
                "stop_loss": pos.get("stop_loss"),
                "take_profit": pos.get("take_profit"),
                "opened_at": pos.get("opened_at"),
            })

        # Process active delta-neutral arbitrage pairs
        arbitrage_out = []
        total_arb_capital = 0.0
        total_arb_accrued_funding = 0.0
        total_arb_basis_pnl = 0.0
        total_arb_net_pnl = 0.0
        now_time = time.time()
        futures_indices = fetch_live_futures_premium_index()
        f_map = {item.get("symbol"): item for item in futures_indices if item.get("symbol")}

        for pair_id, pair in list(w.get("arbitrage_pairs", {}).items()):
            sym = pair["symbol"]
            spot_tk = tickers.get(sym) or {"price": pair["spot_leg"]["price"]}
            live_spot = float(spot_tk["price"])
            f_entry = f_map.get(sym) or f_map.get("1000" + sym) or {}
            live_perp = float(f_entry.get("markPrice") or pair["perp_leg"]["mark_price"])
            cur_fr = float(f_entry.get("lastFundingRate") or pair["metrics"]["funding_rate_8h"])

            # Elapsed time since last accrual check
            last_ts = float(pair.get("last_accrual_ts", now_time))
            elapsed = max(0.0, now_time - last_ts)
            if sim_running and elapsed >= 1.0:
                perp_notional = float(pair["perp_leg"]["notional_usdt"])
                dt_funding = perp_notional * abs(cur_fr) * (elapsed / 28800.0)
                pair["accrued_funding_usdt"] = round(float(pair.get("accrued_funding_usdt", 0.0)) + dt_funding, 6)
                pair["last_accrual_ts"] = now_time

            pnl_info = arbitrage.compute_pair_realtime_pnl(
                pair,
                current_spot_price=live_spot,
                current_perp_mark=live_perp,
                elapsed_seconds=0.0,
                current_funding_rate=cur_fr,
            ) if arbitrage else {
                "basis_pnl_usdt": 0.0,
                "accrued_funding_usdt": float(pair.get("accrued_funding_usdt", 0.0)),
                "accrued_borrow_cost_usdt": 0.0,
                "total_net_pnl_usdt": float(pair.get("accrued_funding_usdt", 0.0)),
                "roe_pct": 0.0,
                "current_net_delta_usdt": 0.0,
                "spot_pnl_usdt": 0.0,
                "perp_pnl_usdt": 0.0,
            }

            total_cap = float(pair["total_capital_usdt"])
            total_arb_capital += total_cap
            total_arb_accrued_funding += pnl_info["accrued_funding_usdt"]
            total_arb_basis_pnl += pnl_info["basis_pnl_usdt"]
            total_arb_net_pnl += pnl_info["total_net_pnl_usdt"]

            arbitrage_out.append({
                "pair_id": pair_id,
                "symbol": sym,
                "strategy": pair["strategy"],
                "total_capital_usdt": total_cap,
                "spot_leg": dict(pair["spot_leg"], live_price=live_spot, pnl_usdt=pnl_info["spot_pnl_usdt"]),
                "perp_leg": dict(pair["perp_leg"], live_mark=live_perp, pnl_usdt=pnl_info["perp_pnl_usdt"]),
                "delta": dict(pair["delta"], current_net_delta_usdt=pnl_info["current_net_delta_usdt"]),
                "metrics": pair["metrics"],
                "live_metrics": {
                    "basis_pnl_usdt": pnl_info["basis_pnl_usdt"],
                    "accrued_funding_usdt": pnl_info["accrued_funding_usdt"],
                    "accrued_borrow_cost_usdt": pnl_info["accrued_borrow_cost_usdt"],
                    "total_net_pnl_usdt": pnl_info["total_net_pnl_usdt"],
                    "roe_pct": pnl_info["roe_pct"],
                },
                "opened_at": pair.get("opened_at"),
            })

        total_equity_usdt = w["cash_usdt"] + total_margin_locked + total_unrealized_pnl + total_arb_capital + total_arb_net_pnl
        total_equity_bnb = round(total_equity_usdt / bnb_price, 4)
        cash_bnb = round(w["cash_usdt"] / bnb_price, 4)
        total_return_usdt = total_equity_usdt - w["starting_cash"]
        total_return_pct = (total_return_usdt / w["starting_cash"] * 100.0) if w["starting_cash"] > 0 else 0.0
        total_return_bnb = round(total_return_usdt / bnb_price, 4)
        unrealized_pnl_bnb = round(total_unrealized_pnl / bnb_price, 4)
        realized_pnl_bnb = round(float(w.get("realized_pnl_usdt", 0.0)) / bnb_price, 4)

        tot_closed = int(w.get("total_trades", 0))
        win_rate = round(100.0 * int(w.get("winning_trades", 0)) / tot_closed, 1) if tot_closed > 0 else 0.0

        watchlist_out = []
        for s in DEFAULT_WATCHLIST:
            if s in tickers:
                item = dict(tickers[s])
                item["price_bnb"] = round(item["price"] / bnb_price, 6)
                watchlist_out.append(item)

        overall_positive = total_unrealized_pnl > 0.0
        overall_negative = total_unrealized_pnl < 0.0

        return {
            "as_of": now_iso(),
            "simulation_running": sim_running,
            "bnb_price": bnb_price,
            "bnb_change_24h_pct": bnb_change_24h_pct,
            "starting_cash": round(w["starting_cash"], 2),
            "starting_cash_bnb": round(w["starting_cash"] / bnb_price, 4),
            "cash_usdt": round(w["cash_usdt"], 2),
            "cash_bnb": cash_bnb,
            "margin_locked_usdt": round(total_margin_locked, 2),
            "margin_locked_bnb": round(total_margin_locked / bnb_price, 4),
            "notional_total_usdt": round(total_notional, 2),
            "notional_total_bnb": round(total_notional / bnb_price, 4),
            "total_equity_usdt": round(total_equity_usdt, 2),
            "total_equity_bnb": total_equity_bnb,
            "unrealized_pnl_usdt": round(total_unrealized_pnl, 2),
            "unrealized_pnl_bnb": unrealized_pnl_bnb,
            "realized_pnl_usdt": round(w.get("realized_pnl_usdt", 0.0), 2),
            "realized_pnl_bnb": realized_pnl_bnb,
            "total_return_usdt": round(total_return_usdt, 2),
            "total_return_bnb": total_return_bnb,
            "total_return_pct": round(total_return_pct, 2),
            "overall_positive": overall_positive,
            "overall_negative": overall_negative,
            "total_trades": tot_closed,
            "winning_trades": int(w.get("winning_trades", 0)),
            "losing_trades": int(w.get("losing_trades", 0)),
            "win_rate_pct": win_rate,
            "positions": positions_out,
            "arbitrage_pairs": arbitrage_out,
            "arbitrage_summary": {
                "total_capital_usdt": round(total_arb_capital, 2),
                "total_accrued_funding_usdt": round(total_arb_accrued_funding, 4),
                "total_basis_pnl_usdt": round(total_arb_basis_pnl, 4),
                "total_net_pnl_usdt": round(total_arb_net_pnl, 4),
                "active_pairs_count": len(arbitrage_out),
            },
            "open_orders": w["open_orders"],
            "history": w["history"][:40],
            "watchlist": watchlist_out,
            "circuit_breakers": sor.evaluate_circuit_breakers(w) if sor else {"status": "CLEAR", "allowed_to_trade": True},
            "ws_stats": dict(WS_STATS),
        }


def place_sim_order(payload):
    """Place a Market or Limit Buy/Sell/Margin paper order."""
    bsym = predict.to_binance(payload.get("symbol") or "BNBUSDT")
    raw_side = str(payload.get("side") or "BUY").upper()
    order_type = str(payload.get("type") or "MARKET").upper()
    leverage = max(1.0, min(10.0, float(payload.get("leverage") or 3.0)))
    usdt_amount = float(payload.get("usdt_amount") or 0.0)
    is_margin_amount = bool(payload.get("is_margin_amount"))
    is_close = bool(payload.get("is_close") or raw_side in ("CLOSE", "FLATTEN"))
    sell_pct = float(payload.get("sell_pct") or 100.0)
    limit_price = float(payload.get("limit_price") or 0.0)
    stop_loss = float(payload["stop_loss"]) if payload.get("stop_loss") not in (None, "", 0, "0") else None
    take_profit = float(payload["take_profit"]) if payload.get("take_profit") not in (None, "", 0, "0") else None

    tickers = fetch_live_tickers([bsym])
    tk = tickers.get(bsym)
    if not tk or tk["price"] <= 0:
        raise ValueError("Could not fetch live Binance price for %s." % bsym)
    live_p = tk["price"]

    if is_close:
        with WALLET_LOCK:
            w = load_wallet()
            rec = _close_margin_position_locked(w, bsym, live_p, close_pct=sell_pct, reason="Manual Market Close (%d%%)" % int(sell_pct))
            save_wallet(w)
        return {"status": "Closed %d%% of %s" % (int(sell_pct), bsym), "trade": rec}

    # Phase 4 Institutional Circuit Breakers Guard
    if sor:
        with WALLET_LOCK:
            w_snap = load_wallet()
        cb = sor.evaluate_circuit_breakers(w_snap)
        if not cb.get("allowed_to_trade", True):
            reasons = "; ".join(cb.get("reasons", ["Circuit breaker active"]))
            raise ValueError("CIRCUIT BREAKER BLOCKED ORDER: %s" % reasons)

    side = "LONG" if raw_side in ("BUY", "LONG") else "SHORT"

    if is_margin_amount and usdt_amount > 0:
        notional_usdt = usdt_amount * leverage
    else:
        notional_usdt = usdt_amount

    with WALLET_LOCK:
        w = load_wallet()
        if order_type == "LIMIT":
            if limit_price <= 0:
                raise ValueError("Limit order requires a valid limit_price.")
            if (side == "LONG" and live_p <= limit_price) or (side == "SHORT" and live_p >= limit_price):
                order_type = "MARKET"
            else:
                margin_req = notional_usdt / leverage
                if margin_req > w["cash_usdt"]:
                    raise ValueError("Insufficient virtual cash ($%.2f free margin available, needed $%.2f)." % (w["cash_usdt"], margin_req))
                oid = "O%d" % int(time.time() * 1000)
                ord_rec = {
                    "id": oid,
                    "symbol": bsym,
                    "side": side,
                    "type": "LIMIT",
                    "leverage": leverage,
                    "limit_price": limit_price,
                    "usdt_amount": round(notional_usdt, 2),
                    "margin_usdt": round(margin_req, 2),
                    "stop_loss": stop_loss,
                    "take_profit": take_profit,
                    "created_at": now_iso(),
                }
                w["open_orders"].insert(0, ord_rec)
                save_wallet(w)
                return {"status": "Resting Limit Order Placed (%s %dx @ %s)" % (side, int(leverage), predict.fmt_p(limit_price)), "order": ord_rec}

        # Market execution (uses live ask for LONG, live bid for SHORT)
        exec_p = tk["ask"] if (side == "LONG" and tk.get("ask", 0) > 0) else (
            tk["bid"] if (side == "SHORT" and tk.get("bid", 0) > 0) else live_p
        )
        rec = _open_margin_position_locked(
            w, bsym, side, exec_p,
            notional_usdt=notional_usdt,
            leverage=leverage,
            stop_loss=stop_loss,
            take_profit=take_profit,
            reason="Market %s %dx" % (side, int(leverage))
        )
        save_wallet(w)
        return {"status": "Filled Immediately (%s %dx @ %s)" % (side, int(leverage), predict.fmt_p(exec_p)), "trade": rec}


def update_position_brackets(payload):
    bsym = predict.to_binance(payload.get("symbol") or "")
    sl = float(payload["stop_loss"]) if payload.get("stop_loss") not in (None, "", 0, "0") else None
    tp = float(payload["take_profit"]) if payload.get("take_profit") not in (None, "", 0, "0") else None
    with WALLET_LOCK:
        w = load_wallet()
        pos = w["positions"].get(bsym)
        if not pos:
            raise ValueError("No open position for %s." % bsym)
        pos["stop_loss"] = sl
        pos["take_profit"] = tp
        save_wallet(w)
        return {"status": "Updated SL/TP for %s" % bsym, "position": pos}


def cancel_open_order(order_id):
    with WALLET_LOCK:
        w = load_wallet()
        before = len(w["open_orders"])
        w["open_orders"] = [o for o in w["open_orders"] if o.get("id") != order_id]
        save_wallet(w)
        return {"status": "Cancelled" if len(w["open_orders"]) < before else "Not Found"}


def reset_sim_wallet(starting_cash=10000.0):
    with WALLET_LOCK:
        w = default_wallet(starting_cash)
        save_wallet(w)
        return {"status": "Reset wallet to $%.2f USDT" % float(starting_cash)}


def get_cached_forecasts():
    """Return latest forecasts from trader/forecast.json + memory cache."""
    out = dict(FORECAST_CACHE)
    if os.path.exists(FORECAST_PATH):
        try:
            with open(FORECAST_PATH, "r", encoding="utf-8") as fh:
                doc = json.load(fh)
            if isinstance(doc, list):
                for item in doc:
                    if isinstance(item, dict) and "symbol" in item:
                        out[item["symbol"]] = item
            elif isinstance(doc, dict) and "symbol" in doc:
                out[doc["symbol"]] = doc
        except Exception:
            pass
    return out


class AppHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return  # keep console clean

    def _send_json(self, doc, status=200):
        body = json.dumps(doc).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, content_type):
        with open(path, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        p = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if p in ("/", "/index.html"):
            return self._send_file(os.path.join(APP_DIR, "index.html"), "text/html; charset=utf-8")
        if p == "/manifest.json":
            return self._send_file(os.path.join(APP_DIR, "manifest.json"), "application/manifest+json; charset=utf-8")

        if p == "/api/ws/status":
            return self._send_json(WS_STATS)

        if p == "/api/state":
            try:
                extra_sym = (qs.get("symbol") or [None])[0]
                state = check_triggers_and_enrich(extra_sym)
                state["lan_ip"] = get_lan_ip()
                return self._send_json(state)
            except Exception as e:
                return self._send_json({"error": str(e)}, status=500)

        if p == "/api/klines":
            sym = predict.to_binance((qs.get("symbol") or ["BNBUSDT"])[0])
            interval = (qs.get("interval") or ["15m"])[0]
            limit = min(200, int((qs.get("limit") or ["90"])[0]))
            try:
                raw = predict.spot_get("/api/v3/klines?symbol=%s&interval=%s&limit=%d" % (sym, interval, limit))
                bars = [
                    {
                        "t": int(k[0]) // 1000,
                        "o": float(k[1]),
                        "h": float(k[2]),
                        "l": float(k[3]),
                        "c": float(k[4]),
                        "v": float(k[7]),
                        "buy_v": float(k[10]),
                    }
                    for k in raw
                ]
                tk = fetch_live_tickers([sym]).get(sym)
                return self._send_json({"symbol": sym, "interval": interval, "ticker": tk, "bars": bars})
            except Exception as e:
                return self._send_json({"error": str(e)}, status=400)

        if p == "/api/forecast":
            sym = predict.to_binance((qs.get("symbol") or ["BNBUSDT"])[0])
            refresh = (qs.get("refresh") or ["0"])[0] == "1"
            cached = get_cached_forecasts()
            if not refresh and sym in cached:
                return self._send_json(cached[sym])
            try:
                res = predict.forecast_symbol(sym)
                FORECAST_CACHE[sym] = res
                return self._send_json(res)
            except Exception as e:
                return self._send_json({"error": str(e)}, status=400)

        if p == "/api/picks":
            cached = get_cached_forecasts()
            picks = sorted(cached.values(), key=lambda x: -float(x.get("composite_score") or 0.0))
            return self._send_json({"picks": picks})

        if p == "/api/arbitrage/opportunities":
            try:
                min_apy = float((qs.get("min_apy") or ["5.0"])[0])
                spots = {s: t["price"] for s, t in fetch_live_tickers().items()}
                prems = fetch_live_futures_premium_index()
                scanned = arbitrage.scan_funding_arbitrage(
                    min_apy=min_apy,
                    spot_prices_cache=spots,
                    premium_indices_cache=prems,
                ) if arbitrage else {"opportunities": []}
                return self._send_json(scanned)
            except Exception as e:
                return self._send_json({"error": str(e)}, status=500)

        if p == "/api/arbitrage/positions":
            try:
                state = check_triggers_and_enrich()
                return self._send_json({
                    "pairs": state.get("arbitrage_pairs", []),
                    "summary": state.get("arbitrage_summary", {}),
                })
            except Exception as e:
                return self._send_json({"error": str(e)}, status=500)

        if p == "/api/microstructure":
            try:
                sym = predict.to_binance((qs.get("symbol") or ["BNBUSDT"])[0])
                raw_1h = predict.spot_get("/api/v3/klines?symbol=%s&interval=1h&limit=100" % sym)
                raw_15m = predict.spot_get("/api/v3/klines?symbol=%s&interval=15m&limit=100" % sym)
                price = float(raw_1h[-1][4]) if raw_1h else 0.0

                vpin_data = microstructure.compute_vpin(raw_15m or raw_1h, num_buckets=20) if microstructure else {}
                wyckoff_data = microstructure.detect_wyckoff_cvd_divergence(raw_1h, klines_15m=raw_15m, current_price=price) if microstructure else {}

                # 1h and baseline ATR
                atr_1h = predict.atr_14(raw_1h, 14) if (raw_1h and len(raw_1h) >= 15) else (price * 0.01)
                atr_base = predict.atr_14(raw_1h, 30) if (raw_1h and len(raw_1h) >= 35) else atr_1h

                # 4h BB Squeeze check
                raw_4h = predict.spot_get("/api/v3/klines?symbol=%s&interval=4h&limit=30" % sym)
                is_squeeze = False
                bb_width = 5.0
                if raw_4h and len(raw_4h) >= 20:
                    c4 = [float(k[4]) for k in raw_4h[-20:]]
                    m20 = statistics.mean(c4)
                    s20 = statistics.pstdev(c4)
                    a4 = predict.atr_14(raw_4h, 20)
                    bb_up, bb_lo = m20 + 2.0 * s20, m20 - 2.0 * s20
                    kc_up, kc_lo = m20 + 1.5 * a4, m20 - 1.5 * a4
                    bb_width = round((bb_up - bb_lo) / max(m20, 1e-6) * 100.0, 2)
                    is_squeeze = (bb_up <= kc_up and bb_lo >= kc_lo)

                adaptive_sizing = microstructure.compute_adaptive_kelly(
                    win_prob=0.55,
                    reward_risk=2.0,
                    atr_1h=atr_1h,
                    atr_baseline=atr_base,
                    bb_width_pct=bb_width,
                    is_ttm_squeeze=is_squeeze,
                    vpin=vpin_data.get("vpin", 0.35),
                    bias="LONG",
                    capital_usdt=10000.0,
                    max_loss_cap=150.0,
                ) if microstructure else {}

                return self._send_json({
                    "symbol": sym,
                    "price": price,
                    "vpin": vpin_data,
                    "wyckoff": wyckoff_data,
                    "adaptive_kelly": adaptive_sizing,
                    "bb_width_pct": bb_width,
                    "is_ttm_squeeze": is_squeeze,
                })
            except Exception as e:
                return self._send_json({"error": str(e)}, status=500)

        if p == "/api/sor/quote":
            try:
                sym = predict.to_binance((qs.get("symbol") or ["BNBUSDT"])[0])
                side = (qs.get("side") or ["BUY"])[0].upper()
                notional = float((qs.get("notional") or ["1000.0"])[0])
                is_limit = (qs.get("is_limit") or ["0"])[0] == "1"

                venue_quotes = sor.fetch_multivenue_quotes(sym) if sor else {}
                routing = sor.smart_route_order(sym, side, notional, venue_quotes, is_limit=is_limit) if sor else {}
                return self._send_json(routing)
            except Exception as e:
                return self._send_json({"error": str(e)}, status=500)

        if p == "/api/deribit/options":
            try:
                curr = (qs.get("currency") or ["BTC"])[0].upper().replace("USDT", "")
                spot = float((qs.get("spot_price") or ["0.0"])[0])
                if spot <= 0:
                    tickers = fetch_live_tickers([curr + "USDT"])
                    spot = float((tickers.get(curr + "USDT") or {}).get("price", 0.0))
                analytics = sor.fetch_deribit_options_analytics(curr, spot_price=spot if spot > 0 else None) if sor else {}
                return self._send_json(analytics)
            except Exception as e:
                return self._send_json({"error": str(e)}, status=500)

        if p == "/api/risk/circuit-breakers":
            try:
                with WALLET_LOCK:
                    w = load_wallet()
                cb = sor.evaluate_circuit_breakers(w) if sor else {"status": "CLEAR", "allowed_to_trade": True}
                return self._send_json(cb)
            except Exception as e:
                return self._send_json({"error": str(e)}, status=500)

        return self._send_json({"error": "Not found"}, status=404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        p = parsed.path
        length = int(self.headers.get("Content-Length") or 0)
        raw_body = self.rfile.read(length).decode("utf-8", "replace") if length > 0 else "{}"
        try:
            payload = json.loads(raw_body) if raw_body.strip() else {}
        except Exception:
            payload = {}

        try:
            if p == "/api/order":
                res = place_sim_order(payload)
                return self._send_json({"ok": True, "result": res, "state": check_triggers_and_enrich()})
            if p == "/api/simulation/start":
                with WALLET_LOCK:
                    w = load_wallet()
                    w["simulation_running"] = True
                    save_wallet(w)
                return self._send_json({"ok": True, "status": "Simulation Running", "state": check_triggers_and_enrich()})
            if p == "/api/simulation/stop":
                with WALLET_LOCK:
                    w = load_wallet()
                    w["simulation_running"] = False
                    save_wallet(w)
                return self._send_json({"ok": True, "status": "Simulation Paused", "state": check_triggers_and_enrich()})
            if p == "/api/simulation/toggle":
                with WALLET_LOCK:
                    w = load_wallet()
                    w["simulation_running"] = not w.get("simulation_running", True)
                    save_wallet(w)
                return self._send_json({"ok": True, "simulation_running": w["simulation_running"], "state": check_triggers_and_enrich()})
            if p == "/api/position/close":
                bsym = predict.to_binance(payload.get("symbol") or "")
                pct = float(payload.get("pct") or 100.0)
                tickers = fetch_live_tickers([bsym])
                tk = tickers.get(bsym) or {}
                live_p = float(tk.get("price") or 0.0)
                if live_p <= 0:
                    raise ValueError("Cannot get live price for %s" % bsym)
                with WALLET_LOCK:
                    w = load_wallet()
                    rec = _close_margin_position_locked(w, bsym, live_p, close_pct=pct, reason="Manual Market Close (%d%%)" % int(pct))
                    save_wallet(w)
                return self._send_json({"ok": True, "result": rec, "state": check_triggers_and_enrich()})
            if p == "/api/position/reverse":
                bsym = predict.to_binance(payload.get("symbol") or "")
                tickers = fetch_live_tickers([bsym])
                tk = tickers.get(bsym) or {}
                live_p = float(tk.get("price") or 0.0)
                if live_p <= 0:
                    raise ValueError("Cannot get live price for %s" % bsym)
                with WALLET_LOCK:
                    w = load_wallet()
                    pos = w["positions"].get(bsym)
                    if not pos:
                        raise ValueError("No open position for %s to reverse" % bsym)
                    old_side = pos.get("side", "LONG")
                    new_side = "SHORT" if old_side == "LONG" else "LONG"
                    lev = float(pos.get("leverage", 3.0))
                    notional = float(pos.get("notional_usdt") or (float(pos["qty"]) * live_p))
                    _close_margin_position_locked(w, bsym, live_p, close_pct=100.0, reason="Reverse Close %s" % old_side)
                    rec = _open_margin_position_locked(w, bsym, new_side, live_p, notional_usdt=notional, leverage=lev, reason="Reversed to %s %dx" % (new_side, int(lev)))
                    save_wallet(w)
                return self._send_json({"ok": True, "result": rec, "state": check_triggers_and_enrich()})
            if p == "/api/brackets":
                res = update_position_brackets(payload)
                return self._send_json({"ok": True, "result": res, "state": check_triggers_and_enrich()})
            if p == "/api/cancel":
                res = cancel_open_order(payload.get("id"))
                return self._send_json({"ok": True, "result": res, "state": check_triggers_and_enrich()})
            if p == "/api/arbitrage/execute":
                sym = payload.get("symbol") or "BNBUSDT"
                strat = payload.get("strategy") or "CASH_AND_CARRY"
                cap = float(payload.get("capital_usdt") or 2000.0)
                with WALLET_LOCK:
                    w = load_wallet()
                    res = _open_arbitrage_pair_locked(w, symbol=sym, strategy=strat, capital_usdt=cap)
                    save_wallet(w)
                return self._send_json({"ok": True, "result": res, "state": check_triggers_and_enrich()})
            if p == "/api/arbitrage/close":
                pair_id = payload.get("pair_id")
                if not pair_id:
                    raise ValueError("Must provide pair_id to unwind.")
                with WALLET_LOCK:
                    w = load_wallet()
                    res = _close_arbitrage_pair_locked(w, pair_id=pair_id, reason="Manual Unwind")
                    save_wallet(w)
                return self._send_json({"ok": True, "result": res, "state": check_triggers_and_enrich()})
            if p == "/api/reset":
                cash = float(payload.get("starting_cash") or 10000.0)
                res = reset_sim_wallet(cash)
                return self._send_json({"ok": True, "result": res, "state": check_triggers_and_enrich()})
        except Exception as e:
            return self._send_json({"ok": False, "error": str(e)}, status=400)

        return self._send_json({"ok": False, "error": "Unknown endpoint"}, status=404)



async def _async_binance_ws_loop():
    global PRICE_CACHE, PRICE_CACHE_TS, WS_STATS
    url = "wss://stream.binance.com:9443/ws/!miniTicker@arr"
    last_rate_calc = time.time()
    tick_counter = 0

    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(url, timeout=12.0) as ws:
                    WS_STATS["connected"] = True
                    WS_STATS["stream"] = url
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            now = time.time()
                            PRICE_CACHE_TS = now
                            WS_STATS["last_tick_iso"] = now_iso()
                            n_ticks = len(data) if isinstance(data, list) else 1
                            WS_STATS["ticks_received"] += n_ticks
                            tick_counter += n_ticks

                            dt = now - last_rate_calc
                            if dt >= 1.0:
                                WS_STATS["rate_per_sec"] = round(tick_counter / dt, 1)
                                tick_counter = 0
                                last_rate_calc = now

                            trigger_check_needed = False
                            with WALLET_LOCK:
                                w = load_wallet()
                                active_symbols = set(w["positions"].keys()) | {o["symbol"] for o in w["open_orders"]}
                                sim_on = w.get("simulation_running", True)

                            if isinstance(data, list):
                                for item in data:
                                    s = item.get("s")
                                    if not s:
                                        continue
                                    c = float(item.get("c") or 0.0)
                                    o = float(item.get("o") or 0.0)
                                    chg = ((c - o) / o * 100.0) if o > 0 else 0.0
                                    PRICE_CACHE[s] = {
                                        "symbol": s,
                                        "price": c,
                                        "change_24h_pct": round(chg, 2),
                                        "high_24h": float(item.get("h") or 0.0),
                                        "low_24h": float(item.get("l") or 0.0),
                                        "volume_24h_usd": float(item.get("q") or 0.0),
                                        "bid": c,
                                        "ask": c,
                                        "source": "websocket_clob_live",
                                    }
                                    if sim_on and s in active_symbols:
                                        trigger_check_needed = True

                            if trigger_check_needed:
                                check_triggers_and_enrich()

                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            WS_STATS["connected"] = False
                            break
        except Exception:
            WS_STATS["connected"] = False
            await asyncio.sleep(2.0)


def start_ws_feeder_thread():
    if not HAS_AIOHTTP:
        return
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_async_binance_ws_loop())


def background_trigger_worker():
    """Continuously monitor open positions & resting limit orders every 2.5 seconds when simulation is running."""
    while True:
        try:
            with WALLET_LOCK:
                w = load_wallet()
                sim_on = w.get("simulation_running", True)
            if sim_on:
                check_triggers_and_enrich()
        except Exception:
            pass
        time.sleep(2.5)


def main():
    ap = argparse.ArgumentParser(description="Local iPhone 13 Crypto Paper-Trading Simulator Server")
    ap.add_argument("--host", default="0.0.0.0", help="Bind host (0.0.0.0 allows local Wi-Fi iPhone access)")
    ap.add_argument("--port", type=int, default=8765, help="Port (default 8765)")
    args = ap.parse_args()

    get_cached_forecasts()

    t = threading.Thread(target=background_trigger_worker, daemon=True)
    t.start()

    if HAS_AIOHTTP:
        ws_t = threading.Thread(target=start_ws_feeder_thread, daemon=True)
        ws_t.start()

    lan_ip = get_lan_ip()
    srv = ThreadingHTTPServer((args.host, args.port), AppHandler)
    print("=" * 72, flush=True)
    print(" GOD-LEVEL CRYPTO MARGIN TRADER SIMULATOR (BINANCE VALUES) IS LIVE!", flush=True)
    print("=" * 72, flush=True)
    print(" * On this PC         : http://localhost:%d" % args.port, flush=True)
    print(" * On your iPhone 13  : http://%s:%d  (same Wi-Fi)" % (lan_ip, args.port), flush=True)
    print("=" * 72, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

_exec_margin_open_locked = _open_margin_position_locked
_exec_margin_close_locked = _close_margin_position_locked
_exec_arbitrage_open_locked = _open_arbitrage_pair_locked
_exec_arbitrage_close_locked = _close_arbitrage_pair_locked
