# Plan: engine1 entry-gate (low-impulse filter)
**Slug:** engine1-entry-gate
**Source grill:** docs/grill/engine1-entry-gate-2026-06-08.md
**Created:** 2026-06-08
**Status:** pending
**Tracer bullet:** Does the low-impulse edge hold on genuinely FUTURE shadow data (not just a historical out-of-sample split)? If forward PF collapses to baseline, the whole gate is curve-fit and dies — at zero code cost.

## Context summary
engine1_trend_pullback is break-even raw (PF ~1.0 on v1d). In-sample slicing + a frozen-cutoff chronological holdout showed that filtering to the LOW tercile of `engine1_impulse_atr_multiple` (cutoff ≤ **2.24**) lifts out-of-sample PF to ~4.5 (testN=33, distributed across 11 winners, post-fee). This plan validates that forward, then — only if it survives — implements the gate. It changes WHICH engine1 signals are taken; it does NOT change the signal, the geometry, or any feature definition (so **no ML_FEATURE_VERSION bump**). Stays shadow throughout. Live execution is explicitly out of scope (gated separately by SYSTEM_BASELINE §7.1).

## Phase 1 — Pre-registered forward validation (NO CODE)
**Status:** pending
**Inputs:**
- Frozen cutoff: `engine1_impulse_atr_multiple <= 2.24` (33rd pct of v1d resolved cohort, N=340, as of 2026-06-08).
- Freeze line: only rows with `created_at > 2026-06-08` count as forward/out-of-time.
- Existing data collection (engine1 already emits in shadow under v1d) — no change needed.
**Outputs:**
- A forward-only validation report: PF/WR of gated vs ungated engine1 on post-freeze rows.
- Go/no-go decision recorded in this plan + grill falsification line.
**Work:**
- Pre-register the test NOW (this file is the registration — prevents moving goalposts, the antidote to data dredging).
- Keep collecting shadow rows as-is. No code, no deploy.
- After the gate condition below is met, re-run `scripts/validate_engine1_gates_oos.py` adapted to filter `created_at > '2026-06-08'` only (or a one-off query) — measure forward PF of the `<=2.24` subset vs the full forward cohort.

**Verification gate:**
- [ ] Automated: forward cohort reaches **N ≥ 50 resolved** post-freeze rows OR **30 days elapsed** (2026-07-08), whichever first. Then: gated-subset **PF ≥ 1.5** AND gated PF beats ungated forward PF by **≥ 0.3**.
- [ ] Manual: confirm forward winners are distributed (top-win share < 30%), not 1-2 fat trades.
- [ ] Rollback if: forward PF < 1.5 OR gated does not beat ungated by ≥0.3 → KILL the gate, mark plan abandoned, write one-line post-mortem. Do NOT proceed to Phase 2.

**Evidence (filled by /phased-implementation):**
<empty until phase runs>

---

## Phase 2 — Implement gate in engine (shadow only) — ONLY if Phase 1 passes
**Status:** pending (blocked by Phase 1)
**Inputs:** Phase 1 pass + the frozen cutoff value (or a re-derived value if Phase 1 recommends, recorded explicitly).
**Outputs:**
- `engine1_impulse_atr_multiple` gate wired in `strategy_service/engines/trend_pullback.py`, controlled by a new `settings.ENGINE1_IMPULSE_GATE_MAX` (default off / very high so behavior is unchanged until deliberately enabled).
- An always-on `extra_features["engine1_gate_low_impulse_pass"]` boolean so future analysis can A/B without a parallel setup_type.
- Tests + SYSTEM_BASELINE §thresholds entry.
**Work:**
- Add `ENGINE1_IMPULSE_GATE_MAX` to `config/settings.py` (per strategy CLAUDE.md rule: no hardcoded thresholds in engine).
- In `trend_pullback.evaluate()`: compute the flag from `impulse.atr_multiple`; when gate enabled and value > max, return None (suppress); always stamp the boolean feature regardless.
- Tests in `tests/test_engine_trend_pullback.py`: gate pass/suppress, flag stamped both ways, default-off leaves emissions unchanged.
- Update `docs/SYSTEM_BASELINE.md` §thresholds + §setup-status.

**Verification gate:**
- [ ] Automated: `pytest tests/test_engine_trend_pullback.py tests/test_engine1_benchmarks.py tests/test_report_engine1_shadow.py -v` — 0 failures; a test proving default-off emission count is byte-identical to pre-change.
- [ ] Manual: deploy to shadow, confirm gated flag appears in new `ml_setups` rows and partitions emissions sanely.
- [ ] Rollback if: default-off changes any existing emission, or tests regress → revert commit.

**Evidence (filled by /phased-implementation):**
<empty until phase runs>

---

## Out of scope (deliberately)
- **Live execution.** Gating engine1 for real money is governed by SYSTEM_BASELINE §7.1 (G1–G6), untouched here. This plan is shadow-only.
- **The other two gates** (`pullback_depth_pct` low, `entry_atr_distance` high) and any combination. Pairwise/triple gates starved N out-of-sample (combined N=4). Single impulse gate only.
- **ML feature changes.** Gate filters an existing captured feature → no version bump.

## Open questions (resolved before Phase 1)
- Cutoff = 2.24, single `impulse_atr_multiple` gate? **Resolved: yes** (grill candidate; pairwise starves N).
- Forward = post-2026-06-08 rows only? **Resolved: yes** (pre-registration freeze line).

## Changelog hook
On completion, append to `docs/SYSTEM_BASELINE.md` §8 changelog:
- `<date> — engine1 low-impulse entry gate <validated/killed> (PR #N). Forward PF <x> vs ungated <y> over N=<n>. Impact: shadow-only, no live, no ML version bump.`
