# Grill: Shadow orphan leak

**Date:** 2026-06-02
**Topic:** In-flight shadow positions are lost across bot restarts and end up marked `shadow_orphaned` (90 since 2026-04-15, in batches, ~2/day avg). Source: Telegram alert "Shadow orphans spiking" (`scripts/shadow_health_alert.py` → `check_orphans`, bar `SHADOW_ORPHAN_ALERT=5`).
**Verdict:** BUILD — proven leak on a working-but-leaking tool. Bug fix, not net-new edge. Shadow-only, low blast radius.

## Evidence
- Bot started `2026-06-01T05:16:33Z` (`docker inspect`, `RestartCount=0`). The 6 orphans flagged were created `05:00–05:15` — in-flight at that restart.
- They were swept 24h later (`2026-06-02 05:20`) by the periodic 6h `_cleanup_orphaned_db_rows`, not at the restart — the 24h delay proves they were lost at the restart but too young (< `max_age` = `SHADOW_ENTRY_TIMEOUT_HOURS + SHADOW_TRADE_TIMEOUT_HOURS` = 24h) to sweep then.
- `bot_metrics`: `shadow_outcome_resolved_ok=3187`, `shadow_outcome_resolved_error=0`, `shadow_redis_save_error=0`. → resolves are NOT failing; the save is NOT failing. Positions leave `_positions` **without** ever calling `_resolve`.
- Orphans arrive in batches (11, 9, 6, 5, 3, 2…) at distinct `resolved_at` timestamps → mass-loss events aligned to restarts.
- 90 total: 54 unfilled + **36 filled** (the filled ones had a real outcome that was discarded).
- All 90 have `shadow_mode=t` + size set → `add_shadow` tracked them; they were genuinely in flight.

## CONFIRMED root cause (2026-06-02, post-deploy)
The instrument-first deploy produced the breadcrumb `Shadow restore from Redis:
skipped — Redis unavailable`. The real bug was a **4th, simpler one not in the
list below**: `ShadowMonitor.__init__` (main.py:1255) runs `_load_from_redis()`
BEFORE `DataService.start()` (main.py:1308) connects Redis → `_get_redis()`
returns None → restore silently skips on **every** restart. Fix: defer restore
to the first `check_candle` tick (`_ensure_restored`). The 3 candidates below
were the pre-instrumentation hypotheses — kept for the record; candidate #1
(per-record isolation) shipped as defence-in-depth.

## Root cause (3 candidate bugs in `shadow_monitor.py`) — pre-instrumentation hypotheses
1. **`_load_from_redis` is all-or-nothing** (lines ~594–618): one `try/except` wraps the whole restore loop. If any single `ShadowPosition(**fields)` raises (schema drift between snapshot and code), the entire restore aborts → every in-flight position lost → mass orphan. No per-record guard.
2. **No resolution on eviction**: when a position falls out of `_positions` (restart didn't restore it), its DB row stays NULL and `_cleanup_orphaned_db_rows` marks it `shadow_orphaned` — discarding a recoverable outcome instead of resolving it.
3. **Latent**: `del self._positions[sid]` runs after `_resolve` even if the DB write failed (0 errors observed today, but a correctness bug — should drop only on confirmed write).

The Telegram alert's own hint ("investigate `_save_to_redis`") points at the wrong method — save works (0 errors); the **load** is the fragile path.

## Cost / no training corruption
`shadow_orphaned` is already in the training-exclusion stop-list (memory `project_ml_v0_baseline`), so no training corruption. Cost = lost shadow samples + recurring false Telegram alerts.

## Decision tree
### Q1 — Root-cause certainty / falsifiability
**Answer:** A — **instrument first** (or alongside). Add diagnostic logging to `_load_from_redis` (raw record count, per-record skip/throw reason, restored vs dropped) so the next restart confirms WHICH mechanism fires. The three fixes are defensible regardless, but the primary fix must target the confirmed trigger or falsification is impossible.
**Grade:** ✅ Confirmed mechanism before claiming a fix.

### Q2 — Eviction semantics + backfill
**Answer:** (a) going forward, **resolve** a position that can't be restored using available candles instead of orphaning it. **No backfill** of the 90 existing orphans (effort/risk high for stale data; focus on stopping the leak).
**Grade:** ✅ Recover forward, don't chase old data.

### Q3 — Falsification + scope
**Falsification:** orphans/24h → **0** across ≥3 restarts/deploys. The Telegram alert (`SHADOW_ORPHAN_ALERT=5`) is the sentinel; recurring restart-aligned batches = fix incomplete, reopen.
**Scope (do NOT touch):** only `shadow_monitor.py` (+ tests). Zero changes to `monitor.py`/`executor.py`/`service.py` (real-order arm), zero `ML_FEATURE_VERSION` / feature-column change, zero training-query change.
**Cost:** ~1 instrumentation commit + ~1 fix commit (3 parts) + tests. Low.
**Grade:** ✅ Closed scope, clear sentinel.

## Final verdict — BUILD
Phased: instrument → confirm → fix the confirmed trigger + the two defensible hygiene fixes → resolve-on-eviction forward. See `docs/plans/shadow-orphan-leak-fix-2026-06-02.md`.
