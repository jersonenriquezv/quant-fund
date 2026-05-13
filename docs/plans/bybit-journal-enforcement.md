# Plan: Bybit journal enforcement
**Slug:** bybit-journal-enforcement
**Source grill:** docs/grill/bot-viability-2026-05-13.md (Q4) + Phase 1 findings of `bybit-leak-measurement.md`
**Source taxonomy:** docs/grill/bybit-rules-taxonomy.md (Rule 14)
**Created:** 2026-05-13
**Status:** pending
**Tracer bullet:** identify the actual point of failure in the existing annotation workflow — is it cost-of-filling (too many fields, slow mobile form), discoverability (Telegram link ignored), or motivation (no consequence for skipping)? The fix depends on the answer.

## Context summary

`bybit_trade_annotations` table fill rate measured in Phase 1 of the leak-measurement plan: **5% thesis_pre, 0% lesson_post, 0% grade_self** across 37 trades since 2026-03-18. Infra exists end-to-end: `bybit_watcher.py` polls Bybit, inserts empty annotation rows on position open, sends Telegram link to mobile form at `dashboard/web/src/app/annotate/`. User does not fill it.

This plan does NOT add new alpha. It enforces an existing rule (rule 14: journal each trade) that user agreed is mandatory but ignores in practice. Without journal data, every other measurement and every future grill is data-starved.

## Out of scope (deliberately)

- **Building a strategy or signal** — this is enforcement of a workflow, not alpha
- **Modifying Bybit trading behavior** — Bybit UI is outside our control; we cannot block trades at the venue
- **Touching `strategy_service/`** — frozen until 6/8 per `bot-viability` grill
- **Pre-trade analysis/automation** — the goal is journaling, not signal generation
- **ML on annotation content** — descriptive only; no model trained on free text

## Open questions (resolve before Phase 1)

- [ ] Is `bybit_watcher.py` running on the production server right now? If not, the Telegram link path is dead and no enforcement matters until it runs.
- [ ] Last time user clicked the annotation Telegram link — does he remember? If never, that's the discoverability failure.
- [ ] User commits to a forcing function (e.g., "no new trade until last lesson_post is filled") or just wants softer nudges?

## Phase 1 — Tracer: audit current annotation workflow end-to-end
**Status:** done
**Inputs:**
- `bybit_watcher.py` source + last-run logs
- `dashboard/web/src/app/annotate/` UI
- `dashboard/api/manual/` endpoints
- `bybit_trade_annotations` schema + recent rows

**Outputs:**
- `docs/grill/journal-workflow-audit-2026-05-13.md` with per-stage friction analysis:
  - Stage 1: position open detected by watcher? (yes/no, latency)
  - Stage 2: empty annotation row inserted? (yes/no, schema)
  - Stage 3: Telegram alert sent? (yes/no, link present)
  - Stage 4: user clicks link? (last-clicked timestamp, or "never")
  - Stage 5: form loads on mobile? (manual test, screenshot)
  - Stage 6: form submission writes to DB? (manual test)
  - Stage 7: lesson_post / grade_self prompted on close? (yes/no)
- Identification of WHICH stage is the failure point

**Work:**
- Read watcher code + check it's running (`docker compose ps`, log tail)
- SQL: `SELECT created_at, annotated_at, thesis_pre IS NOT NULL FROM bybit_trade_annotations ORDER BY created_at DESC` — annotated_at gap reveals stage 4-6 failure
- Manual test: open watcher in dry-run mode, simulate position open, verify Telegram message arrives with working link
- User confirms whether he ever clicked the link in last 30 days

**Verification gate:**
- [ ] Automated: watcher confirmed running OR confirmed stopped (binary state known)
- [ ] Automated: Telegram link in last 5 alerts is well-formed and resolves
- [ ] Manual: user confirms which stage(s) he experiences friction at, in his own words
- [ ] Rollback if: any stage of infra is broken (e.g. watcher dead, form 500s) — fix infra in this phase before designing enforcement

**Evidence (filled by /phased-implementation):**
- 2026-05-13 — Phase 1 audit complete. Full report: `docs/grill/journal-workflow-audit-2026-05-13.md`
- **Stages 1-3, 5-7 all WORKING.** Watcher healthy 13 days, annotations inserted 37/37, Telegram alerts firing 11/11 in last 30d, form loads, PATCH endpoint works, closure alerts fire.
- **Failure isolated to Stage 4 (link click) → Stage 6 (form completion).** Pure discoverability + motivation gap. No bug to fix.
- **Architectural gap surfaced:** current flow is POST-trade (annotation row created after watcher observes order). Rule 6 requires PRE-trade journaling. Need design choice in Phase 2 (Options A pre-trade flow / B post-trade auto-cancel / C hybrid).
- **Automated checks:**
  - Watcher container running: ✅ `docker compose ps` confirms healthy 13 days
  - Telegram link well-formed: ✅ `{DASHBOARD_PUBLIC_URL}/annotate/{id}`, no failed sends in 30d
  - Form loads + writes to DB: ✅ confirmed via 2 successful thesis_pre rows
- **Manual checks pending (user to answer):**
  - [ ] Do you see Telegram alerts when they fire?
  - [ ] Is Tailscale always active on phone (required for link)?
  - [ ] Choose Phase 2 option: A (new pre-trade flow) / B (auto-cancel forcing function) / C (hybrid)
  - [ ] Lesson_post enforcement: yes or just thesis_pre?
- **Rollback trigger fired:** no
- **Files changed:** 2 (`docs/grill/journal-workflow-audit-2026-05-13.md` new, this evidence block)
- **LOC delta:** 0 code, +85 docs

---

## Phase 2 — Build Option B: post-trade auto-cancel forcing function
**Status:** in-review
**Plan revision 2026-05-13:** original Phase 2 was branching/vague pending Phase 1 result. Phase 1 identified Option B as user choice. Phase 2 spec rewritten concretely below.

**Inputs:**
- Phase 1 audit findings (`docs/grill/journal-workflow-audit-2026-05-13.md`)
- User decision: Option B (auto-cancel) chosen 2026-05-13
- Existing infra: `bybit_watcher.py`, `bybit_pending_orders` table, Bybit cancel-order API

**Outputs:**
- Modified `data_service/bybit_watcher.py` — adds enforcement check in `tick()` loop
- Schema migration: add `bybit_pending_orders.enforcement_cancelled_at TIMESTAMPTZ`
- New test `tests/test_bybit_watcher_enforcement.py`
- Operations note in `docs/context/` explaining new behavior to user
- Setting `BYBIT_JOURNAL_ENFORCEMENT_DEADLINE_SEC` (default 300) in `config/settings.py` for tunability

**Work:**

**Behavior spec:**
1. Watcher detects `PENDING_NEW` event (existing). Insert annotation row + send Telegram alert (existing).
2. On every subsequent `tick()` (60s loop), for each open pending order:
   - Check `placed_at` age against `BYBIT_JOURNAL_ENFORCEMENT_DEADLINE_SEC` (default 300s = 5 min)
   - If age > deadline AND order status still `pending`/`new` AND linked annotation `thesis_pre IS NULL`:
     - Call Bybit `cancel_order(order_id)` via existing client
     - On success: stamp `enforcement_cancelled_at = NOW()` in `bybit_pending_orders`
     - Send Telegram alert: `❌ Order auto-cancelled (no thesis_pre after 5 min). Order ID: <id>`
     - Log violation in watcher logs at WARNING level
   - If order already filled before deadline check → log soft violation (`thesis_pre missing on filled order`), do NOT attempt cancel (can't unfill). Send Telegram nudge to fill thesis_pre retroactively.
   - If thesis_pre filled in time → no action (existing path)
3. Edge cases:
   - Cancel API fails (network/exchange error) → log ERROR, send Telegram alert about enforcement failure, retry on next tick
   - Order cancelled by user manually → next tick sees status `cancelled`, no-op
   - Multiple pending orders → each tracked independently (already keyed by order_id)
   - Watcher restart between PENDING_NEW and deadline → on restart, watcher reads `bybit_pending_orders` from DB, deadline still applies based on `placed_at` (no in-memory state needed)

**Configuration:**
- `BYBIT_JOURNAL_ENFORCEMENT_ENABLED` — bool, default `false` initially. User flips to `true` when ready.
- `BYBIT_JOURNAL_ENFORCEMENT_DEADLINE_SEC` — int, default 300.
- `BYBIT_JOURNAL_ENFORCEMENT_WHITELIST_ORDER_TYPES` — list, default `["Market"]` excluded from enforcement (Market orders fill near-instant, no time to journal).

NOTE: With Rule 2 (Limit-only) the whitelist excludes Market because if user uses Market it's already a Rule 2 violation — different problem, different fix.

**Files changed:**
- `data_service/bybit_watcher.py` (~80 LOC added)
- `data_service/bybit_sync.py` (schema migration ~10 LOC)
- `config/settings.py` (~5 LOC)
- `tests/test_bybit_watcher_enforcement.py` (new, ~120 LOC)
- `docs/context/01-data-service.md` (operations note, ~20 LOC)

Total estimate: ~235 LOC, within plan's ≤300 limit.

**Verification gate:**
- [ ] Automated: new pytest suite passes (4+ test cases — enforcement fires, no-op when filled, no-op when cancelled, no-op when thesis present)
- [ ] Automated: existing watcher tests still pass (no regression)
- [ ] Manual: user enables `BYBIT_JOURNAL_ENFORCEMENT_ENABLED=true` and places a test limit order on Bybit testnet OR tiny live limit, does NOT fill thesis, confirms order auto-cancels at 5 min with Telegram alert
- [ ] Manual: user places another test limit, fills thesis_pre within 5 min, confirms order is NOT cancelled
- [ ] Rollback if: enforcement causes accidental cancels of orders user wanted (false positives) → flag disabled, redesign

**Evidence (filled by /phased-implementation):**
<empty>

---

## Phase 3 — 14-day fill rate measurement
**Status:** pending
**Inputs:**
- Phase 2 deployed
- 14 days of new trades (or N≥10, whichever first)

**Outputs:**
- `docs/grill/journal-fill-rate-2026-05-XX.md` — single-page report
- Decision: enforcement worked (≥80% fill) / partial (50-80%) / failed (<50%)

**Work:**
- 14 days from Phase 2 deploy, query `bybit_trade_annotations` for trades opened after deploy
- Compute: % with thesis_pre, % with lesson_post, % with grade_self
- Compare to baseline (5% / 0% / 0%)
- If ≥80%: success → unlocks `/grill-me strategy-edge-on-btc-eth` (Action C from grill verdict — needs ≥30 trades with full journal to be meaningful)
- If 50-80%: partial → adjust enforcement; another 14-day cycle
- If <50%: failure → user must reckon with whether journaling discipline is achievable at all

**Verification gate:**
- [ ] Automated: fill rates computed
- [ ] Manual: user reads report and selects next action

**Evidence (filled by /phased-implementation):**
<empty>

---

## Action C (queued, NOT a phased plan)

After Phase 3 closes with ≥80% fill rate AND ≥30 new annotated trades:

> Run `/grill-me strategy-edge-on-btc-eth`. Question: with rule 11 followed (BTC/ETH only) and rule 14 followed (every trade journaled), does user's manual strategy on Bybit show statistically distinguishable edge from random entry? If yes → continue manual trading, scale capital deliberately. If no → strategy itself is the leak; either learn a different one (with /grill-me first) or stop discretionary trading.

This is one grill session, not a phased plan. Do not pre-plan it; the question must be re-shaped by what the journal data actually shows.

## Changelog hook

On Phase 3 completion, append to `docs/SYSTEM_BASELINE.md` §9:
> `2026-05-XX — Bybit journal enforcement complete. Fill rate <X%>. Next: <action C grill / continued enforcement / abandon>. Plan: docs/plans/bybit-journal-enforcement.md`

## Constraints active during this plan

Same freeze rules as `bybit-leak-measurement.md`:
- **Forbidden:** any commit touching `strategy_service/`, `quick_setups.py`, `scalp_setups.py`, `engines/`, ML feature version bumps
- **Allowed:** changes to `data_service/bybit_watcher.py`, `dashboard/api/manual/`, `dashboard/web/src/app/annotate/`, schema migrations on Bybit tables
