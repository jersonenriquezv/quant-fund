# SL Post-Mortem — Engine 1 + setup_f
**Date:** 2026-05-20
**Window:** last 14 days
**Candle TF for MFE/MAE:** 5m
**Source plan:** docs/plans/_archive/sl-classifier-postmortem.md
**Source grill:** docs/grill/_archive/one-step-down-cascade-2026-05-20.md

## Methodology

For each `shadow_sl` outcome, fetch 5m candles between fill_candle_ts and resolve_candle_ts. Compute Max Favorable Excursion (MFE) and Max Adverse Excursion (MAE) in R units, where R = |entry - sl|. Apply classifier in `scripts/classify_sl_failures.py`. Sanity: MAE must be ≥1.0R for every SL row (since outcome=SL means price reached SL).

## Class definitions

- `sl_too_tight_noise`: MFE ≥ 0.7R — price travelled most of the way to TP1 then reversed. Suggests SL inside noise distance.
- `wrong_direction`: MFE < 0.3R AND MAE ≥ 1.0R AND impulse_purity ≤ 0.85 — market moved against trade immediately.
- `late_entry`: MFE < 0.3R AND MAE ≥ 1.0R AND impulse_purity > 0.85 — entered at impulse terminus.
- `wrong_zone`: htf_bias undefined at entry.
- `counter_trend_valid`: HTF aligned, MFE in 0.3-0.7R range. Avoidable only via fee/sizing not detector.
- `unclassified`: rules did not cover.

## Results

### engine1_trend_pullback

**N (SLs):** 34

**Class distribution:**

| Class | Count | % |
|---|---|---|
| sl_too_tight_noise | 11 | 32.4% |
| wrong_direction | 13 | 38.2% |
| counter_trend_valid | 10 | 29.4% |

**Top examples per class (worst MAE):**

_sl_too_tight_noise_:

| pair | dir | entry | sl | R% | MFE_R | MAE_R | min |
|---|---|---|---|---|---|---|---|
| ETH/USDT | short | 2110.7150 | 2119.9715 | 0.44% | 1.00 | 1.31 | 425 |
| LINK/USDT | short | 9.5375 | 9.6052 | 0.71% | 0.73 | 1.31 | 5 |
| LINK/USDT | short | 9.3790 | 9.4593 | 0.86% | 0.91 | 1.22 | 60 |

_wrong_direction_:

| pair | dir | entry | sl | R% | MFE_R | MAE_R | min |
|---|---|---|---|---|---|---|---|
| BTC/USDT | short | 79383.8500 | 79580.5850 | 0.25% | 0.24 | 1.74 | 20 |
| SOL/USDT | short | 90.1150 | 90.5055 | 0.43% | 0.06 | 1.19 | 35 |
| AVAX/USDT | short | 9.7735 | 9.8180 | 0.46% | 0.15 | 1.13 | 5 |

_counter_trend_valid_:

| pair | dir | entry | sl | R% | MFE_R | MAE_R | min |
|---|---|---|---|---|---|---|---|
| BTC/USDT | short | 78080.4000 | 78172.3600 | 0.12% | 0.33 | 1.25 | 50 |
| ETH/USDT | short | 2175.5250 | 2182.6035 | 0.33% | 0.31 | 1.24 | 110 |
| SOL/USDT | short | 84.6100 | 85.1600 | 0.65% | 0.62 | 1.24 | 15 |

**Modal class:** `wrong_direction` (13/34 = 38.2%)

Modal failure = trade went against intended direction immediately. Suggests bias detection at entry was wrong. Candidate fixes: (a) tighten HTF bias requirement (require 4H confirm not 1H-alone); (b) add 1D bias as veto layer. This is where partial-OSD reasoning starts to have edge — but only a 1D veto, not a 4-step cascade.


### setup_f

**N (SLs):** 5

**Class distribution:**

| Class | Count | % |
|---|---|---|
| wrong_direction | 2 | 40.0% |
| counter_trend_valid | 1 | 20.0% |
| unclassified | 2 | 40.0% |

**Top examples per class (worst MAE):**

_wrong_direction_:

| pair | dir | entry | sl | R% | MFE_R | MAE_R | min |
|---|---|---|---|---|---|---|---|
| LINK/USDT | short | 10.2835 | 10.4613 | 1.73% | 0.11 | 1.03 | 515 |
| LINK/USDT | short | 10.2835 | 10.4613 | 1.73% | 0.09 | 1.03 | 510 |

_counter_trend_valid_:

| pair | dir | entry | sl | R% | MFE_R | MAE_R | min |
|---|---|---|---|---|---|---|---|
| LINK/USDT | short | 10.2835 | 10.4584 | 1.70% | 0.31 | 1.05 | 530 |

_unclassified_:

| pair | dir | entry | sl | R% | MFE_R | MAE_R | min |
|---|---|---|---|---|---|---|---|
| SOL/USDT | long | 90.9400 | 88.9021 | 2.24% | 0.36 | 1.14 | 500 |
| LINK/USDT | long | 10.2925 | 10.1209 | 1.67% | 0.57 | 1.12 | 485 |

**Modal class:** `wrong_direction` (2/5 = 40.0%)

Modal failure = trade went against intended direction immediately. Suggests bias detection at entry was wrong. Candidate fixes: (a) tighten HTF bias requirement (require 4H confirm not 1H-alone); (b) add 1D bias as veto layer. This is where partial-OSD reasoning starts to have edge — but only a 1D veto, not a 4-step cascade.

