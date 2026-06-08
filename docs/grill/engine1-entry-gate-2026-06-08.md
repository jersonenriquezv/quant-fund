# Grill: engine1 entry-gate (low-impulse filter)
**Date:** 2026-06-08
**Topic:** Add an entry gate to engine1_trend_pullback that filters on existing features (impulse ATR multiple / pullback depth / entry ATR distance) to lift PF without changing the signal.
**Verdict:** BUILD — but first phase is forward shadow validation, NOT live. Single-feature low-impulse gate is the candidate.

## Context loaded
- Discovery: `scripts/analyze_engine1_entry_gates.py` (in-sample tercile slices).
- OOS validation: `scripts/validate_engine1_gates_oos.py` (chronological 70/30, cutoffs frozen on train).
- Regime: v1d (`engine1_short_quarantine_v1d_2026_05_22`), short-only, 5 pairs.
- Fees: `pnl_usd` already net of taker RT (memory `feedback_pnl_already_net_of_fees`) — PF figures are post-fee.

## The candidate
Filter engine1 entries to the **low** tercile of `engine1_impulse_atr_multiple` (train cutoff ≈ 2.24). Optionally combine with shallow `pullback_depth_pct` and/or high `entry_atr_distance`, but combining starves N.

## Decision tree

### Q1: Scientific basis — or pure curve fit?
**Answer:** Plausible mechanism. Lower impulse → less-exhausted move → more continuation room after the pullback; shallow pullback → trend intact; far entry → better R:R. Coherent SMC logic, not a reverse-engineered artifact.
**Grade:** ✅ (mild — coherent, not paper-backed)

### Q2: Does it survive out-of-sample? (the null = "in-sample luck")
**Answer:** Yes. Cutoffs frozen on oldest 70%, tested on newest 30% (N=103, test baseline PF 0.92):
- impulse low: testN=33, WR 33.3%, **PF 4.5**
- pullback_depth low: testN=31, **PF 2.16**
- entry_atr_distance high: testN=30, **PF 1.71**
All three pre-selected gates beat baseline on held-out data.
**Grade:** ✅

### Q3: Multiple testing / data dredging?
**Answer:** Discovery tested ~30 slices (dredge risk real). Mitigated two ways: (a) pre-selected only the 3 that were consistent across pooled + v1d before touching the holdout; (b) the holdout test is on those 3 only, not 30. 3-of-3 beating baseline at these magnitudes is unlikely by chance.
**Grade:** ✅ (with caveat: still want walk-forward across multiple splits before live)

### Q4: Is PF driven by 1-2 fat trades? (fragility null)
**Answer:** No. impulse-low test bucket = 11 winners ~$9.5 each, top win 9% of gross, top3 28%. Losses mostly -$0.5 breakevens + a few -$5.6 SLs. Edge is distributed and structural — the gate filters out the real-SL trades.
**Grade:** ✅

### Q5: Edge after fees?
**Answer:** `pnl_usd` is already net of RT taker fees. PF 4.5 / 2.16 are post-fee.
**Grade:** ✅

### Q6: Implementation cost?
**Answer:** Low. Gate filters on a feature already computed at detection (`engine1_impulse_atr_multiple`). Does NOT change feature definitions → **no ML_FEATURE_VERSION bump → no training-data invalidation.** Touches `strategy_service` engine1 path only. Shadow-taggable as a parallel variant for forward comparison.
**Grade:** ⚠️→✅ (touches strategy_service; mitigated by shadow-first)

### Q7: Simpler alternative?
**Answer:** The gate IS the simple alternative — vs building Engine 2 (#25 says don't) or a full meta-label model (§7.1 gates not met). A single threshold filter is the cheapest possible refinement.
**Grade:** ✅

## Remaining risks (do NOT hide)
- N=33 on the OOS slice is modest; PF 4.5 has wide error bars even if the distribution is reassuring.
- Single chronological split, not walk-forward. One good test regime could flatter it.
- Still shadow. No live fills. The §7.1 activation gate is unaffected by this.
- v1d is short-only — gate applies to engine1 shorts only.

## Falsification criterion (concrete, dated)
Ship the gate as a parallel shadow variant (gated vs ungated engine1, same triggers). **Kill the gate if, over the next 30 days OR N≥50 forward-resolved gated outcomes (whichever first), the gated variant's PF < 1.5 OR it fails to beat ungated engine1 PF by ≥ 0.3.** Re-evaluate 2026-07-08.

## Handoff
Verdict BUILD → next step `/phased-plan engine1-entry-gate` using this doc as input. Phase 1 must be forward shadow validation (parallel gated/ungated), not live execution. Do not run phased-plan automatically.
