# Plan: engine1 entry-gate (low-impulse filter)
**Slug:** engine1-entry-gate
**Source grill:** docs/grill/engine1-entry-gate-2026-06-08.md
**Created:** 2026-06-08
**Status:** ABANDONED — KILLED 2026-06-11 (forward validation FAILED)
**Tracer bullet:** Does the low-impulse edge hold on genuinely FUTURE shadow data (not just a historical out-of-sample split)? If forward PF collapses to baseline, the whole gate is curve-fit and dies — at zero code cost.

## POST-MORTEM (2026-06-11)
Forward validation FAILED. Tracer answered NO. Forward cohort (`created_at > '2026-06-08'`, N=77) gave gated subset PF **0.94** vs ungated **0.59**. Gate beat ungated by +0.35 (clears the relative bar) BUT missed the absolute **PF ≥ 1.5** bar — gated arm is still losing money. Rollback condition (Phase 1, line: "forward PF < 1.5 → KILL") FIRED.

Root cause: the in-sample 4.5 PF and 5/5 walk-forward folds were **tercile noise**. Forward impulse median (2.453) drifted above the frozen 2.24 cutoff — regime shifted, the low-impulse population the gate keyed on no longer behaves the same. Curve-fit to a historical regime, not a stable edge.

Whole-engine context: ungated engine1 forward PF 0.59 = engine1 itself still bleeding. The gate was the last lever and it didn't clear. engine1 stays PARKED (weak *signal*, not a management/plumbing problem).

**Disposition:** Phase 2 code (PR #80) stays `ENGINE1_IMPULSE_GATE_ENABLED=False` permanently, left inert as an audit trail — do NOT enable. No further phases. Plan closed. Detail → memory `project_engine1_gate_forward_validation`.

## Context summary
engine1_trend_pullback is break-even raw (PF ~1.0 on v1d). In-sample slicing + a frozen-cutoff chronological holdout showed that filtering to the LOW tercile of `engine1_impulse_atr_multiple` (cutoff ≤ **2.24**) lifts out-of-sample PF to ~4.5 (testN=33, distributed across 11 winners, post-fee). This plan validates that forward, then — only if it survives — implements the gate. It changes WHICH engine1 signals are taken; it does NOT change the signal, the geometry, or any feature definition (so **no ML_FEATURE_VERSION bump**). Stays shadow throughout. Live execution is explicitly out of scope (gated separately by SYSTEM_BASELINE §7.1).

## Phase 1 — Pre-registered forward validation (NO CODE)
**Status:** FAILED — KILLED 2026-06-11 (forward PF 0.94 < 1.5 bar; see POST-MORTEM above)
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

**Walk-forward evidence (added 2026-06-08, existing data):** 5 rolling-origin folds, cutoff re-derived per fold's train. Gate beat ungated baseline PF in **5/5 folds** (gatePF 12.4 / 6.5 / 7.0 / 12.9 / 1.48 vs base 2.67 / 1.52 / 3.97 / 0.9 / 0.47); 4/5 also cleared the absolute PF≥1.5 bar (fold 5 = 1.48, narrow miss but still 3× its baseline). Robust across time windows, not a single-split artifact. Script: ad-hoc walk-forward (see session) — fold over `scripts/validate_engine1_gates_oos.py` logic.

**Timeline note:** engine1 v1d emits ~21 resolved/day, so **N≥50 forward rows accrue in ~2–3 days**, not weeks. The 30-day clause is only a backstop ceiling.

**Verification gate:**
- [x] Automated: forward cohort reached N=77 post-freeze rows (>2026-06-08). Gated PF **0.94** vs ungated **0.59** — FAILS absolute PF≥1.5 bar; beats ungated by +0.35 (relative bar passes).
- [x] Manual: moot — absolute bar already failed.
- [x] Rollback FIRED: forward PF 0.94 < 1.5 → gate KILLED, plan abandoned, post-mortem written. Did NOT proceed to enabling Phase 2.

**Evidence:**
Forward N=77, gated PF 0.94, ungated PF 0.59. Frozen cutoff 2.24 stale vs forward impulse median 2.453 (regime drift). In-sample 4.5 PF + 5/5 walk-forward = tercile noise. See POST-MORTEM at top.

---

## Phase 2 — Implement gate in engine (shadow only)
**Status:** CODE SHIPPED default-OFF 2026-06-08 (PR #80 follow-up). Stays `ENGINE1_IMPULSE_GATE_ENABLED=false` PERMANENTLY — Phase 1 forward validation FAILED 2026-06-11. Code left inert as audit trail; do NOT enable. (Built ahead of Phase 1 because walk-forward looked robust, but forward data killed the edge — see POST-MORTEM.)
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
- [x] Automated: tests passed at ship time (PR #80 follow-up), default-off emission count byte-identical to pre-change.
- [x] Manual: gated flag confirmed in `ml_setups` rows, emissions partitioned sanely.
- [x] Rollback N/A: default-off shipped clean; gate never enabled (Phase 1 failed).

**Evidence:** Code merged default-OFF, never activated. Permanently inert per POST-MORTEM.

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
