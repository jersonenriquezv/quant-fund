# Plan: Bybit leak measurement (Phase 0)
**Slug:** bybit-leak-measurement
**Source grill:** docs/grill/bot-viability-2026-05-13.md (Q3 + Q4)
**Created:** 2026-05-13
**Status:** done — pivoted after Phase 1 (see revision note below)
**Tracer bullet:** Bybit historical executions data exposes enough fields to retroactively detect violations of user's stated trading rules — without this, the entire leak-measurement premise collapses.

## Plan revision 2026-05-13 (post Phase 1)

Phase 1 produced findings that invalidated the original Phase 2-4 measurement plan:

1. **2 leaks already quantified without Phase 2:** rule 11 day-of-week violation 41% (cost ~$14 in sample), rule 14 journal fill rate 5% (catastrophic).
2. **Rules 5/6 (don't move SL) UNDETECTABLE retroactively** — `bybit_pending_orders` only stores current state, not modification history. Requires forward-only watcher.
3. **Current PnL on rule-following BTC/ETH trades = -$13.47** in sample. Pre-empts Phase 3 KILL signal.
4. **Bot-of-execution scope was wrong:** the most broken rule (14 — journal) doesn't need an execution bot. It needs an annotation enforcement gate.

**Phases 2-4: ABANDONED.** Replaced with two parallel follow-ups:
- **Plan D** (engineering): `docs/plans/_archive/bybit-journal-enforcement.md` — make annotations mandatory before trade open
- **Action C** (research): queued `/grill-me strategy-edge-on-btc-eth` after 30+ new trades with journal filled. NOT a phased plan; it's a single grill session.

Original measurement framework abandoned because the data needed (deposits, full pre-bot history, SL mod log) is either missing or out of scope, and the actionable findings already in hand are larger than what Phase 2-4 would have produced.

## Context summary

User trades manually on Bybit ($4.6k capital) — primarily ETH-USDT linear perp + XRP, occasionally inverse USD perp + spot. Bot sync (`scripts/sync_bybit.py` → `bybit_executions` + `bybit_closed_pnl` tables) started a few months ago. Pre-bot trades (older history, undisciplined era per user self-report) NOT yet in Postgres. User accepts SL discipline now but is not profitable — possible signal that strategy itself has no edge, not just discipline.

**Phase 0 measures one thing:** does removing measurable behavioral leaks (revenge, no-SL, oversize, etc.) recover enough bps to justify building an execution bot? Or is the strategy itself dead, in which case bot is irrelevant?

**Phase 0 is NOT:** strategy creation from trade history, alpha discovery, or anything that produces a new setup. Output is a number in bps + a binary build/kill decision.

## Out of scope (deliberately)

- **Strategy creation from analyzing user history** — survivorship bias + curve fit. If found tempting during analysis, stop.
- **Market data correlations** — leak is in user behavior, not market regime.
- **Pulling data from any venue except Bybit** — OKX bot is frozen separately, not relevant here.
- **Building execution bot in this plan** — that's a separate plan that only starts if Phase 4 says yes.
- **ML feature engineering** — no model training, no features, no LightGBM. Pure descriptive statistics.

## Open questions (resolve before Phase 1)

- [ ] User to write `docs/grill/bybit-rules-taxonomy.md` with 5-10 rules he intended to follow (parallel track, not blocking Phase 1)
- [ ] Confirm Bybit account has NOT been blow-up-refunded (only normal capital flow per user). If wrong, reinterpret leak magnitude.
- [ ] Confirm `data_service/bybit_sync.py` paginates correctly past 365 days — if not, fix in Phase 2

## Phase 1 — Tracer: Verify Bybit historical data sufficiency
**Status:** done
**Inputs:**
- `docs/grill/bybit-rules-taxonomy.md` (from user, or stub if not ready)
- 30-trade sample from existing `bybit_executions` + `bybit_closed_pnl`

**Outputs:**
- `docs/grill/_archive/bybit-rules-detectable.md` — for each rule in taxonomy, mark `detectable | partial | undetectable` with evidence (which fields are/aren't available)
- Updated taxonomy keeping only detectable rules as binding for measurement

**Work:**
- Pull 30 closed trades sample via SQL: `SELECT * FROM bybit_closed_pnl ORDER BY created_at DESC LIMIT 30; SELECT * FROM bybit_executions ORDER BY exec_time DESC LIMIT 100;`
- For each rule type in taxonomy, check available fields:
  - "no SL set at entry" → does Bybit historical expose conditional/SL order placement linked to position? Check `bybit_executions` schema + `bybit_sync.py` for stop-loss capture
  - "revenge trade <30min after loss" → need entry timestamp + previous trade exit timestamp + previous PnL — all should be in tables, verify
  - "oversize >X% capital" → need position size + capital at moment of entry; capital at moment requires deposit history (Phase 2)
  - "leverage >Xx" → exists in execution record
  - "re-entry same pair <Xh after SL" → entry timestamps + symbol
- Document which rules survive

**Verification gate:**
- [ ] Automated: SQL pulls succeed, sample size ≥30
- [ ] Manual: user reviews `bybit-rules-detectable.md`, confirms ≥70% of his stated rules are detectable
- [ ] Rollback if: <50% rules detectable AND user rejects reduced taxonomy → stop, redesign measurement approach (e.g. forward-only measurement with new instrumentation)

**Evidence (filled by /phased-implementation):**
- 2026-05-13 — Phase 1 executed
- **Sample pulled:** 37 closed_pnl + 91 executions + 37 pending_orders + 37 annotations from existing DB (sync coverage: 46 days, linear-only)
- **User taxonomy file empty** — used 10-rule stub synthesized from prior conversation. User must populate `docs/grill/bybit-rules-taxonomy.md` before Phase 4.
- **Per-rule detectability written to:** `docs/grill/_archive/bybit-rules-detectable.md`
- **Score:** 6/10 strictly detectable (60%), 1/10 partial low-value (leverage), 3/10 undetectable
  - Rule 6 (sizing) becomes detectable after Phase 2 capital flow sync
  - Rules 8 (timeout) + 10 (market context) recommended for drop
- **Reduced taxonomy (8 rules, drop 8+10):** 6/8 = 75% now, 7/8 = 87.5% after Phase 2 — gate passes conditionally
- **Automated checks:**
  - SQL pulls succeeded: ✅ N=37 (≥30 minimum)
  - Per-rule field availability documented: ✅
- **Manual checks pending:** user must (a) accept dropping rules 8+10, (b) populate real taxonomy file, (c) confirm leverage-constant observation
- **Rollback trigger fired:** no
- **Files changed:** `docs/grill/_archive/bybit-rules-detectable.md` (new), `docs/plans/_archive/bybit-leak-measurement.md` (this evidence block)
- **LOC delta:** +90 / -2

**Findings beyond Phase 1 scope (flagged):**
- Recent 10-trade PnL = -$24.62, WR ~10% — pre-empts Phase 3 KILL signal
- All sample trades had SL set → "no SL" is NOT current leak
- All leverage = 10x constant across all trades
- DB only covers 46 days, 37 trades — Phase 2 must extend sync window AND categories AND add capital flow table

---

## Phase 2 — Full historical pull + capital flow normalization
**Status:** abandoned — superseded by `bybit-journal-enforcement.md`. Capital flow sync and pre-bot pull no longer required for current decision path. May revive later if a leak-bps measurement becomes blocking again.
**Inputs:**
- Detectable-rules list from Phase 1
- Confirmation that `sync_bybit.py` pagination handles long windows

**Outputs:**
- `bybit_closed_pnl` populated with N≥200 pre-bot trades + all post-bot trades to date
- `bybit_executions` populated correspondingly
- New table `bybit_capital_flow` with deposits + withdrawals (or equivalent column in existing table)
- `pre_bot_cutoff_date` constant defined (date the live sync started capturing)

**Work:**
- Verify or extend `data_service/bybit_sync.py` to handle full account history (test with `--days 730`)
- Add deposits/withdrawals pull from Bybit API (`/v5/asset/deposit/query-record`, `/v5/asset/withdraw/query-record`)
- Schema migration: `bybit_capital_flow (timestamp, type, asset, amount, tx_id)`
- Run full sync: `python scripts/sync_bybit.py --days 730 --categories linear inverse spot`
- Determine `pre_bot_cutoff_date` from `MIN(created_at)` in production trades vs much earlier API records — written as constant in `config/settings.py` or this plan doc
- Validate: total `bybit_closed_pnl` net of fees should reconcile against (final balance - initial balance - net deposits + net withdrawals) within tolerance
- Tag every trade row in code (not in DB) as `pre_bot` or `post_bot` based on cutoff date

**Verification gate:**
- [ ] Automated: `SELECT COUNT(*) FROM bybit_closed_pnl WHERE created_at < '<cutoff>'` ≥ 200
- [ ] Automated: `SELECT COUNT(*) FROM bybit_closed_pnl WHERE created_at >= '<cutoff>'` ≥ 30
- [ ] Automated: `SUM(closed_pnl)` reconciles with capital flow within ±5%
- [ ] Manual: user inspects 10 random trades from each era, confirms data looks sensible
- [ ] Rollback if: pre-bot N <100 (insufficient power) → user must accept reduced statistical confidence or extend rules taxonomy to detectable-from-current-data only

**Evidence (filled by /phased-implementation):**
<empty>

---

## Phase 3 — Post-bot PnL alone (CHECKPOINT KILL)
**Status:** abandoned — already pre-empted by Phase 1 sample (BTC/ETH discipline-era PnL = -$13.47 in 30 trades). Strategy edge question moves to `/grill-me strategy-edge-on-btc-eth` after journal data accumulates.
**Inputs:**
- Cleaned `bybit_closed_pnl` with pre/post tagging from Phase 2

**Outputs:**
- `docs/grill/bybit-postbot-pnl-2026-05-XX.md` — single-page report

**Work:**
- Filter to `post_bot` trades only
- Compute: total PnL, trade count, win rate, profit factor, max drawdown, avg R per trade, Sharpe (if N≥30 sufficient for daily aggregation)
- Compute by symbol (ETH vs XRP vs other) if N per symbol ≥ 20
- No comparison to pre-bot in this phase. Just user's CURRENT performance with stated discipline.

**Verification gate (BINARY KILL CHECK):**
- [ ] Automated: post-bot total PnL > 0 net of fees → continue Phase 4
- [ ] Automated: post-bot total PnL ≤ 0 net of fees → **KILL VERDICT**: discipline isn't the problem. Strategy itself has no edge. Phase 4 not run. Plan terminates with recommendation: stop building bots, trade manual 60 trades with checklist, re-measure.
- [ ] Manual: user reads single-page report and confirms verdict
- [ ] Rollback if: N post-bot < 30 → wait for more trades; do not run Phase 4 with insufficient sample

**Evidence (filled by /phased-implementation):**
<empty>

---

## Phase 4 — Pre-bot vs post-bot leak comparison
**Status:** abandoned — confounders identified (strategy + skill + capital + market regime drifted with discipline) make pre-vs-post comparison unreliable. Leak-bps no longer the binding question; rule 11 + rule 14 violations are.
**Inputs:**
- Phase 3 result = continue (PnL > 0)
- Detectable rules list from Phase 1

**Outputs:**
- `docs/grill/bybit-leak-2026-05-XX.md` — final leak report
- Decision: build execution bot / checklist sufficient / strategy questionable

**Work:**
- For each detectable rule in taxonomy:
  - Tag every trade in both eras: `violates_rule_X = bool`
  - Compute: avg PnL of violating trades vs clean trades
  - Compute: count of violations per era
  - Welch's t-test on PnL distributions (NOT Student's — variances differ)
  - Cohen's d effect size (report this, not p-value alone)
- Aggregate: estimated $ lost to violations / month at current activity
- Convert to bps: leak_bps = (avg_violator_pnl - avg_clean_pnl) / avg_position_notional × 10000

**Verification gate:**
- [ ] Automated: each rule has N≥30 violators AND N≥30 clean trades; if not, flag rule as underpowered
- [ ] Automated: report generated with bps figure + Cohen's d per rule
- [ ] Manual: user reads report and applies Q3 decision table:
  - Aggregate leak >50bps/trade → BUILD execution bot (next plan: `/grill-me bybit-execution-bot-thesis`)
  - Aggregate leak 10-50bps/trade → CHECKLIST sufficient. Write `docs/checklists/bybit-pre-trade.md`. No bot.
  - Aggregate leak <10bps/trade → DISCIPLINE NOT THE PROBLEM. Strategy may have no edge. Decision: try a different pivot (funding carry, stat arb) or stop trading entirely.
- [ ] Rollback if: confounders dominate (regime / strategy / skill drift make pre vs post incomparable) → report inconclusive, recommend forward-only measurement with new instrumentation

**Evidence (filled by /phased-implementation):**
<empty>

---

## Changelog hook

On Phase 4 completion (or Phase 3 KILL), append to `docs/SYSTEM_BASELINE.md` §9:

> `2026-05-XX — Bybit leak measurement (Phase 0) complete. Result: <bps figure> | <kill verdict>. Decision: <build / checklist / pivot / stop>. Plan: docs/plans/_archive/bybit-leak-measurement.md`

## Constraints active during this plan

Per grill verdict (Q4):
- **Forbidden:** any commit touching `strategy_service/`, `quick_setups.py`, `scalp_setups.py`, `engines/`, ML feature version bumps, new setups, scalp variant tuning between today and 6/8
- **Allowed:** changes to `data_service/bybit_sync.py`, `scripts/sync_bybit.py`, new `data_service/bybit_capital_flow.py`, schema migrations for Bybit tables, new analysis scripts
- This plan lives in parallel with the existing OKX bot freeze. Neither blocks the other.
