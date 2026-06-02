# Grill: Top-Down Telegram Brief for Manual Bybit Entries
**Date:** 2026-05-20
**Topic:** New script + Telegram handler that emits a multi-TF context summary (1D/4H/1H bias, key zones, suggested side) for each Bybit-traded pair, to assist manual entries.
**Verdict:** BUILD — swing cascade first (4H→1H→30m→15m), opinionated reconciled output, falsified via WR comparison on `topdown_brief_used` journal annotation (N≥20 per bucket).

## Context loaded
- User trades manually on Bybit (~$4.6k capital). Pairs: BTC, ETH, XRP, SOL.
- Workflow: TradingView open on Mac desktop; mobile used for position management.
- Manual edge discipline Phase 1 shipped 2026-05-19 (`feat/manual-edge-discipline-phase1`). Structured `trigger_condition` + `thesis_invalidation` fields. Annotation fill rate target: 5% → ≥80% (per project_bybit_tradelog_system).
- All upstream analyzers exist: `market_structure.py`, `fvg.py`, `order_blocks.py`, `liquidity.py`, `volume_profile.py`. 1D candles exist in DB (~580 per pair).
- FREEZE-safe: brief = read-only, no `strategy_service/` change, no ML feature bump.
- Manual rule edge thesis (per SYSTEM_BASELINE §9 Rules taxonomy v3): POC mean reversion + 4H 50 EMA trend filter + ≥3 confluences + Limit-only.

## Working hypothesis (steelman)
User opens TradingView and BUILDS the top-down picture by hand: switch 1D, eye structure, switch 4H, mark OB, switch 1H, scan liquidity. This takes minutes per pair and is error-prone (forgets to check one TF, misreads a swing). A Telegram brief does the assembly once, consistent, and delivers a pre-checked context list. Reduces decision fatigue + missed checks.

## Decision tree

### Q1: Measured pain
**My recommended answer:** (e) mobile-skip — most likely cause of selectively missing the top-down check.
**User answer:** "Chequeo pero no sé si está correcto, voy saltando bias long↔short al cambiar TF, vi sweeps previos pero pensé que no volvería y terminó barriendome."
**Grade:** ✅ on concreteness (cited specific failures) but ⚠️ unquantified (still ~5% journal fill, no N).
**Notes:** Real failure modes ≠ "missing info on TV". They are:
1. **No confidence in own read** — uncertainty about whether top-down conclusion is right.
2. **TF flip-flop** — different TFs suggest different bias; can't reconcile.
3. **Sweep dismissal** — sees prior liquidity sweeps, predicts they won't re-test, gets stopped out at re-test.

Implication: brief is NOT a duplicate of TV. It must (a) reconcile multi-TF into a single weighted bias call, (b) force visibility of unbroken liquidity (prevent dismissal), (c) optionally provide confidence/score so user can sanity-check their own read against a second opinion.

### Q2: Counterfactual TV — what does the brief ADD that TV doesn't already show?
**My recommended answer:** Only A (reconciliation) + B (threats forcing) attack the 3 measured failures.
**User answer:** "Quiero info / herramientas que me ayuden a visualizar mejor. Que el bot analice por mí y devuelva su conclusión por Telegram. Las 3 cascadas: 1W→1D→4H→1H, 4H→1H→30m→15m, 1H→30m→15m→5m."
**Grade:** ✅ on intent clarity. ⚠️ on scope — wants opinionated bot conclusion across all 3 cascades from day one.
**Notes:** Direction is "delegate interpretation to bot, receive verdict". This means:
- Brief output must be opinionated (suggested side + confidence), not just data.
- Risk: bot's HTF bias detection already has known limits (see grill 1d-htf-veto-layer — 1D doesn't discriminate TP/SL on engine1). Brief inherits those limits — must communicate confidence ranges, not pretend certainty.
- 3 cascades = 3× work AND requires 30m + 1W timeframes that don't currently exist.
- 1W can be aggregated from 1D candles in-process (cheap, no new sub).
- 30m needs WS sub + REST backfill (~$N more API calls + storage).

### Q3: Which cascade first?
**My recommended answer:** Swing cascade (4H→1H→30m→15m). Matches user's rule taxonomy v3 (POC + 4H 50 EMA anchor). 30m backfill is incremental, not new infra.
**User answer:** "Si hazlo así." Swing first, accepts Phase 1 = one cascade with expansion after.
**Grade:** ✅ — focused scope.

### Q4: Falsification — how do you know in 30 days that the brief is worth keeping?
**My recommended answer:** (a) WR comparison via journal annotation. Requires Phase 1 manual-edge discipline to be hitting target fill rate.
**User answer:** "a"
**Grade:** ✅ — concrete, measurable, dated.

## Final verdict — BUILD

6 of 7 build criteria passed (edge in bps not quantified but acceptable for analytical/decision-support tool, not a signal generator).

**Pre-conditions for /phased-plan:**
1. Phase 1 of `manual-edge-discipline` must continue accumulating annotations — falsification depends on journal data.
2. Add `topdown_brief_used` (bool) field to `bybit_trade_annotations` schema in the brief plan (Phase wires it).
3. 30m candle backfill is required for swing cascade. Strategy: REST poll on-demand for Phase 1 tracer (no WS sub yet); add WS sub in Phase 2+.
4. Brief output must communicate **confidence ranges**, not certainty — inherited limit from known bias-detection issues (see grill `1d-htf-veto-layer-2026-05-20`).

## Phased plan outline (to be filled by `/phased-plan`)
- **Phase 1 tracer**: console output of swing brief for 4 pairs (BTC/ETH/XRP/SOL). User reads, confirms reconciliation matches their independent read. NO Telegram yet. NO 30m needed if we use 4H→1H→15m as proxy.
- **Phase 2**: 30m backfill (REST on-demand → progressive WS sub). Complete cascade.
- **Phase 3**: Telegram command `/topdown <pair>` on-demand.
- **Phase 4**: schedule (cron on candle close) + on-change alerts (new BOS / new sweep).
- **Phase 5 (gated by 30-day falsification result)**: replicate motor for macro + scalp cascades if Phase 4 metrics survive.

## Out of scope
- 30m WebSocket subscription in Phase 1 (REST poll only — defer WS to Phase 2).
- Macro + scalp cascades until swing cascade survives 30-day falsification.
- Auto-generating Bybit orders from brief (brief = context, NOT signal).
- Any change to `strategy_service/` (FREEZE-respect).
- Any ML feature version bump.
