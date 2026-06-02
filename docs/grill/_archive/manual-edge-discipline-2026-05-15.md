# Grill: Manual Trading Edge + Journal Discipline

**Date:** 2026-05-15
**Topic:** User asked about porting SMB blog PineScript ORB signal + trade journal prompt + rebuilding Telegram alerts. Real underlying question surfaced mid-grill: "no tengo estrategia para mis trades manuales."
**Verdict:** **PIVOT → BUILD** — kill the surface ideas; build a constrained manual-trading rule system with pre-trade gating and falsification metric, baked into existing Bybit journal.

> **Amendment 2026-05-15 (post-/phased-plan pre-flight):** Grill was written without first reading `docs/grill/bybit-rules-taxonomy.md` v3 + `docs/plans/_archive/bybit-journal-enforcement.md`. The "5 rules" listed in this doc OVERLAP and partially CONFLICT with v3 Rule 13 ("no new binding rules during N=30 forward test"). Specifically: BTC exclusion, long exclusion, and the 13–22 UTC hour window cannot be added as binding rules right now without violating Rule 13. They are recorded here as Action-C hypotheses to test AFTER N=30 closes. The plan derived from this grill (`docs/plans/_archive/manual-edge-discipline-2026-05-15.md`) therefore instruments existing v3 rules (adds structured `trigger_condition` + `thesis_invalidation` fields + falsification widget) instead of adding new rules.

## Context loaded

- `CLAUDE.md` + `dashboard/CLAUDE.md` — architecture, mobile rules, manual module is self-contained
- `docs/SYSTEM_BASELINE.md` §1 (bot SHADOW-ONLY, $108, all setups off live)
- `MEMORY.md` — Bybit integration ~$4.6k UTA, journal enforcement rule 6 just shipped (PR #29), watcher schema fix (PR #30)
- `git log -20` — recent merges: rule 6 enforcement, watcher ensure_tables, bot viability grill
- PostgreSQL `bybit_closed_pnl` table — 38 trades 2026-03-18 → 2026-05-15, all symbols
- `dashboard/web/src/app/annotate/[id]/page.tsx` (928 LOC) — current journal annotation UI
- SMB blog content — extracted PineScript + journal prompt verbatim

## Decision tree

### Q1: Premise transfer — does the SMB Opening Range Breakout signal apply to OKX SWAP / Bybit perps?

**My recommended answer:** No. ORB is built on US equities session structure (NYSE 9:30 ET open, defined session, overnight gap discovery, single closing auction). Crypto perps are 24/7 with no session. The phenomenon does not exist at the proposed timescale. Copy-paste from template without crypto-native thesis = vibes signal.

**User answer:** "Si solo copie el template... queria sber si habia algo qeu podia sacar de ahi."

**Grade:** ❌ KILL signal — admitted no thesis. Honesty saves cycles.

**Notes:** Full salvage scan of all 5 SMB practices vs current stack returned zero net-new value. User already has all 5 categories shipped at higher rigor (ML feature versioning, EXPERIMENT_ID, AFML roadmap, journal enforcement). SMB article is retail tutorial level; user is past it operationally.

---

### Q2: Telegram alerts pivot — "el que esta ahora mismo no sirve"

**My recommended answer:** Cannot grill without concrete pain quote. Three alert surfaces in repo (`shared/notifier.py`, `shared/alert_manager.py`, Bybit `/explain` bot). "No sirve" is vibes — needs incident, expected vs actual, channel.

**User answer:** Did not address. Pivoted instead to real underlying problem: no defined manual strategy + self-listed 7 flaws (no master TF, thesis-first, no predefined trigger, entry premise ≠ target premise, SL without invalidation, anxiety without protocol, manual lacks bot rigor).

**Grade:** ⚠️ Deferred. Telegram rebuild is downstream of strategy definition. Premature optimization until strategy exists.

**Notes:** User reframed honestly. Real grill begins here.

---

### Q3: Does the user have a manual trading edge today? (Data-driven, replaces user's "do I have a strategy" question)

**My recommended answer:** Run the query. Strategy without baseline data = vibes squared.

**Data pulled from `bybit_closed_pnl`:**

| Metric | Value |
|---|---|
| Trades | 38 |
| Window | 2026-03-18 → 2026-05-15 (~2 months) |
| Win rate | 42.1% |
| Profit factor | 0.89 |
| Net PnL | -$10.42 |
| Avg win / loss | +$5.03 / -$4.13 (ratio 1.22) |
| Biggest loss | -$11.41 |
| Biggest win | +$20.47 |

**Conditional cuts where loss concentrates:**

| Cut | N | WR | PnL |
|---|---|---|---|
| BTC | 10 | 20% | **-$23.58** |
| ETH | 14 | 42.9% | +$8.28 |
| Long (Buy) | 21 | 38.1% | **-$13.47** |
| Short (Sell) | 17 | 47.1% | +$3.05 |
| 07-08 UTC | 8 | 12.5% | **-$31.71** |
| 11-12 UTC | 3 | 0% | **-$19.90** |
| 03-04 UTC | 6 | 67% | +$43.65 |
| 18 UTC | 3 | 100% | +$18.29 |

User is in Canada → 07-08 UTC = 2-4 AM local (FOMO / half-asleep bucket), 11-12 UTC = 6-8 AM (morning rush impulse), 03-04 UTC = 10pm-12am (Asia session, clean head), 18 UTC = 1pm (focused daytime).

**Mapping to user's 7 self-flaws:** Each flaw maps to a measurable data leak. Flaw 6 (anxiety without protocol) → 07-08 UTC bucket. Flaw 3 (no predefined trigger) → entire BTC long bleed. Flaw 4 (entry premise ≠ target premise) → 15m trigger targeting 4H POC 8% away.

**Verdict on Q3:** No statistical edge yet (PF 0.89 over N=38 is below random with friction). BUT three subtractive constraints are signal-positive from user's own data: (a) drop BTC, (b) drop longs, (c) drop 06-12 UTC. These are not strategies — they are exclusion rules.

**User answer:** Committed to falsification: "si si me comprometo a eso" — accept 5-rule constraint, 30-trade test, stop manual trading if WR<50% or PF<1.2 by trade 30.

**Grade:** ✅ survives — concrete falsification, dated, stop-loss defined.

---

### Q4 (implicit, user-raised): Journal annotation flow is unclear — adapt to rules.

**Current journal (annotate/[id]/page.tsx) provides:**
- thesis_pre (free text)
- lesson_post (free text)
- emotional_state (enum: calm/confident/FOMO/revenge/tired/uncertain)
- screenshot_url
- auto_grade (A/B/C/D, computed)
- context_snapshot (HTF bias, warnings)

**Gaps for the 5-rule discipline test:**
1. No pre-trade gate — annotation is post-hoc. User can place trade without writing thesis first.
2. No rule-violation flags — cannot answer "did I follow rules on this trade?" deterministically.
3. No invalidation field separate from SL — flaw 5 unaddressed.
4. No trigger field — flaw 3 unaddressed (predefined trigger condition).
5. No running falsification metric — user has no live read on "where am I in the 30-trade test."

**Verdict on Q4:** Journal needs additive fields + pre-trade gate, not rebuild. Rule 6 enforcement (auto-cancel unjournaled limits) is the right enforcement primitive — extend it.

**Grade:** ✅ BUILD candidate. Falsification gates this — without commitment to 30-trade test, no journal change justified.

---

## Final verdict

**PIVOT** from the three surface ideas (PineScript port, journal duplicate, Telegram rebuild) → **BUILD** a constrained manual-trading rule system anchored to the existing Bybit journal.

What survived:
- The 5-rule manual constraint, derived from user's own Bybit PnL data
- 30-trade falsification window with WR≥50% / PF≥1.2 criterion
- Journal adaptation: pre-trade gate + rule-flag fields + falsification metric dashboard

What didn't:
- ORB PineScript port (premise dead)
- Trade journal stats script (already shipped, deeper)
- Telegram alert rebuild (downstream, premature, no concrete pain quoted)

What would change the verdict:
- If user backs out of the 30-trade falsification commitment → KILL all manual trading until bot graduates from shadow.
- If next 30 trades hit WR≥55% / PF≥1.5 → expand to less-constrained rule set.
- If next 30 trades hit WR<40% / PF<0.8 → permanent halt, manual capital reallocated.

## BUILD scope (pre-conditions for /phased-plan)

### 5 enforced rules (the falsification test)
1. **Pair whitelist:** ETH, SOL, DOT only. BTC blocked at journal layer.
2. **Side whitelist:** Sell (short) only. Buy blocked.
3. **Time window:** 13:00–22:00 UTC only. Orders placed outside window auto-cancelled by watcher (extends PR #29 rule 6 logic).
4. **Pre-trade trigger required:** journal field `trigger_condition` (text, required, written BEFORE order placement). Watcher cancels order if trigger empty.
5. **Pre-trade invalidation required:** journal field `thesis_invalidation` (text, required, BEFORE order placement). Distinct from SL price — must describe market behaviour that breaks the thesis.

### Journal schema extensions (additive)
- `trigger_condition TEXT NOT NULL` (pre-trade)
- `thesis_invalidation TEXT NOT NULL` (pre-trade)
- `rule_violations JSONB` (auto-populated by enforcement layer: list of broken rules per trade)
- `falsification_trade_number INT` (1..30, NULL if outside test window)
- Existing `thesis_pre` repurposed strictly to pre-trade (UI prevents post-hoc edit)
- Existing `lesson_post` stays post-hoc

### Pre-trade gate flow
- User opens limit order on Bybit → watcher detects (existing `bybit_watcher.py`)
- Watcher checks: pair in whitelist? side = Sell? UTC hour in 13–22? journal annotation exists with trigger + invalidation populated?
- Any check fails → auto-cancel order + Telegram alert with reason ("BTC blocked", "Long blocked", "outside time window 06-12 UTC", "no trigger written", "no invalidation written")
- All checks pass → order survives. Falsification counter increments.

### Falsification dashboard widget (component for `/bybit` page)
- Live WR and PF computed over `falsification_trade_number IS NOT NULL` rows
- Trade counter "Trade N of 30"
- Threshold lines: WR 50%, PF 1.2
- Status: GREEN if both above, AMBER if one below, RED if both below
- At N=30: auto-emit verdict via Telegram + freeze further manual trades (block all orders at watcher layer until user explicitly resets experiment)

### Telegram pre-trade checklist (replaces vague "alerts no sirve" complaint with concrete need)
- When user opens limit order, watcher posts to Telegram:
  - "Order detected: ETH Sell 79200. Trigger: [text]. Invalidation: [text]. Rule check: ✅/❌ per rule. Status: ACTIVE / AUTO-CANCELLED."
- One message, all info, mobile-readable. No spam.

### Out of scope for this plan
- New trading signals (ORB, etc.) — premise dead
- AI Trade Autopsy automation — `/trade-review` already exists
- Rebuilding `shared/alert_manager.py` — no concrete pain quoted
- Bot live-trading changes — bot stays SHADOW-ONLY per SYSTEM_BASELINE
- Sample-size statistical significance claims — N=30 is a behavioural-discipline test, not edge proof

### Risks to flag in /phased-plan
- Schema migration on `bybit_annotations` table (NOT NULL on new fields needs default or backfill strategy)
- Watcher race condition: order placed faster than annotation can be written. Need order→annotation linking with grace window vs hard pre-block.
- User burnout from over-friction → must keep journal UI mobile-first (375px) and ≤3 fields required pre-trade. Anything more = abandoned.
- BTC block is harsh — user explicitly trades BTC most. Behavioural rule will feel punitive. That's the point. Tag rule as `falsification_window_only`, revisit at N=30 verdict.

## Handoff

Next step: `/phased-plan manual-edge-discipline-2026-05-15` using this grill doc as input.

Do not implement yet. /phased-plan produces the staged delivery plan (DB migration → watcher gate → journal UI → falsification widget → Telegram message). User reviews plan before any code lands.
