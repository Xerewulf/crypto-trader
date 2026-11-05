#!/usr/bin/env python3
"""Unit tests for Notifier and Watchdog modules."""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LIVE = os.path.join(ROOT, "trader", "live")
if LIVE not in sys.path:
    sys.path.insert(0, LIVE)

import notifier
import watchdog


class TestNotifierAndWatchdog(unittest.TestCase):
    def test_notifier_config_loading(self):
        cfg = notifier.load_config()
        self.assertIsInstance(cfg, dict)
        self.assertIn("telegram_token", cfg)
        self.assertIn("telegram_chat_id", cfg)
        self.assertIn("discord_webhook", cfg)

    def test_notify_unconfigured_graceful_handling(self):
        res = notifier.notify("TEST", "Message content")
        self.assertIn("telegram", res)
        self.assertIn("discord", res)
        self.assertIsInstance(res["telegram"]["delivered"], bool)
        self.assertIsInstance(res["discord"]["delivered"], bool)

    def test_event_formatting(self):
        sample_position = {
            "symbol": "BTCUSDT",
            "side": "long",
            "setup": "Wyckoff Spring",
            "entry": 80000.0,
            "stop": 79000.0,
            "tp1_px": 81800.0,
            "tp2_px": 83500.0,
            "risk_usd": 50.0,
            "qty": 0.05,
            "notional": 4000.0,
            "margin": 1333.33
        }
        res_open = notifier.notify_setup_opened(sample_position)
        self.assertIsInstance(res_open, dict)

        res_scale = notifier.notify_scale_out(sample_position, "TP1 (+1.8R)", 81800.0, 90.0)
        self.assertIsInstance(res_scale, dict)

        res_close = notifier.notify_position_closed(sample_position, 81800.0, 90.0, 1.8, "TP1 HIT")
        self.assertIsInstance(res_close, dict)

    def test_watchdog_heartbeat_helper(self):
        hb = watchdog.get_heartbeat_ms()
        # May be int or None depending on state.json
        if hb is not None:
            self.assertIsInstance(hb, int)
            self.assertGreater(hb, 0)


if __name__ == "__main__":
    unittest.main()
