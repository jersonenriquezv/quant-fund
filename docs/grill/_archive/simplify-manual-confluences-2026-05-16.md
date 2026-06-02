# Grill: Simplify manual confluences in Bybit annotation form
**Date:** 2026-05-16
**Topic:** User cannot reliably classify SMC setup_type / confluences when journaling manual Bybit trades. Should those manual fields be removed and replaced by LLM analysis of free-text thesis later?
**Verdict:** PIVOT — staged removal gated on Phase 1 fill rate proof + LLM smoke test on N=5 trades.

## Context loaded
- `CLAUDE.md`, `docs/SYSTEM_BASELINE.md` — bot SHADOW-ONLY, ~$86 capital untouched.
- `MEMORY.md` — Bybit ~$4.6k real money. Phase 1 manual-edge-discipline shipped 2026-05-15 (trigger_condition + thesis_invalidation structured fields). Rule 13 freeze active 2026-05-13 → 2026-06-08.
- `git log` — `b5f2ece feat(bybit): manual edge discipline Phase 1` is the latest substantive commit (yesterday).
- `strategy_service/trade_classifier.py` — deterministic auto-classifier already returns `auto_setup_type ∈ {A_swing_long, A_swing_short, B_sweep, D_choch, D_bos, F_breakout, discretion}` + `auto_confluences` + `auto_grade`. Stored on every `bybit_trade_annotations` row at fill time.
- `bybit_trade_annotations` DB query: 37 historical trades since 2026-03-18. **Only 2 have `thesis_pre` filled (5% rate)**. Last trade 2026-05-03 (13 days ago at grill time). Cadence ~0.8 trades/day, lumpy.

## Decision tree

### Q1: What SQL query against `setup_type` + `confluences` justifies keeping the manual fields?
**My recommended answer:** None — auto_classifier already produces the same taxonomy, manual is redundant or noise. If user trades manually with non-SMC reasons, forcing fit creates wrong-classification noise.
**User answer:** Wants to run an LLM later that compares free-text reason vs auto snapshot to judge "did I read the market right." Manual setup_type/confluences would be removed; thesis_pre + trigger + invalidation (the text fields) feed the model alongside auto.
**Grade:** ✅ survives — coherent design that removes ambiguity instead of adding it.
**Notes:** The plan separates concerns cleanly: auto = market truth, free text = user mental model, LLM = judge of alignment. Manual SMC classification becomes dead weight under that design.

### Q2: Which model? (a) LLM with fixed prompt, (b) embedding similarity, (c) undecided
**My recommended answer:** (a) — embeddings don't produce "right/wrong" judgments, only similarity. LLM with structured JSON output is the right primitive. Sonnet ~$0.01/trade, ~$0.30 for N=30.
**User answer:** (a) — LLM.
**Grade:** ✅ survives.
**Notes:** No paper cited for "LLM as judge" methodology but the pattern is well-known and the cost / output shape fits the use case.

### Q3: (a) How is the LLM verdict itself audited? (b) When does N=30 with rich `thesis_pre` actually arrive?
**My recommended answer:**
- (a) Ground truth = user re-reads first 5 trades and grades the LLM verdict. ≥4/5 concordance → trust; <4/5 → iterate prompt before scaling.
- (b) Do NOT remove manual fields until N=5 trades exist with rich `thesis_pre` AND smoke test passes. Current fill rate 5% historical — Phase 1 enforcement just shipped, fill rate post-fix is unmeasured. At 0.8 trades/day, N=30 with full text is late June if Phase 1 fix hits 100%, late July at 50%.
**User answer:** Accepted both (a) and (b) as stated.
**Grade:** ✅ survives.
**Notes:** This is the load-bearing gate. Removing manual fields now without Phase 1 fill-rate proof would leave the user with no fallback if `thesis_pre` continues to be ignored.

## Final verdict

PIVOT, not BUILD. Original idea ("remove manual setup_type/confluences fields") is correct in direction but premature in timing. User has not yet proven Phase 1 enforcement raises `thesis_pre` fill rate above the historical 5% baseline. Removing manual fields before that proof = blind bet that free text alone will be richer than free text + structured tags. If Phase 1 falls short, the journal is left with nothing queryable.

The pivoted plan stages the removal behind two falsifiable gates that the user explicitly accepted.

## Pivoted plan (one sentence)

Keep manual `setup_type` + `confluences` visible in the form for now; once N=5 new trades have rich `thesis_pre` AND a one-shot LLM smoke test concords with user re-read on ≥4/5, remove the manual fields and turn on LLM analysis as the post-mortem layer.

## Pre-conditions for `/phased-plan`

- [ ] **Gate 0 — Phase 1 fill-rate signal.** Wait for ≥5 new Bybit trades after Phase 1 ship (2026-05-15). Measure: how many have non-empty `thesis_pre` AND `trigger_condition` AND `thesis_invalidation`. Pass = ≥4/5. Fail = redesign Phase 1 fields, do not proceed to LLM work.
- [ ] **Gate 1 — LLM prompt draft + smoke test on the 5 trades from Gate 0.** Prompt produces JSON `{confluences_extracted: [...], match_with_auto: bool, mismatch_reason: str|null, verdict_did_i_read_market: "yes"|"partial"|"no", confidence: 0-1}`. Run on first 5 fully-filled trades. User re-reads each, grades the LLM verdict. ≥4/5 concord → proceed. <4/5 → iterate prompt, do not remove manual fields.
- [ ] **Gate 2 — UI removal scope.** Decide whether to delete `setup_type` + `confluences` from `bybit_trade_annotations` schema or only hide from the form (recoverable). Recommendation: hide-only via column nullable + frontend conditional, no DROP COLUMN — cheap rollback if Gate 1 turns out to be a fluke at N=5.
- [ ] **Decide cron + storage for LLM analysis output.** New column `llm_verdict JSONB`? Or separate table `bybit_llm_reviews`? Picked before `/phased-plan` so the plan can name the artifact.
- [ ] **Confirm this work does NOT introduce a new binding trading rule** — Rule 13 freeze active until 2026-06-08. This is observability + journaling, not a rule, so it is in scope.

## If KILL: reason + what would revive it
N/A — pivoted, not killed.
