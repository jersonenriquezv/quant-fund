# Grill: 1D HTF Bias Veto on Engine 1
**Date:** 2026-05-20
**Topic:** Add a 1-day timeframe bias filter that vetoes engine1 entries when 1D structure disagrees with trade direction. NOT a full 4-TF OSD cascade — just a single-TF veto layer on top of current 4H+1H bias.
**Verdict:** KILL — 1D bias does not discriminate between TPs and SLs on engine1 v1c. SL disagree-1D = 72.9%, TP disagree-1D = 68.4% (Δ=4.5pp, noise). Engine 1 is short-only across all pairs; during 14d window 1D bias was bullish on BTC/SOL/AVAX/LINK so 100% of those entries disagree by construction — affects winners and losers equally.

## Context loaded
- SL post-mortem (today, N=34 engine1 SLs): modal failure = `wrong_direction` 38.2%. Combined with `counter_trend_valid` 29.4%, >65% of losses involve direction-quality issues.
- Current bias = 4H + 1H aggregated. `HTF_BIAS_REQUIRE_4H=False` (1H alone counts).
- 1D candles: NOT subscribed unless `HTF_CAMPAIGN_ENABLED=true` (off).
- `market_structure.py` works on any TF given enough candles — no algorithm change needed.
- ML v0 baseline AUC 0.72 provisional. Re-runs **5/25 (5d)** and **6/8 (19d)**.
- FREEZE on strategy work until 6/8 unless ML kill earlier.
- Engine 1 14d: 19 TP / 48 SL / 103 BE — losing $264, beats benchmarks by ~$1/trade.

## Steelman argument
Engine 1 enters against short-term direction 38% of the time. 4H+1H bias agrees with bot direction at entry, then market reverses. If 1D bias would have flagged "no, this is counter to a larger trend", those 13 wrong_direction SLs become non-emissions. Even if we lose some TPs from the same filter, the question is net edge improvement.

## Decision tree

### Q1: Does 1D bias actually predict `wrong_direction` outcomes?
**Status:** answered 2026-05-20 via `scripts/study_1d_veto.py`.

**Result (engine1 last 14d, N=170 outcomes, 0 skipped):**

| Outcome | agree | disagree | undef | %disagree |
|---|---|---|---|---|
| shadow_tp | 6 | 13 | 0 | 68.4% |
| shadow_sl | 13 | 35 | 0 | 72.9% |
| shadow_breakeven | 17 | 86 | 0 | 83.5% |

| Pair | %disagree |
|---|---|
| BTC/USDT | 100.0% (0 agree / 30 disagree) |
| SOL/USDT | 100.0% (0 / 25) |
| AVAX/USDT | 100.0% (0 / 28) |
| LINK/USDT | 100.0% (0 / 31) |
| ETH/USDT | 28.6% (30 / 12) |
| DOGE/USDT | 62.5% (3 / 5) |
| XRP/USDT | 50.0% (3 / 3) |

**Decision rule:** SL>60% disagree (✅ 72.9%) AND TP<30% disagree (❌ 68.4%). Failed second leg by 38 percentage points.

**Why it fails**: engine1 v1c is short-only across all `TRADING_PAIRS` (since 2026-05-07). During the 14d window 1D bias was bullish on BTC/SOL/AVAX/LINK → every short emitted on those pairs = 100% disagree-1D by construction, regardless of outcome. ETH is the only pair where 1D went bearish often enough to see real variance — and even there the TP/SL ratio under disagree-1D doesn't favor a veto.

**Grade:** ❌ — kills the veto thesis.

**Interpretation correction**: `wrong_direction` 38% from the SL post-mortem is NOT "1D structure said otherwise". It's "5m noise reversed the trade within minutes of entry". The classifier label is correct — the inferred cause was wrong. Need a different lens (entry-trigger quality, not HTF bias) to attack that bucket.

This is the tracer assumption. Before any code, run a read-only counterfactual:
- For each engine1 SL row classified `wrong_direction` (N=13) and each `counter_trend_valid` (N=10) and each `sl_too_tight_noise` (N=11) and each TP (N=19), reconstruct 1D market structure at entry time from existing `candles` table.
- Compute 1D trend (bullish/bearish/undefined) at that moment using `MarketStructureAnalyzer` on the 1D series.
- Tabulate: of `wrong_direction` SLs, what % had 1D-disagreed-with-trade-direction? Of TPs, what % had 1D-disagreed?

**Decision rule:**
- If `wrong_direction` SLs are >60% on disagree-1D AND TPs are <30% on disagree-1D → ✅ veto is informative
- If both are similar (~40-50%) → ❌ 1D adds no signal beyond 4H+1H
- If `wrong_direction` is <60% disagree-1D → ⚠️ weak; needs different cut

**My recommended answer:** Unknown until measured. Prior: 4H+1H already correlated with 1D ~70% of time in trending regimes, so marginal info from 1D may be small. Grill verdict depends entirely on this query.

### Q2 (gated by Q1): Does the veto pass cost/benefit?
Pre-conditions:
- Q1 shows 1D is informative (per decision rule)
- AND ML v0 re-run 5/25 completes (FREEZE lifts conditionally)

Cost surface to grade:
- 1D candle subscription (WS or REST poll) + backfill
- `MarketStructureAnalyzer` for 1D state cache
- New feature column `htf_1d_bias` in `ml_setups` → bumps `ML_FEATURE_VERSION 18→19`
- ⚠️ Bumping ML_FEATURE_VERSION invalidates v0 baseline rows for re-train. Cost = discard N rows or re-collect.

### Q3 (gated by Q2): Falsification criterion
What measurement, observed within 30 days of activation, kills the veto?
- Engine 1 emissions drop >70% (over-filter) AND TP count drops proportionally — veto kills good signal
- Wrong_direction class % among remaining SLs does NOT drop below 25% — veto doesn't fix the modal failure
- Net PnL/trade does not improve by ≥$0.50 vs pre-veto baseline at N≥50 post-activation

### Q4 (gated): Counterfactual — cheaper alternatives that get 70% of the upside?
- Tighten existing 4H+1H by setting `HTF_BIAS_REQUIRE_4H=True` (1 line, zero data cost). Currently False — 1H-alone counts as bias. This MAY explain wrong_direction: 1H bias flips intraday, 4H is more stable. **Try this first.**
- Raise engine1 impulse purity threshold (currently uses `impulse_directional_purity`)
- Filter engine1 by ADX gate on 4H (require trend strength)

### Q5 (gated): Implementation isolation
- Phase A: observability only. Add `would_have_been_vetoed_by_1d` boolean column. Compute offline + log forward. NO emission blocking. NO bump of ML_FEATURE_VERSION if computed offline. → Safe during FREEZE.
- Phase B: enforcement. Bump `EXPERIMENT_ID` → `engine1_1d_veto_<date>`. Old data queryable under prior ID. → Only post 6/8 or post ML-kill.

## Path forward proposed by user
- "Hacer el cambio + labelear / controlar para no contaminar data"
- Maps to **Phase A observability** above. Concrete: offline study answering Q1, NO code on bot pipeline yet.

## Final verdict — KILL

Q1 failed both legs of decision rule. No need to walk Q2-Q5 — gate one is binary.

**What survives:** the underlying observation (engine1 wrong_direction 38% modal) is real but the 1D-HTF hypothesis was the wrong cause assignment. Direction-quality losses are intra-trade 5m noise reversals, NOT macro-bias miscalls.

**What would revive 1D veto:**
- Engine 1 turned bidirectional (longs re-enabled) AND
- A new study showing 1D agreement discriminates >15pp between TP and SL on the bidirectional engine

**Implications for other ideas:**
- Setting `HTF_BIAS_REQUIRE_4H=True` (1-line change) may still help — independent of 1D study. Worth its own grill post-6/8.
- Entry-trigger quality fixes (ATR floor on SL, displacement requirement, impulse threshold) target the actual modal cause. These are the better next grill.
- Original OSD KILL verdict stands.

## Pre-emptive verdict (historical, kept for audit)
- If Q1 fails → **KILL** (1D not informative; back to fee/sizing fixes). ← path taken
- If Q1 passes AND Q4 alternative (`HTF_BIAS_REQUIRE_4H=True`) is cheaper and not yet tried → **PIVOT** to that 1-line change first.
- If Q1 passes AND Q4 alternative already tried/insufficient → **PIVOT to Phase A** observability study (still during FREEZE-safe).
- Full 1D veto enforcement → **NOT before 6/8** regardless.

## Out of scope
- Full OSD cascade (killed in prior grill 2026-05-20).
- Adding 30m or 1W candles (no evidence binding).
- Touching setup_f or scalp variants (separate grills).
- Changing TP1/SL distance (separate fix path, Batch 1 territory).
