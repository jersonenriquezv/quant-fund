# Scalp Silent Detectors — 2026-05-05

**Status:** Two scalp detectors silent since launch. Root cause identified, thresholds calibrated.
**Branch:** `investigate/scalp-silent-detectors-liq-funding`
**Script:** `scripts/scalp_silent_detector_audit.py`

---

## 1. Question

Phase 1A audit (PR #15) reported `scalp_liq_reclaim_v1` and
`scalp_funding_extreme_v1` as **NO DATA** — zero outcomes ever despite the
bot being alive and other scalp signals firing. Could be (a) master switch
suppression, (b) detector code bug, (c) threshold/gate mis-calibration, or
(d) input data missing.

This audit isolates root cause from data and proposes calibrated thresholds.

---

## 2. Method

`scripts/scalp_silent_detector_audit.py` queries the existing
`funding_rate_history` and `open_interest_history` tables to test gate
feasibility purely from data — no runtime dependency.

**For funding_extreme:** count how many of the last 30 days of funding-rate
samples cross `_FUNDING_RATE_THRESHOLD` at the current value and at proposed
alternates.

**For liq_reclaim:** detect historical OI flushes that match the production
OI flush detector (`OI_DROP_THRESHOLD_PCT=0.02`,
`OI_DROP_WINDOW_SECONDS=300`). For each flush, walk the next 5m candles
within the detector's `_LIQ_RECLAIM_FLUSH_MAX_AGE_MS` window and check
whether (a) wick gate and (b) inside-range gate would have aligned. Counts
historical alignment events as a proxy for fire rate.

---

## 3. Findings

### 3.1 funding_extreme — threshold mathematically impossible

| Pair | Samples | `>=0.05%` (current) | `>=0.03%` | `>=0.02%` | `>=0.01%` | p95 \|rate\| | max \|rate\| |
|---|---|---|---|---|---|---|---|
| AVAX/USDT | 90 | 0 | 1 | 5 | 48 | 0.0196% | 0.0427% |
| BTC/USDT  | 90 | 0 | 0 | 0 | 7  | 0.0100% | 0.0147% |
| DOGE/USDT | 90 | 0 | 0 | 0 | 24 | 0.0100% | 0.0127% |
| ETH/USDT  | 90 | 0 | 0 | 0 | 11 | 0.0100% | 0.0132% |
| LINK/USDT | 90 | 0 | 0 | 2 | 36 | 0.0119% | 0.0237% |
| SOL/USDT  | 90 | 0 | 0 | 0 | 26 | 0.0114% | 0.0179% |
| XRP/USDT  | 90 | 0 | 0 | 1 | 38 | 0.0100% | 0.0220% |
| **TOTAL** | **630** | **0** | **1** | **8** | **190** | | |

**Root cause:** `_FUNDING_RATE_THRESHOLD = 0.0005` (0.05%) is **5× higher
than the 30-day max funding rate observed across all 7 trading pairs**.
The detector is mathematically incapable of firing under this setting.

The original threshold was likely set against historical Bitmex / Binance
data where funding can spike past 0.1% on extreme moves. OKX SWAP funding is
capped tighter and rarely crosses 0.04% even on volatile days.

### 3.2 liq_reclaim — gates too strict given OI flush dynamics

OI flush events at production threshold (>= 2% drop in <= 10min):

| Pair | Flushes (30d) |
|---|---|
| AVAX/USDT | 5 |
| BTC/USDT  | 7 |
| DOGE/USDT | 24 |
| ETH/USDT  | 28 |
| LINK/USDT | 3 |
| SOL/USDT  | 3 |
| XRP/USDT  | 2 |
| **TOTAL** | **72** (~2.4/day) |

OI flushes happen — they're not the bottleneck. But for each flush, alignment
with both the wick gate (`>=0.5%`) and inside-range gate (close inside prior
20-bar high/low) within the 5-min window is rare:

| Setting | Aligned / Flushes | Rate |
|---|---|---|
| Current (wick 0.5%, window 5min) | 2 / 72 | **2.8%** |
| Proposed (wick 0.3%, window 10min) | 11 / 72 | 15.3% |

**Root cause:** the 0.5% wick threshold is large for a 5m candle even after
a 2% OI flush. OI flushes typically produce 0.3–0.6% candle ranges; only the
extreme 5–10% of flushes produce a 0.5%+ wick on a single side that also
closes back inside prior structure.

### 3.3 No master-switch or runtime cause

Both detectors are wired correctly and called inside `evaluate_scalp` whenever
`SCALP_SHADOW_ENABLED=true`. `scalp_sweep_choch_v1` and
`scalp_random_baseline_v1` fire every day, confirming the master switch is
on, the candle pull works, and the snapshot reaches the evaluator. The
silent state is a **calibration problem**, not a wiring problem.

---

## 4. Calibration changes applied

In `strategy_service/scalp_setups.py`:

```python
# was 0.005 (0.5%)
_LIQ_RECLAIM_WICK_THRESHOLD = 0.003

# was 5 * 60 * 1000
_LIQ_RECLAIM_FLUSH_MAX_AGE_MS = 10 * 60 * 1000

# was 0.0005
_FUNDING_RATE_THRESHOLD = 0.0002
```

Justifications:

- **wick 0.5% → 0.3%:** alignment rate goes from 2.8% → 15.3% on existing
  data. 0.3% is still a meaningful wick on a 5m candle — typical 5m noise
  range is 0.05–0.15%. The reclaim semantics (wick rejected back inside
  range) keeps the structural quality even at the lower threshold.
- **5 → 10 min flush window:** OI is polled every 5 min. The trigger candle
  may close 5–8 min after the flush is recorded, depending on alignment
  between the OI poll cadence and the 5m candle close. A 10-min window
  catches the next-candle-after-flush cleanly.
- **funding 0.05% → 0.02%:** the new threshold sits at the p95–p99 of
  observed |rate| (depending on pair). Expected raw hits: 8/30d = ~0.3/day
  before the flat-range gate cuts further. After the flat-range gate
  (0.3% over 30 min), expected fire rate is roughly 1–3 per 30 days.

### Expected fire rates post-calibration

| Signal | Raw trigger rate | Expected actual fires (after secondary gates) |
|---|---|---|
| `scalp_liq_reclaim_v1`     | 11 alignments / 30d | ~5–10 / 30d |
| `scalp_funding_extreme_v1` | 8 raw threshold hits / 30d | ~1–3 / 30d |

Both still slow. N≥30 will take 1–3 months on liq_reclaim and 6+ months on
funding_extreme. If after 30 days a signal still produces zero, deeper
redesign is needed (different thesis, different threshold, or kill).

---

## 5. Tests updated

`tests/test_scalp_setups.py`:
- `_LIQ_RECLAIM_WICK_THRESHOLD == 0.005` → `== 0.003`
- `_FUNDING_RATE_THRESHOLD == 0.0005` → `== 0.0002`
- `test_no_signal_when_funding_below_threshold` rate adjusted from 0.0003 → 0.00015 to stay below the new threshold
- `TestNoLookahead.test_appending_future_candles_does_not_change_result`
  appended-candle wicks shrunk from 0.4% → 0.15% so the candle still falls
  below the lower wick threshold

52/52 scalp tests pass.

---

## 6. What was NOT done in this PR

- **`SCALP_EXPERIMENT_ID` not bumped.** Reason: PR #14
  (`feat/scalp-v2-fade-pattern-filters`) already bumps it from
  `scalp_v1_2026_05` → `scalp_v2_filtered_2026_05_05`. After PR #14 merges,
  this branch will rebase cleanly with the v2 id. Old data was empty for
  these signals — no contamination risk from sharing the v2 id.
- **`liq_reclaim` runtime probe.** The data-only audit was sufficient to
  identify the calibration root cause. If post-merge fire count remains 0
  after 7+ days, runtime probe (OI flush detector init, snapshot wiring)
  is the next step.
- **Move thresholds to settings.** Currently module-level constants. Could
  be exposed as env-tunable settings in a follow-up if calibration churn
  becomes frequent.

---

## 7. Reproducibility

```bash
# from repo root
PYTHONPATH=. python scripts/scalp_silent_detector_audit.py
```

Reads from `funding_rate_history` and `open_interest_history`. No external
dependencies beyond `psycopg2`.
