# Grill: Strategy Refinement Guide + criteria consolidation
**Date:** 2026-06-08
**Topic:** A single plain-language doc defining when each signal is "ready to refine / go live", plus consolidating scattered graduation criteria into one home.
**Verdict:** BUILD (refined) — explainer guide + consolidate the one external criteria set (#25) into SYSTEM_BASELINE.

## Context loaded
- CLAUDE.md, MEMORY.md, SYSTEM_BASELINE §7 (ML gates), §9 (roadmap + FREEZE + scalp validation).
- Live data pulled this session (under stated experiment_id regimes):
  - engine1_trend_pullback (v1d): WR 45.2%, net +$16.71, PF 1.03 vs bench_random WR 26.6%, PF 0.48.
  - ml_v0 AUC test 0.716 (N=283) — EDGE CLARO, 3rd stable run.
  - scalp_liq_reclaim_v1 (v4_tune): WR 79.5%, net +$180, PF 2.56, N=44, 2.82 emits/day.
  - scalp_vol_cvd_div_v1: 4 emits, already killed 2026-05-22.
- Key discovery: FREEZE (2026-05-13 → 2026-06-08) ends today; the 6/8 decision was "hard-kill vs extract-platform", not "build Engine 2".

## Decision tree

### Q1: What is this document, really?
**My recommended answer:** An understand/reference page that translates + maps existing finish lines, not a new operational plan.
**User answer:** Option (a) — understand/consult page, in English. Wants it in ONE place, no duplication.
**Grade:** ✅
**Notes:** Confirmed read-only explainer. English.

### Q2: Approve the 2-part move (consolidate criteria into SYSTEM_BASELINE + new explainer page that links, copies no numbers)?
**My recommended answer:** Yes — thresholds are config → live in SYSTEM_BASELINE (user's own doc-hierarchy rule). A new plan restating them = drift risk (edit two files, one goes stale).
**User answer:** "si haz eso."
**Grade:** ✅
**Notes:** During pre-flight found criteria are LESS scattered than feared: §7.1 (ML activation gate G1–G6) and §9 (scalp validation) already live in SYSTEM_BASELINE. Only the ml_v0 / Engine-2 decision rules (#25) live outside. So consolidation = bring #25 rules in as §7.2; everything else is already home, just needs linking from the guide.

## Final verdict
BUILD, minimal scope. The guide adds zero new thresholds; it teaches the concepts (AUC, holdout, PF, N) and shows a "you are here" map. The only SYSTEM_BASELINE write that adds numbers is §7.2 (importing #25's already-decided rules). §9 FREEZE gets a status update because it expires today and the kill/keep fork resolved to keep (edge confirmed).

Honest findings surfaced by the grill (go in the map, not hidden):
- engine1 raw is breakeven (PF 1.03). Its edge is real (AUC 0.716, beats random) but only becomes money via a meta-label filter — that path is gated by §7.1 G1–G6, which are NOT yet met (no purged CV / calibration / Brier run; ml_v0 is only a pre-check).
- §7.1 G1 wants ≥500 *filled* (live) outcomes — impossible in shadow mode. Chicken-and-egg flagged as an OPEN QUESTION, not invented away.
- liq_reclaim passes WR / PF / beats-random but fails frequency (2.82/day vs ≥5/day gate) and N (44/100). So "profitable" ≠ "ready to graduate".

## If BUILD: artifacts
1. `docs/STRATEGY_REFINEMENT_GUIDE.md` — new, English, plain-language explainer + map. Links to §7.1/§7.2/§9. No copied thresholds.
2. SYSTEM_BASELINE §7.2 — import ml_v0 / Engine-2 decision rules from #25 (single canonical home).
3. SYSTEM_BASELINE §9 FREEZE — mark expired 6/8, record decision = keep (edge confirmed), shadow continues toward graduation gates.
4. SYSTEM_BASELINE changelog entry.

## Open questions parked for the user (in the guide, not resolved here)
- G1 shadow-vs-filled ambiguity: does the 500-outcome bar count shadow outcomes, or only live fills? Needs a decision before engine1 can ever "pass" in shadow-only mode.
- SMC / scenario-analysis plan (notes.md): explicitly parked as a future phase with trigger = both engine1 + liq_reclaim graduated AND capital justifies. Not an option now (user confirmed).
