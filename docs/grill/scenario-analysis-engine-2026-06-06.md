# Grill: Scenario-Analysis Engine (Hougaard conditional base-rates)

**Date:** 2026-06-06
**Topic:** Standalone engine computing P(outcome | condition) over historical candles (Tom Hougaard "scenario analysis" style) — weekday/session/post-EQH-EQL patterns on crypto.
**Verdict:** KILL — method is a multiple-testing machine; genre already disproven on crypto by `/topdown` backtest; no pinned live consumer.

## Context loaded
- FREEZE active 2026-05-13 → 2026-06-08 (today 06-06). Strategy work forbidden; read-only analysis allowed. SMC class empirically dead (0/10 setups beat random N≥15) per `docs/grill/bot-viability-2026-05-13.md`.
- 6/8 = final ML v0 re-train decision (hard-kill vs extract-platform).
- Precedent: `/topdown` manual-strategy backtest (2026-05-24) ran conditional-SMC base-rate stats → **NO EDGE** (Δ+2.32pp WR, below 10pp practical floor). `backtest_results/topdown_20260524_192804_report.md`.
- Live money = Bybit manual (~$4.6k, own journal v2). Bot = shadow-only $86 untouched.
- notes.md additions = SMC study (inducement, confirmation entries, Wyckoff) + AFML stations 1–6. Scenario-analysis lines were unsaved editor buffer.

## Decision tree

### Q1: What decision does the output change, whose hand on the trigger?
**My recommended answer:** Only manual Bybit (real capital, discretionary) is a legit consumer. Bot = SMC-dead + frozen. LLM→notes = horoscope.
**User answer:** Three consumers — manual notes via LLM/ia_service shown on dashboard, AND eventually bot automation feeding trade-trigger decisions. Also asked whether v0 can be advanced (2 days not conclusive).
**Grade:** ⚠️ weak
**Notes:** Manual survives. Bot-automation = strategy_service touch = freeze violation + feeds a corpse. LLM-notes = insight-shaped, drives nothing measurable. None pinned.

### Q2: Your method IS the overfitting machine — how do you avoid Sisyphus?
**My recommended answer:** Only survives with (1) pre-registered hypothesis + named null, (2) Bonferroni/BH multiple-testing correction, (3) OOS holdout replication, (4) min effect-size floor (≥10pp like topdown). Else KILL.
**User answer:** "Está bien, kill it." (conceded eyeball/fishing intent; accepted kill). Asked to run ml-data to see distance to gate.
**Grade:** ❌ kill signal (fishing intent confirmed)
**Notes:** User's own notes (lines 116, 160) warn against exactly this. `/topdown` already empirically killed the genre on crypto.

## ml-data snapshot (run 2026-06-06)
- Resolved MARKET shadow outcomes, current experiment `engine1_short_quarantine_v1d_2026_05_22`: **1027** (G1 gate = 500 → met 2×).
- TP 197 / SL 343 / BE 458 / timeout 13 / no_fill 16 / orphan 5.
- TP-vs-SL rate **36.5%**, below G2 band (40–60%) → near-degenerate meta-label target. The edge problem shows in the numbers, not data scarcity.
- Emission 140–328/day, 100% resolving.
- **Advancing v0 buys nothing**: N already past gate; 5/25 + 6/8 are pre-registered decision points. Run once on 6/8, no peeking (re-running until AUC looks good = p-hacking).

## Final verdict
KILL. The scenario-analysis engine is a conditional base-rate pattern miner. Across 7 pairs × weekday × hour × "after X EQH/EQL", it runs hundreds of implicit tests; ~5% fire false-positive by construction. The exact same approach (top-down SMC conditional stats) already returned no tradeable edge on crypto a few weeks ago. User conceded fishing intent. No single pinned live consumer; the bot path is freeze-forbidden and feeds an empirically dead engine.

## What would revive it
All four, together:
- Pre-registered hypothesis + named null, dated, committed to file BEFORE the query runs.
- Multiple-testing correction (Bonferroni or Benjamini-Hochberg), report adjusted p.
- Out-of-sample holdout; pattern must replicate OOS.
- Effect-size floor (≥10pp WR or explicit bps-after-fees), significance alone insufficient.
- AND one named live consumer = manual Bybit only (not bot, not LLM-notes).
