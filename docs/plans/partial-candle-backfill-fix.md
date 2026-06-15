# Plan: Partial-candle backfill bug fix

**Slug:** partial-candle-backfill-fix
**Source grill:** docs/grill/partial-candle-backfill-fix-2026-06-15.md
**Created:** 2026-06-15
**Status:** done (Phase 1+2+3 code/data complete; live deploy pending PR merge)
**Tracer bullet:** Phase 1 proves the repair mechanism works — re-fetching closed bars from OKX REST + an upsert that overwrites (`ON CONFLICT DO UPDATE`) actually drives the parity tracer to **0 mismatches** on ETH 4h. If REST overwrite can't clean a known-dirty pair offline, the whole repair strategy is wrong and the rest of the plan is moot.

## Context summary
Bot's Postgres `candles` holds partial (still-forming) bars: backfill (`exchange_client.py:146`) stores the forming bar `confirmed=True`, and the candle upsert (`data_store.py:948`) uses `ON CONFLICT DO NOTHING`, so the later authoritative WS `confirm=1` bar is dropped. Bad bars accumulate on every startup/reconnect; ~36% of 4h bars, ~12% of 1h, ~1% of 15m, across ALL pairs. Taints SMC detectors (OB/FVG/sweep read high/low), ML features, and /chart. Fix = drop forming bars at source + make the upsert correct stale bars + repair history + re-flag affected ML rows. **Touches `data_service` only — no `risk_service`/`execution_service`, no money path, no `ML_FEATURE_VERSION` bump.** Bot stays shadow-only.

## Phase 1 — Repair-mechanism tracer (ETH 4h offline)
**Status:** done
**Inputs:** Proven root cause (grill). Existing parity tracer `scripts/dual_thrust_candle_parity.py` (reports OHLC + signal mismatch count). `exchange_client.backfill_candles` (REST fetch), `data_store.store_candles` (the `DO NOTHING` upsert), `_timeframe_to_ms` helper.
**Outputs:**
- `data_store.store_candles` gains an `upsert: bool = False` param (or a sibling `upsert_candles`) that switches the conflict clause to `DO UPDATE SET open/high/low/close/volume/volume_quote = EXCLUDED.*`. Default path unchanged for now (still `DO NOTHING`) so this phase touches no live behavior.
- `scripts/repair_partial_candles.py` — for a given pair/tf: re-fetch the last N **closed** bars from OKX REST (drop the forming bar via close-time guard) and upsert-overwrite. Idempotent, re-runnable, no live-bot interaction.
**Work:**
- Add the upsert capability to `store_candles` (param-gated; existing callers keep `DO NOTHING`).
- Write `repair_partial_candles.py`; run it for **ETH/USDT 4h only**.
- Re-run the parity tracer on ETH 4h.

**Verification gate:**
- [x] Automated: `python scripts/dual_thrust_candle_parity.py` → **0 OHLC mismatches + 0 signal diffs** on ETH 4h (was 143 OHLC / 1 signal). PASS.
- [x] Automated: `pytest tests/test_data_store_filters.py tests/test_data_service.py` → 83 passed, 0 failures. PASS.
- [x] Manual: dirty bars overwritten (repair report listed 143 dirty → now 0; tracer eyeball of last 3 bars matches REST).
- [ ] Rollback if: tracer still shows mismatches after repair → STOP, re-diagnose. NOT fired.

**Evidence (filled by /phased-implementation):**
- 2026-06-15 — Automated checks:
  - `python scripts/repair_partial_candles.py --pair ETH/USDT --tf 4h --count 400` → 399 closed bars fetched, **143 partial/dirty overwritten**, 0 missing inserted.
  - `python scripts/dual_thrust_candle_parity.py` → **PARITY PASS** (ohlc_diffs=0, sig_diffs=0) over 299 bars. Was 143 OHLC / 1 signal pre-repair.
  - `pytest tests/test_data_store_filters.py tests/test_data_service.py` → **83 passed**, 0 failures.
- Manual checklist:
  - [x] Repair report showed correct stored→REST corrections (e.g. 2026-04-13 16:00 H 2214.86→2268.25).
  - [ ] User to confirm: satisfied with repair-mechanism proof before Phase 2 wires it into the live backfill path + repairs all 7 pairs.
- Rollback trigger fired: no.
- Files changed: `data_service/data_store.py` (upsert param), `scripts/repair_partial_candles.py` (new), `scripts/dual_thrust_candle_parity.py` (new — the tracer, from Phase 1b-P1).
- LOC delta: ~+155 / −5.

---

## Phase 2 — Prevent at source + full repair + deploy
**Status:** done
**Inputs:** Phase 1 PASS (REST overwrite proven on ETH 4h). The upsert capability from Phase 1.
**Outputs:**
- **Fix A** in `exchange_client.backfill_candles`: drop any bar whose close-time (`ts + _timeframe_to_ms(tf)`) > `now_ms` → never store a forming bar. Unit test.
- **Fix B**: route the live candle-store path (`data_store.store_candles` used by `service._on_candle` AND `_backfill_all`) to the `DO UPDATE` upsert, so an authoritative WS/REST bar always corrects a stale partial. Safe because Fix A guarantees only closed bars enter.
- Full history repair: run `repair_partial_candles.py` across **all 7 pairs × {4h, 1h, 15m, 5m}**.
- Deploy: `docker compose up -d --build bot` + deploy-verification checklist.
**Work:**
- Implement Fix A + test (forming bar dropped, closed bar kept; boundary case ts exactly = bar open).
- Flip `store_candles` callers to upsert; confirm `_on_candle` now overwrites a pre-existing partial.
- Run the repair script for every pair/tf; capture before/after mismatch counts.
- Deploy, run the 9-step checklist.

**Verification gate:**
- [x] Automated: parity tracer (generalized — `--all` loops pairs/TFs) → **21/21 PASS, 0 mismatches on all 7 pairs × {4h,1h,15m}**; ETH 5m spot-check PASS.
- [x] Automated: `pytest tests/test_data_service.py tests/test_data_store_filters.py tests/test_data_integrity.py` → 155 passed, 0 failures (incl. 2 new Fix-A regression tests).
- [ ] Manual/DEFERRED: bot `Up (healthy)` after deploy — **NOT done**. Deploying an unmerged feature branch to prod conflicts with the branching workflow (main=production). Decision surfaced to user: deploy should follow PR review + merge.
- [ ] Rollback if: bot regresses OR a live WS bar wrongly overwritten by a forming bar → revert Fix B to `DO NOTHING` (keep Fix A — strictly safe) + redeploy. NOT fired (not deployed).

**Evidence (filled by /phased-implementation):**
- 2026-06-15 — Automated checks:
  - Fix A (`exchange_client.backfill_candles`): drops any bar with `ts + tf_ms > now_ms`; logs `dropped_forming`. Tests `TestBackfillFormingBarGuard` (2) PASS.
  - Fix B (`service._on_candle` + `_backfill_all`): both now call `store_candles(..., upsert=True)` → authoritative bar overwrites stale partial.
  - Full repair: 7 pairs × {4h,1h,15m} — dirty overwritten: 4h ≈137–143/pair, 1h ≈44–45/pair, 15m ≈7–8/pair (~1000+ bars total). ETH 4h already 0 (Phase 1). ETH 5m: 1 dirty fixed.
  - `python scripts/dual_thrust_candle_parity.py --all` → **ALL PASS (21/21)**.
  - `pytest ... data_service/data_store_filters/data_integrity` → **155 passed**.
- Manual checklist:
  - [ ] User decision: deploy via PR-merge then `docker compose up -d --build bot` (NOT auto-deployed from this branch).
- Rollback trigger fired: no.
- Files changed: `data_service/exchange_client.py` (Fix A), `data_service/service.py` (Fix B ×2), `data_service/data_store.py` (upsert, Phase 1), `scripts/repair_partial_candles.py`, `scripts/dual_thrust_candle_parity.py` (generalized), `tests/test_data_service.py` (+2 tests).
- LOC delta: ~+120 / −10 (Phase 2 only).

**Note on live-bot gap:** prod PG data is now clean, but the *running* container still has pre-fix code — it can re-introduce partials on reconnect until deployed. Bounded: once deployed, Fix B self-corrects any partial on the next authoritative bar. Deploy gated on PR merge.

---

## Phase 3 — Re-flag contaminated ML rows
**Status:** done
**Inputs:** Phase 2 PASS (candles clean going forward + history repaired). Known set of (pair, tf, timestamp) that WERE partial before repair — captured by the repair script's before/after diff in Phase 2.
**Outputs:**
- A quantified report: how many of the 12,370 `ml_setups` had a now-known-partial bar inside their feature lookback window (join on pair + the bar's TF + detection `timestamp` − lookback).
- A re-flag decision applied: either a new exclusion tag added to `NON_MARKET_OUTCOMES` (e.g. `partial_candle_contaminated`) set on affected rows, OR a documented date/experiment-cutoff filter if the contamination is too diffuse to pinpoint per-row.
- Updated training-query guidance in MEMORY.md + SYSTEM_BASELINE §7.
**Work:**
- Build the contamination join: for each affected (pair, tf, ts), find ml_setups whose lookback for that tf covered it. Size the blast radius (likely small for LTF setups since 15m partials ~1%; larger for HTF-bias features).
- Choose method (per-row tag preferred if N is bounded; date cutoff fallback). Apply.
- Document. **No `ML_FEATURE_VERSION` bump** (column meanings unchanged); this is row hygiene, not schema change.

**Verification gate:**
- [x] Automated: proof query — 131 rows tagged `partial_candle_risk`, **0 survive** the training filter, 12,239 pass. Tag excludes them as intended.
- [x] Automated: `pytest data_service/data_store_filters/data_integrity` → 155 passed (migration 22 didn't break anything).
- [x] Manual: flagged count plausible (131 = trigger-bar-on-reconnect, incl. 37 engine1_trend_pullback); method = per-row tag (forward-cutoff rejected: only 50/12,370 rows post-fix).
- [ ] Rollback if: flagging nukes unreasonable fraction → fall back to doc-only. NOT fired (131/12,370 = 1%, justified).

**Evidence (filled by /phased-implementation):**
- **Plan revision 2026-06-15:** precise full per-row ID proved unrecoverable (partial-bar list overwritten by Phase 2 repair; `ws_reconnect` log only 2026-05-18+, misses startup backfills). User chose (over forward-cutoff, which kills 99.6% of rows): **tag the recoverable high-risk subset + document the rest as caveat.**
- 2026-06-15 — Automated checks:
  - Migration 22: `ml_setups.data_quality VARCHAR(30)` (idempotent ADD COLUMN). No `ML_FEATURE_VERSION` bump (row hygiene, not feature schema).
  - `scripts/flag_partial_candle_ml.py --apply` → **131 rows** tagged `partial_candle_risk` (trigger bar coincided with a bar forming at a known reconnect). Idempotent (re-run tagged 0).
  - Proof query: 131 tagged, 0 survive training filter, 12,239 remain.
  - Training filter updated in MEMORY.md + SYSTEM_BASELINE §7 query: `AND (data_quality IS NULL OR data_quality <> 'partial_candle_risk')`.
  - Caveat documented in SYSTEM_BASELINE §7 (131 = lower bound; full set unrecoverable; magnitude small — open always correct).
- Files changed: `data_service/data_store.py` (migration 22), `scripts/flag_partial_candle_ml.py` (new), `docs/SYSTEM_BASELINE.md` §7, MEMORY.md.
- Rollback trigger fired: no.

## Out of scope (deliberately)
- **Dual Thrust shadow wiring (Phase 1b-P2)** — separate plan (`dual-thrust-phase1b-shadow-wiring.md`). DT will read REST `4H` directly regardless; not blocked on this fix. Not bundled here.
- **`risk_service` / `execution_service` / any order path** — untouched. This is data-layer hygiene.
- **`ML_FEATURE_VERSION` bump** — no column meaning changes; re-flagging rows is hygiene, not a schema/version event.
- **Rewriting the WS feed** — `websocket_feeds.py` is clean (only stores `confirm=1`). The bug is 100% backfill + DO-NOTHING; no WS rewrite.
- **Backfilling missing bars older than the repair window** — repair fixes existing dirty bars, does not extend history.

## Open questions (resolved before Phase 1)
- **Fix B upsert predicate — overwrite always vs only-on-diff?** Recommended: `DO UPDATE` unconditionally on OHLCV (one confirmed bar per ts; no thrash). Accept the minor `volume_quote` source difference (backfill approximates `vol*c`, WS uses real quote) — not used by OHLC detectors. If write-churn shows in logs, add a `WHERE candles.* IS DISTINCT FROM EXCLUDED.*` guard. → Decided: unconditional, revisit only if churn observed.
- **Tracer coverage for Phase 2 — extend to all pairs/TFs or spot-check?** Recommended: parameterize `dual_thrust_candle_parity.py` to take pair/tf args and loop; 5m can be spot-checked (partials ~1%). → Decided: parameterize for 4h/1h/15m, spot-check 5m.

## Changelog hook
On completion, append to `docs/SYSTEM_BASELINE.md` §9 changelog:
- `2026-06-?? — Partial-candle backfill bug fixed (PR #N). Backfill no longer stores forming bars; candle upsert now corrects stale partials (DO UPDATE); history repaired across 7 pairs × TFs; N contaminated ml_setups re-flagged. Impact: SMC detectors + ML features + /chart now read faithful OHLC. No risk/execution change, no ML_FEATURE_VERSION bump.`
