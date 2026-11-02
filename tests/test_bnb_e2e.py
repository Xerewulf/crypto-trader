#!/usr/bin/env python3
"""Comprehensive End-to-End Test Suite for BNB Long & Short Simulation Desk.

Tests:
  1. Web Page Integrity (GET /bnb)
  2. Live State & Data Structures (GET /api/bnb/state)
  3. 10-Year Model & Microstructure Data Integrity
  4. Order Lifecycle (RESET, OPEN LONG, BRACKETS, PARTIAL SCALE-OUT, FLIP SHORT, CLOSE)
  5. Real-Time Binance WebSocket Stream (wss://stream.binance.com:9443/ws/bnbusdt@aggTrade)
"""

import asyncio
import json
import os
import sys
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE_DIR = os.path.join(ROOT, "trader", "live")
if LIVE_DIR not in sys.path:
    sys.path.insert(0, LIVE_DIR)

import bnb_sim_engine  # noqa: E402

SERVER_URL = "http://localhost:8765"


class TestBnbDeskE2E(unittest.TestCase):

    def post_order(self, payload):
        req = urllib.request.Request(
            f"{SERVER_URL}/api/bnb/order",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def get_state(self):
        with urllib.request.urlopen(f"{SERVER_URL}/api/bnb/state", timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def test_01_bnb_html_serving(self):
        """Verify GET /bnb serves complete HTML page with all interactive controls."""
        req = urllib.request.Request(f"{SERVER_URL}/bnb")
        with urllib.request.urlopen(req, timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/html", resp.headers.get("Content-Type", ""))
            html = resp.read().decode("utf-8")
            self.assertIn("BNBUSDT Live Order-Flow Chart", html)
            self.assertIn("bnbChart", html)
            self.assertIn("vpBinsContainer", html)
            self.assertIn("newsListContainer", html)
            self.assertIn("activePosCard", html)
            self.assertIn("ticketPreviewBox", html)
            self.assertIn("playbookTableBody", html)
            self.assertIn("yearlyTableBody", html)
            self.assertIn("prevAuditContainer", html)
            self.assertIn("bnbusdt@aggTrade", html)

    def test_02_bnb_state_structure(self):
        """Verify GET /api/bnb/state returns required institutional schema."""
        data = self.get_state()
        self.assertEqual(data.get("symbol"), "BNBUSDT")
        self.assertIn("wallet", data)
        self.assertIn("live_setup", data)
        self.assertIn("model_10y", data)

        w = data["wallet"]
        for key in ("starting_equity", "cash_balance", "total_equity", "auto_pilot", "total_trades"):
            self.assertIn(key, w)

        ls = data["live_setup"]
        self.assertGreater(ls.get("live_price", 0), 100.0)
        self.assertIn(ls.get("recommended_side"), ("LONG", "SHORT"))
        self.assertIn("dual_plans", ls)
        self.assertIn("LONG", ls["dual_plans"])
        self.assertIn("SHORT", ls["dual_plans"])

        ms = ls.get("microstructure", {})
        self.assertGreater(ms.get("vwap_24h", 0), 100.0)
        self.assertGreater(ms.get("vp_poc", 0), 100.0)
        self.assertGreater(ms.get("vp_val", 0), 100.0)
        self.assertGreater(ms.get("vp_vah", 0), 100.0)

    def test_03_ten_year_model_integrity(self):
        """Verify 10-year model (2017-2026) has 4 playbooks and positive total expectancy."""
        data = self.get_state()
        m10 = data.get("model_10y", {})
        self.assertEqual(m10.get("symbol"), "BNBUSDT")
        self.assertGreaterEqual(m10.get("total_1d_bars", 0), 3000)
        self.assertGreaterEqual(m10.get("total_4h_bars", 0), 19000)

        ob = m10.get("overall_backtest", {})
        self.assertGreater(ob.get("total_trades", 0), 100)
        self.assertGreater(ob.get("win_rate_pct", 0), 40.0)
        self.assertGreater(ob.get("cumulative_r", 0), 0.0)
        self.assertGreater(ob.get("profit_factor", 0), 1.0)

        playbooks = m10.get("playbooks", [])
        self.assertEqual(len(playbooks), 4)
        sides = {pb["side"] for pb in playbooks}
        self.assertEqual(sides, {"LONG", "SHORT"})

        yearly = m10.get("yearly_breakdown", [])
        self.assertGreaterEqual(len(yearly), 10)

    def test_04_full_order_lifecycle(self):
        """Test complete paper-trading cycle: RESET -> OPEN LONG -> BRACKETS -> PARTIAL SCALE-OUT -> FLIP SHORT -> CLOSE."""
        # Step 1: Reset Wallet
        res_reset = self.post_order({"action": "RESET", "starting_equity": 10000.0})
        self.assertTrue(res_reset.get("ok"))
        self.assertEqual(res_reset["state"]["wallet"]["cash_balance"], 10000.0)
        self.assertIsNone(res_reset["state"]["wallet"]["active_position"])

        # Step 2: Open LONG position
        live_setup = res_reset["state"]["live_setup"]
        cur_p = live_setup["live_price"]
        long_stop = round(cur_p * 0.985, 2)
        long_tp1 = round(cur_p * 1.025, 2)
        long_tp2 = round(cur_p * 1.050, 2)

        res_open_long = self.post_order({
            "action": "OPEN",
            "side": "LONG",
            "leverage": 2.0,
            "risk_usd": 150.0,
            "stop_loss": long_stop,
            "tp1": long_tp1,
            "tp2": long_tp2,
            "playbook": "E2E Unit Test Long",
        })
        self.assertTrue(res_open_long.get("ok"))
        pos = res_open_long["state"]["wallet"]["active_position"]
        self.assertIsNotNone(pos)
        self.assertEqual(pos["side"], "LONG")
        self.assertEqual(pos["leverage"], 2.0)
        self.assertGreater(pos["qty"], 0)
        self.assertGreaterEqual(pos["liq_buffer_mult"], 2.0)  # Liquidation safety buffer >= 2.0x
        initial_qty = pos["qty"]

        # Step 3: Update Brackets (Trail Stop Upward safely below live price)
        updated_stop = round(pos["entry_price"] * 0.992, 2)
        res_brackets = self.post_order({
            "action": "BRACKETS",
            "stop_loss": updated_stop,
            "tp1": round(cur_p * 1.03, 2),
            "tp2": round(cur_p * 1.06, 2),
            "auto_pilot": False,
        })
        self.assertTrue(res_brackets.get("ok"))
        pos_after_brk = res_brackets["state"]["wallet"]["active_position"]
        self.assertIsNotNone(pos_after_brk)
        self.assertEqual(pos_after_brk["stop_loss"], updated_stop)
        self.assertFalse(res_brackets["state"]["wallet"]["auto_pilot"])

        # Step 4: Partial Scale-Out (Close 40% at TP1)
        res_part = self.post_order({
            "action": "CLOSE",
            "close_pct": 40.0,
            "reason": "E2E Partial TP1 Scale-Out (40%)",
        })
        self.assertTrue(res_part.get("ok"))
        pos_after_part = res_part["state"]["wallet"]["active_position"]
        self.assertIsNotNone(pos_after_part)
        self.assertAlmostEqual(pos_after_part["qty"], initial_qty * 0.60, places=2)

        # Step 5: Position Flip (Issue SHORT order while LONG is open)
        short_stop = round(cur_p * 1.015, 2)
        short_tp1 = round(cur_p * 0.975, 2)
        short_tp2 = round(cur_p * 0.950, 2)
        res_flip = self.post_order({
            "action": "OPEN",
            "side": "SHORT",
            "leverage": 3.0,
            "risk_usd": 150.0,
            "stop_loss": short_stop,
            "tp1": short_tp1,
            "tp2": short_tp2,
            "playbook": "E2E Flip to Short",
        })
        self.assertTrue(res_flip.get("ok"))
        pos_flip = res_flip["state"]["wallet"]["active_position"]
        self.assertIsNotNone(pos_flip)
        self.assertEqual(pos_flip["side"], "SHORT")
        self.assertEqual(pos_flip["leverage"], 3.0)
        self.assertGreaterEqual(pos_flip["liq_buffer_mult"], 2.0)
        self.assertEqual(len(res_flip["state"]["wallet"]["closed_trades"]), 1)

        # Step 6: Close 100% of SHORT position
        res_close = self.post_order({
            "action": "CLOSE",
            "close_pct": 100.0,
            "reason": "E2E Full Close",
        })
        self.assertTrue(res_close.get("ok"))
        self.assertIsNone(res_close["state"]["wallet"]["active_position"])
        self.assertEqual(len(res_close["state"]["wallet"]["closed_trades"]), 2)
        self.assertGreater(res_close["state"]["wallet"]["total_trades"], 0)

        # Step 7: Restore AI Auto-Pilot
        self.post_order({"action": "BRACKETS", "auto_pilot": True})

    def test_05_binance_websocket_ticks(self):
        """Verify real-time Binance aggTrade WebSocket stream connects and delivers live BNB trades."""
        import aiohttp

        async def check_ws():
            uri = "wss://stream.binance.com:9443/ws/bnbusdt@aggTrade"
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(uri) as ws:
                    msg_raw = await asyncio.wait_for(ws.receive_str(), timeout=8.0)
                    return json.loads(msg_raw)

        trade = asyncio.run(check_ws())
        self.assertEqual(trade.get("s"), "BNBUSDT")
        self.assertGreater(float(trade.get("p", 0)), 100.0)
        self.assertGreater(float(trade.get("q", 0)), 0.0)


if __name__ == "__main__":
    unittest.main()
