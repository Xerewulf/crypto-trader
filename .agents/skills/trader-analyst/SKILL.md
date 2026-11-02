---
name: trader-analyst
description: >-
  Crypto Directional Forecaster & Market Microstructure Analyst (plus SEC
  EDGAR Earnings Analyst). For Binance crypto tokens (like BNB, BTC, ETH, SOL),
  runs predict.py to track all 6 price drivers in real time: Perpetual Futures
  Funding Rate, Mark-Index Basis, Open Interest regime, Whale Top-Trader L/S vs
  Retail L/S ratio, Futures & Spot Taker Buy/Sell CVD, Order-Book Bid/Ask Depth
  Walls, Multi-Timeframe Technicals (15m/1h/4h/1d), Relative Strength vs BTC,
  and News/Macro Catalysts, outputting a Composite Score (-100 to +100) and
  4h / 24h / 7d Up/Down probabilities with 80% confidence bands. Use when the
  user asks whether a coin/token will go up or down, wants a price prediction
  or forecast, asks "how does BNB look", "analyze BNB", "will BTC go up or
  down", "whale positioning", "funding rate", or "order book walls".
argument-hint: "[tickers]"
---

# trader-analyst

## 1. Crypto Directional Forecaster & Microstructure Analyst (`predict.py`)

For Binance cryptocurrencies (e.g. `BNB`, `BTC`, `ETH`, `SOL`), run `predict.py` to estimate whether a token leans **UP** or **DOWN** over the next **4 hours, 24 hours, and 7 days** by combining live Binance Spot (`api.binance.com`), Binance USDⓈ-M Perpetual Futures (`fapi.binance.com`), and Yahoo Crypto RSS:

```bash
python3 "${CLAUDE_SKILL_DIR}/predict.py" BNB --write
python3 "${CLAUDE_SKILL_DIR}/predict.py" BNB BTC ETH SOL --write
```

### What `predict.py` measures and writes
- **Perpetual Futures & Leverage (`fapi.binance.com`)**: 8h Funding Rate & annualized carry, Mark-Index Basis %, Open Interest in USD (1h/4h/24h changes & Price×OI regime: Long Build-Up, Short Covering, Short Build-Up, Long Liquidation), Top Trader Whale Position L/S Ratio vs. Global Retail Account L/S Ratio, and Futures Taker Buy/Sell Volume Ratio.
- **Spot Order-Flow & Liquidity Walls (`api.binance.com`)**: Spot Taker Buy % and Net Cumulative Volume Delta (CVD in USD) over 1h, 4h, 24h, and 7d, plus Order-Book Bid vs. Ask Depth within `±1%` and `±2%` (5,000 depth levels) and the largest near-term Bid Support & Ask Resistance walls.
- **Multi-Timeframe Technicals & Relative Strength**: `15m`, `1h`, `4h`, and `1d` EMA20/EMA50/SMA200 alignment, Wilder RSI-14, MACD histogram slope, ATR(14) volatility, and 24h/7d Alpha vs. `BTCUSDT`.
- **News & Macro Catalysts**: Scans token + BTC headlines for burns/buybacks, ETF/institutional flows, upgrades/launches, token unlocks, hacks, regulatory actions, and macro rate/liquidity shifts.
- **Probabilistic Forecast (`trader/forecast.json`)**: Runs a 5,000-path microstructure-adjusted Monte Carlo bootstrap on 1h returns to output `P(UP)`, `P(DOWN)`, expected price, and 80% confidence bands for `4h`, `24h`, and `7d`.

---

## 2. Institutional Margin Trading Doctrine (`margin.py`)

Every leveraged crypto plan adheres to strict institutional risk engineering:
1. **Size by the STOP, Never by the Leverage**: Dollar risk is fixed strictly by $Risk = Qty \times |Entry - Stop|$. Leverage only controls collateral locked and distance to liquidation.
2. **Mandatory Liquidation Safety Buffer**: Liquidation must sit at least $2.0\times$ the stop distance away ($|Entry - Liq| \ge 2.0 \times |Entry - Stop|$).
3. **Liquidity-Tiered Maximum Leverage**: BTC up to 5x, ETH up to 4x, Large Caps (SOL, BNB, XRP) up to 3x, and all other altcoins capped at 2x.
4. **Tiered Maintenance Margin (MMR & CUM)**: Maintenance margin scales with position notional brackets. $MM = (Notional \times MMR) - CUM$.
5. **Dual-Price Mechanism & Mark Price Protection**: Binance liquidations trigger on Mark Price, not Last Price, to protect against local order-book wick stop-hunts.
6. **Cumulative Carry Drag**: Models carry-adjusted liquidation prices after factoring in cumulative funding fee or borrow interest erosion.
7. **Position Health & Margin Call Warnings**: Tracks Spot Margin Level and Futures Margin Ratio with proactive warnings at MARGIN CALL (<=1.30) and CRITICAL (<=1.15) before liquidation (<=1.10).
8. **Auto-Deleveraging (ADL) Protection**: Low institutional leverage (<=5x) keeps capital in ADL Quantile 1 (lowest force-close hazard).
9. **Macro & Scheduled Volatility Event Guards**: FOMC statements (BLOCK 2h before to 1h after; CAUTION 24h before to 6h after), CPI & NFP reports (BLOCK 1h before to 30m after), and monthly/quarterly Deribit options expiries (CAUTION 12h before to 2h after).
10. **Book Correlation Caps**: Max 2 open positions on the same side and gross book leverage capped at 2.0x equity. Excludes tokenized bStocks.

---

## 3. Delta-Neutral Funding & Cash-Carry Arbitrage Suite (`arbitrage.py`)

Phase 2 systematic yield engine for generating consistent cashflow during sideways or ranging crypto markets with zero price directional risk:
1. **Automated Spot vs. Perpetual Basis & Funding Scanner**:
   - Computes absolute basis ($Perp_{mark} - Spot_{price}$) and basis percentage spread.
   - Converts 8-hour funding rate into simple and compound annualized yield (APY).
   - Scans Binance Spot and USDⓈ-M Perps in real time.
2. **Cash & Carry Synthetic Dollar (Long Spot + 1x Short Perp)**:
   - When 8h funding rate is positive ($fr \ge +0.01\%$/8h or $\ge +10.95\%$ APY): Desk allocates 50% capital to buy Spot and locks 50% capital in 1x Short Perp.
   - Collects 8-hour funding cashflows paid by leveraged long traders directly into equity.
   - Net delta exposure is zero ($Delta_{spot} + Delta_{perp} \approx \$0.00$), rendering capital immune to price crashes or rallies.
3. **Negative Funding Rate Short Squeeze Harvester (Short Spot + Long 1x Perp)**:
   - When funding plunges deeply negative ($fr \le -0.30\%$/8h or $\le -328.5\%$ APY): Desk borrows Spot on Isolated Margin to Sell Short, and buys 1x Perpetual Futures.
   - Harvests forced funding payments from aggressive perp short sellers while retaining net zero directional exposure.
4. **Real-Time Continuous Funding Accrual & Basis Convergence Engine**:
   - Incrementally accrues funding yield tick-by-tick and models Mark-to-Market basis convergence PnL.
   - 1-click simultaneous unwinding closes both Spot and Perp legs atomically.

---

## 4. Phase 3 Machine Learning & VPIN Order-Flow Toxicity Suite (`microstructure.py`)

Phase 3 quantitative microstructure engine for predicting order-flow toxicity, informed trader arrival rates, and dynamic volatility adaptation:
1. **Marcos López de Prado's VPIN Engine (`compute_vpin`)**:
   - Volume-Clock Bucketing: Slices continuous transaction volume into equal volume buckets ($V = V_{total} / N$).
   - Decomposes order flow via Bulk Volume Classification (BVC) and exact taker buy/sell execution.
   - Calculates rolling order-flow toxicity:
     $$VPIN = \frac{\sum_{\tau=1}^N |V_\tau^B - V_\tau^S|}{N \times V}$$
   - Classifies toxicity regimes: `LOW_TOXICITY` ($<25\%$), `NORMAL_FLOW` ($25\%-45\%$), `ELEVATED_TOXICITY` ($45\%-60\%$), and `TOXIC_SURGE` ($\ge 60\%$).
   - Estimates informed trader arrival rate ($\alpha \times \mu$) and issues proactive adverse selection warnings.
2. **Wyckoff 2.0 Order-Flow & CVD Divergence Detector (`detect_wyckoff_cvd_divergence`)**:
   - **Wyckoff Spring**: Detects liquidity sweeps hunting retail stop-losses below 24h lows or Value Area Low (`VAL`), confirming aggressive selling absorption ($\ge 51\%$ taker buy) and rapid reclaim back into value.
   - **Wyckoff Upthrust / SFP**: Detects institutional distribution sweeps above 24h highs or Value Area High (`VAH`), confirming buy exhaustion ($<48\%$ taker buy) and immediate rejection below resistance.
   - **CVD Absorption / Exhaustion Divergences**: Evaluates 16h Cumulative Volume Delta slopes against price action to detect smart-money accumulation or distribution.
3. **Volatility-Adaptive Kelly Sizing Engine (`compute_adaptive_kelly`)**:
   - Dynamically scales standard Quarter-Kelly ($0.25 \times f^*$) based on real-time market regimes:
     - **TTM Squeeze Factor ($\kappa_{squeeze}$)**: Boosts sizing ($1.15x - 1.20x$) during Bollinger-inside-Keltner compression to harness explosive coiling energy; discounts ($0.70x$) during hyper-expansion to prevent whipsaws.
     - **ATR Expansion Ratio ($\kappa_{ATR}$)**: Inversely dampens risk ($\kappa_{ATR} = 1 / \sqrt{R_{ATR}}$) when candle volatility spikes ($R_{ATR} > 1.25$).
     - **VPIN Toxicity Adjustment ($\kappa_{VPIN}$)**: Slashes counter-trend risk ($0.50x$) when order-flow toxicity surges ($\ge 0.60$).

---

## 5. Phase 4 Smart Order Routing (SOR) & Multi-Venue Risk Guard (`sor.py`)

Phase 4 multi-venue execution and portfolio circuit breaker engine:
1. **Cross-Exchange Smart Order Router (`smart_route_order`)**:
   - Compares live execution across Binance (Spot & USDⓈ-M Perp), Bybit (Linear Perp), and OKX (Linear Swap).
   - Simulates CLOB depth ladder walking to estimate volume-weighted fill price (VWAP) and market impact slippage.
   - Calculates all-in execution cost including exchange maker/taker fee schedules and 24h projected funding carry.
   - Computes optimal multi-venue child-order splits for large notionals ($\ge \$5,000$) to minimize adverse selection and price impact.
2. **Institutional Portfolio Circuit Breakers (`evaluate_circuit_breakers`)**:
   - **Hard Drawdown Shutdown ($\ge 3.0\%$)**: Automatically blocks all new leveraged orders when portfolio drawdown touches 3.0% from the high-water mark (HWM).
   - **Caution Drawdown ($\ge 1.5\%$)**: Halves position sizing and enforces 50% leverage reduction.
   - **Consecutive Stop Runs Guard**: Enforces mandatory 60-minute cooling-off period upon 2 consecutive stop-outs within 4 hours.
   - **Gross Book Leverage Cap**: Enforces hard 2.0x portfolio equity ceiling across all active margin positions.
3. **Deribit Options Gamma Exposure (GEX) & Max Pain Engine (`fetch_deribit_options_analytics`)**:
   - Slices Deribit BTC and ETH options chains across all strikes to compute the **Max Pain Price Strike** where buyer payout is minimized.
   - Tracks Put/Call Open Interest Ratio (PCR) and dealer net gamma exposure ($M per 1% move).
   - Flags gravitational pinning hazards when spot prices rotate near Max Pain into monthly/quarterly expiries.

---

## 6. Phase 5 Canvas Liquidity Heatmap & Whale Block Trade Radar (`heatmap.py`)

Phase 5 high-performance trading desk observability and progressive web application:
1. **Order Book Depth Matrix & Heatmap Engine (`DepthHeatmapEngine`)**:
   - Maintains rolling time-series ring-buffer of discrete central limit order book snapshots.
   - Computes non-linear logarithmic volume density matrices across configurable price bounds ($\pm 1\%$ to $\pm 10\%$) and price bins (40 to 100 bins).
   - Maps support (bid) and resistance (ask) liquidity walls over time (Footprint / Liquidity Heatmap) alongside mid-price trajectory line.
   - Rendered via zero-lag 60 FPS HTML5 `<canvas>` with crosshairs and interactive price/volume hover tooltips.
2. **Institutional Whale Block Trade Radar (`BlockTradeDetector`)**:
   - Sub-second classification of aggressive market taker prints ($\ge \$100\text{K}$) into institutional tiers:
     - 🐋 `WHALE`: $\$100,000 - \$250,000$
     - 🐳 `SHARK`: $\$250,000 - \$500,000$
     - 🐙 `MEGALODON`: $\$500,000 - \$1,000,000$
     - 👑 `TITAN`: $\ge \$1,000,000$
   - Computes Whale Net Flow ($), cumulative aggressive Taker Buy vs Sell volume, and institutional trade dominance ratio.
   - Visualized via interactive Canvas floating bubble physics simulation and real-time whale tape.
3. **Mobile Progressive Web Application (PWA) & Native Notifications**:
   - Complete PWA implementation (`manifest.json` and `sw.js` Service Worker) with offline shell caching and standalone display.
   - Integrated native system push notifications and synthesized Web Audio chime for instant whale trade and margin liquidation alerts.

---

## 7. Phase 6 Multi-Agent AI Dialectical Debate, HMM Regimes & Pairs Stat-Arb (`ai_agent.py`, `regime_hmm.py`, `stat_arb.py`)

Phase 6 institutional artificial intelligence and advanced quantitative modeling:
1. **Multi-Agent Dialectical Debate Engine (`ai_agent.py`)**:
   - Synthesizes the **TradingAgents** multi-agent architecture (MIT / UCLA / Tauric Research):
     - **Quant Analyst Agent**: Evaluates order-book depth imbalance, VPIN flow toxicity, 7d Volume Profile (VAL/POC/VAH), 24h VWAP, and multi-timeframe indicators.
     - **Macro Catalyst Agent**: Scans Federal Reserve rates, ETF flows, on-chain whale transactions, and token cliff unlocks.
     - **Bull Advocate Agent**: Builds the affirmative case (CVD absorption, liquidity sweep reclaims, short squeeze pressure).
     - **Bear Advocate Agent**: Builds the adversarial counter-case (overhead supply walls, toxic flow adverse selection, liquidation cascades below).
     - **Chief Risk Officer Agent**: Synthesizes the debate into a Consensus Verdict (`STRONG BUY`, `TACTICAL LONG`, `NEUTRAL`, `TACTICAL SHORT`, `STRONG SELL`, `DE-RISK / HALT`), Net Conviction Score (-100 to +100), Crucial Thesis Invalidation Level, and Kelly Position Sizing Multiplier.
   - Run via CLI:
     ```bash
     python3 "${CLAUDE_SKILL_DIR}/ai_agent.py" BTCUSDT
     python3 "${CLAUDE_SKILL_DIR}/ai_agent.py" ETHUSDT --json
     ```

2. **3-State Gaussian Hidden Markov Model (HMM) Market Regime Classifier (`regime_hmm.py`)**:
   - Implements James D. Hamilton & Lawrence Rabiner's discrete-time regime-switching framework:
     - **Regime 0: Low-Vol Bullish Momentum / Trend Expansion**: Positive drift, tight spreads. Policy: Trend-following, ride to VWAP/VAH, 2.5R targets, 1.0x Kelly sizing.
     - **Regime 1: Mean-Reverting Range / Rotational Auction**: Bounded oscillation inside Value Area. Policy: Mean-reversion to POC, fade VAL/VAH extremes, 1.5R targets.
     - **Regime 2: Toxic Volatility Shock / Liquidation Cascade**: High variance, spiking VPIN, wide spreads. Policy: Defensive capital preservation, 0.25x half-Kelly or safety halt.
   - Computes Baum-Welch (EM) parameter estimation, Viterbi global path decoding, and Forward-Backward posterior probabilities P(S_t = k | O_{1:T}).
   - Run via CLI:
     ```bash
     python3 "${CLAUDE_SKILL_DIR}/regime_hmm.py" BTCUSDT --interval 1h --limit 150
     ```

3. **Dynamic Kalman Filter & Ornstein-Uhlenbeck (OU) Pairs Statistical Arbitrage (`stat_arb.py`)**:
   - Implements online state-space Kalman filtering and stochastic differential spread modeling:
     - **Online Kalman Filter**: Dynamically tracks time-varying hedge ratio beta_t and level offset alpha_t between cointegrated crypto pairs with zero look-ahead bias.
     - **Ornstein-Uhlenbeck SDE Calibration**: Models spread mean-reversion speed (theta), spread half-life (tau_1/2 = ln(2)/theta), equilibrium mean (mu), and stationary variance (sigma_eq^2).
     - **Actionable Execution Signals**:
       - Z <= -2.0: LONG_SPREAD (Buy Asset A, Sell beta Asset B)
       - Z >= +2.0: SHORT_SPREAD (Sell Asset A, Buy beta Asset B)
       - |Z| <= 0.25: MEAN_REVERSION_EXIT (Take profit on convergence)
       - |Z| >= 3.5: COINTEGRATION_BREAKDOWN_STOP (Structural break loss cut).
   - Run via CLI:
     ```bash
     python3 "${CLAUDE_SKILL_DIR}/stat_arb.py" --scan --interval 1h
     python3 "${CLAUDE_SKILL_DIR}/stat_arb.py" ETH/BTC --interval 1h
     ```

---

## 8. US Equities Earnings Analyst (`facts.py`)

Earnings move stocks more than anything else on the calendar, and the press
release is written to put the best number first. This skill reads the numbers
the company actually filed with the SEC, lines them up quarter by quarter, and
says plainly what changed. For a day trader it answers two questions before a
trade: what did the last report say, and is the next one inside this trade?

Tool in this folder (standard library Python, no keys):

```bash
python3 "${CLAUDE_SKILL_DIR}/facts.py" --help
```

It needs the SEC User-Agent (`sec_user_agent` in `trader/config.json`, or
`SEC_USER_AGENT`): "Your Name you@example.com" with the user's real name and
email. It refuses to call SEC without one.

## What it reads and writes

- Reads `https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json` (every
  number the company has tagged in its filings), the company's submissions
  list (for the newest 10-Q or 10-K and the newest 8-K with item 2.02,
  "Results of Operations") and that 8-K's folder to find the press release
  (the EX-99.1 exhibit).
- Writes `trader/metrics.json` with `--metrics`: one row per company (TTM
  revenue, net income and diluted EPS, the latest margins and growth), for
  your notes or a spreadsheet. `metrics.example.json` in this folder is a
  real run for AAPL, AXP, GOOGL, KO and BAC on 2026-10-13.
- Earnings calls: the tool does not fetch transcripts. If the user pastes one
  (or the company's IR page has it), read it and sum up what management said
  changed: guidance, margins, demand, anything they declined to answer.
- The next report date is not in SEC data. Look for it in `/trader-news`
  headlines or on the company's investor relations page, and say where it
  came from.

## The rules

- **Filed numbers first.** The table comes from XBRL facts the company filed.
  Use the press release for context (guidance, segment colour), and say which
  numbers came from where.
- **Compare like with like.** Growth is against the same quarter a year
  earlier. Quarter-on-quarter changes are shown but many businesses are
  seasonal (Apple's December quarter is always its biggest).
- **Say what is derived.** Companies do not file a Q4 10-Q. The fourth
  quarter is the 10-K year minus the three quarters inside it and is marked
  "derived"; derived EPS is approximate.
- **Say when the data lags.** SEC's facts API can trail a filing. When the
  newest 10-Q is later than the newest quarter in the facts, the tool prints a
  NOTE with the link: read the 10-Q itself before summarising.
- **Flag one-offs.** When net income is above operating income, gains outside
  the business (investment marks, tax items) are inside it. The tool says so.
  Banks and insurers have no gross margin: blank, not zero.

## Steps

1. Run it for the companies in question (or the tickers on
   `trader/watchlist.json`):

   ```bash
   python3 "${CLAUDE_SKILL_DIR}/facts.py" TSLA
   python3 "${CLAUDE_SKILL_DIR}/facts.py" TSLA NVDA AMD --metrics trader/metrics.json
   ```

   `--quarters 8` shows two years, `--json` everything machine-readable,
   `--cik` a company without a ticker.
2. Open the latest earnings press release (the EX-99 link) and the 10-Q when
   the numbers need context, and read them.
3. Write the summary: one line on the quarter (revenue, EPS, growth), one on
   margins, one on anything unusual (a one-off, a derived quarter, data that
   lags), and the links. Plain words, real numbers, no adjectives the numbers
   do not support.
4. If the next report falls inside the planned trade (an overnight hold, or a
   report before the open), say it before `/trader-exit` sets the stop: a stop
   does not protect against a gap, the stock can open past it.

## Example: the reel's stock

`facts.py TSLA` (run 2026-10-20): quarter ending 2026-06-30 (Q2 2026),
revenue $28.24B, +25.5% against a year earlier, net income $1.11B (-4.9%),
diluted EPS $0.32 (-3.0%); gross margin 16.8% (-4.3 points on the quarter
before), operating margin 1.4%; last four quarters $103.6B revenue and $1.08
EPS. The tool flags that net income is above operating income, so gains
outside the business are inside it. Latest 10-Q filed 2026-07-23. Monday's
headlines said Tesla reports this week, so the summary ends: "The next report
is days away. A day trade that closes before the bell is not exposed to it;
anything held overnight is."

`facts.py AAPL` (2026-10-13): revenue $109.42B, +16.4% against a year
earlier, diluted EPS $2.02 (+28.7%). For Coca-Cola the tool noted that SEC's
facts stopped at 2026-04-03 while the 10-Q for 2026-07-03 was already filed,
so the summary read that 10-Q directly.

## What it never does

- Never says a stock is cheap, expensive, a buy or a sell. It reports what
  the company filed.
- Never invents a number the filings do not have, or estimates a quarter it
  cannot derive.
- Never calls SEC without the User-Agent, and stays well under 10 requests a
  second.

## Safety

Education and tooling, not financial advice. This skill only reads filings.
Nothing places an order except `/trader-broker`, after `/trader-risk` passes
and the user types an explicit yes for that order.
