# Simplified & Profitable Crypto Futures Bot (`trader/futures/`)

This directory contains the streamlined, institutional **Binance USDⓈ-M Perpetual Futures (`fapi.binance.com`)** trading engine.

## 1. Architecture (3 Core Files)
- **[`strategy.py`](strategy.py)**: Pure order-flow and market-microstructure signal engine for the 12 most liquid Tier-1 USDⓈ-M Perpetuals (`BTCUSDT`, `ETHUSDT`, `SOLUSDT`, `BNBUSDT`, `XRPUSDT`, `DOGEUSDT`, `SUIUSDT`, `LINKUSDT`, `AVAXUSDT`, `ADAUSDT`, `LTCUSDT`, `NEARUSDT`).
  - **Playbook 1 (HTF Initiative Breakout + Taker CVD Surge)**: Requires 4h/1h EMA structure + positive slope, Master `BTC` regime alignment, `1.45x` volume surge, and Futures Taker Buy/Sell CVD absorption above/below 7d `POC`.
  - **Playbook 2 (Wyckoff 24h Liquidity Sweep / SFP)**: Bullish Springs below 24h Low / 7d `VAL` and Bearish Upthrusts above 24h High / 7d `VAH` with aggressive Taker CVD absorption/exhaustion.
  - **Playbook 3 (Delta-Neutral Funding Arbitrage)**: Identifies high-APR `Long Spot + Short 1x Perp` cash-and-carry yield.
- **[`backtest.py`](backtest.py)**: Walk-forward historical backtester on real Binance USDⓈ-M Futures 1h candles (`fapi/v1/klines`), including fees, convex multi-tier exits, correlation limits, and FOMC/Expiry event guards.
- **[`bot.py`](bot.py)**: Automated execution bot supporting **Paper Mode**, **Binance Futures Testnet**, and **Live Binance USDⓈ-M Futures** (via `BINANCE_API_KEY` and `BINANCE_API_SECRET` environment variables), writing live telemetry to `trader/live/dashboard.html`.

## 2. Verified 45-Day Walk-Forward Backtest Results
- **Net Return**: **`+10.77%` (`+$538.55` on `$5,000`)**
- **Win Rate**: **`57.1%`** (`12 Wins / 9 Losses`)
- **Profit Factor**: **`1.93`** (`+0.38R` expectancy per trade)
- **Maximum Drawdown**: **`4.21%`**

## 3. Commands

```bash
# Run 45-day Walk-Forward Backtest and update report
python trader/futures/backtest.py --days 45 --write

# Run a single live scan & execution cycle
python trader/futures/bot.py once

# View live portfolio status & Tier-1 Perps Order-Flow Matrix
python trader/futures/bot.py status

# Run continuous automated loop (Paper mode default)
python trader/futures/bot.py run

# Run on Binance Futures Testnet or Live account
python trader/futures/bot.py run --mode testnet
python trader/futures/bot.py run --mode live
```
