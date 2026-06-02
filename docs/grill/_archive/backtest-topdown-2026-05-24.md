# Grill: Backtest /topdown manual strategy

**Date:** 2026-05-24
**Topic:** Historical backtest of `/topdown` SMC top-down manual strategy to measure edge vs random-entry null.
**Verdict:** **BUILD** — strategy is theory-grounded, null is well-defined, fees survive maker-only model, deadline + budget concrete, sample size honest about power limits.

---

## Context loaded

- `CLAUDE.md`, `docs/SYSTEM_BASELINE.md` (partial: §1-9), `MEMORY.md`
- `git log -20`: branch `feat/topdown-v2-pr4-structure-context` — PR4 Structure Context just merged (c6c54fe). PR1-PR4 stack:
  - PR1: PD conflict + sweep gate + R:R triplet + BOS session
  - PR2: Daily Context Memory (PDH/PDL/PWH/PWL + bias chain)
  - PR3: Adaptive TP via daily ATR(14)
  - PR4: HTF duration + LTF flip + impulse + wick tap
- `scripts/topdown_snapshot.py` (2024 LOC) — deterministic SMC brief. No LLM. `_build_snapshot()` + `_trade_triplet()` = mechanically backtestable core.
- Prior grills: `topdown-telegram-brief-2026-05-20.md` (BUILD), `topdown-ict-enhancements-2026-05-23.md`, `topdown-phase4-structure-context-2026-05-23.md`, `topdown-v2-context-simplicity-2026-05-23.md`. Falsificación live ya definida: `topdown_brief_used` flag, N=30 trades, 30d post-Phase 4.
- FREEZE active 2026-05-13 → 2026-06-08. Backtest manual = analysis/infra → FREEZE-safe (no `strategy_service/` touch, no ML version bump).
- Capital: ~$108 OKX shadow-only + ~$4.6k CAD Bybit manual. Bybit non-VIP, 60d volume 109,896 CAD, **limit orders only** → maker fee 0.01% per side, 0.02% RT.

## Key facts pulled

- `/topdown` is rules-only Python. Backtest cost = **zero tokens, zero LLM calls**. User initially proposed Ollama local; clarified unnecessary.
- Strategy is theory-derived (Price Action + Market Structure + SMC top-down cascade: 1W → 4H → 1H → 30m → 15m, candle-close break reads). Not reverse-engineered from recent winners. Lower overfit prior, but PR1-PR4 parameters (sweep_distance ≤2%, R:R triplet, ATR multipliers, displacement thresholds) were tuned by feel — those specific knobs retain overfit risk.
- Bot baseline (60d, no AI, aggressive): 97 trades, 51.5% WR, +$7,558, PF 1.81. `/topdown` adds gates the bot lacks (HTF cascade reconciliation, sweep actionable, BOS session quality, structure context). Backtest measures whether these *additions* add edge.

---

## Decision tree

### Q1: What decision does the backtest unblock?

**Recommended answer:** Tie to concrete action (raise/cut Bybit size; revert PR4; stop manual until redesign; port to bot post-FREEZE).
**User answer:** "Quiero saber si la estrategia tiene edge y si las notificaciones del /topdown son confiables."
**Grade:** ✅
**Notes:** Two distinct artifacts — edge measurement on full triplet, reliability per-signal. Both feed go/no-go on continuing manual + port-to-bot decision post-FREEZE. Concrete enough.

### Q2: Null hypothesis?

**Recommended answer:** Random entry, same SL/TP/timeout — isolates signal edge from R:R money management.
**User answer:** A (random entry).
**Grade:** ✅
**Notes:** Strong null. Forces strategy to beat its own money-management asymmetry. Buy-and-hold deferred as optional secondary sanity check.

### Q3: Window + sample size?

**Recommended answer:** 180d × 7 pairs. Honest about power: N≈400-1000, only Δ ≥ 10pp WR detectable. Smaller real edges will read as "inconclusive."
**User answer:** Accepted ventana. Stated explicitly that small-N limitation is understood.
**Grade:** ✅
**Notes:** No false confidence on power. Inconclusive ≠ no edge.

### Q4: Fees model?

**Recommended answer:** Taker-only worst case (0.11% RT).
**User answer:** Limit-only on Bybit, non-VIP, 60d volume 109,896 CAD. → maker 0.01% × 2 = **0.02% RT**.
**Grade:** ✅
**Notes:** Far below my conservative prior. Fees become trivial vs R:R 2:1. Backtest uses **0.02% RT maker** as base, with **0.11% RT taker** sensitivity check.

### Q5: Deadline?

**Recommended answer:** 2026-06-07 (pre-FREEZE expiration).
**User answer:** Confirmed.
**Grade:** ✅
**Notes:** 14d budget. If edge proves out, post-FREEZE planning has a concrete artifact.

### Q6: Implementation budget?

**Recommended answer:** 3-4 days dev + 1 day analysis.
**User answer:** Confirmed available.
**Grade:** ✅
**Notes:** Reuses `scripts/backtest.py` infra (candle loader, fee model). New code: triplet simulator + random benchmark + report.

### Q7: Anti-overfit / reverse-engineering check?

**Recommended answer:** 70/30 train/holdout split if any rules were chart-mined.
**User answer:** Strategy is doctrinally grounded — Price Action / Market Structure / SMC top-down cascade. Theory-first, not data-mined winners.
**Grade:** ✅ (with caveat)
**Notes:** Gates from canon (HTF cascade, BOS, OB, sweep) accepted as theory prior — no split needed. PR1-PR4 parameter values (sweep ≤2%, R:R triplet ratios, ATR multipliers, displacement thresholds) ARE tuned by feel → those retain overfit risk. **Mitigation:** 70/30 split applied to *parameter sensitivity sweeps only*, not to gate logic itself.

---

## Final verdict

**BUILD.** 7/7 criteria pass:

- ✅ Scientific basis: SMC / Price Action / Market Structure — established retail trading canon (ICT, Wyckoff lineage), implemented in `scripts/topdown_snapshot.py` from theory not chart-mining.
- ✅ Statistical case: random-entry null, N target 400-1000, power floor Δ ≥ 10pp known and accepted.
- ✅ Edge survives fees: maker-only 0.02% RT is trivial vs R:R 2:1.
- ✅ Falsification: Δ ≥ 10pp WR vs random by 2026-06-07.
- ✅ Implementation: 3-4d, zero tokens, reuses existing infra.
- ✅ No simpler alternative ≥70% upside: live N=30 needs months at 1-5 trades/week; backtest delivers N=400+ in days.
- ✅ Not reverse-engineered: doctrinal SMC, not winner-fishing.

## Pre-conditions for /phased-plan

- Confirm 7 pairs (BTC/ETH/SOL/DOGE/XRP/LINK/AVAX) vs 4-pair Bybit scope (BTC/ETH/SOL/XRP). Recommend 7 for N, report breakdown by Bybit-relevant subset.
- Confirm window: 180d historical candles from PostgreSQL `candles` table (verify coverage on 15m + 30m + 1h + 4h for all 7 pairs).
- Confirm random benchmark spec: same pair, same direction distribution, same SL/TP distance distribution, same time-of-day distribution — only the *trigger candle* is randomized.
- Confirm reliability per-signal study scope: separate from edge backtest, or bundled? Recommend bundled (cheap to add — already have triplet labels).
- Output spec: markdown report + CSV of trades + optional Grafana dashboard panel.

## Sensitivity matrix to include in plan

- Fees: 0.02% RT (maker base) + 0.11% RT (taker stress)
- Window: 180d primary + 90d/120d/180d sweep for stability
- Split: full sample primary + 70/30 sensitivity on PR1-PR4 tuned params only

## Handoff

Next step: `/phased-plan backtest-topdown-2026-05-24` using this grill doc as input.

If backtest verdict (2026-06-07) shows edge:
- Plan port of `/topdown`-specific gates (HTF cascade reconcile, sweep actionable, R:R triplet, adaptive TP, structure context) into `strategy_service/` as new variant post-FREEZE.

If verdict shows no edge or inconclusive:
- Do not port. Continue live falsification via `topdown_brief_used` journal flag (already planned).
- Optional: tighten gates and re-backtest, but only once — second re-tune = p-hacking territory.
