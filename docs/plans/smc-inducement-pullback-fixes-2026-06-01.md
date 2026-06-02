# Plan (DEFERRED): SMC inducement + pullback fixes
**Status:** PARKED until 2026-06-08 (after ML v0 baseline re-run)
**Source grill:** `docs/grill/smc-inducement-pullback-2026-06-01.md`
**Source notes:** `notes.md` (Inducement + Confirmation entries)
**Why deferred:** Adding ML features bumps `ML_FEATURE_VERSION 18→19`, invalidating v18 rows mid-baseline. Hard gates would starve already-thin engine1 N. Wait for 6/8 re-run, then ship features as one v19 bump.

## Guiding principle (from grill)
No detector rewrites. No hard emission gates while N thin. **Instrument → let shadow data + ML decide.** Setup-A-style sweep requirement already exists; do not bolt more rejections onto B/F/D/engine1.

## Work items

### W1 — Limit-cancel-on-invalidation (lifecycle fix) [SAFE TO SHIP ANYTIME — no ML feature change]
Standing buy/sell limit at OB zone should be cancelled if structure invalidates before fill (e.g. price closes beyond the swing the setup leaned on, or OB mitigated). Today the limit sits idle and can fill into a broken setup.
- Files: `execution_service/service.py` (pending-order lifecycle), `strategy_service/order_blocks.py` (mitigation/invalidation signal). In shadow this maps to `ShadowMonitor` cancelling pending shadow fills.
- Risk layer: read-only to entry logic; no SL/sizing change.

### W2 — Fill-context ML feature [v19 bump — AFTER 6/8]
At (shadow) fill time, capture: did price show a reaction/bounce near the zone before fill, or did it fill mid-impulse-down? Lets A-vs-B (limit-at-zone vs wait-for-bounce) be decided by EV, not argument.
- Candidate features: `bars_in_zone_before_fill`, `adverse_excursion_pre_fill_pct`, `reaction_wick_before_fill` (bool).
- Files: `shared/ml_features.py` (+`ML_FEATURE_VERSION`), fill path in `ShadowMonitor`.

### W3 — Inducement / unswept-liquidity ML features [v19 bump — AFTER 6/8]
Bot has EQH/EQL (`liquidity.py:204-245`) + sweeps (`liquidity.py:275-349`) but no sequencing. Log, don't gate:
- `inducement_swept_before_entry` (bool) — was opposite-side liquidity swept in last N candles before zone taken?
- `unswept_liquidity_between_entry_and_target_pct` — distance to nearest unswept EQH/EQL on the wrong side (the "zone 1 vs zone 2" magnet from notes).
- Files: `strategy_service/liquidity.py` (expose nearest-unswept query), `shared/ml_features.py`.
- If ML later proves unswept-inducement entries underperform → THEN consider a soft confluence penalty, never before.

## Sequencing
1. (Optional now) W1 lifecycle fix — independent, no feature churn.
2. After 2026-06-08 re-run: bundle W2 + W3 as single `ML_FEATURE_VERSION 18→19` bump. Document discard justification for v18 rows in SYSTEM_BASELINE §7.
3. Run `/phased-plan smc-inducement-pullback-fixes` to expand into phases at that point.

## Open
- Confirm v18 collection has enough rows for the 6/8 decision before bumping to v19.
- notes.md still incomplete (top-down, FVG, imbalances, supply/demand, Wyckoff pending) — revisit scope once full.
