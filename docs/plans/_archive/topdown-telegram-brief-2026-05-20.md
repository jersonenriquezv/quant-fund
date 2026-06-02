# Plan: Top-Down Telegram Brief — Swing Cascade

> **PARTIAL / ARCHIVED.** The base /topdown brief shipped (PRs #43/#44). The v2 enhancement follow-ons (PRs #37–42) were **NOT merged — NO EDGE** (`backtest_results/TRACKER.md`). Archived for rationale.

**Slug:** topdown-telegram-brief-2026-05-20
**Source grill:** docs/grill/_archive/topdown-telegram-brief-2026-05-20.md
**Created:** 2026-05-20
**Status:** pending
**Tracer bullet:** Phase 1 proves the reconciled multi-TF interpretation (4H→1H→15m as proxy) matches user's independent TradingView read on ≥3 of 4 pairs.

## Context summary
Build a read-only analytical tool that ingests existing candles + analyzers, reconciles multi-TF state into one opinionated brief per pair (suggested side + confidence + unbroken liquidity threats), and delivers it via Telegram. Targets manual Bybit entries (BTC, ETH, XRP, SOL). Swing cascade first (4H→1H→30m→15m). FREEZE-safe: NO `strategy_service/` touches, NO ML feature changes. Falsification = WR comparison via `topdown_brief_used` journal field after N≥20 trades per bucket.

## Phase 1 — Tracer: console brief on existing TFs
**Status:** done
**Inputs:**
- Existing candles in PG: 4H, 1H, 15m, 5m for BTC/USDT, ETH/USDT, XRP/USDT, SOL/USDT
- Existing analyzers: `strategy_service/market_structure.py`, `fvg.py`, `order_blocks.py`, `liquidity.py`, `volume_profile.py`
- Grill verdict + Q1-Q4 conclusions

**Outputs:**
- New script `scripts/topdown_snapshot.py` (~250 LOC)
- Console-rendered brief per pair with the swing cascade as 4H→1H→15m (30m PROXY-SKIPPED, marked as TODO in output)
- Brief contents per pair:
  - Reconciled bias (`long`/`short`/`undefined` + confidence `high`/`medium`/`low`)
  - Per-TF bias + last structure break + key swing levels
  - Unbroken liquidity zones (eq highs/lows from `liquidity.py`)
  - Active OB / FVG zones near current price (1H + 4H)
  - 4H volume profile POC/VAH/VAL
  - Suggested side + invalidation level
  - Explicit `30m_TODO: 30m cascade slot pending Phase 2` line

**Work:**
- Implement `scripts/topdown_snapshot.py` with subcommand `snapshot <pair>`
- Helper `_load_candles(pair, tf, count)` — direct psycopg2 query (model existing scripts pattern)
- Helper `_analyze_tf(candles, pair, tf)` — wraps MarketStructureAnalyzer
- Helper `_reconcile(tf_states: dict)` — produces single bias + confidence:
  - All 3 TFs agree → confidence `high`
  - 2/3 agree → confidence `medium`, side = majority
  - Split 1/1/1 or any `undefined` → confidence `low`
- Helper `_render(snapshot)` — multi-line text formatter
- Run on all 4 pairs sequentially

**Verification gate:**
- [ ] Automated: `PYTHONPATH=. venv/bin/python scripts/topdown_snapshot.py snapshot BTC/USDT` exits 0
- [ ] Automated: same command for ETH, XRP, SOL exits 0
- [ ] Automated: each output contains all sections (bias, per-TF, liquidity, OB/FVG, VP, suggested side, invalidation, TODO line)
- [ ] Manual: user reviews the 4 briefs against TradingView and confirms reconciliation matches their independent read on **≥3 of 4 pairs**
- [ ] Rollback if: user disagrees on ≥2 pairs → reconciliation logic flawed → re-grill `_reconcile()` weighting

**Evidence (filled by /phased-implementation):**
- 2026-05-20 — Automated checks:
  - `PYTHONPATH=. venv/bin/python scripts/topdown_snapshot.py snapshot BTC/USDT` → exit 0
  - Same for ETH/USDT → exit 0
  - Same for XRP/USDT → exit 0
  - Same for SOL/USDT → exit 0
  - `... all` → exit 0
  - All 4 outputs include required sections (header, current price, [4H]/[1H]/[15M], [4H VP], RECONCILED with Side/Confidence/Invalidation, 30m_TODO line)
- Reconciliation snapshots (2026-05-20 19:15 UTC):
  - BTC: SHORT medium — invalidation 77,882 (4H bear BOS@78011, 1H bull BOS@77379, 15m bear CHoCH@77270)
  - ETH: SHORT medium — invalidation 2,167 (4H bear, 1H bear, 15m bull CHoCH)
  - XRP: SHORT medium — invalidation 1.369 (4H bear, 1H bear, 15m bull CHoCH)
  - SOL: LONG medium — invalidation 85.47 (4H bear, 1H bull, 15m bull) — note: LTF-leaning vote
- Manual checklist:
  - [ ] User to read 4 briefs against TradingView and confirm reconciliation matches independent read on ≥3 of 4 pairs
  - [ ] Note: SOL voted LONG by 2/3 LTF majority despite 4H bearish — user should verify if this LTF-leaning weighting feels right or if HTF-weighting needed in Phase 2
- Rollback trigger fired: no
- Files changed: `scripts/topdown_snapshot.py` (new, 386 LOC)
- LOC delta: +386 / -0
- Fixes applied during implementation:
  - First run crashed on `FairValueGap.top` (correct attr is `.high` / `.low`). Patched both call sites.

---

## Phase 2 — 30m backfill + narrative interpretation layer
**Status:** done
**Inputs:**
- Phase 1 reconciliation working on 3 TFs
- 4 pairs needing 30m: BTC/USDT, ETH/USDT, XRP/USDT, SOL/USDT
- Existing `data_service/exchange_client.py` REST patterns
- User feedback 2026-05-20: "quiero explicación simple corta pero interpretando la info" — brief must lead with plain-language interpretation, not just data tables.

**Outputs:**
- 30m candles populated in `candles` table for 4 pairs, last 200 each minimum (REST on-demand, no WS sub yet)
- `scripts/topdown_snapshot.py` extended to:
  - Include 30m slot in cascade (4H→1H→30m→15m)
  - Generate a 3-5 line narrative interpretation prepended to the output ("Plain read")
  - Move technical detail below the narrative (still printed, but reader can stop at the narrative)
- TODO line removed

**Work:**
- Add REST fetch helper for 30m using existing ccxt client in `exchange_client.py` (one-shot pull on each snapshot invocation)
- Update reconciliation to include 30m in the vote
- Add `_interpret(snap) -> list[str]` rule-based narrative generator. Lines describe:
  1. Reconciled side + confidence in plain words ("Sellers in control" / "Buyers pushing up but HTF still down")
  2. Most actionable level near price (nearest unbroken liquidity or OB)
  3. Invalidation distance + meaning ("Tight" / "Comfortable")
- Render output in new order: narrative on top, technical detail below
- Re-run for 4 pairs, confirm narrative is short (≤5 lines) and matches data

**Verification gate:**
- [ ] Automated: after `snapshot` invocation, `SELECT COUNT(*) FROM candles WHERE timeframe='30m' AND pair IN (BTC,ETH,XRP,SOL)/USDT` ≥ 100 per pair
- [ ] Automated: brief output for 4 pairs shows 30m bias section (no TODO line)
- [ ] Automated: narrative section ("Plain read:") present, ≤5 lines
- [ ] Manual: user confirms narrative interprets the info usefully + is short
- [ ] Rollback if: 30m fetch hits rate limit / OKX returns empty / narrative reads obviously wrong on ≥2 pairs

**Evidence (filled by /phased-implementation):**
- 2026-05-20 — Automated checks:
  - `PYTHONPATH=. venv/bin/python scripts/topdown_snapshot.py all` → exit 0
  - 30m candle counts in PG: BTC=300, ETH=300, XRP=300, SOL=300 (all ≥100)
  - All 4 outputs include `Plain read:` section + `[30M]` section, no `30m_TODO` line
  - Narrative section ≤5 lines for all 4 pairs
  - `pytest tests/test_data_service.py` → 69/69 pass
- Reconciliation snapshots (4-TF cascade, 2026-05-20 19:25 UTC):
  - BTC: UNDEFINED low (2 bull / 2 bear split) — watch 77,683 sell-stops below
  - ETH: UNDEFINED low — HTF/LTF split (4H bear, lower TFs disagree) — watch 2,146 buy-stops above
  - XRP: UNDEFINED low — HTF/LTF split — watch 1.376 sell-stops below
  - SOL: LONG medium — 3/4 bullish, HTF/LTF split — watch 85.665 buy-stops above, invalidation 85.47 (0.80% moderate)
- Plain read examples:
  - BTC: "No clear direction — TFs disagree. Wait or size small."
  - ETH: "No clear direction. HTF/LTF split: 4H=bearish but lower TFs disagree — likely pullback or trend exhaustion."
  - SOL: "Mixed but buyers ahead (3/4 bullish). Lean LONG with caution."
- Manual checklist:
  - [ ] User confirms narrative interprets info usefully + is short
  - [ ] User notes if HTF-weighted reconciliation desired (4-TF tie often = "undefined"; HTF-weighting would resolve more cases)
- Rollback trigger fired: no
- Files changed: `scripts/topdown_snapshot.py` (+90 / -16), `data_service/exchange_client.py` (+1 added "30m" to `_timeframe_to_ms`)
- LOC delta: +91 / -16
- Fixes during implementation:
  - `_timeframe_to_ms` missing 30m → added (FREEZE-safe: lookup dict, not setup behavior)
  - `_interpret` tuple sort fell into dataclass comparison → fixed with explicit `key=lambda x: x[0]`
- 2026-05-20 (revision) — HTF-weighted reconciliation per user request "weight htf":
  - 4H gets weight 2, others weight 1. Total weighted vote = 5.
  - Confidence: high if score ≥4/5, medium if majority, low if HTF disagrees with side.
  - Re-run results (4-TF with HTF-weight):
    - BTC: UNDEFINED low → SHORT medium (3/5, 4H bear)
    - ETH: UNDEFINED low → SHORT medium (3/5, 4H bear)
    - XRP: UNDEFINED low → SHORT medium (3/5, 4H bear)
    - SOL: LONG medium → LONG low (3/5, 4H disagrees, demoted)
  - Narrative now reflects HTF anchoring explicitly ("HTF-anchored SHORT" / "LONG lean but 4H disagrees").
  - All gates still PASS post-revision.

---

## Phase 3 — Telegram on-demand `/topdown <pair>`
**Status:** done
**Inputs:**
- Phase 2 complete cascade output
- Existing Telegram infra: `shared/notifier.py`, any existing bot handler (e.g., `scripts/explain_bot.py` pattern)

**Outputs:**
- Telegram command handler `/topdown <pair>` (e.g., `/topdown btc`)
- Same brief output formatted for mobile (line-break tweaks if needed)
- Routes to `TELEGRAM_CHAT_ID` only (no public bot)

**Work:**
- Wire handler in existing Telegram polling loop (or extend `scripts/explain_bot.py`)
- Pair normalization: accept `btc`, `BTC`, `BTC/USDT`, etc → canonical form
- Latency-acceptable: brief generation must complete in <10s end-to-end

**Verification gate:**
- [ ] Manual: send `/topdown btc` from phone → reply arrives within 10s
- [ ] Manual: brief is readable on mobile (no horizontal scroll, sections separable)
- [ ] Automated: 4 pairs all respond to command
- [ ] Rollback if: latency >15s or output unreadable on mobile → revisit format

**Evidence (filled by /phased-implementation):**
- 2026-05-20 — Automated checks:
  - `normalize_pair()` unit-tested: 8/8 cases pass (btc/BTC/BTC-USDT/sol/XRP/eth → canonical; foo/doge → None since DOGE not in PAIRS).
  - `build_brief_text(pair)` for all 4 supported pairs: BTC=46 lines/2047 chars/0.05s, ETH=50/2287/0.04s, XRP=45/1987/0.04s, SOL=42/1772/0.04s.
  - All briefs include `Plain read:` section.
  - Server-side generation latency 40-50ms (well under 10s gate, Telegram round-trip adds 1-3s polling, still <5s end-to-end).
  - Each brief <4096 chars → fits in single Telegram message (no chunking needed in practice; chunker present as guard).
- Implementation details:
  - `/topdown <pair>` handler added to `scripts/explain_bot.py` alongside existing `/explain`, `/check`, `/stats`, etc.
  - Runs `build_brief_text` in `asyncio.to_thread` so polling loop never blocks.
  - Reply wrapped in ```...``` code block to force monospace on mobile (aligns columns/numbers).
  - Help text updated (`/help` now lists `/topdown <pair>`).
- Manual checklist:
  - [ ] Restart explain-bot container: `docker compose up -d --build explain-bot`
  - [ ] From phone send `/topdown btc` → confirm reply arrives within 10s
  - [ ] From phone confirm reply is readable on mobile (monospace, no horizontal scroll)
  - [ ] Test `/topdown sol`, `/topdown eth`, `/topdown xrp` work
  - [ ] Test `/topdown foo` returns the usage hint
- Rollback trigger fired: no
- Files changed: 2
  - `scripts/topdown_snapshot.py` (+38 LOC — `normalize_pair`, `build_brief_text`, `_PAIR_ALIASES`)
  - `scripts/explain_bot.py` (+30 LOC — `/topdown` handler + help text update)
- LOC delta: +68 / -3

---

## Phase 4 — Scheduled push + on-change alerts + journal field
> **Plan revision 2026-05-20**: Phase 4 split into 4a (falsification enabler — schema + dashboard toggle) and 4b (automation — scheduled push + on-change watcher). Original Phase 4 exceeded the one-context-window budget (4 independent components, 8+ files, likely >500 LOC). 4a unblocks falsification data collection immediately; 4b is automation, can land any time.

---

## Phase 4a — Falsification enabler: schema + dashboard toggle
**Status:** done
**Inputs:**
- Phase 3 `/topdown` deployed and live
- `bybit_trade_annotations` schema in `data_service/bybit_sync.py:110`
- Dashboard manual route: `dashboard/api/routes/bybit.py`
- Dashboard frontend trade form (Next.js)

**Outputs:**
- Schema migration: `ALTER TABLE bybit_trade_annotations ADD COLUMN IF NOT EXISTS topdown_brief_used BOOLEAN` (idempotent, appended to ddl_annotations in bybit_sync.py per existing pattern)
- Backend route accepts/returns `topdown_brief_used` (GET + PATCH annotation endpoints)
- Frontend trade-entry / annotation form has a checkbox: "Used /topdown brief before entry"

**Work:**
- Add `ALTER TABLE ... ADD COLUMN IF NOT EXISTS topdown_brief_used BOOLEAN` to `data_service/bybit_sync.py` ensure_tables ddl
- Update `update_annotation` route in `dashboard/api/routes/bybit.py` to whitelist the new field
- Add checkbox to annotation form in frontend `dashboard/web/`
- Verify `mobile-responsive` (per CLAUDE.md rule)

**Verification gate:**
- [ ] Automated: after ensure_tables runs, `SELECT column_name FROM information_schema.columns WHERE table_name='bybit_trade_annotations' AND column_name='topdown_brief_used'` returns one row
- [ ] Automated: backend PATCH endpoint accepts `topdown_brief_used: true|false` and persists it
- [ ] Manual: open dashboard on mobile (375px) → annotation form shows checkbox without overflow
- [ ] Manual: toggle checkbox → save → reopen annotation → checkbox state persisted
- [ ] Rollback if: column add breaks bybit_sync startup OR frontend crashes on mobile → drop column + revert form

**Evidence (filled by /phased-implementation):**
- 2026-05-25 — `topdown_brief_used BOOLEAN` added to `bybit_trade_annotations` ddl in `bybit_sync.py`; `ensure_tables()` applied, `information_schema` confirms one row (`boolean`).
- Backend `AnnotationUpdate` + `AnnotationOut` + `_row_to_out` carry the field; PATCH whitelists it via the existing dynamic SET builder (no handler change needed).
- Frontend: api.ts `BybitAnnotation` + `BybitAnnotationPatch` typed; annotate form has state + hydrate + payload + styled checkbox (44px touch target, first editable field). `npm run build` ✓ compiled.
- Tests: `test_bybit_*` + `test_manual_trading` 33 pass. CSS folded into the page's single `<style jsx>` block (nested styled-jsx tag is a build error).
- Pending manual: mobile 375px visual + toggle-persist round-trip (needs running dashboard — deploy step).
- Rollback trigger fired: no.

---

## Phase 4b — Automation: scheduled push + on-change watcher
**Status:** done
**Inputs:**
- Phase 4a schema column + toggle live
- Existing systemd user timer infra (`shadow-health-alert.timer` pattern in MEMORY)

**Outputs:**
- New script `scripts/topdown_push.py` — wraps `build_brief_text` for all 4 pairs, sends to Telegram via the same bot token
- systemd user timer firing every 4H (aligned to candle close boundary)
- State file `/tmp/topdown_last_state.json` storing last reconciled side per pair
- On-change push triggered if reconciled side flips OR a new BOS/sweep appears since last snapshot

**Work:**
- Implement `scripts/topdown_push.py push-all` (uses `build_brief_text`, sends via Telegram HTTP API)
- Implement `scripts/topdown_push.py watch` daemon mode (poll every 15m, compare against state file, emit on change)
- Add systemd user unit + timer (under `~/.config/systemd/user/`)
- Decide cadence: scheduled = 4H boundary; watch = 15m diff
- Persist state across restarts

**Verification gate:**
- [ ] Automated: 24h dry-run with `--dry-run` flag → 6 scheduled fires/pair/day × 4 pairs = 24 ± 2 messages
- [ ] Automated: simulated state change → on-change watcher fires extra push
- [ ] Manual: user confirms cron cadence not spammy after 7 days
- [ ] Rollback if: user reports spam OR scheduled push misses >10% expected fires → revert to on-demand only

**Evidence (filled by /phased-implementation):**
- 2026-05-25 — `scripts/topdown_push.py` new. `push-all` + `watch` (--interval, --state-file, --once, --dry-run). Helper `build_brief_and_state(pair)` added to `topdown_snapshot.py`.
- `push-all --dry-run` → 4 briefs rendered. `watch --once` on fresh state → seeds 4 pairs, 0 push. Forced BTC side flip → next pass pushes exactly 1 pair with `🔄 BTC bias change` header.
- 4 automated tests (`tests/test_topdown_push.py`): seed-no-push, no-change-no-push, side-flip-one, confidence-change. Full suite 1276 passed.
- systemd: `topdown-push.{service,timer}` (4H at HH:01, `OnCalendar=*-*-* 00/4:01:00`) + `topdown-watch.service` (daemon, Restart=on-failure). All 3 pass `systemd-analyze --user verify`.
- **Deliberately not enabled** — units version-controlled only. Install + `systemctl --user enable --now` is the post-merge operator action (would start live Telegram pushes). Falsification clock starts then.
- Rollback trigger fired: no.

---

## Out of scope (deliberately)
- **Macro cascade (1W→1D→4H→1H)** — gated by 30-day falsification result. Plan in successor doc.
- **Scalp cascade (1H→30m→15m→5m)** — same gate.
- **30m WebSocket subscription** — Phase 2 uses REST only. WS sub is a separate optimization if REST proves laggy.
- **Auto-Bybit order generation** — brief is context, NEVER signal. No order placement automation.
- **Multi-user / public Telegram bot** — single-user (user's `TELEGRAM_CHAT_ID` only).
- **Brief output translation/multi-language** — English output, period.
- **Any `strategy_service/` modification** — FREEZE-respect.
- **Any ML feature column** — no `ML_FEATURE_VERSION` bump.

## Open questions (must resolve before starting)
- **Q: 30m fetched on-demand or backfilled async?** Decision: Phase 2 = on-demand REST fetch in `snapshot` call. WS sub if Phase 3 reveals latency. (Bounded.)
- **Q: confidence levels — 3 (high/medium/low) or numeric score?** Decision: 3 levels for first version (matches grill recommendation; numeric is reverse-engineerable later).
- **Q: brief uses settings.TRADING_PAIRS or Bybit-only subset?** Decision: subset BTC/ETH/XRP/SOL (4 manual pairs). Hardcoded list in script for now.
- **Q: when does falsification clock start?** Decision: 30 days from Phase 4 ship date (scheduled push live + journal toggle wired).

## Changelog hook
On completion, append to `docs/SYSTEM_BASELINE.md` §9 changelog:
- `2026-05-XX — Top-down Telegram brief shipped (PR #N). Swing cascade 4H→1H→30m→15m for BTC/ETH/XRP/SOL. Manual-trading aid only, NO bot pipeline impact. Falsification deadline 2026-06-XX via WR comparison on topdown_brief_used annotation field.`
