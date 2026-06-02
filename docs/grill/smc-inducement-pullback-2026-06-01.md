# Grill: SMC inducement + pullback-retest gates
**Date:** 2026-06-01
**Topic:** Make bot detectors honor two SMC concepts from `notes.md` — confirmation-entry pullback + inducement liquidity sequencing
**Verdict:** PIVOT (Q1) + DEFER to 2026-06-08 (Q2) — no detector rewrite, no hard gates, instrument-and-let-data-decide

## Context loaded
- Bot SHADOW-ONLY (`ENABLED_SETUPS=[]`, ~$86 untouched).
- Engine1 v1d: 5 pairs (ETH/SOL/LINK/AVAX/XRP), short-only, thin N.
- ML v0 baseline AUC 0.72 provisional, re-run 2026-06-08. `ML_FEATURE_VERSION=18` collection in progress.
- SYSTEM_BASELINE:1584 — Setup H killed for "chases impulse tips instead of waiting for OB retest. Kept for redesign with pullback requirement." → pullback need is documented internal, not vibes.
- `OrderBlock.retest_count` already exists (counts candles wicking into OB).
- Setup A already requires a liquidity sweep; B/F/D/engine1 do not.
- Source: user `notes.md` (Inducement + Confirmation entries sections).

## Decision tree

### Q1: Is "no pullback-retest confirmation" a real gap?
**My recommended answer:** No — bot's standing buy-limit at OB 50% IS the confirmation entry the user's own note describes ("place a buy limit order at this zone"). The audit's "wait for bounce then enter" is a different, unwritten, lower-fill model. Real sub-issue = stale pending order filling into an invalidated structure.
**User answer:** Lifecycle fix yes. A-vs-B (limit-at-zone vs wait-for-bounce) not decided by argument — in shadow, log fill context (was there a reaction before fill, or filled mid-drop?) and compare EV. Let data choose.
**Grade:** ✅ survives
**Notes:** Reframes from detector rewrite → (a) cancel-limit-on-invalidation lifecycle fix + (b) fill-context logging feature. Cost low, edge measurable.

### Q2: Build the inducement (unswept-liquidity) gate now?
**My recommended answer:** Do not hard-gate (starvation risk: engine1 already thin N, B/F/D would emit less → never reach N=100, kills shadow experiment). Adding inducement features bumps `ML_FEATURE_VERSION 18→19` → invalidates v18 rows mid-baseline.
**User answer:** (c) Defer entirely until after 2026-06-08 ML re-run. Don't disturb v18 collection.
**Grade:** ✅ survives
**Notes:** Correct sequencing. Inducement is the real conceptual gap (bot has EQH/EQL + sweep detection but zero "unswept liquidity beneath zone" sequencing) but must wait.

## Final verdict
Q1 → PIVOT: kill the "bounce-confirm market entry" idea (contradicts user's note). Build instead (1) limit-cancel-on-structure-invalidation lifecycle fix, (2) fill-context ML feature. Q2 → DEFER to 2026-06-08: inducement work blocked until v18 ML baseline re-run completes, to avoid feature-version churn + emission starvation.

## Pre-conditions for /phased-plan (run AFTER 2026-06-08)
- v18 ML baseline re-run done; decision recorded on whether feature-version bump is acceptable.
- Decide whether fill-context + inducement features ship together as v19 (single bump) to amortize cost.
- Confirm none of this introduces a hard emission gate while N still thin — log-as-feature only until ML proves the edge.
