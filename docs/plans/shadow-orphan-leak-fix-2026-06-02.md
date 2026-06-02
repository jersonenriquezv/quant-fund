# Plan: Shadow orphan leak fix

**Slug:** shadow-orphan-leak-fix-2026-06-02
**Source grill:** docs/grill/shadow-orphan-leak-2026-06-02.md
**Created:** 2026-06-02
**Status:** in-progress
**Scope:** `execution_service/shadow_monitor.py` + `tests/` only. Shadow-only, no real-order code, no feature-version bump, no training-query change.
**Falsification:** orphans/24h → 0 across ≥3 restarts. Sentinel = Telegram `SHADOW_ORPHAN_ALERT`.

## Phase 1 — Instrument the restore path (diagnostic, ship first)
**Goal:** the next restart tells us WHICH loss mechanism fires before we claim a fix.
**Work (`_load_from_redis`):**
- Log raw record count read from the Redis snapshot.
- Per-record: on `ShadowPosition(**fields)` failure, log the exception + setup_id (do NOT yet change behavior beyond not aborting — see note).
- Log restored vs skipped(max_age) vs failed counts at the end.
- Emit `shadow_redis_restored` / `shadow_redis_load_dropped` metrics with a reason label.
**Note:** Phase 1 already needs the per-record try/except to *observe* per-record failures — so candidate-bug #1 fix lands here naturally. That is intended: it is the most defensible fix and enables the diagnosis.
**Gate:** unit test proving one bad record no longer aborts the whole restore; deploy; wait for ≥1 restart; read logs/metrics.

## Phase 2 — Resolve-on-eviction (forward recovery)
**Goal:** a position that can't be restored is resolved at its last-known state instead of being orphaned.
**Work:** when the restore drops a position (or on the cleanup path), if enough state exists (filled + last candle), route through `_resolve` with the correct outcome (timeout/sl/tp from candle history) rather than leaving the row NULL for `_cleanup_orphaned_db_rows`. Define the exact data available and the fallback when it isn't.
**Decision deferred to implementation:** exact mechanism (resolve during load vs a reconcile pass) — depends on Phase 1 findings.
**Gate:** unit test: an evictable filled position resolves to a real outcome, not `shadow_orphaned`.

## Phase 3 — Latent correctness: drop only on confirmed write
**Goal:** `del self._positions[sid]` must not drop a position whose `_resolve` DB write failed.
**Work:** `_resolve` returns success/failure; `check_candle` only removes from `_positions` (and Redis) on confirmed write. On failure, keep tracking so a later tick retries.
**Gate:** unit test: simulated write failure keeps the position tracked; no silent drop.

## Verification (all phases)
```bash
python -m pytest tests/test_execution.py tests/test_shadow_monitor_sizing.py tests/test_shadow_infra.py -v --tb=short
```
Post-deploy: watch `shadow_outcome_resolved_*`, `shadow_redis_*` metrics + orphan count over the next 3 restarts.

## Docs
Update SYSTEM_BASELINE §changelog (behavior fix) + `docs/context/05-execution.md` shadow section per `/doc-update` rules after the fix lands.
