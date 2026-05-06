# Scalp Fee Viability — Phase 1A — 2026-05-05

**Status:** Investigation complete. Verdicts per signal below.
**Branch:** `feat/scalp-fee-viability-phase1a`
**Script:** `scripts/scalp_fee_viability.py`
**Sample:** All scalp `ml_setups` rows across experiments (`scalp_v1_2026_05`, `redesign_pre_2026_04_27`, `engine1_eth_short_v1b_2026_05_04`).

---

## 1. Question

Track 1 Phase 1A from the /ultraplan: do current scalp signal TP/SL parameters
yield positive expectancy under realistic fee models? Identify which signals to
keep, which to redesign, which to kill.

Three fee models considered (round-trip, including both legs):

| Model | RT cost | Notes |
|---|---|---|
| taker + taker | 0.10% | OKX VIP-0 SWAP, current shadow assumption |
| maker + taker | 0.07% | post_only entry, market exit on TP/SL |
| maker + maker | 0.04% | post_only entry AND exit; SL still market-emergency |

OKX VIP-0 SWAP fees: taker 0.05%, maker 0.02% per leg. No maker rebate at this
tier. Maker rebate available only at VIP-1+ (~$1M monthly volume) — not
reachable at $86 capital. Drop those scenarios.

---

## 2. Method

The script (`scripts/scalp_fee_viability.py`) does four passes:

**Part 1 — Theoretical breakeven WR (binary TP/SL only).**
For each signal in `SCALP_SIGNAL_PARAMS`, compute the breakeven win-rate
required to break even *if* every trade resolved as TP or SL (ignoring BE and
time-stops). Closed form:

```
WR = (sl + fee_rt) / (tp + sl)
```

This is the floor — the realistic breakeven (Part 3) is higher because BE and
time-stop outcomes also pay fees but produce no offsetting wins.

**Part 2 — Observed v1 outcomes.**
Aggregate all `ml_setups` rows per `setup_type` across every `experiment_id`,
filter to terminal outcomes (`shadow_tp`, `shadow_sl`, `shadow_breakeven`,
`shadow_time_stop`, `shadow_timeout`).

**Part 3 — Realistic breakeven WR with observed BE+TS drag.**
Hold the observed BE-rate and time-stop-rate fixed; solve for the TP rate that
makes EV = 0:

```
EV = p_tp × (tp - fee)
   + p_sl × -(sl + fee)
   + p_be × -fee
   + p_ts × (avg_ts_pct - fee)
```

`avg_ts_pct` is grossed up by adding back the current taker×2 fee (0.10%)
since `pnl_pct` in the database already deducts it.

**Part 4 — Verdict.** Per-signal classification: VIABLE / MARGINAL / KILL /
NO DATA / STARVED based on observed WR vs maker+maker breakeven.

---

## 3. Results

### 3.1 Theoretical breakeven (Part 1)

| Signal | TP% | SL% | R:R | BE-WR taker | BE-WR mk+ta | BE-WR mk+mk |
|---|---|---|---|---|---|---|
| `scalp_liq_reclaim_v1`     | 0.40 | 0.20 | 2.00 | 50.0% | 45.0% | 40.0% |
| `scalp_sweep_choch_v1`     | 0.30 | 0.15 | **2.00** | **55.6%** | **48.9%** | **42.2%** |
| `scalp_vol_cvd_div_v1`     | 0.50 | 0.20 | 2.50 | 42.9% | 38.6% | 34.3% |
| `scalp_funding_extreme_v1` | 0.80 | 0.30 | 2.67 | 36.4% | 33.6% | 30.9% |
| `scalp_random_baseline_v1` | 0.40 | 0.20 | 2.00 | 50.0% | 45.0% | 40.0% |

**`sweep_choch_v1` has the worst breakeven** because the small absolute
SL (0.15%) means fees consume a larger fraction. Even maker+maker requires
**42.2% WR** in the binary case — and the BE-rate plus time-stop drag in Part 3
push that higher.

### 3.2 Observed outcomes (Part 2)

| Signal | TP | SL | BE | TS | TO | N | TP-rate | SL-rate | avg TS% (net) |
|---|---|---|---|---|---|---|---|---|---|
| `scalp_liq_reclaim_v1` | — | — | — | — | — | 0 | no data | | |
| `scalp_sweep_choch_v1` | 6 | 34 | 8 | 25 | 0 | **73** | **8.2%** | 46.6% | -0.069% |
| `scalp_vol_cvd_div_v1` | 0 | 0 | 0 | 1 | 0 | 1 | starved | | |
| `scalp_funding_extreme_v1` | — | — | — | — | — | 0 | no data | | |
| `scalp_random_baseline_v1` | 5 | 12 | 3 | 44 | 0 | **64** | **7.8%** | 18.8% | -0.090% |

**Critical finding:** `scalp_sweep_choch_v1` TP-rate (8.2%) is statistically
indistinguishable from `scalp_random_baseline_v1` TP-rate (7.8%). The
detector adds no edge beyond random direction emission at the v1 parameter
set. The 5.6× higher SL-rate (46.6% vs 18.8%) reflects the active entry
geometry — sweep_choch enters into a known "sweep then close-back" structure
that places SL inside noise, while baseline samples random points where price
drifts neutrally before time-stop fires.

### 3.3 Realistic breakeven WR (Part 3)

| Signal | p_be | p_ts | avg TS% gross | BE taker | BE mk+ta | BE mk+mk | observed |
|---|---|---|---|---|---|---|---|
| `scalp_sweep_choch_v1` | 11.0% | 34.2% | +0.031% | **38.1%** | **31.4%** | **24.8%** | **8.2%** |
| `scalp_random_baseline_v1` | 4.7% | 68.8% | +0.010% | 24.3% | 19.3% | 14.3% | 7.8% |
| `scalp_vol_cvd_div_v1` | — | — | — | (n=1, ignore) | | | |

The realistic breakevens are *lower* than the binary numbers because BE and
time-stop outcomes have small near-zero PnL (less punitive than SL), but they
remain far above observed WRs.

### 3.4 Per-signal verdict (Part 4)

| Signal | N | Verdict | Reasoning |
|---|---|---|---|
| `scalp_liq_reclaim_v1` | 0 | **NO DATA** — theoretical only | Need to enable + accumulate |
| `scalp_sweep_choch_v1` | 73 | **KILL v1 params** | Observed WR (8.2%) is 16.6 pp below maker+maker breakeven (24.8%). Not closeable by fee changes alone. |
| `scalp_vol_cvd_div_v1` | 1 | **STARVED** | Need ≥30 outcomes |
| `scalp_funding_extreme_v1` | 0 | **NO DATA** — theoretical only | Need to enable + accumulate |
| `scalp_random_baseline_v1` | 64 | **KILL** (expected — control) | Confirms baseline is at random-noise level |

---

## 4. Implications

### 4.1 v2 sweep_choch filters (PR #14) must clear a high bar

The v2 ADX + book_imbalance filters bumped `SCALP_EXPERIMENT_ID` to
`scalp_v2_filtered_2026_05_05`. This audit shows the bar to clear:

- **Maker+maker breakeven WR for sweep_choch ≈ 25%** (assuming v2 BE+TS
  distribution stays roughly similar to v1)
- v1 baseline TP-rate is ~8%
- v2 filters must lift TP-rate **3× over v1** to break even at maker+maker fees,
  or **4–5× over v1** at maker+taker / taker rates

Hypothesis from PR #14: ADX + book_imbalance reject the worst v1 SL conditions
(sub-trend regimes, stacked-bid traps). Realistic v2 TP-rate ceiling without
also expanding TP target: ~15–20% — still likely below breakeven.

**Recommendation:** if v2 outcomes after N≥30 land below 20% TP-rate, do not
proceed to live; instead retune TP/SL geometry rather than just gates.

### 4.2 Most-promising-by-design signal: `funding_extreme`

By Part 1 alone, `scalp_funding_extreme_v1` has the lowest breakeven WR
(30.9% maker+maker, 36.4% taker). Its 2.67:1 R:R absorbs more fee drag.
Risk: 900s time-stop is the longest of any signal — likely many TS outcomes,
which Part 3 shows can move breakeven up. Worth enabling next.

### 4.3 Two signals with no data — enable in shadow

`scalp_liq_reclaim_v1` and `scalp_funding_extreme_v1` show 0 outcomes. Either
the master switch (`SCALP_SHADOW_ENABLED=false`) suppresses everything, or the
detection conditions never fire. Confirm by toggling on in a controlled
window and watching emission rates before assuming broken detector.

### 4.4 TP/SL geometry redesign is more likely the answer than fees

Even at the most generous fee model (maker+maker, 0.04% RT), `sweep_choch_v1`
needs WR 24.8%. v1 detector produces 8.2%. The gap is too large to close
purely with fees. Either:

- Loosen SL (0.15% → 0.25%+) so each loss is bigger but fewer happen
- Tighten TP toward "first profitable exit" rather than 2× SL
- Replace symmetric R:R with asymmetric (HFT-fund pattern from /ultraplan
  Track 1 thesis): wide TP, time-stop as primary exit
- Increase the entry filter strength so emission rate drops but quality lifts

PR #14's v2 filters take the last route. Numbers will tell after N≥30.

---

## 5. Recommendation

**Phase 1A produces three concrete actions:**

1. **Hold v1 sweep_choch as KILLED.** The v1 experiment is closed. Treat any
   `scalp_sweep_choch_v1` outcome under `experiment_id=scalp_v1_2026_05` or
   pre-PR#14 timestamps as final.
2. **Wait on v2 sweep_choch numbers.** Bar to clear: TP-rate ≥ 25% at maker+maker.
   If post-N≥30 v2 hits 15–20%, redesign TP/SL geometry rather than tightening
   gates further.
3. **Enable `liq_reclaim` and `funding_extreme` shadow paths.** Both have zero
   data. funding_extreme has the most fee-tolerant geometry — likely most
   promising of the five if detector emits at all. Investigate why current
   shadow has produced 0 outcomes for both.

---

## 6. Followups not in this audit

- Phase 1B: 1m candle data foundation (separate work)
- Phase 1C: per-setup `risk_pct` decoupled from uniform 1% (separate work)
- Phase 1D: time-stop as primary exit (separate work — current code already
  has time-stops, but they fire after SL/TP precedence)
- Phase 1E: signal validation gate (N≥500, only after retuning shows promise)
- Investigate why `liq_reclaim` and `funding_extreme` produce 0 outcomes:
  detector silent? gate too strict? master switch off?

---

## 7. Reproducibility

```bash
# from repo root
PYTHONPATH=. python scripts/scalp_fee_viability.py
```

No external dependencies beyond `psycopg2` already in venv. Reads
`SCALP_SIGNAL_PARAMS` from `config/settings.py` so any TP/SL retune is
reflected on next run.
