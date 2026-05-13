# Grill: Rewrite Bybit trading rules
**Date:** 2026-05-13
**Topic:** User's current 14-rule taxonomy mixes rules he follows, rules he violates, philosophy, and emotional aspirations. Strip to what's actually evidence-backed and operationally enforceable. Add rules data justifies.
**Verdict:** BUILD — taxonomy rewritten from 14 AI-generated theatre rules to 14 evidence-backed operational rules. New taxonomy saved to `docs/grill/bybit-rules-taxonomy.md` (old version archived inline below).

## Context loaded
- Current taxonomy: `docs/grill/bybit-rules-taxonomy.md` (14 rules across 5 blocks)
- Detectability audit: `docs/grill/bybit-rules-detectable.md`
- Phase 1 sample: 37 closed_pnl trades, 91 executions (2026-03-18 → 2026-05-03)
- Bot viability grill: `docs/grill/bot-viability-2026-05-13.md` (verdict PIVOT, freeze active)

## Evidence from current behavior (37 trades sample)

**Rules user PROVABLY FOLLOWS:**
- Rule 2 (SL definido) — 100% compliance, every trade has SL trigger captured
- Rule 8 (no leverage escalation) — leverage constant 10x across all 37 trades (or default; user must clarify)

**Rules user PROVABLY VIOLATES:**
- Rule 11 (day-of-week, lun-mie AM only) — **41% violation** (15/37 trades on jue/vie/sáb/dom). PnL on violation days = -$13.88 in sample.
- Rule 11 (BTC/ETH only) — **19% violation** (7/37 on SOL/DOT/LINK). Counterintuitively those trades had +$4.84 PnL vs BTC/ETH -$13.47 (small N noise probable).
- Rule 14 (journal) — **95% violation**. 5% thesis_pre fill rate, 0% lesson_post, 0% grade_self.
- Rule 1 (plan escrito) — proxy via order type. ~50% Market orders (no preset price = no full plan).

**Rules user MOSTLY FOLLOWS:**
- Rule 9 (max 2 positions) — 5 overlap events in 37 trades, mostly 2-position overlaps (within rule).

**Rules UNMEASURABLE retroactively:**
- 4 (boredom), 5 (don't touch), 6 (don't move SL), 7 (no add without invalidación), 8 (reduce when want to increase), 10 (2 losses stop day, partial), 12-13 (philosophy).

## Decision tree

### Q1: Why did you write 14 rules you don't follow?
**My recommended answer:** (a) or (b) — copied from somewhere, never internalized. Theatre rules are worse than no rules.
**User answer:** (e) — "platico mucho con Claude y ChatGPT, le pedí que me diera un libro de reglas, no son reglas que sigo." Real rules in practice are 3:
1. Risk per trade $5-7 (data confirms — 0.4 ETH × ~0.5% SL distance = ~$4.60 loss matches sample)
2. R:R ≥2:1
3. SL always set (data confirms — 100% compliance)

Real entry process: opens chart, looks for rebound/rejection at S/R, RSI, decreasing volume, volume profile POC.

**Self-identified leak (the most important fact in the entire session):**
> "a veces me precipito y solo quiero entrar y busco adaptar el risk a la entrada inmediata y termina tocando mi SL. esos han sido mis últimos trades, en lugar de esperar el POC, entro y toca mi SL."

Translation: identifies a planned level (POC), does NOT wait for price to reach it, enters at current price, retroactively constructs SL around current price. SL gets hit because the entry premise was already broken (entered without the edge condition being met).

**Grade:** ✅✅✅ breakthrough — full honesty, root-cause leak named without prompting.
**Notes:**
- The 14-rule taxonomy is officially dead. New taxonomy must be built from his ACTUAL process.
- His PnL leak is not lack of risk discipline (he sets SL, sizes small). It's PATIENCE — entering before the trigger condition is met.
- Math note: $5-7 per trade on $4.6k = 0.11-0.15% risk, far below his stated 1-2%. He's actually CONSERVATIVE on sizing. The conservative sizing means even his bad entries don't blow up — they just bleed slowly.

### Q2: Commit to "wait for level, no chase, Limit-only enforcement"?
**My recommended answer:** (a) committed, but only credible if backed by structural enforcement (Limit-only, no Market) since willpower historically fails ~50% of trades.
**User answer:** (a) committed, 100%.
**Grade:** ✅ — strong commit. Risk: data shows historical contradiction (50% Market). Structural enforcement required, not promised.

**Locked rules so far:**
- **Rule 1.** No entries outside planned level. POC/S/R/OB defined before order. If price doesn't reach → setup dies.
- **Rule 2.** Limit orders only. Zero Market. Physical enforcement of Rule 1.

### Q3: Define minimum confluence to place a limit order
**My recommended answer:** (b) 3 confluences min: POC obligatory + 2 of (structure, volume, RSI).
**User answer:** (b) accepted. Confessed historical behavior was (d): "entro cuando siento que el setup está bien pero no confirmo del todo."
**Grade:** ✅ accepted, with explicit confession that he's been violating his own intuition by not confirming.

**Locked Rule 3.** Minimum 3 confluences before placing limit order: POC obligatory + 2 of {structure (rebote/rechazo confirmado), volume (decreasing toward level), RSI (overbought/oversold)}. If only 2 → no trade.

### Meta-question raised by user: "¿mi estrategia es sólida o mala?"
**Answer:** Cannot be determined from current data.
- Sample of 37 trades was with broken discipline (50% Market orders, no confluence rule). Those results don't validate or invalidate the new ruleset.
- POC mean reversion has theoretical basis (resting liquidity magnet). Real microstructure phenomenon, not astrology.
- Risk: POC reversion fails in trending markets — no trend filter currently. RSI alone is weak in trends.
- Verdict: forward test 30-50 trades with new rules strictly applied → measure WR + PF vs benchmark. If WR ≥40% and PF ≥1.3 → strategy has edge. If WR <35% → kill or pivot.
- Backtest path rejected for now (engineering work + subjective criteria don't backtest well).
- This will be encoded as Rule N (forward-test gate before scaling).

### Q4: Daily loss cap?
**My recommended answer:** (a) AND (c) combined — 2 SLs OR -$15 cumulative.
**User answer:** Accepted (a) + (c).
**Grade:** ✅ accepted, fast.

**Locked Rule 4.** Stop trading the day if: 2 SLs hit in same day (any gap) OR cumulative loss ≥ -$15. Close app physically — not "watch but don't trade."

### Q5: Trend filter to avoid POC-reversion-into-trend deaths
**My recommended answer:** (b) 4H 50 EMA binary filter.
**User answer:** Accepted. Confessed prior process was checking 4H trend visually + POC + liquidity but no fixed rule. Knew limit-at-support without trend filter was wrong but did it anyway. Has 150 EMA on chart already.
**Grade:** ✅ accepted with confession of unstructured prior workflow.

**Locked Rule 5.** 4H 50 EMA defines allowed direction.
- Price > 50 EMA (4H) AND >0.5% above → LONGS only at POC support
- Price < 50 EMA (4H) AND >0.5% below → SHORTS only at POC resistance
- Price within ±0.5% of EMA → no trades that day (chop regime)

User can keep 150 EMA on chart as macro context. Operational filter is 50 EMA.

### Q6: Journal as pre-trade forcing function?
**My recommended answer:** (a) journal mandatory pre-trade, auto-rejects on bad emotional state.
**User answer:** (a) accepted.
**Grade:** ✅ accepted.

**Locked Rule 6.** Journal pre-trade mandatory. Mobile form (par/direction/entry/SL/TP/confluences/thesis/emotional state) filled BEFORE placing limit. Form auto-rejects if emotional ∈ {impaciente, FOMO, revanchero}. No journal = no trade. Engineering work to enforce this lives in `bybit-journal-enforcement.md` plan.

### Locked from existing practice (no grilling needed — already followed in data)
- **Rule 7.** Risk per trade = $5-7 USD. Position sized from SL distance, not arbitrary $ value.
- **Rule 8.** R:R minimum 2:1. TP1 ≥ 2× SL distance. Setup without 2:1 in reasonable structure → no trade.
- **Rule 9.** SL always set at order placement. SL is structural (below S, above R), never mathematical.

### Q7: Position management post-fill
**My recommended answer:** (b) partial TP1 50% + BE + runner TP2.
**User answer:** Accepted both proposed rules.
**Grade:** ✅ accepted.

**Locked Rule 10.** Partial 50% at TP1 (2R), move SL to BE immediately. Runner 50% targets TP2 (3R). Zero discretion post-fill.
**Locked Rule 11.** Escape pre-TP1 only if technical thesis invalidated clearly. Reason mandatory in journal `exit_reason_early`. Not for fear, "looks weird," or news.

### Q8: Re-entry after SL
**My recommended answer:** (b) cooldown 4h same pair same direction.
**User answer:** Accepted.
**Grade:** ✅ accepted.

**Locked Rule 12.** After SL, no re-entry same pair same direction for 4h. Other pair OK. Opposite direction OK.

### Q9: Forward test gate + weekly review
**My recommended answer:** (a) both rules.
**User answer:** (a) accepted both.
**Grade:** ✅ accepted.

**Locked Rule 13.** Forward test gate: N=30 trades with journal full under rules 1-12 before scaling capital or changing rules. Decision table:
- WR ≥40% AND PF ≥1.3 → continue, consider +25% capital scaling at N=60
- WR 35-40% AND PF 1.0-1.3 → continue another 30 trades, no scale
- WR <35% OR PF <1.0 → KILL strategy, re-grill from scratch

**Locked Rule 14.** Weekly review ritual: Sunday 30 min. Read all trades, check rule compliance per trade, write `lesson_post` per trade, compute WR/PnL/violation count.

## Final verdict

**BUILD.** Taxonomy completely rewritten. 14 rules, all evidence-backed or commitment-backed. Original AI-generated 14-rule taxonomy archived.

Verdict reasoning:
- User confessed prior taxonomy was AI-generated theatre with 5-95% violation rates
- Real edge mechanism identified by user: POC mean reversion with proper patience
- Real leak identified by user: FOMO entries adapting risk to current price instead of waiting for level
- New ruleset addresses BOTH the edge thesis (rules 1, 3, 5) and the leak (rules 1, 2, 6)
- Forward test gate (rule 13) prevents premature scaling
- Forcing functions (rule 2 limit-only, rule 6 journal-mandatory, rule 4 daily stop) reduce willpower dependency

## What survives from original 14-rule taxonomy
- SL always set (was rule 2, now Rule 9)
- R:R ≥2:1 (implicit, now Rule 8 explicit)
- Risk per trade (was rule 3, now Rule 7 with concrete $5-7)
- TP1 partial + BE (was rule 5 partial, now Rule 10 explicit)
- Don't move SL (was rule 6, now Rule 11)
- Don't add to losers (was rule 7, partially captured in Rule 11)

## What was killed from original taxonomy
- Rule 1 (plan escrito) — replaced by Rule 6 (journal forcing function, stronger)
- Rule 4 (no operar aburrimiento) — replaced by Rule 6 emotional-state field
- Rule 8 (reduce when want to increase) — replaced by Rule 6 emotional-state field
- Rule 11 (only BTC/ETH lun-mie AM) — KILLED. Was 41% violated. Symbol filter dropped (BTC/ETH preferred but not enforced — keep open if confluences present). Day-of-week killed entirely — was AI-generated, not user's belief.
- Rule 12 (sobrevivir primero) — philosophy, dropped
- Rule 13 (ningún trade vale cuenta) — philosophy, dropped

## Pre-conditions for /phased-plan (already met)
- `bybit-journal-enforcement.md` plan already exists. Rule 6 is the rule it enforces.
- User accepts that no further plans are needed until journal data accumulates under new rules.

## Process commitments going forward (re-stated)
- Sequential: don't change rules mid-test. N=30 first.
- One thesis at a time. POC mean reversion with these 14 rules is THE strategy until N=30 disproves it.
- Kill list grows, not feature list. If a rule isn't working, drop it. Don't add a sub-rule to patch it.

