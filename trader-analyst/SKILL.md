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

## 4. US Equities Earnings Analyst (`facts.py`)

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
