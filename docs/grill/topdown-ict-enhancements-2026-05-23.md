# Grill: /topdown ICT Enhancements
**Date:** 2026-05-23
**Topic:** Extend existing `/topdown` Telegram brief (branch `feat/manual-edge-discipline-phase1`, commit `bb7de40`) with pure-SMC / ICT-named complements to bias detection. Bug fix on target distance. Telegram-Markdown reformat.
**Verdict:** BUILD — all additions map 1:1 to documented ICT Top-Down Analysis vocabulary. No new data sources. No `strategy_service/` change. FREEZE-safe.

## Context loaded
- `/topdown` lives on branch `feat/manual-edge-discipline-phase1` (not merged to main). Container `quant-fund-explain-bot-1` is running it. Original grill `docs/grill/topdown-telegram-brief-2026-05-20.md` verdict BUILD.
- Current implementation: `scripts/topdown_snapshot.py` — cascade 4H(×2)→1H→30m→15m. Output: reconciled bias + invalidation + nearest unbroken liq + play idea. Renderer `_render_short` for mobile.
- User trades manually on Bybit (~$4.6k capital), BTC/ETH/XRP/SOL. Rules taxonomy v3 (`docs/grill/bybit-rules-taxonomy.md`) anchors edge thesis around POC mean reversion + 4H trend filter + ≥3 confluences + Limit-only.
- FREEZE 2026-05-13 → 2026-06-08 forbids `strategy_service/` changes. Read-only analyzers (`market_structure.py`, `order_blocks.py`, `fvg.py`, `liquidity.py`, `volume_profile.py`) are reachable from `topdown_snapshot.py` — FREEZE-safe.
- User explicitly rejected classic indicators on manual-trading output (RSI, ADX, EMA-as-bias) — see memory `feedback_pure_smc_no_classic_indicators.md`. Pure SMC required.
- User asked to dial down grill maximalism on enhancements to working tools — see memory `feedback_grill_intensity.md`.

## Decision tree

### Q1: What specific incident motivated revisiting /topdown?
**My recommended answer:** Bug in `_play_idea` target selection — picks nearest unbroken liquidity below regardless of distance, producing "targets" that are essentially flat to the sweep entry.
**User answer:** "Ayer /topdown sol decía watch 84.12, invalidación 84.82, sweep entry short pero target 84.123 — confuso, sin sentido."
**Grade:** ✅ Real bug confirmed. `scripts/topdown_snapshot.py:381-385` — `liq_below.price` used directly as target with no minimum R distance filter.
**Notes:** Independent bug fix. Ship in same PR but flag as fix, not feature.

### Q2: What complement to bias detection adds most value WITHOUT bloating the brief?
**My recommended answer:** Pure-SMC strength reads (displacement, PD array position, inducement) — replace classic indicators (RSI/ADX/EMA) since user rejected those.
**User answer:** "Quiero apegarme más al SMC, market structure, top-down strategy. Cero RSI/ADX."
**Grade:** ✅ Specific + named methodology.
**Notes:** All proposed reads map directly to ICT Top-Down Analysis vocabulary. Zero new data sources required — analyzers already extract this information; just need to surface it in the brief.

### Q3: Is this strategy documented or are we inventing?
**My recommended answer:** ICT Top-Down Analysis is the canonical documented framework. Every proposed enhancement maps to a named ICT concept.
**User answer:** "Tiene que tener sustento, no inventarlo."
**Grade:** ✅ Sustento mapping:

| Enhancement | ICT name | Source |
|---|---|---|
| Cascade 4H→1H→30m→15m | Top-Down Analysis | ICT Core Content "Time and Price" |
| Displacement strength | Displacement Candle | ICT Mentorship 2022 "Market Maker Models" |
| Premium/Discount range position | PD Array / Dealing Range | ICT "Premium and Discount Arrays" |
| Inducement flag | Inducement (IDM) | ICT direct term |
| OB pristine vs mitigated | Mitigation Block / Unmitigated OB | ICT Order Block + Mitigation theory |
| Unfilled FVG magnets | Fair Value Gap / Liquidity Void | ICT "Liquidity Voids and Fair Value Gaps" |
| Equal highs/lows | Buyside/Sellside Liquidity (BSL/SSL) | ICT Liquidity Pools — already in `liquidity.py` |
| Killzone overlay | ICT Killzones (Asian/London/NY) | ICT Killzones direct term |

Public references: ICT YouTube official, Maven Trading curriculum, "The Inner Circle Trader" Patreon, TradingView SMC Lux indicator (100k+ users implementing same concepts), Babypips SMC section.

### Q4: Telegram readability requirements?
**My recommended answer:** Telegram Markdown (`*bold*`, `_italic_`, `` `code` ``), short lines, emoji as visual flag (✅⚠️🟢🔴) for 3-second scan, one mobile screen total (~25 lines).
**User answer:** "Que sea fácil de leer e interpretar."
**Grade:** ✅ Concrete spec.
**Notes:** Sample brief mockup approved (see Final verdict). Current `_render_short` already mobile-aware — only needs Markdown formatting + section reorganization.

### Q5: Falsification for the enhancements?
**My recommended answer:** Original grill (2026-05-20) committed to WR delta via `topdown_brief_used` annotation flag, N≥20 per bucket, 30d window. Enhancements inherit that gate. Plus: count "confusing target" incidents pre/post bug fix — should drop to zero.
**User answer:** (not explicitly asked — user is in build-it mode after rejection of grill maximalism, per `feedback_grill_intensity.md`)
**Grade:** ⚠️ Carrying forward original gate, not adding new falsification.
**Notes:** Acceptable for read-only tool enhancement. The bug fix has zero downside (current target output is broken). The new reads add information without changing existing reconciled bias logic. Worst case = noise in brief, fixable by hiding sections.

### Q6: Implementation cost vs FREEZE constraint?
**My recommended answer:** ~5-6 days dev. Phase 1 = bug fix + reformat + 3 reads = single PR off `feat/manual-edge-discipline-phase1`. Zero `strategy_service/` touches.
**User answer:** (implicit accept)
**Grade:** ✅ FREEZE-safe.
**Notes:** All analyzers used are read-only (`market_structure.py`, `order_blocks.py`, `fvg.py`, `liquidity.py`, `volume_profile.py`). No ML feature version bump. No new WS subs. New file `scripts/topdown_snapshot.py` extensions + Telegram handler in `scripts/explain_bot.py`.

## Final verdict — BUILD

All 6 grill criteria pass for enhancement scope:
- Scientific basis ✅ (ICT Top-Down Analysis, publicly documented + widely adopted)
- Concrete failure (SOL target bug) ✅
- Expected edge: decision-support tool, not signal generator — falsification inherits original gate
- Falsification criterion ✅ (existing `topdown_brief_used` annotation WR delta, N≥20)
- Implementation cost justified ✅ (~5-6 days for full Phase 1+2, FREEZE-safe)
- No simpler alternative — current brief is the simpler alternative; we're extending it incrementally

Sample Phase 1 output (Telegram Markdown):

```
*SOL/USDT* — 14:32 UTC (lag 1m ✅)

*BIAS:* 🔴 SHORT — _medium_ (4/5)
4H bear desde 5/21 04:00 (2d 10h)

*ICT STRENGTH:*
• Displacement 4H: 🟢 _strong_ (3/3 bear, body 1.4% vs 0.5%)
• PD Array 4H: 72% _premium_ (favorable shorts)
• Last BOS: 🟢 _IDM confirmed_ (swept 87.80 first)
• Killzone: 🟢 _London active_

*KEY ZONES:*
🔴 4H Supply OB `85.40` PRISTINE (+1.5%)
🔴 Bear FVG 1H `85.40-85.78` unfilled (6h)
🔴 BSL `85.50` × 3 toques (engineered)

*MAGNETS BELOW:*
🟢 SSL `82.00` × 2 (-2.5%)
🟢 Bull FVG 4H `82.10-82.45` unfilled

*PLAY:*
Wait sweep above `84.82` → short rejection
SL: 4H close > `85.78`
TP: `82.10` (~3R)
_⚠️ skipped 84.12 target — too tight_

*INVALIDATION:* 4H close > `85.78`
```

## Pre-conditions for /phased-plan

1. Branch base: `feat/manual-edge-discipline-phase1` (where `/topdown` lives) — phased plan should branch off this, NOT main.
2. Killzone session boundaries must match ICT spec (Asian 20:00-00:00 UTC, London 02:00-05:00, NY AM 12:00-15:00, NY PM 18:00-20:00) — already in `ml_features.py` v14 trading_session feature, reuse.
3. PD Array range definition: last major 4H swing high → swing low (or vice versa). Equilibrium = 50%, premium >50%, discount <50%. Use `MarketStructureState.swing_highs/swing_lows` already extracted.
4. Displacement strength formula: last 3 candles avg body % vs prior 30-candle avg body %. Strong = ≥2× avg + same direction + close ≥80% to extreme.
5. Inducement detection: scan last 50 candles before the BOS for a liquidity sweep (`liquidity.LiquidityLevel.swept=True`) in opposite direction within ±N candles of the BOS. Threshold N=10 candles to start.
6. Reference doc: create `docs/topdown_brief_reference.md` mapping every brief element to its ICT concept + public source link — required for grounding.

## Phased plan outline (skeleton for /phased-plan)

**Phase 1 — single PR off `feat/manual-edge-discipline-phase1`:**
- Bug: `_play_idea` target distance min 1.5R floor (drop noise targets)
- Reformat `_render_short` → Telegram Markdown with emoji flags
- Displacement strength per TF (4H + 1H) in new `_displacement_read()` helper
- PD Array range position in new `_pd_range_position()` helper
- Inducement (IDM) flag on last BOS/CHoCH in new `_inducement_check()` helper
- `docs/topdown_brief_reference.md` with ICT concept mapping table
- Tests: unit tests on each helper + golden-file test on rendered brief sections

**Phase 2 — after 2 weeks of Phase 1 in use:**
- OB pristine/mitigated status per TF
- Unfilled FVG magnets section (above + below, ranked by distance + age)
- Killzone session overlay using `ml_features.py` v14 trading_session
- Equal highs/lows engineered liquidity flag

**Phase 3 (gated by N≥20 `topdown_brief_used` annotations + WR delta measurement):**
- Replicate cascade for macro (1W→1D→4H→1H) and scalp (1H→30m→15m→5m) per original grill outline
- Push alerts on new BOS / new sweep when subscribed pair changes state

## If KILL: would-revive condition
N/A — verdict BUILD.

## Handoff

Next step: user invokes `/phased-plan topdown-ict-enhancements-2026-05-23` using this doc as input. Phased plan should:
- Confirm branch base `feat/manual-edge-discipline-phase1`
- Detail file-level changes for Phase 1
- Specify tests + acceptance criteria per Phase 1 deliverable
- Reference SYSTEM_BASELINE §9 FREEZE-safe constraints
