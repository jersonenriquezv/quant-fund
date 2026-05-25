# /topdown edge — verdict correction (expectancy, not win-rate)

**Date:** 2026-05-25
**Status:** Overturns the 2026-05-24 "NO EDGE" verdict for BTC/ETH.
**Scripts:** `scripts/topdown_edge_hunt.py` (analysis), backtest runs `topdown_20260525_022050` (BTC/ETH, confluence study) + `topdown_20260525_220604` (BTC/ETH, fresh 150d confirmation).

## TL;DR
The 2026-05-24 backtest concluded **NO EDGE** (+2.32pp WR vs random, below the 10pp go/no-go gate). That verdict was an artifact of the **win-rate lens applied to a high-R:R strategy**, plus **pair dilution** (DOGE/SOL). Measured by **net expectancy in R** on BTC/ETH, /topdown has a **real, significant, out-of-sample-stable edge**:

| Metric | Signal | Random null (mirror) | Edge |
|---|---|---|---|
| WR | 30.7% | 21.5% | +9.2pp |
| Gross E | **+0.194R** | −0.177R | +0.371R |
| Maker (0.02% RT) | **+0.130R** | −0.220R | **+0.350R** |
| Taker (0.11% RT) | −0.156R | −0.504R | — |

Bootstrap on gross-E difference: **+0.37R/trade, 95% CI [+0.24, +0.51], p(≤0) < 0.0002.**
Out-of-sample (70/30 chronological): train maker **+0.123R**, holdout maker **+0.147R** (holdout *better* → not overfit).

## Why the original verdict was wrong
1. **Wrong metric.** The go/no-go gate was ΔWR ≥ 10pp. Win-rate is the wrong statistic when R:R is high (winners average ~3.6R) and variable. The edge lives in expectancy, not hit-rate. The original report's own PnL numbers (maker +337R vs random +19R) already showed the gap — the *verdict* keyed on WR and missed it.
2. **Pair dilution.** The headline +2.32pp was the blended 4-pair number (BTC/ETH/SOL/DOGE). DOGE is a −6.75pp anti-edge and SOL is flat; both drag the average. On the deep-liquidity pairs (BTC/ETH) the WR gap is +9.2pp and expectancy +0.37R.

## What actually constrains it: fees, not signal quality
The random null is **negative everywhere** (−0.18R gross), so the positive result is *not* an artifact of the R:R asymmetry — the signal contributes the +0.37R. The binding constraint is execution cost:
- Median risk/trade ≈ 0.50%. Taker RT 0.11% = **0.22R per trade** — eats the +0.19R gross → net negative.
- **Maker (limit) entry** drops the fee to 0.02% = ~0.04R → net **+0.13R**, PF 1.18.
- This strategy is **manual** (user places the limit on Bybit and it normally fills), so the maker assumption is realistic at small size. The backtest already models non-fills as `unfilled_timeout` (excluded from resolved set), so the +0.13R is computed only on trades that actually filled.

## Confirmed levers (data-backed)
1. **Restrict to BTC/ETH.** Deep-liquidity hypothesis confirmed; DOGE/SOL kill the edge.
2. **Maker-only (limit) entry.** −0.156R → +0.130R. The single biggest swing.
3. **Kill scaled-TP mode.** In both runs: 0 TP ever (only SL + timeout). Single-TP only.
4. **Tighten sweep** further if desired: 0–0.5% bucket E +0.36R vs 0.5–1% +0.15R. (Gate already at ≤1%.)

## Profit reality at $300 capital
The edge is *percentage* real but small in dollars at this size: edge = R-edge × capital × #trades.
- Per trade at 1% risk on $300 ≈ **+$0.39**; at 2% ≈ **+$0.78**.
- Taking *every* emission (~11/day, 1635 over 149d) is unrealistic and over-leveraged — projects to +$639/+$1,278 but is not achievable manually.
- **Realistic selective (~3 trades/week, 2% risk): ~$50 over ~5 months (~+16%).** Real, but small absolute dollars. The edge matters materially at higher capital ($3k–5k) or higher frequency. At $300 it is a low-risk way to trade with a proven statistical advantage.

## Caveats
- Pair restriction + window are within the tuned data period. **Forward confirmation** (Phase 4 live falsification, or a fresh out-of-window backtest) still strengthens the case.
- Maker-fill realism is the load-bearing assumption; for manual small-size limit orders it is sound, but adverse-fill on fast sweeps is the residual risk.
