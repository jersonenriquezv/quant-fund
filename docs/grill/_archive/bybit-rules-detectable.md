# Bybit Rules — Detectability Audit (Phase 1 tracer)
**Date:** 2026-05-13
**Plan:** docs/plans/_archive/bybit-leak-measurement.md
**Source taxonomy:** docs/grill/bybit-rules-taxonomy.md (**EMPTY at time of audit** — user must populate before Phase 4)

## Sample
- `bybit_closed_pnl`: 37 rows total, range 2026-03-18 → 2026-05-03 (46 days, all `linear` category)
- `bybit_executions`: 91 rows, range 2026-03-18 → 2026-04-16
- `bybit_pending_orders`: 37 rows with `stop_order_type` distribution: 11 StopLoss, 11 TakeProfit, 15 empty
- `bybit_trade_annotations`: 37 rows, schema rich (setup_type, confluences, thesis, emotional_state, grade_self) — fill rate not audited in this phase

## Stub taxonomy used
User's `bybit-rules-taxonomy.md` is empty. The 10 rules below were synthesized from prior conversation context. **Replace with user's real list before Phase 4.**

## Per-rule verdict

| # | Rule | Detectable? | Method | Notes |
|---|------|-------------|--------|-------|
| 1 | SL order placed at entry | ✅ DETECTABLE | JOIN `bybit_pending_orders p` ON `p.symbol=c.symbol AND p.stop_order_type='StopLoss' AND ABS(EPOCH(p.placed_at - c.created_time)) < 600` | Confirmed in 10/10 recent sample. SL captured by `bybit_watcher` watcher daemon. |
| 2 | TP order placed at entry | ✅ DETECTABLE | Same with `stop_order_type='TakeProfit'` | Confirmed in 10/10 recent sample. |
| 3 | Revenge trade <30min after loss | ✅ DETECTABLE | `LAG(closed_pnl) OVER (ORDER BY created_time)` + gap calc. Negative prev_pnl + gap < 30min = revenge candidate. | Works at global level. Should also be checked per-symbol. |
| 4 | Re-entry same pair <Xh after SL hit | ✅ DETECTABLE (with caveat) | `LAG()` partitioned by symbol; classify exit as SL when `avg_exit_price ≈ pending_orders.trigger_price` (StopLoss). | Caveat: `closed_pnl.exec_type` is uniformly `'Trade'` — no SL flag. Must heuristic-match exit price to SL trigger price (±0.1% tolerance). |
| 5 | Leverage >Xx | ⚠️ LOW VALUE | `closed_pnl.leverage` column. | All 37 sample trades at exactly 10x. Rule produces zero signal unless user varies leverage. Demote to informational. |
| 6 | Sizing >X% capital | ❌ UNDETECTABLE NOW (✅ after Phase 2) | `qty × avg_entry_price = notional`; need capital at moment of entry to compute %. | Capital at moment requires deposits/withdrawals history → NEW table required in Phase 2. Block on this. |
| 7 | Multiple concurrent positions same symbol | ✅ DETECTABLE | Overlap window in `closed_pnl` filtered by symbol where `created_time < other.updated_time AND updated_time > other.created_time`. | Straightforward. |
| 8 | Holding losing position past planned timeout | ❌ UNDETECTABLE | No "plan timeout" stored anywhere. | **Drop from taxonomy** — unmeasurable without forward-only instrumentation. |
| 9 | R defined at entry (SL distance non-zero, sane) | ✅ DETECTABLE | From rule 1 join: `R_pct = ABS(SL.trigger_price - entry_price) / entry_price`. Flag if R<0.1% (too tight) or R>5% (no real SL). | Powerful — measures SL placement quality, not just presence. |
| 10 | Trade in low-liquidity hour / extreme RSI | ❌ UNDETECTABLE | Requires market data join + indicator computation outside Bybit data. | **Drop from taxonomy** — out of scope per plan. |

## Score
- **Detectable now: 6 / 10 = 60%**
- **Partially detectable: 1 / 10 (leverage — informational only)**
- **Undetectable now: 3 / 10**
  - 1 becomes detectable after Phase 2 (sizing, conditional on capital flow sync)
  - 2 must be dropped (timeout, market context)

**After Phase 2 + dropping rules 8 and 10:** effective taxonomy = 7 rules, 7 detectable = **100%** of remaining rules.

## Gate evaluation (Phase 1 verification)

Plan-defined gate: **≥70% of stated rules detectable** OR taxonomy reduces to detectable-only.

- **Strict pass at current state:** ❌ FAIL (60% < 70%)
- **Conditional pass after taxonomy reduction:** ✅ PASS (rules 8 + 10 dropped → 6/8 = 75%)
- **Conditional pass after Phase 2 capital flow sync:** ✅ PASS (7/8 = 87.5%)

**Recommended action:** accept conditional pass. Drop rules 8 + 10 from taxonomy. Phase 2 must add capital flow sync to enable rule 6.

## Findings unrelated to detectability (flagged for grill)

These are NOT Phase 1 deliverables but emerged during sampling and deserve user attention:

1. **Total recent PnL (last 10 trades): -$24.62. WR ~10% (1 win out of 10).** Pre-empts Phase 3 KILL signal — current discipline-era PnL is negative.
2. **All sample trades had SL set.** "No SL" is NOT user's current leak — discipline on that rule is real.
3. **Leverage constant at 10x across all 37 trades.** Either user always uses 10x (consistent) OR Bybit returns default leverage rather than user-configured. Verify before Phase 4.
4. **Sync only covers 46 days of history (2026-03-18 → 2026-05-03), 37 closed PnL rows total.** Plan's Phase 2 N≥200 pre-bot trades is NOT yet achievable from current DB. Phase 2 must extend `sync_bybit.py` to pull longer history (Bybit retains ~2 years for closed_pnl).
5. **Sync only ran for `linear` category.** Inverse (ETHUSD) appears in 7 rows but probably incomplete. Spot category not synced. Phase 2 must run all 3 categories.
6. **No deposits/withdrawals table exists.** Rule 6 (sizing) blocked. Phase 2 must add `bybit_capital_flow` + sync.

## Required edits before Phase 2

1. User writes real `docs/grill/bybit-rules-taxonomy.md` — list of 5-10 rules he intended to follow, with thresholds (e.g., "SL distance ≤2%", "max 2% notional/capital", "no entry within 30min of last loss")
2. User confirms acceptance of dropping rules 8 + 10 (or replaces with detectable variants)
3. User decides whether to keep rule 5 (leverage) given current 10x-constant pattern
