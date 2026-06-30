# Plan: main.py god-file split (Refactor Phase 6, Approach A)
**Slug:** main-py-split-phase6
**Source grill:** docs/grill/main-py-split-phase6-2026-06-29.md
**Created:** 2026-06-29
**Status:** pending
**Tracer bullet:** Phase 1 tests the ONE assumption the whole approach rests on — that replacing ~20 module-level globals with a shared `rt` singleton object propagates state correctly to both the runtime wiring and the test-injection pattern (`main._data_service = x` → `rt.data_service = x`). If that breaks, A is dead and we fall back to B.

## Context summary
Split `main.py` (1546 LOC) into `pipeline_runtime.py` + 4 modules (`persistence`, `monitoring_loops`, `ml_instrumentation`, `pipeline_router`), leaving `main.py` as wiring + `main()` only (~250 LOC). Pure hygiene/comprehension before VPS migration — **zero behavior change, zero edge/PnL/WR impact**. Bot is shadow-pure (`ENGINE1_LIVE_GATED_ENABLED=false`, `ENABLED_SETUPS=[]`) so no capital is at risk; worst case of a regression is minutes of lost ML logging until redeploy. Does NOT rename anything, does NOT change logic, does NOT touch the notifier/alert_manager smell or split data_store/bybit_watcher (Phase 6b).

## Phase 1 — Tracer: `pipeline_runtime.py` singleton + globals→`rt` in place
**Status:** done (committed e4b508b on chore/refactor-phase6-mainpy, not pushed)
**Inputs:** Grill pre-conditions; global inventory (services `_data_service`…`_alert_manager`; state `_setup_dedup_cache`, `_last_setup_detected_time`, `_atr_history`, `_bot_start_time`, `_dry_spell_alerted`, `_engine1_kill_alert_ts`, `_vol_spike_cooldown`, `_funding_extreme_cooldown`, `_EMIT_METRIC_FAILURES`, `_EMIT_METRIC_LAST_WARN`).
**Outputs:** `pipeline_runtime.py` with `rt` object; `main.py` reads/writes `rt.x` everywhere (functions NOT moved yet); 3 test files set `rt.x`; new branch-coverage tests.
**Work:**
- Create `pipeline_runtime.py`: a `Runtime` class instance `rt` holding all service refs (default `None`) + mutable state fields. Attributes, not module globals — so `from pipeline_runtime import rt; rt.x = …` propagates across modules.
- In `main.py`: replace every `_data_service` etc. read with `rt.data_service`; delete the `global …` statements; `main()` sets `rt.data_service = …`. No function relocation this phase — isolates the singleton mechanism from the move mechanics.
- Update `tests/test_main_pipeline.py`, `test_engine1_live_gate.py`, `test_engine1_scorer.py`: `main._data_service = x` → `rt.data_service = x`, `main._setup_dedup_cache` → `rt.setup_dedup_cache`. Function refs (`main.on_candle_confirmed`, `main._process_pipeline_setup`) stay — functions still live in main this phase.
- Add coverage for branches not currently exercised: HTF-campaign-blocked path, dual-thrust shadow hook, position guardian early-return. These run BEFORE any move so "green" means more.

**Verification gate:**
- [ ] Automated: `python -m pytest tests/ -q` → 1409+ pass, 0 fail (incl. new branch tests + `test_engine1_live_gate.py`).
- [ ] Automated: `grep -rnE '\b_(data|strategy|ai|risk|execution)_service\b|\b_setup_dedup_cache\b' main.py` → 0 matches (every global converted; only `rt.` access remains).
- [ ] Manual: read `pipeline_runtime.py` — confirm every migrated field preserves its original default (`None` for services, `{}`/`0.0`/`False` for state) and mutate-between-calls semantics.
- [ ] Rollback if: any test red that was green before, OR the singleton fails to propagate (a function reads stale `None` after `main()` set it) → revert commit, approach falls back to B.

**Evidence (filled by /phased-implementation):**
- 2026-06-29 — Tracer assumption HELD: tests inject state via `rt.x` and the
  (still-in-main) pipeline functions read it correctly. Singleton propagation works.
- Automated checks:
  - `python -m pytest tests/ -q` → **1439 passed, 1 xfailed, 0 failed** (baseline 1436 + 3 new branch tests). Stable across 8 consecutive full-suite runs after clearing bytecode.
  - Orphan-global grep (`main.py`, then repo-wide `main._<global>`) → **0 matches**; module-global decls remaining in main.py → **0**.
  - `python -c "import ast; ast.parse(...)"` → main.py + pipeline_runtime.py parse OK.
  - Targeted: `pytest test_main_pipeline test_engine1_live_gate test_engine1_scorer` → 21 passed; `TestUncoveredBranches` → 3 passed.
- Flaky-failure note (investigated, NOT a regression): one early full-suite run during active editing showed 2 failures in `tests/test_shadow_queries.py` (`KeyError: recent_gross_profit`). That file does not import main/rt; the error path (`_finish` uses `dict.pop(..., None)`, cannot raise that KeyError) is not explicable from this diff. Did not reproduce in 8 subsequent runs incl. after `__pycache__` purge. Baseline (changes stashed) was stable 1436×3. Conclusion: transient stale-bytecode/collection artifact while files were mid-write, not caused by the rt conversion.
- Manual: `pipeline_runtime.py` reviewed — every migrated field keeps its original default (services `None`; `setup_dedup_cache={}`, `atr_history={}`, cooldowns `{}`, `last_setup_detected_time=0.0`, `bot_start_time=0.0`, `dry_spell_alerted=False`, `engine1_kill_alert_ts=0.0`, `emit_metric_failures=0`, `emit_metric_last_warn=0.0`). The `_EMIT_METRIC_*` list-holder trick (`[0]`) became plain scalar attributes (behaviour-identical; attribute assignment replaces the list-mutation hack).
- Rollback trigger fired: **no**.
- Files changed: `pipeline_runtime.py` (new), `main.py` (+183/−206), `tests/test_main_pipeline.py` (rt + 3 branch tests + extended reset fixture), `tests/test_engine1_live_gate.py` (rt). NOT yet committed (awaiting user OK).
- LOC delta (main.py): +183 / −206.

---

## Phase 2 — Extract leaf modules + dead-code kill
**Status:** done (commits c624394 move + fdd606d dead-code on chore/refactor-phase6-mainpy, not pushed)
**Inputs:** Phase 1 outputs — `rt` singleton live, all globals migrated, tests green.
**Outputs:** `persistence.py` (`_persist_ai_decision`, `_persist_ai_pre_filter`, `_persist_risk_event`, `_log_trade_rejection`, `_emit_metric`); `monitoring_loops.py` (`_session_alert_loop`, `_dry_spell_loop`, `_market_monitor_loop`, `_liquidation_alert_loop`, `_send_liquidation_alert`, `TRADING_SESSIONS` const + their module-level cooldown state now on `rt`); dead code removed.
**Work:**
- `git mv`-style relocation (preserve history): move the leaf writers + monitoring loops into the two new modules. They reference `rt` for state/services.
- main.py keeps thin re-exports so `main.<fn>` test/wiring references resolve.
- Separate commit "chore: drop dead daily-summary loop + unused trade_id param": delete `_daily_summary_loop()` + its `status_task` in `main()`; remove always-`None` `trade_id` param from `_persist_ai_decision` (+ its single caller).

**Verification gate:**
- [ ] Automated: `python -m pytest tests/ -q` → still 1409+ pass, 0 fail.
- [ ] Automated: `grep` orphan-global sweep across `main.py` + new modules → 0 stragglers.
- [ ] Automated: `python -c "import main, persistence, monitoring_loops"` → no ImportError / circular import.
- [ ] Manual: confirm dead-code commit is isolated (revertable on its own) and the two moves are logic-identical (diff = relocation only, no edits inside function bodies).
- [ ] Rollback if: import cycle, OR any monitoring loop changes cadence/behavior → revert the offending module's commit.

**Evidence:**
- 2026-06-30 — Two-commit split as planned.
  - Commit 1 (c624394) pure move: `persistence.py` (131 LOC: `_emit_metric`,
    `_persist_ai_decision`, `_persist_ai_pre_filter`, `_persist_risk_event`,
    `_log_trade_rejection`) + `monitoring_loops.py` (242 LOC: 4 alert loops +
    `_send_liquidation_alert` + `TRADING_SESSIONS` + threshold consts). Bodies
    unchanged; state/services via `rt`. main.py re-imports all symbols; dropped
    now-unused `datetime/timezone` + `liquidation_estimator` imports.
  - Commit 2 (fdd606d) dead-code, isolated/revertable: deleted `_daily_summary_loop`
    (+ `status_task` create + cancel-list entry); dropped always-None `trade_id`
    param from `_persist_ai_decision` (+ single call site).
- Automated checks:
  - `python -m pytest tests/ -q` → **1437 passed, 1 xpassed, 0 failed** (identical
    to the pre-move baseline on this branch; the 2-test delta vs Phase 1's 1439 is
    the shadow-PR-#115 tests absent off main, not a regression).
  - Import smoke: `python -c "import main, persistence, monitoring_loops"` → no
    ImportError / circular import; re-exports present (`main._emit_metric`,
    `main._session_alert_loop`, `main.TRADING_SESSIONS`, `main._send_liquidation_alert`).
  - Orphan-global sweep across all 4 modules → 0 (every service/state via `rt`).
  - `ast.parse` on all three files → OK.
- LOC: main.py 1524 → 1190 (−334); persistence.py +131; monitoring_loops.py +242.
- Branch-hygiene fix (pre-Phase-2): the Phase 1 tracer commit had landed on
  `feat/shadow-recency-decay` (stacked on the OPEN shadow PR #115, unpushed).
  Cherry-picked onto `chore/refactor-phase6-mainpy` (clean off main) as 359d3a7;
  reset shadow branch back to origin so PR #115 stays pure. Nothing was pushed.
- Rollback trigger fired: **no**.

---

## Phase 3 — Extract core (`ml_instrumentation` + `pipeline_router`); main.py → wiring only
**Status:** code done (commit 794e01b on chore/refactor-phase6-mainpy, not pushed); real-startup deploy smoke PENDING (last gate item)
**Inputs:** Phase 2 outputs — leaves extracted, tests green, imports clean.
**Outputs:** `ml_instrumentation.py` (`_ml_log_setup`, `_ml_resolve_outcome`, `_engine1_score_log`, `_engine1_kill_check`, `_engine1_emit_kill_alert`); `pipeline_router.py` (`on_candle_confirmed`, `_process_pipeline_setup`, `_evaluate_htf_pipeline`, `_evaluate_with_claude`, `_pre_filter_for_claude`, `_publish_strategy_state`); `main.py` ≈250 LOC = imports + `validate_config` + `_log_pair_diagnostics` + `_send_crash_alert` + `main()` + `__main__` + re-exports.
**Work:**
- Move ML instrumentation + the pipeline core (incl. the inert `engine1_live` branch — relocated verbatim, flag still OFF). Wire `DataService(on_candle_confirmed=…)` in `main()` via import from `pipeline_router`.
- Re-exports in main.py for every symbol a test references.
- `/doc-update`: tracker Phase 6 → DONE; SYSTEM_BASELINE §8 changelog line; update `project_refactor_solidify_2026_06` memory.

**Verification gate:**
- [ ] Automated: `python -m pytest tests/ -q` → 1409+ pass, 0 fail; `test_engine1_live_gate.py` + `test_engine1_scorer.py` + `test_main_pipeline.py` all green (proves dormant live branch + scorer + pipeline intact).
- [ ] Automated: final orphan-global grep across whole repo → 0.
- [ ] Manual (real-startup net, B-reinforced): `python main.py` (or test deploy `docker compose up -d --build bot`) boots, connects OKX WS, writes an `ml_setups` row, publishes strategy state to Redis — same as before. Run the 9-step `reference_deploy_verification` checklist.
- [ ] Manual: `main.py` line count ≈250; no logic left in it beyond wiring.
- [ ] Rollback if: bot fails to boot, OR `ml_setups` writes stop, OR any pipeline branch diverges in logs → redeploy previous image (Docker auto-restart) + `git revert` Phase 3 commit.

**Evidence:**
- 2026-06-30 — Core extracted in one focused commit (794e01b).
  - `ml_instrumentation.py` (leaf): `_ml_log_setup`, `_ml_resolve_outcome`,
    `_engine1_score_log`, `_engine1_kill_check`, `_engine1_emit_kill_alert`
    (+ `_ENGINE1_KILL_ALERT_TTL`). Imports `_emit_metric` from persistence.
  - `pipeline_router.py` (core): `_publish_strategy_state`, `on_candle_confirmed`,
    `_process_pipeline_setup`, `_evaluate_htf_pipeline`, `_evaluate_with_claude`,
    `_pre_filter_for_claude` (+ dedup-TTL consts). The dormant `engine1_live`
    branch relocated VERBATIM, flag `ENGINE1_LIVE_GATED_ENABLED` still OFF.
  - `main.py` 1190 → **390 LOC**: imports + re-exports + `validate_config`
    + `_log_pair_diagnostics` + `_send_crash_alert` + `main()` + `__main__`.
    `main()` wires `DataService(on_candle_confirmed=…)` via import from
    pipeline_router. (Slightly above the ~250 estimate — `_log_pair_diagnostics`
    + `validate_config` are larger than the grill guessed; both are pure
    wiring/startup, left in main by design.)
- Test fix (monkeypatch-after-move): `test_engine1_live_gate._run` patched
  `main._ml_log_setup / _engine1_score_log / _engine1_kill_check`; since
  `_process_pipeline_setup` now resolves those in `pipeline_router`'s globals,
  patches repointed to `pipeline_router.*` (+ `import pipeline_router`). All
  other `main.<fn>` test refs are CALLS (resolve via re-export); `main.settings.*`
  patches mutate the shared settings object so they still take.
- Automated checks:
  - `python -m pytest tests/ -q` → **1437 passed, 1 xpassed, 0 failed** (identical
    baseline; 8 stable on repeat).
  - Targeted `test_engine1_live_gate + test_engine1_scorer + test_main_pipeline`
    → **24 passed** (dormant live branch + scorer + pipeline callback intact).
  - Import smoke `import main, pipeline_router, ml_instrumentation` → no
    ImportError / circular import; 4 re-exports present on `main`.
  - `ast.parse` on all 5 modules → OK.
  - Repo-wide orphan sweep (`main._<global>` reads) → 0; dedup consts only in
    pipeline_router.
- Rollback trigger fired: **no** (automated gate). 
- PENDING gate item: real-startup deploy smoke (`docker compose up -d --build bot`)
  — boot, OKX WS connect, one `ml_setups` row written, strategy state published
  to Redis, 9-step `reference_deploy_verification`. Bot is shadow-pure so worst
  case is minutes of lost ML logging; awaiting user go to deploy.

## Out of scope (deliberately)
- **notifier/alert_manager `notify_*` overlap smell** — real but separate concern; its own PR (Phase 2 carry-over in tracker). Bundling it here inflates an already-large diff.
- **data_store.py / bybit_watcher.py splits** — Phase 6b territory; large-class decomposition, lowest priority, "only if appetite".
- **Any rename or logic improvement** — move ≠ refactor internals. A hidden behavior change inside a "while I'm here" edit is exactly what breaks a live pipeline.
- **config/settings.py split (Phase 5)** — skipped/deferred by user decision (weakest ROI, 1238-site churn, cosmetic).
- **Turning the engine1 live gate ON** — stays OFF; refactor preserves the dormant branch, does not re-activate it.

## Open questions (must resolve before starting)
- None outstanding. Verification method (B reinforced), extraction order (leaf→core, per-commit), scope boundary, dead-code-in-this-PR, and re-exports-for-tests all resolved in the grill (Q1–Q3, user-approved 2026-06-29).

## Changelog hook
On completion, append to `docs/SYSTEM_BASELINE.md` §8 changelog:
- `2026-06-?? — Refactor Phase 6: main.py (1546 LOC) split into pipeline_runtime + persistence + monitoring_loops + ml_instrumentation + pipeline_router (PR #N). Impact: NONE on live/shadow/ML behavior — pure structural hygiene; main.py → wiring-only ~250 LOC; dead _daily_summary_loop + unused trade_id param removed.`
