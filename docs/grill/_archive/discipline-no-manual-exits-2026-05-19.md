# Grill: Discipline commitment — no manual exits, no TP move, SL to BE only
**Date:** 2026-05-19
**Topic:** Trader confessed 3 behaviors (early market close, stuck close-limit, moved TP/SL) and asked what to improve in journaling. Pivoted away from new fields/UI to discipline commitment + minimal infra.
**Verdict:** PIVOT — KILL feature build, commit to operational refinement of existing Rules 10+11 + fix periodic Bybit sync. No new rule (Rule 13 freeze respected).

## Context loaded
- `CLAUDE.md`, `docs/SYSTEM_BASELINE.md` — bot SHADOW-ONLY, ~$86 untouched; ML v0 next re-run 2026-05-25.
- `MEMORY.md` — Bybit ~$4.6k real money. Phase 1 (`trigger_condition` + `thesis_invalidation`) shipped 2026-05-15 (commit b5f2ece).
- `docs/grill/bybit-rules-taxonomy.md` v3 — Rules 1-14, Rule 13 freeze until 2026-06-08, Rule 14 = weekly review.
- `docs/grill/_archive/simplify-manual-confluences-2026-05-16.md` — prior grill: PIVOT to LLM judge gated on Gate 0 (≥4/5 fill rate).
- DB queries:
  - 5 Bybit annotations post-2026-05-15 → only **1/5 filled** trigger_condition + thesis_invalidation + thesis_pre (20%). Gate 0 failing hard. All 5 had `order_id IS NULL` → Rule 6 enforcement (PR #29) doesn't fire because no tracked limit order.
  - 60d execs all-time: 27/39 closes via Market (69%), 12/39 via Limit. `bybit_executions` sync dead 2026-04-16 → 2026-05-19 (manual `sync_bybit.py` not invoked for 33 days).
  - Watcher already calls `sync_closed_pnl(days=1)` on close events but never `sync_executions`.

## Decision tree

### Q1: Is the problem "no place to journal X" or "no auto-measurement of X"?
**My recommended answer:** auto-measurement. More UI fields will not be filled — Gate 0 proves 4/5 trades skip the form. Discipline problems don't get solved by more form widgets.
**User answer:** Agreed.
**Grade:** ✅ survives.

### Q2: Of (a) discipline/blocking, (b) passive telemetry, (c) data quality for LLM judge — which is the primary objective?
**My recommended answer:** (c) primary + (b) weekly report as secondary. (a) deferred — Rule 6 enforcement already exists in that lane and Gate 0 shows it gets sidestepped via untracked Market orders.
**User answer:** Accepted recommendation.
**Grade:** ✅ survives.

### Q3: Counterfactual — would a self-imposed rule ("don't override pre-set exits") kill the problem cheaper than building auto-detection?
**My recommended answer:** Yes. Rules eliminate the data need; auto-detection without behavior change just logs telemetry on losses.
**User answer:** Accepted — committed to discipline.
**Grade:** ✅ survives.

### Q4: Scope of commitment — total freeze, partial (SL to BE allowed), or no-close-only?
**My recommended answer:** (II) partial. Free-trade SL move to breakeven at +1R is standard AFML position management (Lopez de Prado, ch. 13 path-dependent exits) and removes ~90% of discretionary variance while keeping defensible risk reduction.
**User answer:** (II).
**Grade:** ✅ survives.

### Q5: How to close the "but I might need to move TP if price reverses" loophole?
**My recommended answer:** β + γ together.
- β: configure TP on Bybit as **trigger-Market** (not trigger-Limit). Solves "stuck limit close" mechanically. Fee math: $0.175 maker savings per close vs $5-10 swing when TP misses. Trigger-Limit only wins if miss rate <1/57 — user's confession says miss rate is much higher.
- γ: manual close permitted **only** when price touches `thesis_invalidation` value already recorded pre-trade (Phase 1 field). All other manual closes = rule violation. Pre-trade definition removes retrospective bias.
**User answer:** Confused about whether code/orders would change. Clarified scope: zero changes to entry/exit code (bot is shadow-only on OKX, doesn't trade Bybit). User accepted β + γ as discipline commitment.
**Grade:** ✅ survives.

### Q6: Sub-question on fees raised by user
**Mechanical clarification:** maker 0.02% vs taker 0.055% — difference 0.035%. On ~$500 notional swing trade, $0.175 per close. Single missed TP = $5-10. Net: trigger-Market dominates for swing TP exits. Trigger-Limit valid only for tight-spread scalps (out of Bybit scope here).
**User answer:** Understood, no rebuttal.
**Grade:** ✅ survives.

## Final verdict

PIVOT, not BUILD. The original ask ("improve journaling flow") gets killed: no new UI fields, no new exit_reason columns, no LLM judge yet. Instead:

1. **Discipline commitment (no code, just behavior):**
   - After entry, TP does not move.
   - SL only moves to breakeven (free trade) once price runs +1R.
   - Manual close permitted only when price touches `thesis_invalidation` recorded pre-trade.
   - Any other override = violation. Tolerance ≤2 violations per 30 trades.
   - This is an **operational clarification** of existing Rules 10 + 11, not a new Rule (Rule 13 freeze respected — same precedent as Phase 1 framing).

2. **Bybit config (no code):** set TP on every new Bybit position with trigger-Market mode. Eliminates stuck-limit case.

3. **Minimal infra fix (one code change):** add periodic `sync_executions` + `sync_closed_pnl` loop to `bybit_watcher.py`. Replaces the manual `scripts/sync_bybit.py` cron the user wasn't running. Without this, no honest measurement of rule compliance.

4. **Re-evaluation date:** N=30 fully-compliant trades (per Rule 13 gate). At trader's current ~1 trade/day cadence, that lands late June 2026, dovetailing with the Engine 2 decision point (2026-06-15).

## If BUILD: pre-conditions for /phased-plan

N/A — verdict is PIVOT-no-build for the journaling extension. The single code change (periodic Bybit sync) is small enough to ship in the same commit as this grill doc.

## What would revive the journaling-extension build

- Discipline commitment fails: ≥3 violations in next 30 trades. Then auto-detection has a use case (force LLM judge to score "real edge" vs "edge if you had held"), proceed to phased plan for exit_reason classification + Bybit order amend log.
- OR: Gate 0 from `simplify-manual-confluences-2026-05-16.md` reaches ≥4/5 fill rate in next 30 trades → proceed to LLM smoke test, which then may demand exit_reason structure.

Until then, more fields = more empty fields.
