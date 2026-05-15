# Plan: Manual Edge Discipline — instrument v3 rules for N=30 forward test

**Slug:** manual-edge-discipline-2026-05-15
**Source grill:** `docs/grill/manual-edge-discipline-2026-05-15.md` (amended with v3 alignment note)
**Source taxonomy:** `docs/grill/bybit-rules-taxonomy.md` v3 (rules 1-14 — authoritative)
**Source plan in motion:** `docs/plans/bybit-journal-enforcement.md` (Phase 2 shipped PR #29 #30, Phase 3 fill-rate measurement in flight)
**Created:** 2026-05-15
**Status:** pending
**Tracer bullet:** Rule 13's N=30 forward test cannot meaningfully start until **trigger** and **thesis_invalidation** are captured as structured fields (today both are buried inside free-text `thesis_pre`). Phase 1 adds the two fields + watcher enforcement and tests whether user fills them on the next 5 attempted trades.

## Context summary

Grill verdict was BUILD, but pre-flight on existing plans showed the user's discipline question is already 60% answered: v3 taxonomy defines rules 1-14, Rule 13 forbids new binding rules during the N=30 forward test, and `bybit-journal-enforcement.md` Phase 2 already auto-cancels limit orders that lack `thesis_pre` within 5 min. What's missing: structured capture of (a) the **trigger** that fires the entry and (b) the **thesis-invalidation condition** distinct from SL price. Without those as queryable fields, the post-N=30 grill (Action C) cannot statistically distinguish rule-compliant trades from prose-only ones. This plan closes that instrumentation gap without inventing new rules.

What this plan does NOT do: invent BTC/long/hour exclusion rules from the grill's data cuts. Those exclusions would violate Rule 13 ("no new binding rules until N=30 closes"). Hour data is already captured as v3 guideline field `outside_optimal_hours`. Pair and side restrictions are deferred to Action C post-N=30.

## Out of scope (deliberately)

- **No new binding rules** — Rule 13 forbids it during the N=30 forward test. The grill's "no BTC / no long / 13-22 UTC" exclusions are explicitly NOT in this plan.
- **No replacement of `shared/alert_manager.py`** — "alerts no sirve" was vague; specific Telegram gap (pre-trade consolidated checklist) is addressed in Phase 3 but no broader rebuild.
- **No bot live changes** — strategy freeze active 2026-05-13 → 2026-06-08 per SYSTEM_BASELINE §9.
- **No ORB/PineScript signal port** — premise killed in grill Q1.
- **No new journal endpoints in `dashboard/api/manual/`** — manual trading module stays self-contained; Bybit annotation uses `dashboard/api/routes/bybit.py`.
- **No ML on journal free text** — descriptive fields only.

## Open questions — RESOLVED 2026-05-15

- [x] **Q1 — Parallel vs chain with `bybit-journal-enforcement.md` Phase 3.** Decision: **parallel.** Adding the 2 fields now strengthens Phase 3's fill-rate signal because Action C wants ≥30 trades with FULL journal (trigger + invalidation, not prose-only).
- [x] **Q2 — Distinct vs combined fields.** Decision: **distinct fields** (`trigger_condition`, `thesis_invalidation`). Reason: queryability for Action C — Phase 2 falsification widget joins on `IS NOT NULL` for both. Combined free-text field cannot be SQL-filtered cleanly.
- [x] **Q3 — Enforcement timing.** Decision: **observe in Phase 1, enforce in Phase 2 only if fill rate <80% without enforcement.** Phase 1 logs WARNING on missing fields but does not auto-cancel. If user naturally fills ≥4 of 5 next trades the fields are validated as low-friction; enforcement deferred. If <4 of 5, Phase 2 watcher extension cancels on missing fields (same code path as PR #29 thesis_pre cancel).

## Phase 1 — Tracer: add structured fields, observe fill behaviour (no enforcement)

**Status:** in-review

**Inputs:**
- Grill verdict + v3 rules taxonomy
- Existing `bybit_trade_annotations` schema and the watcher's PENDING_NEW insert path
- Current annotate UI: `dashboard/web/src/app/annotate/[id]/page.tsx`

**Outputs:**
- Schema migration: `bybit_trade_annotations` adds `trigger_condition TEXT`, `thesis_invalidation TEXT`. Both nullable initially.
- Backend: `dashboard/api/routes/bybit.py` `BybitAnnotation` + `BybitAnnotationPatch` Pydantic models gain the 2 fields.
- Frontend: `annotate/[id]/page.tsx` adds 2 required-marked text inputs above `thesis_pre`, with placeholder text examples from v3 (e.g. trigger: "rebote en POC 4H 79.2k con vela cuerpo entero + RSI<30 5m"; invalidation: "cierre 15m > 80.1k = thesis short rota").
- Logging: watcher logs `journal_fields_missing` WARNING for closed trades where either field is NULL.
- Documentation: append rationale to `docs/grill/bybit-rules-taxonomy.md` under Rule 6 — "trigger + invalidation are structured sub-fields of pre-trade journaling, not new rules."

**Work:**
- DB migration (idempotent ALTER TABLE) — add to `data_service/bybit_sync.py` `ensure_tables()` so it runs on watcher startup (same path PR #30 fixed).
- Pydantic schema update in `dashboard/api/routes/bybit.py`.
- React form update in `annotate/[id]/page.tsx` — 2 new `<textarea>` blocks, required attr at form level only (not browser-enforced — soft observe phase).
- Loguru WARNING in `bybit_watcher.py` close handler.

**Files changed (estimate):**
- `data_service/bybit_sync.py` (~15 LOC)
- `data_service/bybit_watcher.py` (~10 LOC)
- `dashboard/api/routes/bybit.py` (~10 LOC)
- `dashboard/web/src/app/annotate/[id]/page.tsx` (~80 LOC)
- `dashboard/web/src/lib/api.ts` (type updates, ~10 LOC)
- `docs/grill/bybit-rules-taxonomy.md` (~15 LOC clarification)
- New test: `tests/test_bybit_annotation_fields.py` (~60 LOC)

Total estimate: ~200 LOC, within ≤500 limit.

**Verification gate:**
- [ ] Automated: `python -m pytest tests/test_bybit_annotation_fields.py -v` — 3 tests pass (migration idempotent, PATCH accepts new fields, GET returns them).
- [ ] Automated: `cd dashboard/web && npm run build` — 0 type errors.
- [ ] Manual: user views `/annotate/<id>` at 375px wide — both fields visible, no overflow, mobile-keyboard-friendly.
- [ ] Manual: user opens next 5 limit orders on Bybit. Fills `trigger_condition` AND `thesis_invalidation` on **≥4 of 5** before order fills or 5-min auto-cancel. ← This is the tracer signal.
- [ ] Rollback if: user fills <3 of 5 → fields are wrong shape, NOT a discipline problem. Re-design (e.g. single combined field, or pre-filled templates per setup type, or drop the fields entirely and accept thesis_pre prose).
- [ ] Rollback if: schema migration breaks watcher startup on the production server (PR #30 was the last fix here — extra care).

**Evidence (filled by /phased-implementation):**
- 2026-05-15 — Phase 1 code complete. Status moved to `in-review` pending manual verification.
- **Automated checks:**
  - `python -m pytest tests/test_bybit_annotation_fields.py -v --tb=short` → **5/5 PASSED** (0.49s). Covers: AnnotationUpdate accepts both new fields; unset-field exclusion; `_row_to_out` roundtrips populated values; `_row_to_out` handles NULL; bybit_sync DDL source contains both ALTER lines.
  - `python -m pytest tests/test_bybit_watcher_enforcement.py -v --tb=short` → **6/6 PASSED** (regression). PR #29 enforcement path unaffected.
  - `cd dashboard/web && npm run build` → **compiled successfully in 5.9s, 0 errors, 0 warnings.** `/annotate/[id]` route bundle 7.2 kB (was ~7 kB before).
- **Manual checks pending (user to verify before "advance"):**
  - [ ] View `/annotate/<id>` at 375px wide on iPhone SE — both new TRIGGER and INVALIDATION fields visible above THESIS, no overflow, mobile keyboard works.
  - [ ] Save annotation with values in both new fields → GET returns them, persisted in DB.
  - [ ] Open next limit order on Bybit. Fill `trigger_condition` AND `thesis_invalidation` in the form before order fills or 5-min auto-cancel.
  - [ ] Repeat across **5 attempted trades** — tracer gate is ≥4 of 5 with both fields filled.
  - [ ] On close of any trade with NULL trigger or invalidation, watcher logs WARNING `journal_fields_missing` (tail logs to confirm).
- **Rollback trigger fired:** no
- **Files changed:** 7
  - `data_service/bybit_sync.py` (+2 LOC — migration ALTER lines)
  - `data_service/bybit_watcher.py` (+13 LOC — WARNING log in `_close_annotation`, SELECT updated to include new fields)
  - `dashboard/api/routes/bybit.py` (+6 LOC — Pydantic + row mapper)
  - `dashboard/web/src/lib/api.ts` (+4 LOC — TS types)
  - `dashboard/web/src/app/annotate/[id]/page.tsx` (+25 LOC — 2 new useState, 2 new fields in form, payload updated)
  - `docs/grill/bybit-rules-taxonomy.md` (+5 LOC — Rule 6 clarification note)
  - `tests/test_bybit_annotation_fields.py` (new, +118 LOC)
- **LOC delta:** +173 / -0. Under plan estimate (~200) and well under ≤500 limit.
- **Schema migration runs on next watcher restart** via existing `BybitSync.ensure_tables()` (idempotent ALTER COLUMN IF NOT EXISTS — same pattern as PR #30).

---

## Phase 2 — Falsification dashboard widget on `/bybit` page

**Status:** pending

**Inputs:** Phase 1 outputs verbatim. Specifically: 2 structured fields populated on ≥80% of recent annotations (or whatever Phase 1 observation reveals as the realistic floor).

**Outputs:**
- New widget component: `dashboard/web/src/components/manual/FalsificationTracker.tsx`
- New API endpoint: `GET /bybit/falsification` in `dashboard/api/routes/bybit.py` returning `{ trade_count, wr_pct, pf, status, threshold_wr, threshold_pf, next_decision_at_trade }`.
- Widget mounted on `dashboard/web/src/app/bybit/page.tsx`.

**Behaviour spec:**
- Eligibility: trade counts as "rule-compliant" (included in falsification N) iff `bybit_trade_annotations.thesis_pre IS NOT NULL AND trigger_condition IS NOT NULL AND thesis_invalidation IS NOT NULL`. Join `bybit_trade_annotations` to `bybit_closed_pnl` on `order_id`.
- WR = wins / total. PF = sum(wins) / abs(sum(losses)). Same math as the grill query.
- Thresholds from v3 Rule 13: target `WR ≥ 50%`, `PF ≥ 1.2`. Status colour: GREEN both above, AMBER one below, RED both below.
- Counter: `Trade N of 30`. At N=30 the widget surfaces "Decision due — run `/grill-me strategy-edge-on-btc-eth` (Action C)."
- Polling: 60s (same as sentiment per `dashboard/CLAUDE.md`).
- Mobile: stacks under existing manual stats card at ≤639px.

**Files changed (estimate):**
- `dashboard/api/routes/bybit.py` (~50 LOC)
- `dashboard/api/queries.py` (~20 LOC)
- `dashboard/web/src/components/manual/FalsificationTracker.tsx` (new ~180 LOC)
- `dashboard/web/src/app/bybit/page.tsx` (~10 LOC)
- `dashboard/web/src/lib/api.ts` (~15 LOC)
- New test: `tests/test_falsification_query.py` (~80 LOC)

Total estimate: ~355 LOC, within ≤500 limit.

**Verification gate:**
- [ ] Automated: `python -m pytest tests/test_falsification_query.py -v` — eligibility join correct, WR / PF arithmetic correct on fixture data, edge case N=0 returns null status not divide-by-zero.
- [ ] Automated: `cd dashboard/web && npm run build` — 0 errors.
- [ ] Manual: user views `/bybit` at 375px — widget renders, numbers match a hand-run SQL `SELECT COUNT(*), WR, PF FROM rule_compliant_view`.
- [ ] Manual: widget colour transitions correctly across 3 sample states (force via SQL inserts on a scratch row, then DELETE).
- [ ] Quantitative thresholds at this gate: query latency < 300ms on production data volume (Bybit table is small, easy bar).
- [ ] Rollback if: widget query slows the `/bybit` page > 1s — move to background job + cache.

**Evidence (filled by /phased-implementation):**
<empty>

---

## Phase 3 — Consolidated Telegram pre-trade checklist

**Status:** pending

**Inputs:** Phase 1 fields populated regularly + Phase 2 widget live.

**Outputs:**
- New emit path in `bybit_watcher.py` PENDING_NEW handler: replaces current ad-hoc message with one structured message.
- Telegram message format (mobile-readable, single screen):
```
🟡 ORDER PENDING (auto-cancel in 5 min if not journaled)
ETH-USDT • Sell • 79,200 • qty 0.05
4H EMA: bearish ✅ (Rule 5)
Limit order ✅ (Rule 2)
Annotate: <link>
Required: trigger + invalidation + thesis
```
- After user submits annotation: confirmation message `✅ JOURNALED — order armed. Falsification trade N of 30.`
- After auto-cancel: `❌ AUTO-CANCEL — missing [trigger|invalidation|thesis]. Trade not counted.`

**Files changed (estimate):**
- `data_service/bybit_watcher.py` (~80 LOC — 3 new message paths replace the existing pending alert)
- `shared/notifier.py` (~20 LOC — markdown template helper if not already there)
- Test: extend `tests/test_bybit_watcher_enforcement.py` (~50 LOC)

Total estimate: ~150 LOC.

**Verification gate:**
- [ ] Automated: extended watcher tests pass — pending message format includes all v3 rule indicators, journaled confirmation fires once, auto-cancel message names the missing fields.
- [ ] Manual: user places 3 limit orders. Each time, single Telegram message arrives with all info needed to decide on phone. User does not need to scroll, switch app, or open dashboard before deciding.
- [ ] Manual: of those 3 orders, 1 is left unjournaled deliberately — auto-cancel message arrives at 5 min with correct missing-field name.
- [ ] Rollback if: message becomes too long (>1 screen on iPhone SE) → split or shorten.

**Evidence (filled by /phased-implementation):**
<empty>

---

## Phase 4 (conditional, not pre-planned) — Hand-off to Action C

After Phases 1-3 are done AND N=30 rule-compliant trades have accumulated in the falsification widget:

- Run `/grill-me strategy-edge-on-btc-eth` (queued in `docs/plans/bybit-journal-enforcement.md`).
- That grill answers: with full journal + structured trigger + structured invalidation on N=30, does the user's discretionary strategy show statistically distinguishable edge from random?
- This plan does NOT pre-shape that grill. Per Rule 13, the question must be re-formed by what the data actually shows.

## Changelog hook

On Phase 1 completion, append to `docs/SYSTEM_BASELINE.md` §9:
> `2026-05-XX — Manual edge discipline Phase 1 shipped (PR #N). bybit_trade_annotations now captures trigger_condition + thesis_invalidation as structured fields. Tracer outcome: <fill rate over next 5 trades>.`

On Phase 3 completion, append:
> `2026-05-XX — Manual edge discipline complete. Falsification widget live. N=<count> rule-compliant trades. Next: Action C grill when N=30.`

## Constraints active during this plan

Same freeze rules as `bybit-leak-measurement.md` and `bybit-journal-enforcement.md`:
- **Forbidden:** any commit touching `strategy_service/`, `quick_setups.py`, `scalp_setups.py`, `engines/`, ML feature version bumps.
- **Allowed:** `data_service/bybit_watcher.py`, `data_service/bybit_sync.py`, `dashboard/api/routes/bybit.py`, `dashboard/api/queries.py`, `dashboard/web/src/app/annotate/`, `dashboard/web/src/app/bybit/`, `dashboard/web/src/components/manual/`, `shared/notifier.py`, schema migrations on Bybit tables.

## Quality bar self-check

- ✅ Phase 1 fails fast (≤1 day work, soft observation only — if fields are wrong shape user reveals it in next 5 trades).
- ✅ Each gate has a number (≥4/5 fill, ≤300ms query, ≥3 orders user-confirmed).
- ✅ Open questions resolved before Phase 1 starts (3 questions above with recommended defaults).
- ✅ Out-of-scope section non-empty (5 items defended).
- ✅ No conflict with strategy freeze (Bybit-side work only).
- ✅ No conflict with Rule 13 (no new binding rules added; structured capture is instrumentation of existing Rule 6).
