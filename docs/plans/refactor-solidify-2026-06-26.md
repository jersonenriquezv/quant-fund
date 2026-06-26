# Refactor & Solidify — Pre-VPS Cleanup (2026-06-26)

**Goal:** Understand the system 100%, remove dead code, split god-files, and document
each service — before migrating to a VPS. Driven service-by-service, one session each.

**Non-goal:** This does NOT improve trading edge. Refactor = hygiene + comprehension.
Zero expected change to WR/PnL. Only indirect bot benefit: fewer places for bugs to hide
(cf. partial-candle bug). Decision to do this taken at 300+ collected trades.

## ▶ RESUME — next session (start here)

1. **Merge PR #103 first** (`chore/refactor-pre-vps-cleanup`, phases 0-2, tests green).
   Pending from 2026-06-26 — user deferred the merge.
2. After merge: `git checkout main && git pull`, then branch `chore/refactor-phase3-data`.
3. **Phase 3 — data_service/ study** (Layer 1, already 0 dead — comprehension only, no
   deletions expected). Caveman walkthrough like Phase 2 did for shared/. Then document.
4. Carry-overs / open threads:
   - Pre-existing uncommitted mods on `fix/chart-perf-cpu`: `docs/plans/dual-thrust-phase1b-shadow-wiring.md`, `docs/plans/engine1-entry-gate.md` (NOT part of refactor — leave or handle separately).
   - `scripts/alert_ml_milestone.sh` still untracked (user's ML-milestone systemd helper — decide whether to commit).
   - Deferred refactor: notifier.py / alert_manager.py notify_* overlap → its own PR (Phase 2 smell).

## Key distinction: DEAD vs UNREACHABLE

- **DEAD** = nobody imports it → safe to delete.
- **UNREACHABLE** = imported but never runs at runtime because bot is shadow-only
  (`ENABLED_SETUPS=[]`, `HTF_CAMPAIGN_ENABLED=false`, etc.). This is **dormant code, the
  path back to live trading. DO NOT DELETE.** ~68% of ai/execution is dormant by design.

## System map (2026-06-26 baseline, ~73k LOC Python)

| Layer | Files | LOC | State |
|---|---|---|---|
| data_service | 17 | 8.8k | solid, 0 dead |
| strategy_service | 15 | 6.4k | solid, dead stubs inside live files |
| ai_service | 4 | 0.7k | UNREACHABLE (bypassed) — keep dormant |
| risk_service | 5 | 1.0k | live (dry-run) |
| execution_service | 9 | 5.0k | 68% UNREACHABLE (shadow) — keep dormant |
| shared | 7 | 2.4k | solid |
| main.py | 1 | 1.4k | god-file |
| config | 1 | 1.2k | flat monolith (264 fields) |
| scripts | 54 | 18.5k | dead zone (one-off scripts) |
| dashboard + telegram | 33 | 5.2k | solid, 0 dead |

## Phases (one session each, easy → hard)

### Phase 0 — Plans consolidation ✅ DONE 2026-06-26
Root `/plans` (4 stale March concept docs) merged into `docs/plans/_archive/`. Root dir
removed. `notes.md` stale path fixed. One plans folder now: `docs/plans/`.

### Phase 1 — scripts/ dead-code purge — HIGH-conf DONE 2026-06-26
Delete one-off scripts whose job is done and nothing references (cross-checked vs
.claude/ skills, systemd/ timers, docs/). ~6.5k LOC target.

13 HIGH-confidence scripts deleted (verified: zero refs in live code / skills / systemd /
tests; only docs narrate them historically, which is fine). 1415 tests still collect clean.
MEDIUM-confidence batch still PENDING (next pass).

HIGH-confidence delete (✅ done):
- `repair_partial_candles.py`, `flag_partial_candle_ml.py` (bug fixed, merged)
- `dual_thrust_candle_parity.py`, `dual_thrust_shadow_parity.py` (gates PASS)
- `validate_engine1_gates_oos.py`, `analyze_engine1_entry_gates.py` (gate killed)
- `be_knob_comparison.py`, `engine1_fillrate_study.py`, `scalp_fee_viability.py`,
  `scalp_silent_detector_audit.py` (analyses done, findings in memory/docs)
- `backfill_bybit_annotations.py`, `bybit_ping.py`, `check_public_ip.py`

MEDIUM-confidence batch ✅ verified 2026-06-26 — Explore agent's list was too aggressive,
2 candidates were LIVE. Only 4 truly-done one-offs deleted:
`chart_c3_fidelity.py`, `topdown_edge_hunt.py`, `study_1d_veto.py`, `ml_manual_report.py`.

KEPT (real refs found — DO NOT delete):
- `chart_retest_stats.py` → used by live `dashboard/api/routes/chart.py`
- `dual_thrust/forward_resim.py` → active systemd `dual-thrust-forward.service`
- `dual_thrust/okx_revalidation.py` → refs in `execution_service/dual_thrust_shadow.py` +4
- `backtest_bootstrap.py`, `backtest_stability.py` → imported by tests
- `cascade_shadow.py` → PARKED experiment (awaits N≥30), not dead
- `reconcile_bybit_partial_pnl.py` → break-glass repair tool (`--apply` re-runnable)
- `backtest_regime_split.py` → reusable diagnostic util

scripts/ went 52 → 35 .py files across phase 1 (17 deleted total).

KEEP (run by skills/systemd/cron or active dev): backtest, optimize, sync_bybit,
report_*, daily_status, signal_scanner, weekly_edge_audit, topdown_push,
weekly_review_bybit, check_docs_truth, feature_importance, topdown_snapshot, explain_bot,
classify_sl_failures, dual_thrust_parity, dual_thrust_live_check, compute_bybit_mae_mfe,
pretrade_check, shadow_health_alert, ml_v0_engine1, ml_v1_*, backtest_topdown,
fetch_history, reconcile_topdown_falsification.

### Phase 2 — shared/ study + document — STUDIED 2026-06-26 (no code change)
Smallest, foundational (data contract for whole system). Read-through done; map:
- `models.py` (216) — THE CONTRACT. 14 frozen dataclasses, one per layer output
  (Candle/MarketSnapshot → TradeSetup → AIDecision → RiskApproval). frozen=True enforces
  the "no raw dicts between layers" rule. MarketSnapshot = only mutable (aggregator bundle).
- `pnl_engine.py` (199) — pure TP/SL/BE state machine. Single source of truth shared by
  shadow_monitor + backtest + execution so all three agree on win/loss. No DB/IO.
- `logger.py` (88) — loguru wrap, pytest-aware (no file sink in tests).
- `notifier.py` (260) — raw Telegram transport (fire-and-forget HTTP).
- `alert_manager.py` (642) — smart layer over notifier: priority, rate-limit, auto-silence.
- `ml_features.py` (1018) — feature factory: extract_setup_features + ~15 indicator helpers
  (RSI/ADX/Bollinger/Stoch/WaveTrend) → ml_setups table. Biggest file.

SMELL (deferred to its own PR, not cleanup): notifier.py and alert_manager.py both expose
notify_* methods → confusing ownership. Fix: raw notify_* stays in notifier, routing-only
in alert_manager.

### Phase 3 — data_service/ study + document — PENDING
Layer 1, already clean (0 dead). Study + document only. No deletions.

### Phase 4 — strategy_service/ dead stubs + study — PENDING
Delete dead setup stubs inside live files:
- `quick_setups.py`: `evaluate_setup_c/e/h()` → return None (removed 2026-04-13)
- `scalp_setups.py`: `evaluate_sweep_choch/vol_cvd_divergence/funding_extreme()` → killed
Keep tests that assert None? Decide: delete stub + its assert-None test together.
Then study SMC detectors. Smell: `setups.py` 1616 LOC monolith → defer split to Phase 6b.

### Phase 5 — config/settings.py split — PENDING
264 flat fields → split into `RiskConfig`, `StrategyConfig`, `ExchangeConfig`, `MLConfig`
sub-dataclasses. High blast radius (every service imports) — needs full test pass after.

### Phase 6 — main.py god-file split (final boss) — PENDING
1416 LOC → extract:
- `pipeline_router.py` — on_candle_confirmed + _process_pipeline_setup + HTF path
- `monitoring_loops.py` — session/dry_spell/market_monitor/liquidation loops
- `ml_instrumentation.py` — _ml_log_setup + _ml_resolve_outcome
- `persistence.py` — _persist_* + _log_* writers
Delete dead: `_daily_summary_loop()` (sleep 86400, disabled), `_persist_ai_decision`
unused `trade_id` param. Globals → PipelineState dataclass.

### Phase 6b (optional) — setups.py / monitor.py decomposition — DEFERRED
Big refactors of large classes. `monitor.py` (1576) is dormant — lowest priority.
`setups.py` (1616) extract OBSelector/TPCalculator/SetupGeometry. Only if appetite.

### Phase 7 — ai/risk/execution study (NO logic changes) — PENDING
Read + document the dormant live-trading path. Understand it for the day shadow ends.
Touch nothing functional.

## Rules for every phase
- After changes: run `python -m pytest tests/ -v` (must stay green).
- Follow `/doc-update`: SYSTEM_BASELINE for config, docs/context/ for behavior.
- One PR per phase off a feature branch (never commit to main).
- Deletions: `git mv`/`git rm` so history is preserved/traceable.
