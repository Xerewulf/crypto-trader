#!/usr/bin/env python3
"""Binance Futures & Margin Authenticated Live Execution Client.
Standard library only. No external dependencies required (e.g. no ccxt/python-binance).

Supports:
  1. Testnet and Production endpoints:
     - Testnet: https://testnet.binancefuture.com (Zero risk, free testnet keys)
     - Production: https://fapi.binance.com (Real trading)
  2. HMAC-SHA256 authenticated REST signing.
  3. Automatic server timestamp synchronization (guards against recvWindow clock drift).
  4. Precise Exchange Rules formatting (LOT_SIZE, PRICE_FILTER, stepSize, tickSize).
  5. Position management:
     - Leverage setting (e.g. 2x, 3x, 5x)
     - Margin type configuration (ISOLATED / CROSSED)
     - Algo Orders API integration (native conditional STOP_MARKET & TAKE_PROFIT_MARKET)
     - Bracket order creation (Entry + Native Algo Stop-Market + Algo Take-Profit Market)
     - Emergency Position Flattening (reduceOnly Market)
  6. Account metrics:
     - Real-time USDT equity, available balance, unrealized P&L, liquidation distance.

Usage:
  python trader/live/binance_client.py test               # Validate connectivity and API keys
  python trader/live/binance_client.py account            # View account balance and equity
  python trader/live/binance_client.py positions          # List active live positions
  python trader/live/binance_client.py flatten BTCUSDT    # Emergency close position
"""

import argparse
import datetime
import hashlib
import hmac
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
CREDENTIALS_FILE = os.path.join(ROOT, "trader", "credentials.json")

TESTNET_BASE_URL = "https://testnet.binancefuture.com"
PROD_BASE_URL = "https://fapi.binance.com"


class BinanceAPIError(Exception):
    def __init__(self, code, msg):
        self.code = code
        self.msg = msg
        super().__init__(f"Binance API Error {code}: {msg}")


class BinanceClient:
    def __init__(self, api_key=None, api_secret=None, testnet=True):
        self.testnet = testnet
        self.base_url = TESTNET_BASE_URL if testnet else PROD_BASE_URL

        # Load keys from environment or credentials.json
        self.api_key = api_key or os.getenv("BINANCE_API_KEY")
        self.api_secret = api_secret or os.getenv("BINANCE_API_SECRET")

        if not self.api_key or not self.api_secret:
            if os.path.exists(CREDENTIALS_FILE):
                try:
                    with open(CREDENTIALS_FILE, "r", encoding="utf-8") as fh:
                        data = json.load(fh)
                        self.api_key = self.api_key or data.get("api_key")
                        self.api_secret = self.api_secret or data.get("api_secret")
                        if "testnet" in data and api_key is None:
                            self.testnet = bool(data["testnet"])
                            self.base_url = TESTNET_BASE_URL if self.testnet else PROD_BASE_URL
                except Exception:
                    pass

        self.server_time_offset = 0
        self.exchange_info = {}
        self.symbol_filters = {}

    def is_configured(self):
        return bool(self.api_key and self.api_secret and len(self.api_key) > 10)

    # ---------------------------------------------------------------- network & signing

    def _sync_time(self):
        """Syncs local clock offset with Binance server time."""
        url = f"{self.base_url}/fapi/v1/time"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            server_time = int(data["serverTime"])
            self.server_time_offset = server_time - int(time.time() * 1000)

    def _request(self, method, endpoint, params=None, signed=False, timeout=12):
        if params is None:
            params = {}

        if signed:
            if not self.is_configured():
                raise BinanceAPIError(-1000, "API credentials not configured. Please set BINANCE_API_KEY and BINANCE_API_SECRET.")
            # Ensure timestamp with server offset
            now_ms = int(time.time() * 1000) + self.server_time_offset
            params["timestamp"] = now_ms
            params["recvWindow"] = 5000

            query_str = urllib.parse.urlencode(params)
            sig = hmac.new(
                self.api_secret.encode("utf-8"),
                query_str.encode("utf-8"),
                hashlib.sha256
            ).hexdigest()
            query_str += f"&signature={sig}"
        else:
            query_str = urllib.parse.urlencode(params)

        headers = {
            "User-Agent": "Mozilla/5.0",
            "Content-Type": "application/x-www-form-urlencoded"
        }
        if self.api_key:
            headers["X-MBX-APIKEY"] = self.api_key

        if method.upper() in ("GET", "DELETE"):
            url = f"{self.base_url}{endpoint}" + (f"?{query_str}" if query_str else "")
            req = urllib.request.Request(url, headers=headers, method=method.upper())
        else:
            url = f"{self.base_url}{endpoint}"
            data_bytes = query_str.encode("utf-8")
            req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method.upper())

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8")
            try:
                err_json = json.loads(err_body)
                code = err_json.get("code", exc.code)
                msg = err_json.get("msg", err_body)
            except Exception:
                code = exc.code
                msg = err_body
            raise BinanceAPIError(code, msg)

    # ---------------------------------------------------------------- public market rules

    def load_exchange_info(self, symbol=None):
        """Loads filter rules (tick size, lot size) for symbol precision formatting."""
        data = self._request("GET", "/fapi/v1/exchangeInfo")
        self.exchange_info = data
        for s in data.get("symbols", []):
            sym = s["symbol"]
            filters = {f["filterType"]: f for f in s.get("filters", [])}
            lot = filters.get("LOT_SIZE", {})
            price = filters.get("PRICE_FILTER", {})
            min_notional = filters.get("MIN_NOTIONAL", {}).get("notional", "5.0")

            self.symbol_filters[sym] = {
                "baseAsset": s.get("baseAsset"),
                "quoteAsset": s.get("quoteAsset"),
                "status": s.get("status"),
                "stepSize": float(lot.get("stepSize", "0.001")),
                "minQty": float(lot.get("minQty", "0.001")),
                "tickSize": float(price.get("tickSize", "0.01")),
                "minNotional": float(min_notional),
            }
        return self.symbol_filters

    @staticmethod
    def _precision_from_step(step):
        s = f"{step:.10f}".rstrip("0")
        return len(s.split(".")[1]) if "." in s else 0

    def format_qty(self, symbol, qty):
        """Rounds quantity down to exact Binance lot stepSize with float-underflow guard."""
        rules = self.symbol_filters.get(symbol)
        if not rules:
            self.load_exchange_info()
            rules = self.symbol_filters.get(symbol, {"stepSize": 0.001, "minQty": 0.001})
        step = rules["stepSize"]
        precision = self._precision_from_step(step)
        rounded = math.floor(float(qty) / step + 1e-9) * step
        return f"{rounded:.{precision}f}"

    def format_price(self, symbol, price):
        """Rounds price to exact Binance tickSize with precision guard."""
        rules = self.symbol_filters.get(symbol)
        if not rules:
            self.load_exchange_info()
            rules = self.symbol_filters.get(symbol, {"tickSize": 0.01})
        tick = rules["tickSize"]
        precision = self._precision_from_step(tick)
        rounded = round(float(price) / tick) * tick
        return f"{rounded:.{precision}f}"

    # ---------------------------------------------------------------- authenticated account

    def get_account(self):
        """Returns account balance, total equity, and margin summary."""
        self._sync_time()
        data = self._request("GET", "/fapi/v2/account", signed=True)
        total_wallet = float(data.get("totalWalletBalance", 0.0))
        total_unrealized = float(data.get("totalUnrealizedProfit", 0.0))
        total_margin = float(data.get("totalMarginBalance", total_wallet + total_unrealized))
        available_balance = float(data.get("availableBalance", 0.0))

        positions = []
        for p in data.get("positions", []):
            amt = float(p.get("positionAmt", 0.0))
            if abs(amt) > 0:
                positions.append({
                    "symbol": p["symbol"],
                    "amount": amt,
                    "side": "LONG" if amt > 0 else "SHORT",
                    "entryPrice": float(p.get("entryPrice", 0.0)),
                    "markPrice": float(p.get("markPrice", 0.0)),
                    "unrealizedProfit": float(p.get("unrealizedProfit", 0.0)),
                    "liquidationPrice": float(p.get("liquidationPrice", 0.0)),
                    "leverage": int(p.get("leverage", 1)),
                    "isolated": bool(p.get("isolated", False)),
                })

        return {
            "mode": "TESTNET" if self.testnet else "PRODUCTION",
            "totalWalletBalance": total_wallet,
            "totalMarginBalance": total_margin,
            "totalUnrealizedProfit": total_unrealized,
            "availableBalance": available_balance,
            "positions": positions
        }

    def set_leverage(self, symbol, leverage):
        """Configures symbol leverage (e.g. 2, 3, 5)."""
        self._sync_time()
        return self._request("POST", "/fapi/v1/leverage", params={
            "symbol": symbol,
            "leverage": int(leverage)
        }, signed=True)

    def set_margin_type(self, symbol, margin_type="ISOLATED"):
        """Configures margin type (ISOLATED or CROSSED)."""
        self._sync_time()
        try:
            return self._request("POST", "/fapi/v1/marginType", params={
                "symbol": symbol,
                "marginType": margin_type.upper()
            }, signed=True)
        except BinanceAPIError as err:
            # -4046 means "No need to change margin type" (already set)
            if err.code == -4046:
                return {"msg": "Margin type already configured"}
            raise

    # ---------------------------------------------------------------- order execution

    def place_order(self, symbol, side, order_type="MARKET", qty=None, price=None,
                    reduce_only=False, time_in_force=None):
        """Sends a standard order (MARKET / LIMIT) to Binance Futures."""
        self._sync_time()
        params = {
            "symbol": symbol,
            "side": side.upper(),
            "type": order_type.upper(),
        }
        if qty is not None:
            params["quantity"] = self.format_qty(symbol, float(qty))
        if price is not None:
            params["price"] = self.format_price(symbol, float(price))
        if reduce_only:
            params["reduceOnly"] = "true"
        if time_in_force:
            params["timeInForce"] = time_in_force
        elif order_type.upper() == "LIMIT":
            params["timeInForce"] = "GTC"

        return self._request("POST", "/fapi/v1/order", params=params, signed=True)

    def place_algo_order(self, symbol, side, order_type="STOP_MARKET", trigger_price=None, qty=None, reduce_only=True):
        """Places conditional algorithmic order (e.g. STOP_MARKET, TAKE_PROFIT_MARKET) via /fapi/v1/algoOrder."""
        self._sync_time()
        params = {
            "symbol": symbol,
            "side": side.upper(),
            "type": order_type.upper(),
            "algoType": "CONDITIONAL",
            "triggerPrice": self.format_price(symbol, float(trigger_price)),
            "workingType": "MARK_PRICE",
            "reduceOnly": "true" if reduce_only else "false",
        }
        if qty is not None:
            params["quantity"] = self.format_qty(symbol, float(qty))

        return self._request("POST", "/fapi/v1/algoOrder", params=params, signed=True)

    def place_bracket(self, symbol, side, qty, stop_loss_price, tp1_price=None, entry_type="MARKET", limit_price=None):
        """Places an entry order with native conditional algo stop-loss and take-profit orders."""
        self._sync_time()
        # 1. Ensure isolated margin & leverage
        try:
            self.set_margin_type(symbol, "ISOLATED")
        except Exception:
            pass

        # 2. Place Entry Order
        entry_order = self.place_order(
            symbol=symbol,
            side=side,
            order_type=entry_type,
            qty=qty,
            price=limit_price
        )

        exit_side = "SELL" if side.upper() == "BUY" else "BUY"

        # 3. Place Native Algorithmic STOP_MARKET via Algo Order API
        stop_order = self.place_algo_order(
            symbol=symbol,
            side=exit_side,
            order_type="STOP_MARKET",
            qty=qty,
            trigger_price=stop_loss_price,
            reduce_only=True
        )

        # 4. Optional TAKE_PROFIT_MARKET via Algo Order API
        tp_order = None
        if tp1_price is not None:
            try:
                tp_order = self.place_algo_order(
                    symbol=symbol,
                    side=exit_side,
                    order_type="TAKE_PROFIT_MARKET",
                    qty=qty,
                    trigger_price=tp1_price,
                    reduce_only=True
                )
            except Exception as tp_err:
                # If TP fails, keep stop order active
                pass

        return {
            "entry": entry_order,
            "stop": stop_order,
            "take_profit": tp_order
        }

    def flatten_position(self, symbol):
        """Immediately closes any open position for the symbol at market and cancels outstanding bracket orders."""
        self.cancel_all_orders(symbol)
        acct = self.get_account()
        for p in acct.get("positions", []):
            if p["symbol"] == symbol:
                amt = p["amount"]
                if abs(amt) > 0:
                    exit_side = "SELL" if amt > 0 else "BUY"
                    return self.place_order(
                        symbol=symbol,
                        side=exit_side,
                        order_type="MARKET",
                        qty=abs(amt),
                        reduce_only=True
                    )
        return {"msg": f"No open position found for {symbol}"}

    def cancel_all_orders(self, symbol):
        """Cancels all open conditional and regular limit orders for symbol."""
        self._sync_time()
        res = {"regular": None, "algo": []}
        try:
            res["regular"] = self._request("DELETE", "/fapi/v1/allOpenOrders", params={"symbol": symbol}, signed=True)
        except Exception:
            pass

        try:
            open_algos = self._request("GET", "/fapi/v1/openAlgoOrders", params={"symbol": symbol}, signed=True)
            for algo in open_algos:
                algo_id = algo.get("algoId")
                if algo_id:
                    del_res = self._request("DELETE", "/fapi/v1/algoOrder", params={"algoId": algo_id}, signed=True)
                    res["algo"].append(del_res)
        except Exception:
            pass

        return res


# ---------------------------------------------------------------- CLI

def main():
    parser = argparse.ArgumentParser(description="Binance Futures Authenticated Live Client.")
    parser.add_argument("cmd", choices=["test", "account", "positions", "flatten"])
    parser.add_argument("symbol", nargs="?", default="", help="Cryptocurrency symbol (e.g. BTCUSDT)")
    parser.add_argument("--prod", action="store_true", help="Use production live API instead of testnet")
    args = parser.parse_args()

    client = BinanceClient(testnet=not args.prod)

    if args.cmd == "test":
        print(f"[*] Testing connection to Binance Futures ({'TESTNET' if client.testnet else 'PRODUCTION'})...")
        try:
            client._sync_time()
            print(f"[+] Server time synced successfully! Offset: {client.server_time_offset}ms")
            if client.is_configured():
                acct = client.get_account()
                print(f"[+] API Credentials VALID! Total Wallet Equity: ${acct['totalMarginBalance']:,.2f}")
                print(f"[+] Available Free Margin: ${acct['availableBalance']:,.2f}")
                print(f"[+] Active Open Positions: {len(acct['positions'])}")
            else:
                print("[-] API keys not configured. Set BINANCE_API_KEY/SECRET or create trader/credentials.json.")
        except Exception as e:
            print(f"[-] Connection failed: {e}")

    elif args.cmd == "account":
        acct = client.get_account()
        print("=" * 60)
        print(f" BINANCE FUTURES ACCOUNT OVERVIEW ({acct['mode']})")
        print("=" * 60)
        print(f"Total Wallet Balance : ${acct['totalWalletBalance']:,.2f}")
        print(f"Total Margin Equity  : ${acct['totalMarginBalance']:,.2f}")
        print(f"Unrealized P&L       : {'+' if acct['totalUnrealizedProfit'] >= 0 else ''}${acct['totalUnrealizedProfit']:,.2f}")
        print(f"Available Margin     : ${acct['availableBalance']:,.2f}")
        print(f"Active Positions     : {len(acct['positions'])}")
        print("=" * 60)

    elif args.cmd == "positions":
        acct = client.get_account()
        positions = acct.get("positions", [])
        print("=" * 75)
        print(f" ACTIVE POSITIONS ({acct['mode']}) - Count: {len(positions)}")
        print("=" * 75)
        if not positions:
            print("  No active open positions on Binance Futures.")
        else:
            for p in positions:
                print(f"  * {p['symbol']} [{p['side']}]: Amount {p['amount']} | Entry ${p['entryPrice']:,.2f} | Mark ${p['markPrice']:,.2f}")
                print(f"    Unrealized P&L: {'+' if p['unrealizedProfit'] >= 0 else ''}${p['unrealizedProfit']:,.2f} | Liq Price: ${p['liquidationPrice']:,.2f} | Leverage: {p['leverage']}x ({'Isolated' if p['isolated'] else 'Cross'})")
        print("=" * 75)

    elif args.cmd == "flatten":
        if not args.symbol:
            print("Error: You must specify a symbol to flatten (e.g. python binance_client.py flatten BTCUSDT)")
            return
        res = client.flatten_position(args.symbol.upper())
        print(f"[+] Flatten result for {args.symbol.upper()}: {res}")


if __name__ == "__main__":
    main()
