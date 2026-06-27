# YouTube Video Ideas — jerdev_quant

Angles pulled from real bot facts (no hype). Channel = "Intelligence Layer for Retail Traders" — interpretation + proprietary data, not signals.

> Best fits for the channel (your differentiator): **#2, #3, #4, #5** — nobody else has your benchmark-vs-random data or your own data-leakage case.

## Ideas

1. **"Built a bot that does NOT trade — on purpose"**
   Shadow-only since April 2026. Detects signals 24/7, computes theoretical outcome, places 0 real orders. Why: collect data before risking money. Counterintuitive = strong hook. Retail wants a robot that wins now; you show the opposite discipline.

2. **"The coin-flip that beat my strategy"**
   Engine1: 420 shadow trades, 44% WR. The brutal part: you run `random_direction` and `market_now` benchmarks alongside the "smart" signal. Your signal lost LESS than random but still doesn't win. Lesson: most retail strategies don't beat a coin flip. Strong visual.

3. **"Edge ≠ profit" — retail's #1 misconception**
   ML model AUC 0.78 (predicts better than chance = real edge) BUT strategy loses money. How both are true at once. Kills the "if you predict the market you win" myth.

4. **"The bug that inflated my model from 0.78 to 0.85"**
   Partial-candle bug: bot stored forming candles as closed → model "saw the future" → inflated metrics. You caught it, cleaned it, number dropped. Real data-leakage story. Almost nobody on YT explains it with their own case.

5. **"Backtest said Sharpe 1.72. Reality: 7 stops in a row"**
   Dual Thrust won in backtest, but recent replay = 7/7 SL, -14.5%. The backtest-vs-live gap firsthand. Viral topic in quant.

6. **"Why my bot runs from Europe, not the US"**
   OKX geo-blocks US harder than Canada. Real infra decision (Hetzner EU). Angle: the boring-but-critical part nobody teaches (where your bot lives matters).

7. **"A 29-hour crash from ONE bad datapoint"**
   OKX served 1 instrument with id=None → ccxt TypeError → 29h crash loop. How one null takes everything down. Lesson: error handling in 24/7 systems.

8. **"Architecture: 5 layers, anyone says NO = no trade"**
   Detection → Risk → Execution, same Python process. Deterministic philosophy. Good for a "how it's built inside" video.

9. **"I added AI (Claude) to the trades... then turned it off"**
   AI v1 destroyed Setup B (49%→21% WR), AI v2 approved 89.6% = useless. Honesty: AI added no value. Anti-hype, rare on YT, builds trust.

10. **"I track on-chain whales but the API dies on its own"**
    External free-API reality (mempool.space timeouts). The unglamorous side of depending on free external feeds.

## By-the-numbers (B-roll / on-screen stats) — measured 2026-06-18

- **Markets tracked live:** 7 OKX linear perps — BTC, ETH, SOL, DOGE, XRP, LINK, AVAX (/USDT).
- **Feeds:**
  - Candles via OKX native WebSocket, only *confirmed* (closed) bars processed. Stored across 7 timeframes: 1m / 5m / 15m / 30m / 1h / 4h / 1d.
  - Polling cadence: Open Interest every 5 min, Funding every 8 h, on-chain whales (Etherscan + mempool.space) every 5 min, news every 5 min.
- **Uptime:** current bot container up ~3 days, **0 restarts** (started 6/15 18:57 UTC). Postgres container up ~3 months, Redis ~2 months. (Note: 29h crash loop earlier in June from OKX null-id bug — now fixed.)
- **Data history depth:** candles span **2024-11-03 → now** (~19 months).
- **Postgres:** 25 tables, **267 MB**. Top tables by rows:
  - candles **571,796**
  - open_interest_history 189,800
  - cvd_history 189,198
  - bot_metrics 182,851
  - ml_setups 12,348
  - funding_rate_history 3,037
- **Distinct data types ingested (~8, not just 4):** candles, open interest, CVD (cumulative volume delta), funding rate, on-chain whale moves (ETH + BTC), liquidation cascades, news, signal-scanner alerts.
- **Pipeline latency (data → decision), N=3,798 samples:** avg **609 ms**, p50 **471 ms**, p95 **1,430 ms**, max 3,001 ms. (Measured live via `pipeline_latency_ms` in `bot_metrics`.)
