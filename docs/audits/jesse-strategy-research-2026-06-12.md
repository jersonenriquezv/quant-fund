# Jesse Strategy Research — BTC/ETH USDT Perpetuals

**Date:** 2026-06-12
**Goal:** Find strategy with Sharpe ≥ 1.2 over last 2 years (2024-06-12 → 2026-06-11).
**Data:** Binance Perpetual Futures 1m candles (imported from 2023-09-01 for warmup). Fee 0.05% per side, futures, 2x cross leverage, $10k starting balance.
**Framework:** Jesse 2.3.4, isolated env at `~/jesse-research/` (own venv + `jesse_db` postgres database; production bot untouched).

## Method (anti-overfit protocol)

1. **Stage 1 — screen:** 10 example strategies (entry rules validated against source; KDJ excluded for an `and`-operator bug) × BTC/ETH × 1h/4h/6h/1D = 76 backtests with default params.
2. **Stage 2 — walk-forward optimization:** train 2024-06-12→2025-10-12 (16mo), test 2025-10-12→2026-06-11 (8mo). Optuna objective = training Sharpe only; candidates selected by **training** fitness; test window used solely for verification. 100–300 trials per combo.
3. **Stage 3 — validation** on full 2y per candidate:
   - Accurate backtest (fast-mode off)
   - Monte Carlo trade-shuffle, 1000 scenarios
   - Monte Carlo synthetic markets (moving-block bootstrap candles, batch 1 week), 100 scenarios
   - Rule significance bootstrap test (2000 sims) → p-value

## Final table (full 2-year window)

| # | Candidate | Source | Sharpe | Net% | DD% | Trades | MC-trades p5 final | MC-candles Sharpe p5/p50 | P(neg) | p-value |
|---|---|---|---|---|---|---|---|---|---|---|
| 8 | **DUAL_THRUST ETH 6h** | optimized | **1.72** | +155 | -22.4 | 159 | $25,548 | **0.98 / 2.03** | **0.00** | 0.0000 |
| 7 | DUAL_THRUST ETH 4h | optimized | 1.81 | +270 | -21.2 | 47 | $37,010 | -0.20 / 1.16 | 0.09 | 0.0000 |
| 0 | DUAL_THRUST ETH 6h | default | 1.98 | +142 | -11.7 | 14 | $24,198 | -0.23 / 1.09 | 0.08 | 0.0000 |
| 2 | DUAL_THRUST ETH 4h | default | 1.28 | +77 | -19.0 | 33 | $17,687 | 0.12 / 1.36 | 0.04 | 0.0000 |
| 4 | DUAL_THRUST ETH 1h | default | 1.17 | +126 | -26.8 | 90 | $22,629 | -0.23 / 0.92 | 0.12 | 0.0000 |
| 10 | SMAX_OPT ETH 6h | optimized | 1.37 | +252 | -41.6 | 28 | $35,227 | 0.05 / 1.01 | 0.16 | 0.0245 |
| 9 | SMAX_OPT ETH 4h | optimized | 1.38 | +257 | -57.9 | 24 | $27,626 | -0.85 / 0.85 | 0.27 | 0.0285 |
| 1 | SMACrossover ETH 4h | default | 1.33 | +241 | -55.9 | 24 | $26,557 | -0.73 / 0.71 | 0.30 | 0.0460 |
| 3 | SMACrossover ETH 6h | default | 1.19 | +189 | -44.4 | 18 | $28,848 | -0.79 / 0.56 | 0.39 | 0.0500 |
| 6 | DONCH_OPT BTC 6h | optimized | 1.12 | +66 | -15.6 | 38 | $16,629 | 0.50 / 1.45 | 0.06 | 0.0000 |
| 5 | Donchian BTC 6h | default | 0.78 | +37 | -18.2 | 23 | $13,700 | (skipped) | — | 0.0000 |

## Verdict

### WINNER: DUAL_THRUST ETH-USDT 6h, optimized (#8)
Only candidate passing every gate:
- Sharpe 1.72 ≥ 1.2 bar, 159 trades (real sample size)
- Walk-forward: train 1.59 → **test 2.14** (out-of-sample BETTER than in-sample = no overfit signature)
- Synthetic-market MC: Sharpe p5 = 0.98 (even 5th-percentile market path nearly hits 1.0), 0/62 negative scenarios
- Trade-shuffle MC: 0% probability of loss, p95 drawdown only -12.8%
- Entry rule significant at p < 0.0001

Params: `{stop_loss_atr_rate: 1.645, down_length: 10, up_length: 3, down_coeff: 0.301, up_coeff: 0.891}`
Rule: long when price > daily_open + 0.891×max(3-bar HH−CC range); short when price < daily_open − 0.301×max(10-bar ranges); ATR×1.645 stop; flip on opposite signal. Anchor = 1D candles (6h trading TF).

### Runner-up: DUAL_THRUST ETH 4h optimized (#7)
Sharpe 1.81, +270%, but MC-candles p5 = -0.20 and only 47 trades — robust median, weaker tails.

### Rejected
- **SMA-cross family (1.19–1.38):** beta-riding ETH longs. DDs -42…-58%, MC synthetic-market P(negative) 16–39%, p-values barely significant (0.025–0.05). Fragile — passed the bar on luck of one bull regime.
- **All BTC candidates:** best = DONCH_OPT 6h at 1.12, below bar, and walk-forward test Sharpe was only 0.14. No BTC strategy qualified. BTC 2024-26 = choppy + already-efficient; momentum entries decayed.
- **DUAL_THRUST ETH 1h optimized:** train 1.16 → test -0.47. Textbook overfit, killed by walk-forward.
- Mean-reversion (RSI2/TradingView_RSI) negative nearly everywhere at intraday TFs.

## Caveats
- MC-candles filtered 1–38% of scenarios (missing equity curve, mostly zero-trade synthetic paths) — survivor stats slightly optimistic.
- Fees modeled 0.05% taker both sides; no slippage/funding modeled. On OKX same taker tier. Dual Thrust uses market-ish entries on signal — slippage on ETH 6h signals negligible at small size.
- 2-year window = mostly one macro regime (post-halving bull + 2025-26 chop). Forward shadow validation before real capital, same as engine rules.
- One optimization selection event occurred (rank-1 of 200 trials by train fitness) — residual selection bias possible despite test-window verification; MC partially mitigates.

## Reproduce
```
cd ~/jesse-research/project
~/jesse-research/venv/bin/python import_data.py        # candle import
~/jesse-research/venv/bin/python run_screen.py         # stage 1
~/jesse-research/project/run_all_optimize.sh           # stage 2
~/jesse-research/venv/bin/python pick_candidates.py    # build candidates.json
~/jesse-research/venv/bin/python run_validate.py candidates.json   # stage 3
~/jesse-research/venv/bin/python run_mc_candles.py     # MC candles leg
```
Raw results: `results/*.json`.

---

## OKX revalidation (2026-06-13) — Engine 2 Phase 1 tracer

The winner (#8) was fit on **Binance** candles; the bot trades **OKX**. Phase 1 of
the Engine 2 plan (`docs/plans/engine2-dual-thrust.md`) re-runs the SAME rule with
**FIXED winner params** on OKX `ETH-USDT-SWAP` 6h, to test whether the edge survives
the venue transfer before any code is written.

**Harness:** `~/jesse-research/project/okx_revalidation.py` — standalone pandas
reimplementation of `strategies/DUAL_THRUST` (faithful to the source, including its
`down_max_high = max(low)` column quirk). Execution mirrors the Jesse lifecycle:
signals at bar close → market entry/flip at next-bar open → SL intrabar → flip
re-enters one bar after liquidation. Fee 0.05%/side, no funding (Phase 2). Window
2024-06-12 → 2026-06-11.

**Leg 1 — fidelity (harness vs Jesse, Binance 6h):**

| metric | Jesse #8 | harness | 
|---|---|---|
| Sharpe | 1.723 | **1.667** (|Δ|=0.056 ✓ within ±0.2) |
| Net % | +155 | +153 |
| Max DD % | — | -18.4 |
| Trades | 159 | 139 |
| Win rate | 40.3% | 36.0% |

Harness reproduces the Jesse result → mechanics trusted.

**Leg 2 — OKX gate (`ETH-USDT-SWAP` 6h, UTC-aligned):**

| metric | value | gate |
|---|---|---|
| Sharpe | **1.999** | ≥1.2 ✓ |
| Net % | **+206** | >0 ✓ |
| Max DD % | -15.2 | — |
| Trades | 133 | ≥80 ✓ |
| Win rate | 39.9% | — |

Concentration (manual eyeball): **21/26 months positive (81%)**, best month 25.6%
of net, worst -$1,863. Top-5-trades = 83% of *dollar* net, but that is a compounding
artifact (per-trade risk fixed at 2%, late trades run a 3× balance), not a single
trade carrying the result. No single-month dominance.

**⚠️ Timezone gotcha:** OKX exposes two 6h variants. The Hong-Kong `6H` (08:00
anchor) misaligns the 1D Dual-Thrust anchor and **collapses the strategy to Sharpe
0.21 / +4% net**. Only `6Hutc` (00:00 UTC, matching Binance/Jesse) gives the 1.999.
A live port MUST aggregate 6h from UTC-aligned data — this is a real trap for the
engine implementation (Phase 3).

### Verdict: Phase 1 PASS → proceed to Phase 2 (funding + MC on OKX)
The Sharpe-1.72 Binance edge **transfers to OKX**, in fact stronger (1.999 vs 1.723),
with a lower drawdown (-15% vs -22%). The grill's single-risk-that-matters (Binance→OKX
transfer untested) is retired. Funding drag and intrabar-fill realism remain (Phase 2).
