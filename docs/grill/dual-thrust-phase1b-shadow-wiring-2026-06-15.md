# Grill: Dual Thrust Phase 1b — pipeline shadow wiring

**Date:** 2026-06-15
**Topic:** Wire the (validated) Dual Thrust engine into the live bot pipeline as a shadow evaluator (ETH 4h) before live-small.
**Verdict:** BUILD (with hard conditions + collapsed timeline) — the edge is already proven; this is an infra-risk question, and the risk is real but cheap to retire.

_Self-conducted (designer = evaluator). Edge of Dual Thrust is NOT re-litigated — Phase 0 parity (133 trades identical, Sharpe 1.9967) + Phase 1a fresh parity already settled it. The grill judges only the cost/risk of touching `main.py` on the running bot._

## Context loaded
- Dual Thrust validated: walk-forward (test>train), MC P(loss)=0, Binance→OKX transfer (Sharpe 1.999). Phase 0 + 1a merged (PR #87, #88).
- Bot is shadow-only (`ENABLED_SETUPS=[]`), $86 OKX live balance.
- Pipeline entry: `main.py:on_candle_confirmed(candle)` fires on every confirmed candle; already branches on `candle.timeframe == HTF_CAMPAIGN_SIGNAL_TF` (4h).
- WS feed (`websocket_feeds.py`): subscribes `candle5m/15m/1H/4H/1D`. **`candle4H` = UTC-aligned. NO 6h channel. `candle1D` = Hong-Kong-aligned (08:00) — the daily-anchor trap.**
- Engine derives the 1D anchor by resampling trade-TF bars itself (`day_open_map`), NOT from a 1D feed → avoids the HK trap *iff* it is never fed the bot's `1d` candles.

## Decision tree

### Q1: Counterfactual — what does pipeline-shadow give that the weekly REST `live_check` does not?
**My answer:** The REST `live_check` validates the *brain on REST candles*. Live execution, if it reuses the bot's architecture, will read candles from the bot's WS-built store — a DIFFERENT source. Pipeline-shadow validates that exact source + the real-time flip lifecycle. That is the only thing it adds, and it is the right thing to validate before money.
**Grade:** ✅ — concrete value, not "more data."

### Q2: Is the feed-drift risk real, and how is it retired?
**My answer:** Real on two axes. (a) Bot `candle4H` (UTC, WS) vs REST `4H` — should be identical but MUST be asserted (timestamps + OHLC). (b) Bot `candle1D` is HK-aligned — a wrong anchor if used; avoided because the engine self-derives the anchor from 4h bars. Retirement: a one-time/periodic candle-parity assertion comparing the bot's stored ETH 4h candles vs REST `4H` over the last N bars. If they match, axis (a) is retired immediately — no multi-week wait needed.
**Grade:** ✅ — risk identified precisely; cheap deterministic test retires most of it.

### Q3: Does it touch `risk_service/` or `execution_service/`?
**My answer:** No. Phase 1b is shadow — logs signals + a theoretical flip position (faithful real-time replay of the harness fill model). No orders, no SL/TP placement, no sizing through risk_service. It lives in an isolated module + a guarded hook. The one-shot SL/TP setup path and its invariants are untouched.
**Grade:** ✅ — no money-path code touched.

### Q4: Rollback plan if it regresses live behavior?
**My answer:** Hook is flag-gated (`DUAL_THRUST_SHADOW_ENABLED`, default off) and wrapped in try/except so an engine error can never break `on_candle_confirmed` for the rest of the bot. Rollback = flip the flag off (no redeploy of logic) or revert the small hook commit. Pipeline cost: one extra eval for ETH 4h only, gated — negligible latency.
**Grade:** ✅ — isolated, reversible, fail-safe.

### Q5: Simpler alternative that gets ≥70% of the upside?
**My answer:** Candidate = a standalone REST-driven Dual Thrust runner (separate process, REST candles, own orders) — skips `main.py` entirely. Rejected: it duplicates execution, position monitoring, SL reconciliation, and orphan handling that `execution_service` + `monitor` already provide. More code + more surface than a guarded hook reusing the bot's candle-driven architecture. The pipeline hook IS the simpler path.
**Grade:** ✅ — simpler alt considered and is actually more expensive.

### Q6: Timeline — is a 2-week shadow soak justified?
**My answer:** No, not as a rigid gate. At 4h cadence the soak yields ~8–12 signals in 2 weeks — thin, and the feed-drift question is answered deterministically by Q2's candle-parity assertion, not by waiting. PIVOT: retire feed-drift via the candle-parity assertion (immediate), run shadow for a few days only to confirm the real-time flip state machine behaves (entry → flip → SL), then proceed to live-small. Respects the operator's (valid) frustration with "collect forever."
**Grade:** ⚠️→✅ — original 2-week soak was cargo-cult; collapsed to a deterministic check + short behavioral confirm.

### Q7: Implementation cost / overfit / ML churn?
**My answer:** No new ML feature, no `ML_FEATURE_VERSION` bump, no training-data invalidation. New isolated shadow module + a small guarded hook + a shadow log/table + the candle-parity check. Edge not reverse-engineered (came from an external Jesse screen, then survived walk-forward + transfer). Low cost, justified.
**Grade:** ✅

## Final verdict
**BUILD.** Six of seven branches clean; the seventh (timeline) pivots from a 2-week soak to a deterministic candle-parity check + short behavioral confirm. The edge is settled; this step's only job is to retire feed/real-time-path risk cheaply before $86 goes live. It touches `main.py` but only via a flag-gated, try/except-wrapped, ETH-4h-only hook that cannot regress the rest of the bot, and touches no risk/execution code.

What would flip it to KILL/PIVOT: if the candle-parity assertion FAILS (bot `candle4H` ≠ REST `4H`) and can't be reconciled → the bot's feed can't reproduce the validated signals, so either fix the feed or pivot live execution to read REST candles directly (making 1b moot).

## If BUILD: pre-conditions for /phased-plan
- **HARD:** the engine must be fed the bot's **4h** bars and derive the 1D anchor itself. NEVER pass the bot's HK-aligned `candle1D` store as the anchor.
- Scope ETH 4h only. 6h deferred to Phase 2 (needs a `candle6Hutc` WS subscription — bot has no 6h channel).
- Deliverables: (1) candle-parity check (bot ETH 4h store vs REST `4H`), (2) isolated flip-aware shadow tracker (real-time harness fill replay, no orders), (3) flag-gated + try/except hook in `on_candle_confirmed`, (4) shadow persistence (reuse a shadow/log table), (5) a short real-time behavioral confirm before Phase 1c.
- Gate to Phase 1c (live-small): candle-parity PASS + shadow flip state machine observed correct on ≥3–5 real signals.
