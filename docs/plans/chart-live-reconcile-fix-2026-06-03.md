# Plan: Chart live-candle reconciliation fix

**Slug:** chart-live-reconcile-fix-2026-06-03
**Source grill:** inline (this conversation, 2026-06-03) — light grill, fix to working tool
**Created:** 2026-06-03
**Status:** IMPLEMENTED 2026-06-03 on branch `fix/chart-live-reconcile` (phases 1-5 done). Pending: commit + PR. Verified: build clean, reconcile fires exactly 1 `/history` refetch on `visibilitychange`, chart renders, viewport preserved via offset, replay guarded.

## Confirmed scope of the bug (data proven clean)
User reported visual "connection gaps" on `/chart` for BTC/ETH (e.g. 1h, Jun-2
~15:00). **Verified the gap is NOT in the data:** every 1h candle around Jun-2
15:00 exists in PostgreSQL `candles` AND in the `/api/chart/history` response,
identical to OKX, fully continuous. DB has zero internal gaps on 5m/15m/1h since
2026-03-15 (only legacy gap = 4h/1h outage 2025-12-07..12, far-left, unrelated).

**Root cause (frontend, page.tsx:218-273 — live poll):**
The 2s live-candle poll rebuilds HTF (1h/4h) bars **client-side** from the 5m
Redis forming candle and **never re-fetches `/history`** after initial load.
Two consequences:
1. **Gap:** tab backgrounded → browser throttles/pauses `setInterval` → a poll
   lands ≥1 HTF period later → `formed.timestamp > last.timestamp` pushes the new
   bar but the skipped period(s) were never pushed → permanent hole in that
   session (never reconciled because `/history` is never re-pulled).
2. **Synthetic closed bars (broader bug):** every forming bar pushed (open=prev
   close, `volume:0`, OHLC approximated from 5m polls) is **never replaced** by
   the real closed candle. In a long session every closed HTF bar is synthetic —
   volume 0, approximate OHLC — diverging from the exchange.

**Fix:** reconcile against `/history`. Decisions (grilled 2026-06-03):
- Scope = **full reconciliation** (fixes gap AND synthetic closed bars).
- **Fix direct** (no repro-first — `/history` re-fetch reconciles any desync
  regardless of exact mechanism; cheap + safe).
- Approach = **visibilitychange + period-skip detection**, preserving viewport.

**Files:** `dashboard/web/src/app/chart/page.tsx` (logic), possibly
`dashboard/web/src/lib/chartDatafeed.ts` (reuse fetch), `docs/context/06-dashboard.md` (doc).
**Out of scope:** bot/data/Redis, other dashboard routes, SYSTEM_BASELINE
(no bot-config change). Isolated to `/chart`.

## Phase 1 — `reconcile()` lightweight
**Goal:** re-pull closed bars from `/history` and merge, WITHOUT the heavy `load()`
side effects (no spinner, no viewport reset, no asOfIdx reset).
**Work:** new `reconcile()` — `fetchHistory(symbol, resolution)` → replace
`barsRef.current` with real closed bars → re-append current forming bar if it
hasn't closed. Silent (no `loading`/`error` state). Guard `if (replay) return`.
**Gate:** function exists, unit-testable; manual call reconciles a desynced array.

## Phase 2 — Trigger on tab refocus
**Goal:** kill the dominant case (backgrounded tab on phone/Tailscale).
**Work:** `useEffect` listening `document.visibilitychange`; on `visible` &&
!replay → `reconcile()`. Debounce/guard if reconciled <Ns ago.
**Gate:** background tab ≥1 HTF period, return → gap gone, closed bars have real volume.

## Phase 3 — Period-skip detection in the poll
**Goal:** cover foreground desync (missed poll, slow net).
**Work:** in poll (page.tsx:~254), before blind push: if
`formed.timestamp > last.timestamp + pms` (skipped ≥1 period) → call `reconcile()`
instead of `bars.push(formed)`.
**Gate:** simulated skip triggers reconcile, no hole.

## Phase 4 — Preserve viewport + no regressions
**Goal:** reconcile must not jump scroll or break replay/detections overlay.
**Work:** klinecharts `applyNewData` resets scroll → save visible range/offset
before, restore via `scrollToDataIndex` after. Anti-flicker: only re-apply if
closed bars changed (last ts + length). Verify replay mode and detections effect
(page.tsx:205) still valid post-reconcile in live mode.
**Gate:** scroll position preserved across reconcile; replay unaffected.

## Phase 5 — Verify
- `cd dashboard/web && npm run build` (type + bundle; klinecharts stays lazy on /chart).
- Browser: 1h BTC, background tab / force skip, return → no gap, real volume.
- Mobile 375px: nothing breaks.
- Replay mode still works (does not reconcile).
- Compare closed-bar OHLC+volume vs OKX → match.

## Docs
- `docs/context/06-dashboard.md`: document live reconciliation behavior (`/doc-update`).
- No SYSTEM_BASELINE change (not bot config).

## Risks / rollback
- Race on `barsRef` during 2s poll → reconcile replaces array atomically, forming
  bar re-derived from last closed.
- Scroll jump if viewport-preserve fails → Phase 4 + explicit test.
- `visibilitychange` noise → debounce guard.
- **Rollback:** isolated PR to `/chart`; revert = remove 2 effects + 1 fn. Zero
  impact on bot/data/other routes.
