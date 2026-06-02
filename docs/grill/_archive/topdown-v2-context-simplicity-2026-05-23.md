# Grill: /topdown v2 — Daily Context + Simplicity + Adaptive TP

> **⛔ ABANDONED — NO EDGE.** The /topdown v2 enhancement stack (PRs #37–42) was never merged; backtest verdict was **NO EDGE** (`backtest_results/TRACKER.md`). Disregard any in-review/in-progress status below — archived for decision rationale only.

**Date:** 2026-05-23
**Topic:** Second iteration of `/topdown` after Phase 1 deployment. Bundle: Daily Context Memory (PDH/PDL/PWH/PWL + daily bias chain), simpler output (less noise), explicit entry/SL/TP triplet in PLAY, adaptive TP scaling, PD-bias conflict flag, sweep distance gate, BOS session quality.
**Verdict:** BUILD — all 6 grill questions resolved 2026-05-23. Scope: 3 sequenced PRs under one plan, ~2 days total. Architecture: pure derivation over existing `candles` table, no new collection cron, no cache table.

## Context loaded
- Phase 1 (`docs/plans/_archive/topdown-ict-enhancements-2026-05-23.md`) deployed 2026-05-23 in `quant-fund-explain-bot-1`. PR #37 open. Working, but user testing surfaced gaps.
- Memory `feedback_brief_output_preferences.md` written 2026-05-23 — closes > wicks, simple > dense, adaptive TP, explicit entry/SL/TP, flag bias/PD conflicts, sweep distance gate, session quality.
- Candle data inventory (`docker exec quant-fund-postgres-1 psql ...`):
  ```
  BTC/USDT: 1d=584 candles back to 2024-11-03, 4h=1026, 1h=3982, 15m=15858, 5m=47533
  ETH/USDT: 1d=584, 4h=1026, 1h=3982, 15m=15857, 5m=47533
  SOL/USDT: 1d=580, 4h=958, 1h=3832, 15m=15331, 5m=45995
  XRP/USDT: 1d=578, 4h=907, 1h=2131, 15m=7030, 5m=20090
  ```
  All TFs continuously updated by OKX WS ingestor in `quant-fund-bot-1`. Today's daily candle still forming (closes at 00:00 UTC tomorrow).
- FREEZE active until 2026-06-08. Zero `strategy_service/` changes allowed. All proposed work is on the manual-trading aid surface (`scripts/topdown_snapshot.py` + `scripts/explain_bot.py` + reads from `candles` table).

## Working hypothesis (steelman)
User testing Phase 1 v1 found real interpretation gaps:
- Brief is point-in-time snapshot, no memory of today's price action trajectory
- PD says discount + bias says short → contradiction not flagged
- Sweep level can be 15% away (unreachable) but still rendered as actionable
- Asian-session BOS treated same as London BOS (different quality in ICT)
- R:R not shown explicitly; user has to infer from text
- No entry/SL/TP triplet → user has to construct trade from narrative
- TP recommendation is binary (target X) — doesn't adapt to whether the trade has scaling room or not

User's core insight: **closes are price action truth, wicks are bait** (ICT/Wyckoff axiom). Tracking historical daily closes + computing PDH/PDL/PWH/PWL creates persistent context that a snapshot can never have. Combined with simpler output + explicit trade triplet + adaptive TP, this turns `/topdown` from "structural snapshot" into "decision-grade brief".

## Decision tree

### Q1: Does this idea require new data collection (cron) or only derivation over existing candles?
**My recommended answer:** Derivation only. The `candles` table already has every close for every TF for every pair, continuously ingested by `quant-fund-bot-1`. User's instinct ("track closes daily") is right that closes matter — but the collection is solved. What's missing is aggregation logic that computes PDH/PDL/PWH/PWL + daily bias chain from existing candles, and a display section in the brief.
**User answer:** (pending)
**Grade (mine):** ✅ avoids redundant infra. No new cron, no new schema except possibly a thin cache table if SQL aggregation becomes hot path (defer until measured).
**Notes:** Two architectural options remain:
- **Option A — Pure derivation, no cache.** Helpers compute PDH/PDL etc inline from `candles` per `/topdown` call. Latency: probably <100ms. Simpler. No new state to manage.
- **Option B — Materialized cache.** New table `daily_context_cache(pair, snapshot_date, pdh, pdl, pwh, pwl, daily_close, daily_bias)` populated by a daily cron at 00:05 UTC after daily close. `/topdown` reads cache. Faster, but adds a moving part for marginal speed gain.
**Recommendation:** A first. Measure latency. Promote to B only if A is slow (>200ms p95). Premature caching = wasted infra.

### Q2: Should this be ONE bundled grill+plan or split into smaller ships?
**My recommended answer:** ONE bundled plan, multiple PRs. User said "saltarnos Phase 1b, ir directo a Daily Context" implying he wants ONE coherent next iteration. But shipping all 7 enhancements in one PR is risky (huge diff, harder review, slower deploy). Split:
- **PR 1 (today/tomorrow, ~150 LOC):** Quick wins — PD-bias conflict flag, sweep distance gate, R:R explicit line, entry/SL/TP triplet, BOS session quality. All small render-layer changes.
- **PR 2 (1-2 days, ~300 LOC):** Daily Context Memory — PDH/PDL/PWH/PWL helpers + brief section + tests.
- **PR 3 (1 day):** Adaptive TP logic — scaled vs single TP recommendation based on distance ratio.
**User answer:** (pending)
**Grade:** —
**Notes:** Both PRs sit under one plan doc `docs/plans/_archive/topdown-v2-context-simplicity-2026-05-23.md`. Each PR has its own verification gate. Total grill effort = one doc.

### Q3: Define "long-distance" vs "short-distance" for adaptive TP — by what metric?
**My recommended answer:** Distance ratio OR daily ATR multiple — user pick.
**User answer:** **(b) Daily ATR multiple.** Target distance ≥ 2× daily ATR → scaled TPs. Else single TP.
**Grade:** ✅ volatility-aware, more SMC-aligned than fixed R ratio (crypto regime shifts make fixed R obsolete fast).
**Notes:** Daily ATR = ATR(14) on 1D candles per pair. Recompute per `/topdown` call (cheap, ~5ms over 14 candles). Implementation:
```python
def _daily_atr(daily_candles: list[Candle], period: int = 14) -> float:
    trs = []
    for i in range(1, len(daily_candles)):
        c = daily_candles[i]
        prev = daily_candles[i-1]
        tr = max(c.high - c.low, abs(c.high - prev.close), abs(c.low - prev.close))
        trs.append(tr)
    if len(trs) < period:
        return 0.0
    return sum(trs[-period:]) / period
```
Scaled TPs: TP1 at first valid liquidity inside 2× ATR, TP2 at final magnet at/beyond 2× ATR. 50/50 split suggested.

### Q4: Daily Context section — what exactly is shown? Avoid bloat per simplicity requirement.
**My recommended answer:** Minimum viable context, 3 lines:
```
*DAILY CONTEXT:*
Today: 🔴 bear forming (PDH 86.20 untaken, PDL 83.10 untaken)
Daily chain (5d): bear → bear → bull → bear → bear (4/5 bear)
Weekly: inside (PWH 89.50, PWL 81.00)
```
NOT shown (defer until requested): intraday close trajectory per TF, monthly levels, volume profile by day. Keep it tight.
**User answer:** (pending)
**Grade:** —
**Notes:** Lines 2-3 give the meta-bias the user said is missing ("¿qué pasó hoy / esta semana?"). Line 1 gives today's working candle + key levels yet to take.

### Q5: Falsification — same gate as Phase 1 (WR delta via `topdown_brief_renders` JOIN) or stricter?
**My recommended answer:** Same gate, extended window. Phase 1 gate already says "N≥20 brief-informed + N≥20 control, 30-60d window, KEEP if WR delta ≥ +5pp with 80% CI clean". v2 inherits but resets the window from v2 deploy date (Phase 1 N accrued under different output, not comparable).
**User answer:** (pending)
**Grade:** —
**Notes:** Same Phase 3 of plan `topdown-ict-enhancements`. No separate falsification doc needed.

### Q6: Implementation cost vs same-day-ship target
**My recommended answer:** Bundle can't all ship today. PR1 same-day (3-4 hrs). PR2 + PR3 = 1-2 days more. Total ~2 days.
**User answer:** **2 days OK.** Accepted sequencing.
**Grade:** ✅ realistic timeline accepted.
**Notes:** PR1 starts immediately after `/phased-plan` produces the plan doc.

## Final verdict — BUILD
All 6 Qs resolved. Locked:
- **Architecture:** Pure derivation over `candles` table. No new collection cron, no cache table.
- **Scope:** 3 PRs sequenced under one plan.
  - PR1 (~3-4 hrs, ~150 LOC): PD-bias conflict flag, sweep distance gate (>5% = spectator), R:R explicit line, entry/SL/TP triplet in PLAY, BOS session quality flag.
  - PR2 (~1 day, ~300 LOC): Daily Context Memory — PDH/PDL/PWH/PWL helpers + daily bias chain (last 5d) + brief section (3 lines tight).
  - PR3 (~½ day, ~100 LOC): Adaptive TP via daily ATR(14) — scaled if target ≥ 2× ATR, else single.
- **Daily Context display:** 3 lines max (today + chain + weekly). No bloat.
- **Falsification:** Same Phase 1 gate via `topdown_brief_renders` JOIN. Window resets at v2 deploy (v1 data not comparable).
- **Timeline:** 2 days total accepted.

## Pre-conditions for /phased-plan
All resolved. Ready to plan.

## Out of scope (defended)
- New cron / data collection — candles table already has it (Q1)
- AI / Claude layer over brief — premature, see prior recommendation; revisit after v2 deploys + 2 weeks usage
- `strategy_service/` changes — FREEZE until 2026-06-08
- Macro cascade (1W→1D→4H→1H) — original grill deferred to Phase 4 post-falsification
- Push alerts on new BOS / sweep — separate plan, requires Redis subscription daemon

## Handoff
On user Q1-Q6 answers → `/phased-plan topdown-v2-context-simplicity-2026-05-23`.
