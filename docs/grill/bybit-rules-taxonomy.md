# Bybit Manual Trading — Rules Taxonomy v3
*Version 3.0 — built from grill `docs/grill/rules-rewrite-2026-05-13.md`. Replaces v2 (AI-generated theatre with 5-95% violation rates).*

**Effective:** 2026-05-13
**Validation gate:** Rule 13 — N=30 trades with full journal before any change.

---

## BLOCK 1 — ENTRY DISCIPLINE

### Rule 1 — No entries outside planned level
POC, S/R, or OB defined BEFORE order placement. If price doesn't reach the level → setup dies. Zero chasing.

### Rule 2 — Limit orders only, zero Market
Physical enforcement of Rule 1. If you find yourself wanting Market, you've already failed Rule 1.

### Rule 3 — Minimum 3 confluences
POC obligatory + 2 of {structure (rebote/rechazo confirmado), volume (decreasing into level), RSI (oversold/overbought)}. Only 2 → no trade.

### Rule 5 — 4H 50 EMA trend filter
- Price > 50 EMA (4H) AND >0.5% above → LONGS only at POC support
- Price < 50 EMA (4H) AND >0.5% below → SHORTS only at POC resistance
- Price within ±0.5% of EMA → no trades that day (chop regime)

150 EMA stays on chart as macro context. Operational filter is 50 EMA.

### Rule 6 — Journal pre-trade mandatory
Mobile annotation form filled BEFORE placing limit order:
- Pair + direction
- Entry / SL / TP1 / TP2 (concrete prices)
- 3+ confluences listed
- Thesis 1-3 lines
- Emotional state honest
- **`trigger_condition`** — concrete fire-event (e.g. "rebote en POC 4H 79.2k con vela cuerpo entero + RSI<30 5m"). Structured sub-field of Rule 1 ("planned level"). Added 2026-05-15.
- **`thesis_invalidation`** — market behaviour that breaks the thesis, distinct from SL price (e.g. "cierre 15m > 80.1k = thesis short rota"). Structured sub-field of Rule 11 ("pre-TP1 escape only on invalidation"). Added 2026-05-15.

Form auto-rejects if emotional state ∈ {impaciente, FOMO, revanchero}.
No journal entry = no trade. Engineering enforcement in `docs/plans/bybit-journal-enforcement.md`.

**Note 2026-05-15:** `trigger_condition` and `thesis_invalidation` are STRUCTURED SUB-FIELDS of existing Rules 1 + 11, NOT new rules. Rule 13 (no new binding rules during N=30 test) is therefore not violated — these capture content the user was already supposed to write inside `thesis_pre` prose, but in queryable columns. See `docs/plans/manual-edge-discipline-2026-05-15.md` Phase 1 for instrumentation rationale.

---

## BLOCK 2 — SIZE & RISK

### Rule 7 — Risk $5-7 per trade
Position size derived from SL distance, never an arbitrary $ value. `qty = risk_$ / SL_distance`.

### Rule 8 — R:R minimum 2:1
TP1 ≥ 2× SL distance. If structure doesn't permit 2:1 → no trade.

### Rule 9 — SL always set, structural
SL placed at order time. Structural (below S, above R), never mathematical ("at $X because that's the risk I want").

---

## BLOCK 3 — POSITION MANAGEMENT

### Rule 10 — Partial + BE + runner
- TP1 hit (2R): close 50%, move SL to breakeven
- TP2 hit (3R): close remaining 50%
- Zero discretion post-fill

### Rule 11 — Pre-TP1 escape only on invalidation
Close before TP1 ONLY if technical thesis invalidated (structure broken in LTF, opposite structure stronger). Reason mandatory in `exit_reason_early` field.

Not for fear. Not for "looks weird." Not for news.

---

## BLOCK 4 — GUARDRAILS

### Rule 4 — Daily stop
Stop trading the day if:
- 2 SLs hit in same day (any gap), OR
- Cumulative loss ≥ -$15

Stop = close app physically, not "watch but don't trade."

### Rule 12 — Re-entry cooldown
After SL: no re-entry same pair same direction for 4 hours. Different pair OK. Opposite direction OK. Protects against revenge disguised as "setup came back."

---

## BLOCK 5 — META (system evolution)

### Rule 13 — Forward test gate
No capital scaling or rule changes until N=30 trades with full journal under all rules 1-12.

| WR (post-fees) | PF | Action |
|---|---|---|
| ≥40% | ≥1.3 | Continue. Consider +25% capital at N=60 |
| 35-40% | 1.0-1.3 | Continue 30 more, no scale, re-evaluate |
| <35% | <1.0 | KILL strategy. Re-grill from scratch. |

Mid-test rule changes invalidate the test. Hold the line.

### Guideline (NOT binding rule yet) — Optimal trading hours
**Source:** `docs/grill/optimal-hours-2026-05-13.md` (90d OKX data analysis).

Default trading window: **9:00 AM — 12:00 PM EDT** (UTC 13-16). Best mean-reversion regime for BTC and ETH simultaneously.
Avoid: 2-7 PM EDT (UTC 18-23) — ETH dead, BTC mediocre.

If you trade outside the window, journal field `outside_optimal_hours: true` for retrospective analysis. After N=30 trades, if hour-of-day correlates with your PnL, this becomes Rule 15. Until then, guideline only — Rule 13 forbids new binding rules mid-test.

### Rule 14 — Weekly review
Sunday 30 min ritual:
1. Read all trades of the week in dashboard
2. Per trade: did all rules get followed? Yes/No
3. Per rule break: which rule? Why?
4. Write `lesson_post` per trade
5. Compute: WR, PnL, rule violation count

Non-negotiable. Sunday. Without review, the journal is dead data.

---

## Summary

| # | Rule | Block | Enforcement |
|---|---|---|---|
| 1 | Wait for level | Entry | Self + Rule 2 |
| 2 | Limit only | Entry | Self (refuse Market button) |
| 3 | 3 confluences min | Entry | Journal form |
| 4 | Daily stop 2SL or -$15 | Guardrails | Self + dashboard alert |
| 5 | 4H 50 EMA trend filter | Entry | Self + journal form |
| 6 | Pre-trade journal mandatory | Entry | Auto-reject form |
| 7 | Risk $5-7 | Size/Risk | Position sizer |
| 8 | R:R ≥2:1 | Size/Risk | Journal form |
| 9 | SL always structural | Size/Risk | Bybit conditional order |
| 10 | Partial TP1 + BE + runner | Management | Pre-set TP/SL orders |
| 11 | Pre-TP1 escape only on invalidation | Management | Self + journal `exit_reason_early` |
| 12 | Re-entry cooldown 4h same direction | Guardrails | Self + dashboard alert |
| 13 | Forward test gate N=30 | Meta | Hard discipline |
| 14 | Weekly review Sunday | Meta | Calendar block |

---

## What changed from v2 (AI-generated)

**Killed:**
- "Only BTC/ETH lun-mie AM" — was 41% violated in last 37 trades. Not user's actual belief. Symbol preference noted but not enforced.
- "Plan escrito antes" — replaced by stronger Rule 6 (form blocks order).
- "No operar aburrimiento" / "Reduce size when want increase" — replaced by Rule 6 emotional-state auto-reject.
- "Sobrevivir primero" / "Ningún trade vale cuenta" — philosophy, not operational.

**Strengthened:**
- "SL always" → now explicit structural, not mathematical.
- "Journal" → from optional post-trade nice-to-have to forcing function that blocks orders.
- "TP1 partial" → fully spec'd 50% + BE.

**Added:**
- Trend filter (Rule 5) — was missing entirely.
- Re-entry cooldown (Rule 12) — was missing entirely.
- Forward test gate (Rule 13) — was missing entirely.
- Weekly review (Rule 14) — was implied but not ritual.

---

*"My job is not to predict. My job is to manage risk, execute the plan, and accumulate enough trades to know if my plan has edge."*

*Last updated: 2026-05-13 — replaces v2 from May 2026 (AI-generated, 5-95% violation rate confirmed by data).*
