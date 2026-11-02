#!/usr/bin/env python3
"""Unit tests for Phase 6: Multi-Agent AI Dialectical Debate Engine."""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from _load import load

ai_agent = load("trader-analyst", "ai_agent")


class TestMultiAgentAI(unittest.TestCase):
    def setUp(self):
        self.engine = ai_agent.MultiAgentDebateEngine()

    def test_quant_analyst_agent(self):
        agent = ai_agent.QuantAnalystAgent()
        metrics = {
            "spot_price": 50000.0,
            "depth_imbalance_1pct": 0.25,
            "funding_rate_8h": -0.0002,
            "whale_top_trader_ls": 1.6,
            "retail_account_ls": 1.0,
            "vwap_24h": 49500.0,
            "volume_profile_7d": {"val": 48000.0, "poc": 49800.0, "vah": 51500.0},
            "rsi_14_1h": 55.0,
            "hmm_regime": {"current_regime": 0, "regime_name": "BULL_MOMENTUM"},
        }
        report = agent.analyze("BTCUSDT", metrics, [])
        self.assertEqual(report["agent"], "QuantAnalystAgent")
        self.assertTrue(len(report["bull_evidence"]) >= 2)

    def test_macro_catalyst_agent(self):
        agent = ai_agent.MacroCatalystAgent()
        news = [
            {"title": "Institutional ETF inflow reaches historic high", "tag": "ETF"},
            {"title": "Token unlock dilution looms for competitor", "tag": "UNLOCK"},
        ]
        report = agent.analyze("BTCUSDT", news)
        self.assertEqual(report["agent"], "MacroCatalystAgent")
        self.assertTrue(len(report["bull_catalysts"]) >= 1)
        self.assertTrue(len(report["bear_catalysts"]) >= 1)

    def test_advocates_and_cro_arbitration(self):
        quant = {
            "agent": "QuantAnalystAgent",
            "spot_price": 50000.0,
            "vwap": 49500.0,
            "levels": {"val": 48000.0, "poc": 49800.0, "vah": 52000.0},
            "hmm_regime": "BULL_MOMENTUM",
            "bull_evidence": ["Bid wall imbalance +25%", "Whale L/S 1.6 vs Retail 1.0"],
            "bear_evidence": [],
        }
        macro = {
            "agent": "MacroCatalystAgent",
            "bull_catalysts": ["Institutional ETF inflows expanding"],
            "bear_catalysts": ["Macro risk benign"],
        }

        bull_agent = ai_agent.BullAdvocateAgent()
        bear_agent = ai_agent.BearAdvocateAgent()
        cro_agent = ai_agent.ChiefRiskOfficerAgent()

        bull_report = bull_agent.advocate(quant, macro)
        bear_report = bear_agent.advocate(quant, macro)
        cro_report = cro_agent.arbitrate("BTCUSDT", quant, macro, bull_report, bear_report)

        self.assertIn("verdict", cro_report)
        self.assertTrue(cro_report["net_conviction_score"] > 0)
        self.assertTrue("BUY" in cro_report["verdict"] or "LONG" in cro_report["verdict"])
        self.assertTrue(cro_report["invalidation_price"] <= 49500.0)
        self.assertTrue(cro_report["primary_target"] >= 50000.0)

    def test_cro_arbitration_volatile_shock_override(self):
        quant = {
            "agent": "QuantAnalystAgent",
            "spot_price": 50000.0,
            "vwap": 50000.0,
            "levels": {"val": 48000.0, "poc": 50000.0, "vah": 52000.0},
            "hmm_regime": "VOLATILE_SHOCK",  # Shock override
            "bull_evidence": ["Whale buys"],
            "bear_evidence": ["Order book thin"],
        }
        macro = {"agent": "MacroCatalystAgent", "bull_catalysts": [], "bear_catalysts": []}
        bull_report = ai_agent.BullAdvocateAgent().advocate(quant, macro)
        bear_report = ai_agent.BearAdvocateAgent().advocate(quant, macro)
        cro_report = ai_agent.ChiefRiskOfficerAgent().arbitrate("BTCUSDT", quant, macro, bull_report, bear_report)

        # In volatile shock, sizing multiplier must be capped at 0.25x
        self.assertTrue(cro_report["recommended_sizing_multiplier"] <= 0.25)
        self.assertIn("HMM Volatile Shock", cro_report["arbitration_summary"])

    def test_debate_engine_run_with_custom_metrics(self):
        custom = {
            "spot_price": 600.0,
            "depth_imbalance_1pct": 0.20,
            "funding_rate_8h": 0.0001,
            "whale_top_trader_ls": 1.5,
            "retail_account_ls": 1.1,
            "vwap_24h": 598.0,
            "volume_profile_7d": {"val": 580.0, "poc": 596.0, "vah": 620.0},
            "rsi_14_1h": 52.0,
            "hmm_regime": {"current_regime": 0, "regime_name": "BULL_MOMENTUM"},
        }
        res = self.engine.run_debate("BNBUSDT", custom_metrics=custom)
        self.assertEqual(res["symbol"], "BNBUSDT")
        self.assertIn("verdict", res)
        self.assertIn("debate", res)
        self.assertIn("evidence", res)


if __name__ == "__main__":
    unittest.main()
