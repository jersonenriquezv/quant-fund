# Plan: Shadow orphan leak fix

**Slug:** shadow-orphan-leak-fix-2026-06-02
**Source grill:** docs/grill/shadow-orphan-leak-2026-06-02.md
**Created:** 2026-06-02
**Status:** root-cause CONFIRMED 2026-06-02 — fix shipped (see "Confirmed root cause" below)

## Confirmed root cause (2026-06-02, post-deploy breadcrumb)
The instrument-first deploy (#64/#65) produced the breadcrumb
`Shadow restore from Redis: skipped — Redis unavailable` on restart. The real
bug is **initialization ordering**, NOT corrupt records or max_age:
- `main.py:1212` constructs `DataService` (builds `RedisStore()` but does **not** connect).
- `main.py:1255` constructs `ShadowMonitor`, whose `__init__` called `_load_from_redis()` → `_get_redis()` saw an unconnected client → returned None → **restore silently skipped**.
- `main.py:1308` later runs `DataService.start()` which calls `self._redis.connect()` (`data_service/service.py:436`) — too late.
Result: restore no-opped on **every** restart → all in-flight shadows lost → aged out as `shadow_orphaned`. Consistent with 0 resolve-errors + 0 save-errors (those run at runtime, Redis up) and per-restart orphan batches.

**Fix (shipped):** defer restore — `__init__` no longer restores; a one-time
`_ensure_restored()` runs on the first `check_candle` tick, by which point
candles are flowing so Redis/Postgres are guaranteed connected. Phase 1's
per-record isolation (#64) is retained as defence-in-depth.
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

## Phase 2 — Defer restore until Redis is connected ✅ DONE (the actual fix)
**Goal:** restore must run when Redis is up, not in `__init__` (always pre-connect).
**Work (shipped):** `__init__` sets `_restored=False` and no longer restores; `_ensure_restored()` runs the one-time `_load_from_redis()` + `_cleanup_orphaned_db_rows()` on the first `check_candle` tick. Idempotent.
**Gate:** ✅ `TestShadowRestoreDeferral` — restore NOT in `__init__`, runs once on first tick, not re-run after. Full suite 1356 passed.

> The original speculative Phase 2 (resolve-on-eviction) is moot: with restore actually working, positions are no longer evicted unresolved. Kept only as a note.

## Phase 3 — Latent correctness: drop only on confirmed write (PARKED)
**Status:** parked — `shadow_outcome_resolved_error=0` in production, so the
drop-after-failed-write path doesn't fire. Defence-in-depth only; revisit if
resolve errors ever appear. Not blocking the orphan fix.

## Verification (all phases)
```bash
python -m pytest tests/test_execution.py tests/test_shadow_monitor_sizing.py tests/test_shadow_infra.py -v --tb=short
```
Post-deploy: watch `shadow_outcome_resolved_*`, `shadow_redis_*` metrics + orphan count over the next 3 restarts.

## Docs
Update SYSTEM_BASELINE §changelog (behavior fix) + `docs/context/05-execution.md` shadow section per `/doc-update` rules after the fix lands.
