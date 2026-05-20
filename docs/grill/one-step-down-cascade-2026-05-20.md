# Grill: One-Step-Down Multi-TF Cascade
**Date:** 2026-05-20
**Topic:** Refactor bot from 2-layer bias+entry (4H+1H → 15m/5m) into 3 explicit cascades (Macro 1W→1D→4H→1H, Swing 4H→1H→30m→15m, Scalp 1H→30m→15m→5m) with supply/demand zone framing.
**Verdict:** KILL — measured pain does not map to absence-of-cascade. Cost = 15× simpler alternative. Timing kills ML v0 baseline mid-validation.

## Context loaded
- Bot SHADOW-ONLY since 2026-04-15. `ENABLED_SETUPS=[]`. ~$86 untouched.
- 7 pairs. `HTF_TIMEFRAMES=["4h","1h"]`. `LTF_TIMEFRAMES=["15m","5m"]`. `SWING_LOOKBACK=5`.
- WS subscribes 5m/15m/1h/4h only. 1d only if `HTF_CAMPAIGN_ENABLED=true` (off). **1W and 30m do not exist anywhere.**
- `market_structure.py` HAS swings + BOS + CHoCH + trend. Recomputes each call (no persistence of daily H/L). Fractal algo, close-confirmed breaks (wick-only ignored). Algorithmically sound.
- `ML_FEATURE_VERSION=18`. ML v0 meta-label baseline AUC test 0.72 provisional. Re-runs scheduled **2026-05-25 + 2026-06-08** (5 + 19 days away).
- Engine 1 v1c (short only, all pairs) since 2026-05-07.

### Data pulled during grill (last 14d shadow PnL by setup)
| Setup | TP | SL | BE | PnL | $/trade | N |
|---|----|----|----|------|---------|---|
| engine1_trend_pullback | 19 | 48 | 103 | **−$264** | −$1.55 | 171 |
| bench_random | 18 | 76 | 88 | −$427 | −$2.33 | 183 |
| bench_market_now | 18 | 93 | 84 | −$527 | −$2.67 | 197 |
| setup_b | 4 | 4 | 1 | **+$25** | +$1.64 | 15 |
| setup_f | 1 | 8 | 0 | −$28 | −$1.89 | 15 |
| scalp_liq_reclaim | 8 | 1 | 0 | **+$47** | +$3.63 | 13 |

Key findings:
- Engine 1 (volume #1) LOSES absolute but beats both benchmarks by $0.78-$1.12/trade. Edge exists but sub-fee.
- **60% BE rate on engine1** (103/171). TP1 at 1.0R triggers BE move, then reversal stops out flat.
- setup_b + scalp_liq_reclaim in profit but N too low (15, 13) — anecdotal.
- scalp_random_baseline is statistical control — N=775, by-design negative.

## Decision tree

### Q1: Scientific basis for One-Step-Down
**My recommended answer:** No peer-reviewed paper demonstrates cascade-N-TF > bias-2-TF on crypto perps. ICT/SMC literature is anecdotal, unfalsifiable, survivor-biased.
**User answer:** "Vi videos de Price Action y me resuena." Steelmanned later: "estrategia no está funcionando — puros SLs $7-8."
**Grade:** ❌ on basis. ⚠️ on steelman (measured pain valid, but causation not proven).
**Notes:** User shifted argument from "videos resonated" to "current strategy losing — therefore add structure". Latter requires proof of binding constraint, not assumption.

### Q2: Which setup loses, and what failure mode?
**My recommended answer:** Need post-mortem classification before assuming bias-error is binding. Candidate modes: wrong direction / SL tight / late entry / wrong zone.
**User answer:** "scalp_random_baseline (no — that's a control, ignore) and setup_b (no — it's in profit). Not sure on others. Got be_rate_high_cleared alert."
**Grade:** ❌ — user could not identify losing setup or classify failure mode.
**Notes:** Perception biased by recency (engine1 emits 171/14d, user sees flood of SL/BE alerts). Setup_b he named is actually profitable. Real losing setup (engine1) was not identified by user.

### Q3: Accept that binding constraint is NOT bias cascade, or defend?
**My recommended answer:** Accept. Real binding constraint = BE rate 60% + sub-fee edge. OSD does not fix either. Simple fix: move TP1 from 1.0R to 1.3R, raise impulse threshold, filter compression regime. Cost = 1-2 lines vs OSD = WS+REST infra + new evaluator + ML version bump.
**User answer:** "Sí espera al 5/25 + clasifica SLs offline" (chose option A).
**Grade:** ✅ — accepted. Pivot logged.

## Final verdict — KILL

OSD survives 0 of 7 BUILD criteria:
- ❌ Scientific basis (folk theory)
- ❌ Statistical case (no N proposed)
- ❌ Expected edge (no estimate)
- ❌ Falsification (no criterion)
- ❌ Cost justified (massive — invalidates ML v0 mid-flight)
- ❌ Simpler alternatives exist (TP1 distance, impulse threshold, compression filter)
- ❌ Not reverse-engineered from winners (it IS — ICT/SMC content is survivor-curated)

**What would revive it:** post-mortem of ≥50 engine1 SLs shows modal failure = "wrong HTF direction" (>40% of losses). Then 1D bias addition (NOT full cascade) becomes targeted, cheap, falsifiable. Even then, only after 2026-05-25 ML v0 re-run completes.

## Pivot accepted
**New work**: SL classifier post-mortem. Read-only analysis script. Does NOT touch detectors, settings, or ML feature surface. Safe to run during ML v0 validation window.

→ See plan: `docs/plans/sl-classifier-postmortem.md`
