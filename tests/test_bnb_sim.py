import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE_DIR = os.path.join(ROOT, "trader", "live")
ANALYST_DIR = os.path.join(ROOT, "trader-analyst")
if LIVE_DIR not in sys.path:
    sys.path.insert(0, LIVE_DIR)
if ANALYST_DIR not in sys.path:
    sys.path.insert(0, ANALYST_DIR)

import bnb_sim_engine  # noqa: E402


class TestBnbSimEngine(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.orig_state = bnb_sim_engine.BNB_STATE_PATH
        self.orig_trades = bnb_sim_engine.BNB_TRADES_PATH
        bnb_sim_engine.BNB_STATE_PATH = os.path.join(self.tmpdir.name, "bnb_state.json")
        bnb_sim_engine.BNB_TRADES_PATH = os.path.join(self.tmpdir.name, "bnb_trades.jsonl")

    def tearDown(self):
        bnb_sim_engine.BNB_STATE_PATH = self.orig_state
        bnb_sim_engine.BNB_TRADES_PATH = self.orig_trades
        self.tmpdir.cleanup()

    def test_previous_bnb_history_audit(self):
        audit = bnb_sim_engine.audit_previous_bnb_history()
        self.assertTrue(audit.get("found"))
        self.assertEqual(audit.get("last_plan_entry"), 743.31)
        self.assertEqual(audit.get("past_26_days_triggered"), 26)
        self.assertGreater(len(audit.get("root_cause_diagnosis", [])), 0)
        self.assertGreater(len(audit.get("institutional_upgrades_applied", [])), 0)

    def test_volume_profile_and_indicators(self):
        sample_bars = [
            {
                "t": 1700000000000 + i * 3600000,
                "ct": 1700000000000 + (i + 1) * 3600000 - 1,
                "o": 700.0 + (i % 10),
                "h": 705.0 + (i % 10),
                "l": 695.0 + (i % 10),
                "c": 702.0 + (i % 10),
                "v_base": 1000.0,
                "vol_usd": 700000.0,
                "tb_usd": 360000.0,
                "tb_pct": 51.4,
                "cvd_usd": 20000.0,
            }
            for i in range(60)
        ]
        vp = bnb_sim_engine.calc_volume_profile(sample_bars, bins=16)
        self.assertGreater(vp["poc"], 0)
        self.assertLessEqual(vp["val"], vp["poc"])
        self.assertGreaterEqual(vp["vah"], vp["poc"])
        vwap = bnb_sim_engine.calc_rolling_vwap(sample_bars, 24)
        self.assertGreater(vwap, 690.0)

    def test_open_and_close_long_and_short_positions(self):
        fake_setup = {
            "live_price": 710.0,
            "conviction_score": 88,
            "recommended_side": "SHORT",
            "dual_plans": {
                "LONG": {"side": "LONG", "playbook": "L1", "entry": 710.0, "stop": 698.0, "tp1": 729.2, "tp2": 746.0},
                "SHORT": {"side": "SHORT", "playbook": "S1", "entry": 710.0, "stop": 722.0, "tp1": 690.8, "tp2": 674.0},
            },
        }
        bnb_sim_engine.reset_bnb_state(10000.0)
        pos_short = bnb_sim_engine.open_bnb_position(
            side="SHORT",
            leverage=2.0,
            risk_usd=150.0,
            stop_loss=722.0,
            tp1=690.8,
            tp2=674.0,
            playbook="Test Short Playbook",
            live_setup=fake_setup,
        )
        self.assertEqual(pos_short["side"], "SHORT")
        self.assertGreaterEqual(pos_short["liq_buffer_mult"], 2.0)

        # Flip directly to LONG
        pos_long = bnb_sim_engine.open_bnb_position(
            side="LONG",
            leverage=2.0,
            risk_usd=150.0,
            stop_loss=698.0,
            tp1=729.2,
            tp2=746.0,
            playbook="Test Long Playbook",
            live_setup=fake_setup,
        )
        self.assertEqual(pos_long["side"], "LONG")
        self.assertGreaterEqual(pos_long["liq_buffer_mult"], 2.0)

        closed = bnb_sim_engine.close_bnb_position(close_pct=100.0, reason="Unit Test Close", live_price=720.0)
        self.assertEqual(closed["status"], "CLOSED")
        self.assertGreater(closed["trade"]["net_pnl_usd"], 0)


if __name__ == "__main__":
    unittest.main()
