# Grill: /topdown Phase 4 — Structure Context (HTF duration + LTF flip + big candle + wick-into-liq)

> **⛔ ABANDONED — NO EDGE.** Part of the /topdown v2 stack (PRs #37–42), never merged; backtest verdict **NO EDGE** (`backtest_results/TRACKER.md`). Archived for decision rationale only.

**Date:** 2026-05-23
**Topic:** Surface STRUCTURE memory in the brief so a recent big LTF candle doesn't mislead user when HTF structure is unchanged. Addresses live user observation 2026-05-23: "bot parece seguir el momentum no a la estructura — venimos de bear y ahora vela gigante bull, pero estructuralmente sigue bear".
**Verdict:** BUILD — pure derivation over Snapshot already in memory. No new infra. Single PR ~½ day.

## Context loaded
- v2 PRs #38, #39, #40 deployed 2026-05-23. User tested live and surfaced gap.
- Existing `/topdown` brief already surfaces: BIAS (reconciled weighted), PD conflict, displacement strength, killzone, OB/FVG zones, magnets, daily context, adaptive TP. Lacks: trend-duration + LTF-flip-vs-HTF + big-impulse-event + wick-tap-live.
- Memory updated 2026-05-23: closes commit DIRECTION but wicks reveal LIQUIDITY — both matter, surface both.
- All data needed already in Snapshot: `tf_results[tf].state.structure_breaks` (timestamps of past BOS/CHoCH) + `raw_candles[tf]` (last-candle wicks + bodies) + `liquidity` per TF.

## Working hypothesis (steelman)
Brief is currently moment-in-time read on STRUCTURE + PD + magnets but does not surface HOW LONG that structure has held nor flag fresh LTF flips that contradict HTF. User sees `BIAS: SHORT — medium (3/5)` and gets correct directional read but doesn't know:
- HTF bias held for 14 candles vs 2 candles (durability)
- LTF flipped bull 3 candles ago against HTF bear (countertrend rally vs real reversal)
- Last 1H candle is a 2.8% bull impulse with body x4 baseline (institutional move? exhaustion? watch list)
- Last wick tapped a BSL cluster — possible liquidity sweep ending the rally

Surface these and the brief becomes durable across the kind of intraday event user described.

## Decision tree

### Q1: Architecture — pure derivation or persist trend state?
**My recommended:** Pure derivation. Compute trend duration from `structure_breaks[-1].timestamp` (when current trend last changed). Compute LTF flip vs HTF similarly. Big candle + wick-tap = pure read of `raw_candles[tf][-1]`. Zero new state.
**User answer:** (proceeding with rec)

### Q2: Where in the brief? New section vs extend existing
**My recommended:** New section `*STRUCTURE CONTEXT:*` between DAILY CONTEXT and ICT STRENGTH. Max 4 lines. Keeps existing sections untouched. Render only when data is non-trivial (no flip → omit flip line; no big candle → omit; etc).
**User answer:** (proceeding with rec)

### Q3: Big candle threshold
**My recommended:** Body % vs last 30-candle avg body %. `≥3×` = big impulse (flag). `≥5×` = extreme (call attention). Same metric as `_displacement_read` baseline but scoped to LAST single candle, not last 3.
**User answer:** (proceeding with rec)

### Q4: Fresh LTF flip definition
**My recommended:** "Fresh" = last ≤4 candles on that LTF. Only flag when LTF flipped to direction OPPOSITE the HTF (4H) trend. Same-direction LTF flip = continuation, not noteworthy. Check 1H + 30m + 15m, prioritize the LOWEST-TF flip (fastest signal).
**User answer:** (proceeding with rec)

### Q5: Wick-into-liquidity TF choice
**My recommended:** Last 1H candle. 4H wicks rarely surprise. 5m/15m too noisy. 1H = sweet spot for intraday tap detection. Check if 1H candle high crossed any unbroken BSL (level.price ≤ candle.high) or low crossed unbroken SSL, with close back inside the level (= sweep). Reuse existing `LiquidityLevel.swept` semantics.
**User answer:** (proceeding with rec)

### Q6: Falsification — same Phase 1 gate?
**My recommended:** Same `topdown_brief_renders` JOIN gate. Window resets at Phase 4 deploy date (v2 evolved enough that prior data is not comparable).
**User answer:** (proceeding with rec)

### Q7: Implementation cost
**My recommended:** ~½ day. 4 small helpers + 1 render section + tests. Single PR. Same-day ship after PR3 lands.
**User answer:** (proceeding with rec)

## Final verdict — BUILD
4 sub-features bundled into one PR (Phase 4 of plan `topdown-v2-context-simplicity-2026-05-23.md`):
1. `_trend_duration(tf_state, candle_count_back)` — return age of current trend in candles + ms
2. `_ltf_flip_vs_htf(tf_results)` — return any LTF that flipped recently against 4H
3. `_last_candle_impulse(candles)` — body x baseline of last single candle
4. `_wick_into_liquidity(htf_or_1h_candles, liquidity_levels)` — last candle's wick taps unbroken level

Render section title: `*STRUCTURE CONTEXT:*` (≤4 lines).

## Out of scope (defended)
- Modifying `_reconcile` weighting to make HTF stickier — risky, would change Phase 1 behavior user already likes
- Persisting trend-flip history in a new table — pure-derivation works
- Multi-TF wick-tap (only 1H per Q5)
- Auto-alerts on big candle or wick tap — out of scope for read-only brief; that's "push alerts" which is Phase 5+

## Handoff
Single PR `feat/topdown-v2-pr4-structure-context` off `feat/topdown-v2-pr3-adaptive-tp`. No phased-plan refresh needed — Phase 4 added inline to existing plan doc.
