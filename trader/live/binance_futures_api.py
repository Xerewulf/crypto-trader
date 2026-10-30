#!/usr/bin/env python3
"""Binance USD-M Perpetual Futures REST API Client (Standard Library Only).
Handles authenticated HMAC-SHA256 requests to https://fapi.binance.com.
Used for querying real balances, setting leverage/margin mode, and executing
real futures orders (market, limit, stop-loss, take-profit, flatten).
"""

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request


class BinanceFuturesAPI:
    """Production-grade Binance USD-M Futures Client (Standard Library Only)."""

    BASE_URL = "https://fapi.binance.com"
    TESTNET_URL = "https://testnet.binancefuture.com"

    def __init__(self, api_key=None, api_secret=None, testnet=False, timeout=10):
        self.api_key = str(api_key or "").strip()
        self.api_secret = str(api_secret or "").strip()
        self.base_url = self.TESTNET_URL if testnet else self.BASE_URL
        self.timeout = timeout

    @property
    def is_configured(self):
        return bool(self.api_key and self.api_secret)

    def _sign(self, params):
        query_string = urllib.parse.urlencode(params)
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()
        return query_string + "&signature=" + signature

    def _request(self, method, endpoint, params=None, signed=False):
        params = params or {}
        headers = {
            "User-Agent": "GodLevel-CryptoTrader/2.0 (Binance Autonomous Engine)",
            "Accept": "application/json",
        }

        if signed:
            if not self.is_configured:
                raise ValueError("Binance API credentials missing. Provide API key and secret.")
            params["timestamp"] = int(time.time() * 1000)
            params["recvWindow"] = 10000
            query = self._sign(params)
        else:
            query = urllib.parse.urlencode(params)

        if self.api_key:
            headers["X-MBX-APIKEY"] = self.api_key

        url = self.base_url + endpoint
        data = None

        if method in ("GET", "DELETE"):
            if query:
                url += ("?" + query)
        elif method in ("POST", "PUT"):
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            data = query.encode("utf-8")

        req = urllib.request.Request(url, data=data, headers=headers, method=method)

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body)
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", "replace")
            try:
                err_json = json.loads(err_body)
                msg = err_json.get("msg", err_body)
                code = err_json.get("code", exc.code)
                raise RuntimeError(f"Binance Futures API Error [code {code}]: {msg}")
            except Exception:
                raise RuntimeError(f"Binance Futures HTTP {exc.code}: {err_body}")
        except Exception as exc:
            raise RuntimeError(f"Network error connecting to Binance Futures: {exc}")

    # ------------------------------------------------------------- Public Endpoints

    def ping(self):
        """Test connectivity to Binance Futures."""
        res = self._request("GET", "/fapi/v1/ping")
        return res == {}

    def get_server_time(self):
        """Check server time synchronization."""
        return self._request("GET", "/fapi/v1/time")

    def get_mark_price(self, symbol):
        """Fetch real-time mark price, index price and funding rate."""
        sym = symbol.upper()
        return self._request("GET", "/fapi/v1/premiumIndex", {"symbol": sym})

    # ------------------------------------------------------------- Account & Balances

    def get_balance(self):
        """Fetch asset balances in USD-M Futures account."""
        return self._request("GET", "/fapi/v2/balance", signed=True)

    def get_account(self):
        """Fetch comprehensive futures account information."""
        return self._request("GET", "/fapi/v2/account", signed=True)

    def get_usdt_equity(self):
        """Extract available USDT cash and total equity."""
        balances = self.get_balance()
        for b in balances:
            if b.get("asset") == "USDT":
                return {
                    "balance": float(b.get("balance", 0.0)),
                    "available": float(b.get("availableBalance", 0.0)),
                    "cross_unrealized_pnl": float(b.get("crossUnPnl", 0.0)),
                }
        return {"balance": 0.0, "available": 0.0, "cross_unrealized_pnl": 0.0}

    def get_positions(self, symbol=None):
        """Fetch active positions on USD-M Futures."""
        params = {"symbol": symbol.upper()} if symbol else {}
        positions = self._request("GET", "/fapi/v2/positionRisk", params, signed=True)
        active = []
        for p in positions:
            amt = float(p.get("positionAmt", 0.0))
            if abs(amt) > 0:
                active.append({
                    "symbol": p.get("symbol"),
                    "amount": amt,
                    "side": "LONG" if amt > 0 else "SHORT",
                    "entry_price": float(p.get("entryPrice", 0.0)),
                    "mark_price": float(p.get("markPrice", 0.0)),
                    "unrealized_pnl": float(p.get("unRealizedProfit", 0.0)),
                    "liquidation_price": float(p.get("liquidationPrice", 0.0)),
                    "leverage": int(p.get("leverage", 1)),
                    "margin_type": p.get("marginType", "isolated"),
                    "isolated_margin": float(p.get("isolatedMargin", 0.0)),
                })
        return active

    # ------------------------------------------------------------- Leverage & Settings

    def set_leverage(self, symbol, leverage):
        """Set position leverage for a symbol (1x - 5x recommended)."""
        sym = symbol.upper()
        lev = int(max(1, min(leverage, 125)))
        return self._request("POST", "/fapi/v1/leverage", {"symbol": sym, "leverage": lev}, signed=True)

    def set_margin_type(self, symbol, margin_type="ISOLATED"):
        """Set margin type: ISOLATED or CROSSED."""
        sym = symbol.upper()
        m_type = margin_type.upper()
        try:
            return self._request("POST", "/fapi/v1/marginType", {"symbol": sym, "marginType": m_type}, signed=True)
        except RuntimeError as e:
            # Code -4046: "No need to change margin type" is benign
            if "No need to change" in str(e):
                return {"code": 200, "msg": "Already set"}
            raise

    # ------------------------------------------------------------- Orders

    def place_order(self, symbol, side, order_type="MARKET", quantity=None,
                    price=None, stop_price=None, reduce_only=False, time_in_force=None):
        """Place an order on Binance USD-M Futures."""
        sym = symbol.upper()
        side = side.upper()
        order_type = order_type.upper()

        params = {
            "symbol": sym,
            "side": side,
            "type": order_type,
        }

        if quantity is not None:
            params["quantity"] = str(quantity)

        if price is not None and order_type in ("LIMIT", "STOP", "TAKE_PROFIT"):
            params["price"] = str(price)
            params["timeInForce"] = time_in_force or "GTC"

        if stop_price is not None and order_type in ("STOP", "STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET"):
            params["stopPrice"] = str(stop_price)

        if reduce_only:
            params["reduceOnly"] = "true"

        return self._request("POST", "/fapi/v1/order", params, signed=True)

    def cancel_all_orders(self, symbol):
        """Cancel all resting orders for a symbol."""
        sym = symbol.upper()
        return self._request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": sym}, signed=True)

    def flatten_position(self, symbol):
        """Emergency flatten / close an open position immediately at market."""
        sym = symbol.upper()
        self.cancel_all_orders(sym)
        positions = self.get_positions(sym)
        results = []
        for p in positions:
            amt = abs(p["amount"])
            if amt <= 0:
                continue
            close_side = "SELL" if p["side"] == "LONG" else "BUY"
            res = self.place_order(
                symbol=sym,
                side=close_side,
                order_type="MARKET",
                quantity=amt,
                reduce_only=True
            )
            results.append(res)
        return results

    def flatten_all(self):
        """Master Emergency Kill-Switch: Flattens ALL open positions across all symbols."""
        active = self.get_positions()
        reports = []
        for p in active:
            sym = p["symbol"]
            try:
                res = self.flatten_position(sym)
                reports.append({"symbol": sym, "status": "FLATTENED", "result": res})
            except Exception as e:
                reports.append({"symbol": sym, "status": "ERROR", "error": str(e)})
        return reports
