# Grill: main.py god-file split (Refactor Phase 6, Approach A)
**Date:** 2026-06-29
**Topic:** Split 1546-line main.py into pipeline_runtime + 4 modules, incremental, tests-green gate
**Verdict:** BUILD — approach A (full split) is low *capital* risk because the bot is shadow-pure; do it incrementally with a reinforced verification net.

## Context loaded
- `CLAUDE.md`, `MEMORY.md`, `docs/SYSTEM_BASELINE.md §8`, `git log -10`, `git status`
- `main.py` (full, 1546 LOC), `config/settings.py`, the 3 test files that reach into `main._*`
- Refactor tracker `docs/plans/refactor-solidify-2026-06-26.md` — Phase 6 spec
- **Ground-truth correction during grill:** memory/baseline headline said engine1 "LIVE / first real money". Verified `.env` `ENGINE1_LIVE_GATED_ENABLED=false`, settings default false, baseline §8 newest entry = REVERTED same day (6/28). Bot is **shadow-pure, zero live trades ever executed, no money touched**. The `engine1_live` branch in `_process_pipeline_setup` exists but is inert (flag OFF). Stale memory headlines fixed (MEMORY.md index + memory file description/lead).

## Grill stance
Per `feedback_grill_intensity`: this is a refactor of code that already works, not a new edge idea — default-KILL dropped. The decision to do Phase 6 was already taken by the user. Grill purpose = de-risk the *how*, not relitigate the *whether*. Refactor branch of the decision tree (current pain / rollback / invariants).

## Decision tree

### Q1: Verification beyond "1409 tests green"
**My recommended answer:** Green tests are not sufficient — branches like HTF campaign (OFF), engine1 live gate, dual-thrust shadow, position guardian are not all covered, so a refactor can pass tests yet diverge on an uncovered branch. Need a second net.
**User answer:** Chose **B reinforced** over A (old-vs-new behavioral diff).
**Grade:** ✅
**Notes:** Full old-vs-new parallel-run diff (A) is over-engineering for a mechanical move + slow (ETH 4h = 6 candles/day → days to useful N, blocks merge). B reinforced = (1) 1409 tests, (2) add 2–3 tests to cover today-uncovered branches *before* moving, (3) grep for orphaned globals (`_data_service\b` etc.) after the `rt.x` conversion — catches the dominant "forgot to convert one global → NameError" risk, (4) real `python main.py` startup + 9-step deploy checklist, (5) incremental with 1–4 between each step. Real old-vs-new diff stays as a fallback only if post-deploy looks off — not a merge gate.

### Q2: Extraction order + rollback
**My recommended answer:** leaf→core, one commit per module. `pipeline_runtime` (singleton, no logic) → `persistence` → `monitoring_loops` → `ml_instrumentation` → `pipeline_router` (core, last). Per-commit revert. No extra freeze needed because the live gate is already OFF.
**User answer:** Asked *why*, then approved.
**Grade:** ✅
**Notes:** Dependency direction drives the order — leaves depend on nothing, core calls the leaves; moving the core first creates half-moved cross-import states. Per-commit = surgical `git revert` + partial work survives if we stop at "B". No capital at risk (shadow-pure) → worst case is minutes of lost ML logging until redeploy (Docker auto-restart). `test_engine1_live_gate.py` stays green each step to prove the dormant live branch remains correct for re-activation day — without turning it on now.

### Q3: Scope creep — what NOT to touch
**My recommended answer:** Stays in main.py: `main()` wiring, `validate_config`, `_log_pair_diagnostics`, `_send_crash_alert`, `__main__`. NOT done: no renames (pure moves), no logic changes (move ≠ refactor internals), no notifier/alert_manager smell (own PR), no data_store/bybit_watcher split (Phase 6b). Allowed dead-code kill (plan-mandated, own commit): `_daily_summary_loop` + unused `_persist_ai_decision(trade_id)` param. Tests: thin re-exports in main keep `main.fn` references resolving; only state-setting `main._data_service = x` → `rt.data_service = x` changes.
**User answer:** Approved ("SI ESTA BIEN") incl. (a) dead code in this PR own commit, (b) re-exports to minimize test churn.
**Grade:** ✅
**Notes:** Keeps A as "move boxes + delete 2 dead things", not "redesign the pipeline". The tempting-but-risky (rename, improve logic, split other god-files) is explicitly out.

## Final verdict
BUILD. The scary framing at the start assumed live money in the pipeline; once corrected (shadow-pure, flag OFF, zero trades), Approach A drops from "dangerous" to "tedious but safe". The verification net (B reinforced), leaf→core per-commit order, and a tight scope boundary make a 1546-line split manageable. No edge/PnL impact — pure hygiene + comprehension, which is the whole point of the refactor and aligns with the plan's "before VPS" intent.

## Pre-conditions for /phased-plan
- Singleton mechanism: `pipeline_runtime.py` holds a `rt` object (attributes, not module globals) so submodules `from pipeline_runtime import rt` and `rt.x = ...` propagates everywhere. Module-global reassignment would NOT propagate — this is the core mechanism that makes the split work.
- Inventory the ~20 globals to migrate: services (`_data_service` … `_alert_manager`), state (`_setup_dedup_cache`, `_last_setup_detected_time`, `_atr_history`, `_bot_start_time`, `_dry_spell_alerted`, `_engine1_kill_alert_ts`, `_vol_spike_cooldown`, `_funding_extreme_cooldown`, `_EMIT_METRIC_FAILURES`, `_EMIT_METRIC_LAST_WARN`).
- Add coverage for uncovered branches (HTF campaign blocked-path, dual-thrust shadow hook, position guardian) BEFORE moving.
- One PR off `chore/refactor-phase6-mainpy` (branch already created), same merge flow as #103–#105. Update tracker Phase 6 + SYSTEM_BASELINE per /doc-update.
