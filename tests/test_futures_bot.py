import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FUT_DIR = os.path.join(ROOT, "trader", "futures")
ANALYST_DIR = os.path.join(ROOT, "trader-analyst")
for d in (FUT_DIR, ANALYST_DIR):
    if d not in sys.path:
        sys.path.insert(0, d)

import strategy
import bot
import margin


def make_synthetic_bars(n=150, start_px=100.0, step_pct=0.002, tb_pct=56.0):
    bars = []
    px = float(start_px)
    t0 = 1790000000000
    for i in range(n):
        # Realistic trend with mild pullbacks on alternating bars so RSI stays ~62 (within 53..68)
        chg = -abs(step_pct) * 0.65 if (i % 2 == 1 and i < n - 1) else step_pct
        nxt = px * (1.0 + chg)
        lo = min(px, nxt) * 0.996
        hi = max(px, nxt) * 1.004
        vol = 1000000.0 * (2.2 if i == n - 1 else 1.0)
        tb_usd = vol * (tb_pct / 100.0)
        bars.append({
            "t": t0 + i * 3600000,
            "ct": t0 + (i + 1) * 3600000 - 1,
            "o": px,
            "h": hi,
            "l": lo,
            "c": nxt,
            "vol_usd": vol,
            "tb_usd": tb_usd,
            "tb": tb_pct,
            "cvd_usd": 2.0 * tb_usd - vol,
        })
        px = nxt
    return bars


class TestSimplifiedFuturesBot(unittest.TestCase):

    def test_core_universe_excludes_tokenized_stocks(self):
        for sym in strategy.CORE_FUTURES_UNIVERSE:
            self.assertTrue(sym.endswith("USDT"))
            self.assertFalse(margin.is_tokenized_stock(sym), f"{sym} should not be a tokenized stock")

    def test_volume_profile_and_vwap_math(self):
        bars = make_synthetic_bars(140, start_px=100.0, step_pct=0.001)
        vp = strategy.volume_profile(bars, n_bars=120)
        vwap = strategy.rolling_vwap(bars, n_bars=24)
        self.assertLessEqual(vp["val"], vp["poc"])
        self.assertLessEqual(vp["poc"], vp["vah"])
        self.assertGreater(vwap, 100.0)

    def test_playbook1_htf_initiative_breakout_signal(self):
        bars = make_synthetic_bars(150, start_px=100.0, step_pct=0.0018, tb_pct=57.0)
        # Ensure final bar breaks above 8-bar high with strong bullish close and healthy RSI (~62)
        for k in range(-8, -1):
            bars[k]["h"] = bars[k]["o"] * 1.001
        last = bars[-1]
        high_8 = max(x["h"] for x in bars[-9:-1])
        last["o"] = bars[-2]["c"]
        last["c"] = high_8 * 1.0012
        last["h"] = last["c"] * 1.0002
        last["l"] = last["o"] * 0.9998

        sig, diag = strategy.evaluate_futures_setup(
            "SOLUSDT",
            bars,
            btc_regime="BULL",
            deriv={
                "funding_8h": 0.0001,
                "oi_change_4h_pct": 2.5,
                "whale_ls_ratio": 1.45,
                "retail_ls_ratio": 1.05,
                "imb_1pct": 12.0,
            }
        )
        self.assertIsNotNone(sig, f"Expected Playbook 1 signal, got diag={diag}")
        self.assertEqual(sig["side"], "long")
        self.assertEqual(sig["playbook"], "PB1_BREAKOUT")
        self.assertGreaterEqual(sig["score"], 82)
        # Structural stop must never be compressed below 1.1%
        self.assertGreaterEqual(sig["stop_dist_pct"], 1.1)
        self.assertGreater(sig["tp1_px"], sig["entry"])
        self.assertGreater(sig["tp2_px"], sig["tp1_px"])

    def test_btc_regime_detector(self):
        bull_bars = make_synthetic_bars(150, start_px=60000.0, step_pct=0.002, tb_pct=54.0)
        bear_bars = make_synthetic_bars(150, start_px=60000.0, step_pct=-0.002, tb_pct=45.0)
        self.assertEqual(strategy.detect_btc_regime(bull_bars), "BULL")
        self.assertEqual(strategy.detect_btc_regime(bear_bars), "BEAR")

    def test_margin_safety_and_correlation_guard(self):
        plan = margin.margin_plan(
            equity=5000.0,
            risk_usd=50.0,
            entry=100.0,
            stop=98.0,
            side="long",
            base="SOL",
            venue="perp",
        )
        self.assertTrue(plan["ok"])
        self.assertEqual(plan["risk_usd"], 50.0)
        self.assertGreaterEqual(plan["liq_to_stop_ratio"], 2.0)

        # Correlation guard blocks 3rd same-side position
        open_pos = [
            {"side": "long", "notional": 1500.0},
            {"side": "long", "notional": 1500.0},
        ]
        err = margin.correlation_guard(open_pos, "long", 5000.0, 1000.0, max_same_side=2)
        self.assertIsNotNone(err)


if __name__ == "__main__":
    unittest.main()
