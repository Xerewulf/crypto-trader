#!/usr/bin/env python3
"""
BNB & Multi-Coin Profit Direction Planner (trader-analyst/profit_planner.py)

Evaluates BNB and 5 selected coins using live Binance order flow, cross-exchange
perpetual funding, open interest regimes, and Monte Carlo probability models.
Compares LONG vs SHORT profitability, win probability %, expected return %,
and ranks the coins by risk-adjusted profit suitability.
"""

import os
import sys
import json
import argparse

# Add current folder to path to import predict & margin
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import predict

DEFAULT_COINS = ["BNB", "SOL", "ETH", "BTC", "DOGE", "SUI"]

def plan_coin_profits(symbols=None, write_path=None):
    if not symbols:
        symbols = DEFAULT_COINS
    
    results = []
    
    for raw_sym in symbols:
        bsym = predict.to_binance(raw_sym)
        try:
            fc = predict.forecast_symbol(bsym)
            h24 = fc.get("horizons", {}).get("24h", {})
            h7d = fc.get("horizons", {}).get("7d", {})
            plan = fc.get("kelly_trade_plan") or {}
            margin_p = plan.get("margin_plan") or {}
            
            price = fc.get("price", 0.0)
            comp = fc.get("composite_score", 0.0)
            verdict = fc.get("verdict", "NEUTRAL")
            
            p_up_24 = h24.get("prob_up_pct", 50.0)
            p_down_24 = h24.get("prob_down_pct", 50.0)
            exp_move_24 = h24.get("expected_move_pct", 0.0)
            
            p_up_7d = h7d.get("prob_up_pct", 50.0)
            p_down_7d = h7d.get("prob_down_pct", 50.0)
            exp_move_7d = h7d.get("expected_move_pct", 0.0)
            
            # Determine Directional Action & Win Prob
            bias = plan.get("bias", "WAIT")
            if "SHORT" in bias.upper() or p_down_24 > p_up_24:
                action = "SHORT"
                win_prob = p_down_24
                exp_profit_24 = -exp_move_24
                exp_profit_7d = -exp_move_7d
            else:
                action = "LONG"
                win_prob = p_up_24
                exp_profit_24 = exp_move_24
                exp_profit_7d = exp_move_7d

            # Ensure valid trade geometry
            raw_entry = plan.get("limit_pullback_entry") or plan.get("spot_entry") or price
            raw_stop = plan.get("stop_loss")
            raw_tp1 = plan.get("tp1")
            raw_tp2 = plan.get("tp2")

            atr = (fc.get("timeframes", {}).get("4h", {}) or {}).get("atr", price * 0.015)
            
            if action == "SHORT":
                entry = raw_entry if raw_entry >= price * 0.98 else price
                stop = raw_stop if (raw_stop and raw_stop > entry) else round(entry + max(atr, entry * 0.012), 4)
                risk = stop - entry
                tp1 = raw_tp1 if (raw_tp1 and raw_tp1 < entry) else round(entry - (risk * 1.5), 4)
                tp2 = raw_tp2 if (raw_tp2 and raw_tp2 < tp1) else round(entry - (risk * 2.5), 4)
                tp1_gain_pct = round(((entry - tp1) / entry) * 100.0, 2)
                tp2_gain_pct = round(((entry - tp2) / entry) * 100.0, 2)
                stop_risk_pct = round(((stop - entry) / entry) * 100.0, 2)
            else:
                entry = raw_entry if raw_entry <= price * 1.02 else price
                stop = raw_stop if (raw_stop and raw_stop < entry) else round(entry - max(atr, entry * 0.012), 4)
                risk = entry - stop
                tp1 = raw_tp1 if (raw_tp1 and raw_tp1 > entry) else round(entry + (risk * 1.5), 4)
                tp2 = raw_tp2 if (raw_tp2 and raw_tp2 > tp1) else round(entry + (risk * 2.5), 4)
                tp1_gain_pct = round(((tp1 - entry) / entry) * 100.0, 2)
                tp2_gain_pct = round(((tp2 - entry) / entry) * 100.0, 2)
                stop_risk_pct = round(((entry - stop) / entry) * 100.0, 2)

            # Validated Math Edge: E[R] = (Win Prob * R) - (Loss Prob * 1.0)
            reward_risk = 1.5
            p_win = win_prob / 100.0
            p_loss = 1.0 - p_win
            math_edge_R = round((p_win * reward_risk) - p_loss, 2)

            leverage = margin_p.get("leverage") if margin_p.get("ok") else 1.0
            if not leverage or leverage < 1.0:
                leverage = 1.0
            
            # Profitability Score:
            # High win prob + strong 7d directional move + solid math edge + conviction
            conviction_factor = abs(comp) / 50.0  # scale from composite score
            profitability_score = round(
                (win_prob * 0.40) +
                (max(0.0, exp_profit_7d) * 5.0) +
                (max(0.0, math_edge_R) * 20.0) +
                (conviction_factor * 10.0),
                2
            )
            
            coin_summary = {
                "symbol": bsym,
                "price": price,
                "action": action,
                "action_probability_pct": round(win_prob, 1),
                "composite_score": comp,
                "verdict": verdict,
                "24h_expected_profit_pct": round(exp_profit_24, 2),
                "7d_expected_profit_pct": round(exp_profit_7d, 2),
                "tp1_gain_pct": tp1_gain_pct,
                "tp2_gain_pct": tp2_gain_pct,
                "stop_risk_pct": stop_risk_pct,
                "math_edge_R": math_edge_R,
                "reward_risk_ratio": reward_risk,
                "recommended_leverage": leverage,
                "entry_price": round(entry, 4),
                "stop_loss": round(stop, 4),
                "tp1": round(tp1, 4),
                "tp2": round(tp2, 4),
                "profitability_score": profitability_score,
                "details": fc
            }
            results.append(coin_summary)
        except Exception as e:
            sys.stderr.write(f"Error processing {bsym}: {e}\n")
    
    # Rank by profitability score descending
    results.sort(key=lambda x: x["profitability_score"], reverse=True)
    
    if write_path:
        os.makedirs(os.path.dirname(os.path.abspath(write_path)), exist_ok=True)
        # Omit full details from json dump to keep it clean unless needed
        clean_dump = []
        for r in results:
            d = dict(r)
            d.pop("details", None)
            clean_dump.append(d)
        with open(write_path, "w", encoding="utf-8") as f:
            json.dump(clean_dump, f, indent=2)

    return results

def render_table(results):
    lines = []
    lines.append("=" * 86)
    lines.append("BNB & SELECTED 5 COINS: DIRECTION & PROFITABILITY RANKING")
    lines.append("=" * 86)
    lines.append(f"{'RANK':<5}{'COIN':<10}{'ACTION':<8}{'WIN PROB':<11}{'24h EXP':<10}{'7d EXP':<10}{'TP1 GAIN':<11}{'EDGE (R)':<10}{'SCORE':<7}")
    lines.append("-" * 86)
    
    for idx, r in enumerate(results, 1):
        rank_str = f"#{idx}"
        act_str = f"{r['action']}"
        prob_str = f"{r['action_probability_pct']:.1f}%"
        exp24_str = f"+{r['24h_expected_profit_pct']:.2f}%" if r['24h_expected_profit_pct'] >= 0 else f"{r['24h_expected_profit_pct']:.2f}%"
        exp7d_str = f"+{r['7d_expected_profit_pct']:.2f}%" if r['7d_expected_profit_pct'] >= 0 else f"{r['7d_expected_profit_pct']:.2f}%"
        tp1_str = f"+{r['tp1_gain_pct']:.2f}%"
        edge_str = f"+{r['math_edge_R']:.2f}R"
        score_str = f"{r['profitability_score']:.1f}"
        
        lines.append(f"{rank_str:<5}{r['symbol']:<10}{act_str:<8}{prob_str:<11}{exp24_str:<10}{exp7d_str:<10}{tp1_str:<11}{edge_str:<10}{score_str:<7}")
    
    lines.append("-" * 86)
    if results:
        top = results[0]
        lines.append(f"MOST PROFITABLE SETUP: {top['symbol']} -> {top['action']} (Win Prob: {top['action_probability_pct']:.1f}%, 7d Profit: +{top['7d_expected_profit_pct']:.2f}%, Edge: +{top['math_edge_R']:.2f}R)")
        lines.append(f"Entry: ${top['entry_price']} | Stop-Loss: ${top['stop_loss']} | TP1: ${top['tp1']} | TP2: ${top['tp2']}")
    lines.append("=" * 86)
    return "\n".join(lines)

def main():
    parser = argparse.ArgumentParser(description="Multi-Coin Profit & Direction Planner")
    parser.add_argument("symbols", nargs="*", default=DEFAULT_COINS, help="Coins to evaluate (defaults to BNB + 5)")
    parser.add_argument("--write", nargs="?", const="trader/profit_plan.json", help="Save results to JSON")
    args = parser.parse_args()

    results = plan_coin_profits(args.symbols, write_path=args.write)
    print(render_table(results))

if __name__ == "__main__":
    main()
