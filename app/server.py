#!/usr/bin/env python3
"""Local iPhone 13 Crypto Paper-Trading Simulator & God-Level Analyst Server.
Runs strictly on your local network (LAN / localhost) with made-up virtual USDT
and real-time live Binance Spot & Perpetual Futures data. Zero external dependencies.

Usage:
    python app/server.py --port 8080
"""

import argparse
import datetime
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.dirname(os.path.abspath(__file__))
ANALYST_DIR = os.path.join(ROOT_DIR, "trader-analyst")
if ANALYST_DIR not in sys.path:
    sys.path.insert(0, ANALYST_DIR)

import predict  # noqa: E402

WALLET_PATH = os.path.join(ROOT_DIR, "trader", "sim_wallet.json")
FORECAST_PATH = os.path.join(ROOT_DIR, "trader", "forecast.json")
WALLET_LOCK = threading.Lock()
PRICE_CACHE = {}
PRICE_CACHE_TS = 0.0
FORECAST_CACHE = {}

DEFAULT_WATCHLIST = [
    "BNBUSDT", "BTCUSDT", "ETHUSDT", "SOLUSDT",
    "PHAUSDT", "PEPEUSDT", "ENAUSDT", "ERAUSDT",
    "DOGEUSDT", "SUIUSDT", "XRPUSDT", "ADAUSDT",
]

FEE_RATE = 0.001  # 0.10% standard Binance Spot taker fee simulation


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


def default_wallet(starting_cash=10000.0):
    return {
        "starting_cash": round(float(starting_cash), 2),
        "cash_usdt": round(float(starting_cash), 2),
        "realized_pnl_usdt": 0.0,
        "total_trades": 0,
        "winning_trades": 0,
        "losing_trades": 0,
        "positions": {},       # symbol -> {symbol, qty, avg_entry, invested_usdt, stop_loss, take_profit, opened_at}
        "open_orders": [],     # [{id, symbol, side, type, limit_price, usdt_amount, qty, stop_loss, take_profit, created_at}]
        "history": [],         # [{id, time, symbol, action, qty, price, usdt_total, fee_usdt, pnl_usdt, pnl_pct, reason}]
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
        w.setdefault("open_orders", [])
        w.setdefault("history", [])
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


def _exec_buy_locked(w, bsym, exec_price, usdt_amount, stop_loss=None, take_profit=None, reason="Market Buy"):
    usdt_amount = float(usdt_amount)
    if usdt_amount <= 0:
        raise ValueError("Order amount must be greater than $0.")
    if usdt_amount > w["cash_usdt"] + 1e-6:
        raise ValueError("Insufficient virtual cash ($%.2f available, tried $%.2f)." % (w["cash_usdt"], usdt_amount))
    usdt_amount = min(usdt_amount, w["cash_usdt"])
    fee_usdt = round(usdt_amount * FEE_RATE, 4)
    net_usdt = usdt_amount - fee_usdt
    qty = net_usdt / exec_price

    pos = w["positions"].get(bsym)
    if pos:
        old_qty = float(pos["qty"])
        old_inv = float(pos["invested_usdt"])
        new_qty = old_qty + qty
        new_inv = old_inv + usdt_amount
        pos["qty"] = new_qty
        pos["invested_usdt"] = round(new_inv, 4)
        pos["avg_entry"] = new_inv / new_qty
        if stop_loss is not None and float(stop_loss) > 0:
            pos["stop_loss"] = float(stop_loss)
        if take_profit is not None and float(take_profit) > 0:
            pos["take_profit"] = float(take_profit)
    else:
        w["positions"][bsym] = {
            "symbol": bsym,
            "qty": qty,
            "avg_entry": exec_price,
            "invested_usdt": round(usdt_amount, 4),
            "stop_loss": float(stop_loss) if stop_loss and float(stop_loss) > 0 else None,
            "take_profit": float(take_profit) if take_profit and float(take_profit) > 0 else None,
            "opened_at": now_iso(),
        }

    w["cash_usdt"] = round(max(0.0, w["cash_usdt"] - usdt_amount), 4)
    trade_rec = {
        "id": "T%d" % int(time.time() * 1000),
        "time": now_iso(),
        "symbol": bsym,
        "action": "BUY",
        "qty": qty,
        "price": exec_price,
        "usdt_total": round(usdt_amount, 2),
        "fee_usdt": fee_usdt,
        "pnl_usdt": None,
        "pnl_pct": None,
        "reason": reason,
    }
    w["history"].insert(0, trade_rec)
    w["history"] = w["history"][:100]
    return trade_rec


def _exec_sell_locked(w, bsym, exec_price, sell_qty=None, sell_pct=None, reason="Market Sell"):
    pos = w["positions"].get(bsym)
    if not pos or float(pos["qty"]) <= 0:
        raise ValueError("No open position in %s to sell." % bsym)
    total_qty = float(pos["qty"])
    if sell_pct is not None and float(sell_pct) > 0:
        qty = total_qty * min(1.0, float(sell_pct) / 100.0)
    elif sell_qty is not None and float(sell_qty) > 0:
        qty = min(total_qty, float(sell_qty))
    else:
        qty = total_qty

    frac = qty / total_qty if total_qty > 0 else 1.0
    cost_basis = float(pos["invested_usdt"]) * frac
    gross_proceeds = qty * exec_price
    fee_usdt = round(gross_proceeds * FEE_RATE, 4)
    net_proceeds = gross_proceeds - fee_usdt
    pnl_usdt = round(net_proceeds - cost_basis, 4)
    pnl_pct = round((pnl_usdt / cost_basis) * 100.0, 2) if cost_basis > 0 else 0.0

    w["cash_usdt"] = round(w["cash_usdt"] + net_proceeds, 4)
    w["realized_pnl_usdt"] = round(w.get("realized_pnl_usdt", 0.0) + pnl_usdt, 4)
    w["total_trades"] = int(w.get("total_trades", 0)) + 1
    if pnl_usdt >= 0:
        w["winning_trades"] = int(w.get("winning_trades", 0)) + 1
    else:
        w["losing_trades"] = int(w.get("losing_trades", 0)) + 1

    rem_qty = total_qty - qty
    if rem_qty <= total_qty * 1e-5 or rem_qty * exec_price < 0.05:
        del w["positions"][bsym]
    else:
        pos["qty"] = rem_qty
        pos["invested_usdt"] = round(float(pos["invested_usdt"]) - cost_basis, 4)

    trade_rec = {
        "id": "T%d" % int(time.time() * 1000),
        "time": now_iso(),
        "symbol": bsym,
        "action": "SELL",
        "qty": qty,
        "price": exec_price,
        "usdt_total": round(net_proceeds, 2),
        "fee_usdt": fee_usdt,
        "pnl_usdt": round(pnl_usdt, 2),
        "pnl_pct": pnl_pct,
        "reason": reason,
    }
    w["history"].insert(0, trade_rec)
    w["history"] = w["history"][:100]
    return trade_rec


def check_triggers_and_enrich(extra_symbol=None):
    """Evaluate resting Limit Orders + Stop-Loss / Take-Profit triggers against live Binance prices,
    and return the enriched portfolio state."""
    with WALLET_LOCK:
        w = load_wallet()
        if extra_symbol:
            bs = predict.to_binance(extra_symbol)
            if bs and bs not in DEFAULT_WATCHLIST:
                DEFAULT_WATCHLIST.insert(0, bs)
        needed = set(DEFAULT_WATCHLIST) | set(w["positions"].keys()) | {o["symbol"] for o in w["open_orders"]}
    tickers = fetch_live_tickers(needed)

    with WALLET_LOCK:
        w = load_wallet()
        changed = False

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
            side = o["side"].upper()
            if side == "BUY" and live_p <= lim_p:
                try:
                    _exec_buy_locked(
                        w, bsym, lim_p, o["usdt_amount"],
                        stop_loss=o.get("stop_loss"),
                        take_profit=o.get("take_profit"),
                        reason="Limit Buy Filled @ %s" % predict.fmt_p(lim_p),
                    )
                    changed = True
                except Exception:
                    remaining_orders.append(o)
            elif side == "SELL" and live_p >= lim_p:
                try:
                    _exec_sell_locked(
                        w, bsym, lim_p, sell_qty=o.get("qty"), sell_pct=o.get("sell_pct"),
                        reason="Limit Sell Filled @ %s" % predict.fmt_p(lim_p),
                    )
                    changed = True
                except Exception:
                    remaining_orders.append(o)
            else:
                remaining_orders.append(o)
        if len(remaining_orders) != len(w["open_orders"]):
            w["open_orders"] = remaining_orders
            changed = True

        # 2. Check Stop-Loss & Take-Profit on Open Positions
        for bsym, pos in list(w["positions"].items()):
            tk = tickers.get(bsym)
            if not tk:
                continue
            live_p = tk["price"]
            sl = pos.get("stop_loss")
            tp = pos.get("take_profit")
            if sl and live_p <= float(sl):
                _exec_sell_locked(w, bsym, live_p, sell_pct=100.0, reason="STOP-LOSS Triggered @ %s" % predict.fmt_p(live_p))
                changed = True
            elif tp and live_p >= float(tp):
                _exec_sell_locked(w, bsym, live_p, sell_pct=100.0, reason="TAKE-PROFIT Hit @ %s" % predict.fmt_p(live_p))
                changed = True

        if changed:
            save_wallet(w)

        # 3. Compute live mark-to-market equity
        positions_out = []
        total_pos_value = 0.0
        total_unrealized_pnl = 0.0
        for bsym, pos in w["positions"].items():
            tk = tickers.get(bsym) or {"price": pos["avg_entry"], "change_24h_pct": 0.0}
            live_p = float(tk["price"])
            qty = float(pos["qty"])
            mkt_val = qty * live_p
            inv = float(pos["invested_usdt"])
            upnl = mkt_val - inv
            upnl_pct = (upnl / inv * 100.0) if inv > 0 else 0.0
            total_pos_value += mkt_val
            total_unrealized_pnl += upnl
            positions_out.append({
                "symbol": bsym,
                "qty": qty,
                "avg_entry": pos["avg_entry"],
                "live_price": live_p,
                "market_value_usdt": round(mkt_val, 2),
                "invested_usdt": round(inv, 2),
                "unrealized_pnl_usdt": round(upnl, 2),
                "unrealized_pnl_pct": round(upnl_pct, 2),
                "stop_loss": pos.get("stop_loss"),
                "take_profit": pos.get("take_profit"),
                "opened_at": pos.get("opened_at"),
            })

        total_equity = w["cash_usdt"] + total_pos_value
        total_return_usdt = total_equity - w["starting_cash"]
        total_return_pct = (total_return_usdt / w["starting_cash"] * 100.0) if w["starting_cash"] > 0 else 0.0
        tot_closed = int(w.get("total_trades", 0))
        win_rate = round(100.0 * int(w.get("winning_trades", 0)) / tot_closed, 1) if tot_closed > 0 else 0.0

        watchlist_out = [tickers[s] for s in DEFAULT_WATCHLIST if s in tickers]
        return {
            "as_of": now_iso(),
            "starting_cash": round(w["starting_cash"], 2),
            "cash_usdt": round(w["cash_usdt"], 2),
            "positions_value_usdt": round(total_pos_value, 2),
            "total_equity_usdt": round(total_equity, 2),
            "unrealized_pnl_usdt": round(total_unrealized_pnl, 2),
            "realized_pnl_usdt": round(w.get("realized_pnl_usdt", 0.0), 2),
            "total_return_usdt": round(total_return_usdt, 2),
            "total_return_pct": round(total_return_pct, 2),
            "total_trades": tot_closed,
            "winning_trades": int(w.get("winning_trades", 0)),
            "losing_trades": int(w.get("losing_trades", 0)),
            "win_rate_pct": win_rate,
            "positions": positions_out,
            "open_orders": w["open_orders"],
            "history": w["history"][:40],
            "watchlist": watchlist_out,
        }


def place_sim_order(payload):
    """Place a Market or Limit Buy/Sell paper order."""
    bsym = predict.to_binance(payload.get("symbol") or "BNBUSDT")
    side = str(payload.get("side") or "BUY").upper()
    order_type = str(payload.get("type") or "MARKET").upper()
    usdt_amount = float(payload.get("usdt_amount") or 0.0)
    sell_pct = payload.get("sell_pct")
    sell_qty = payload.get("qty")
    limit_price = float(payload.get("limit_price") or 0.0)
    stop_loss = float(payload["stop_loss"]) if payload.get("stop_loss") not in (None, "", 0, "0") else None
    take_profit = float(payload["take_profit"]) if payload.get("take_profit") not in (None, "", 0, "0") else None

    tickers = fetch_live_tickers([bsym])
    tk = tickers.get(bsym)
    if not tk or tk["price"] <= 0:
        raise ValueError("Could not fetch live Binance price for %s." % bsym)
    live_p = tk["price"]

    with WALLET_LOCK:
        w = load_wallet()
        if order_type == "LIMIT":
            if limit_price <= 0:
                raise ValueError("Limit order requires a valid limit_price.")
            # If limit price is already marketable, fill immediately at live price
            if (side == "BUY" and live_p <= limit_price) or (side == "SELL" and live_p >= limit_price):
                order_type = "MARKET"
            else:
                if side == "BUY" and usdt_amount > w["cash_usdt"]:
                    raise ValueError("Insufficient virtual cash ($%.2f available)." % w["cash_usdt"])
                oid = "O%d" % int(time.time() * 1000)
                ord_rec = {
                    "id": oid,
                    "symbol": bsym,
                    "side": side,
                    "type": "LIMIT",
                    "limit_price": limit_price,
                    "usdt_amount": round(usdt_amount, 2),
                    "qty": sell_qty,
                    "sell_pct": sell_pct,
                    "stop_loss": stop_loss,
                    "take_profit": take_profit,
                    "created_at": now_iso(),
                }
                w["open_orders"].insert(0, ord_rec)
                save_wallet(w)
                return {"status": "Resting Limit Order Placed", "order": ord_rec}

        # Market execution (uses live ask for BUY, live bid for SELL)
        exec_p = tk["ask"] if (side == "BUY" and tk.get("ask", 0) > 0) else (
            tk["bid"] if (side == "SELL" and tk.get("bid", 0) > 0) else live_p
        )
        if side == "BUY":
            rec = _exec_buy_locked(w, bsym, exec_p, usdt_amount, stop_loss=stop_loss, take_profit=take_profit, reason="Market Buy")
        elif side == "SELL":
            rec = _exec_sell_locked(w, bsym, exec_p, sell_qty=sell_qty, sell_pct=sell_pct, reason="Market Sell")
        else:
            raise ValueError("side must be BUY or SELL.")
        save_wallet(w)
        return {"status": "Filled Immediately", "trade": rec}


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
            if p == "/api/brackets":
                res = update_position_brackets(payload)
                return self._send_json({"ok": True, "result": res, "state": check_triggers_and_enrich()})
            if p == "/api/cancel":
                res = cancel_open_order(payload.get("id"))
                return self._send_json({"ok": True, "result": res, "state": check_triggers_and_enrich()})
            if p == "/api/reset":
                cash = float(payload.get("starting_cash") or 10000.0)
                res = reset_sim_wallet(cash)
                return self._send_json({"ok": True, "result": res, "state": check_triggers_and_enrich()})
        except Exception as e:
            return self._send_json({"ok": False, "error": str(e)}, status=400)

        return self._send_json({"ok": False, "error": "Unknown endpoint"}, status=404)


def background_trigger_worker():
    """Continuously monitor open positions & resting limit orders every 3 seconds."""
    while True:
        try:
            check_triggers_and_enrich()
        except Exception:
            pass
        time.sleep(3.0)


def main():
    ap = argparse.ArgumentParser(description="Local iPhone 13 Crypto Paper-Trading Simulator Server")
    ap.add_argument("--host", default="0.0.0.0", help="Bind host (0.0.0.0 allows local Wi-Fi iPhone access)")
    ap.add_argument("--port", type=int, default=8765, help="Port (default 8765)")
    args = ap.parse_args()

    # Pre-warm forecasts cache from trader/forecast.json
    get_cached_forecasts()

    t = threading.Thread(target=background_trigger_worker, daemon=True)
    t.start()

    lan_ip = get_lan_ip()
    srv = ThreadingHTTPServer((args.host, args.port), AppHandler)
    print("=" * 72, flush=True)
    print(" GOD-LEVEL CRYPTO PAPER TRADER (iPHONE 13 LOCAL SIMULATOR) IS LIVE!", flush=True)
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
