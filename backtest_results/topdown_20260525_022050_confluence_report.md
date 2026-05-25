# /topdown Confluence Reliability — topdown_20260525_022050

**Generated:** 2026-05-25 02:45 UTC
**Trades:** backtest_results/topdown_20260525_022050_trades.csv (N=3104)
**Baseline WR (all emissions, resolved):** 27.56% (511/1854)

## 1. WR conditional on each confluence (present vs absent)

`lift` = WR(present) − WR(absent). Positive lift = the signal selects winners.

| Confluence | N present | WR present | N absent | WR absent | Lift |
|---|---|---|---|---|---|
| fvg_aligned | 2851 (1695 res) | 28.26% | 253 (159 res) | 20.13% | +8.13pp |
| ob_aligned_near | 2077 (1263 res) | 30.01% | 1027 (591 res) | 22.34% | +7.67pp |
| structure_flip | 1318 (845 res) | 31.12% | 1786 (1009 res) | 24.58% | +6.55pp |
| wick_tap_aligned | 95 (63 res) | 33.33% | 3009 (1791 res) | 27.36% | +5.97pp |
| htf_4h_aligned | 2332 (1347 res) | 28.43% | 772 (507 res) | 25.25% | +3.19pp |
| conf_high | 1652 (940 res) | 27.13% | 1452 (914 res) | 28.01% | -0.88pp |
| impulse_aligned | 191 (95 res) | 26.32% | 2913 (1759 res) | 27.63% | -1.31pp |
| htf_1h_aligned | 2549 (1490 res) | 26.31% | 555 (364 res) | 32.69% | -6.38pp |
| inducement | 2626 (1557 res) | 26.46% | 478 (297 res) | 33.33% | -6.87pp |
| ltf_15m_aligned | 2275 (1364 res) | 25.51% | 829 (490 res) | 33.27% | -7.75pp |

## 2. WR by confluence count

Count = number of positive confluences present (structure_flip excluded).

| Count | N | resolved | TP | WR |
|---|---|---|---|---|
| 2 | 25 | 19 | 1 | 5.26% |
| 3 | 110 | 75 | 16 | 21.33% |
| 4 | 655 | 383 | 115 | 30.03% |
| 5 | 920 | 615 | 186 | 30.24% |
| 6 | 751 | 415 | 103 | 24.82% |
| 7 | 602 | 322 | 84 | 26.09% |
| 8 | 41 | 25 | 6 | 24.00% |

## 3. WR at/above each confluence threshold (cumulative)

Answers: 'if I required ≥N confluences, what WR + how many trades survive?'

| Min count | N (survive) | resolved | WR | % of emissions kept |
|---|---|---|---|---|
| ≥2 | 3104 | 1854 | 27.56% | 100.0% |
| ≥3 | 3079 | 1835 | 27.79% | 99.2% |
| ≥4 | 2969 | 1760 | 28.07% | 95.7% |
| ≥5 | 2314 | 1377 | 27.52% | 74.5% |
| ≥6 | 1394 | 762 | 25.33% | 44.9% |
| ≥7 | 643 | 347 | 25.94% | 20.7% |
| ≥8 | 41 | 25 | 24.00% | 1.3% |

## 4. Out-of-sample validation (70/30 chronological split)

The §1 lifts are **in-sample**. A gate only matters if its edge survives on unseen data. Train = first 70% chronologically, holdout = last 30%. If holdout WR collapses to the holdout baseline, the gate is overfit.

| Gate | Train res | Train WR | Holdout res | Holdout WR |
|---|---|---|---|---|
| baseline (all) | 1330 | 27.1% | 524 | 28.8% |
| fvg_aligned | 1215 | 27.8% | 480 | 29.4% |
| ob_aligned_near | 896 | 30.8% | 367 | 28.1% |
| fvg AND ob | 808 | 31.7% | 330 | 28.2% |
| fvg AND ob AND structure_flip | 386 | 34.5% | 129 | 27.1% |

## 5. Reading

- Strongest single confluence by in-sample lift: **fvg_aligned** (+8.13pp) — but see §4 before trusting it.
- **Confluence COUNT is not monotonic** (§2): WR peaks at 4-5 then falls. Stacking confluences past 5 dilutes, because several tags (ltf_15m_aligned, htf_1h_aligned, inducement) carry NEGATIVE lift — they are anti-signals, not confluences. 'Require more confluences' is the wrong frame.
- **Decision rule:** trust a gate only if its holdout WR in §4 clears the holdout baseline by a margin comparable to its in-sample lift. If holdout collapses to baseline, the gate is an in-sample artifact — do NOT build it into /topdown.
