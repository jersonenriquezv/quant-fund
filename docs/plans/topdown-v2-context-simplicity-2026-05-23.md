# Plan: /topdown v2 — Context + Simplicity + Adaptive TP
**Slug:** topdown-v2-context-simplicity-2026-05-23
**Source grill:** docs/grill/topdown-v2-context-simplicity-2026-05-23.md
**Created:** 2026-05-23
**Status:** in-progress
**Tracer bullet:** PR1 quick wins — does adding 5 render-layer refinements (PD-bias conflict flag, sweep distance gate, R:R line, entry/SL/TP triplet, BOS session quality) keep the Telegram brief ≤35 lines and pass all existing tests without breaking the deployed `/topdown` command?

## Context summary
Phase 1 of `/topdown` (`topdown-ict-enhancements-2026-05-23` plan) shipped 2026-05-23 in production. User testing surfaced 5 interpretation gaps + 1 architectural addition (Daily Context Memory) + 1 UX enhancement (adaptive TP). All addressed in this single plan, sequenced as 3 PRs. Architecture: pure derivation over existing `candles` table — no new collection cron, no cache table. FREEZE-safe — zero `strategy_service/` changes, zero ML feature bump. All reads remain read-only over existing analyzers + raw candles.

## Phase 1 — PR1 quick wins (TRACER)
**Status:** in-review (work + gate complete 2026-05-23, awaiting user manual validation post-deploy)
**Branch:** `feat/topdown-v2-pr1-quickwins` off `feat/topdown-ict-enhancements-phase1`
**Inputs:**
- Grill `docs/grill/topdown-v2-context-simplicity-2026-05-23.md` (BUILD verdict)
- Existing Phase 1 helpers in `scripts/topdown_snapshot.py`: `_displacement_read`, `_pd_array_position`, `_inducement_check`, `_killzone_now`, `_render_telegram_markdown`
- Memory `feedback_brief_output_preferences.md` — simpler > dense, explicit triplet, flag conflicts, sweep gate, session quality

**Outputs:**
- `scripts/topdown_snapshot.py` — 5 new helpers + render integration
- `tests/test_topdown_snapshot.py` — unit tests + updated golden-file
- Deployed `quant-fund-explain-bot-1` running PR1 code
- PR opened against `feat/topdown-ict-enhancements-phase1`

**Work:**
1. `_pd_bias_conflict(reconciled_side, pd_zone) -> bool` — true when (side=short AND zone=discount) or (side=long AND zone=premium). Render: `⚠️ *PD-BIAS CONFLICT* — counter-PD trade, downgrade conviction.` Inserted in ICT STRENGTH section right after PD line. Confidence label appends `(PD conflict)` suffix.
2. `_sweep_distance_actionable(price, sweep_level, max_pct=5.0) -> bool` — false when sweep distance >5% from price. `_play_idea` and `_render_telegram_markdown` short-circuit PLAY section: `⚠️ Sweep too far ({pct}%) — spectator zone. Wait for LTF setup or skip pair.` Skip Entry/SL/TP triplet when not actionable.
3. `_trade_triplet(snap, side) -> dict | None` — computes entry (sweep level), SL (invalidation_level), TP (valid target post 1.5R floor). Returns `{entry, sl, tp, rr, valid: bool}`. Rendered as explicit lines:
   ```
   Entry: `X`
   SL: `Y`  (Δ Z%)
   TP: `Q`  (Δ W%)
   R:R: `N.N`
   ```
4. `_bos_session_quality(latest_break_ts) -> dict` — derive killzone of BOS timestamp via existing `_killzone_now` extension (accept arbitrary timestamp). Return `{session: str, quality: "high"|"medium"|"low"}`. Asian = low (often inducement/liquidity grab), London = high, NY AM/PM = high, dead zone = low. Surfaced in ICT STRENGTH as `• Last BOS: <emoji> _<session>_ session ({quality})`.
5. `_render_telegram_markdown` wired to call helpers 1-4 in correct sections. Update `TELEGRAM_REQUIRED_SECTIONS` if any new headers added. Keep total ≤35 lines.
6. Tests: 5 new test classes covering each helper + integration test verifying renderer surfaces all 5 enhancements + golden-file test for SOL/USDT snapshot run.

**Verification gate:**
- [ ] Automated: `PYTHONPATH=. venv/bin/python -m pytest tests/test_topdown_snapshot.py -v` → all tests pass (existing 41 + new ones, target ≥55)
- [ ] Automated: `PYTHONPATH=. venv/bin/python -m pytest tests/ -x -q` → full suite ≥1191 passed, 0 regressions
- [ ] Automated: `build_brief_text('SOL/USDT', mode='telegram')` produces ≤35 lines, contains all sections + new lines for triplet / R:R / session quality / conflict flag when applicable
- [ ] Manual: user runs `/topdown sol`, `/topdown btc`, `/topdown eth`, `/topdown xrp` and confirms: explicit Entry/SL/TP/R:R block visible, conflict flag appears when present, sweep-too-far message renders when sweep >5% away, BOS session quality annotated
- [ ] Rollback if: brief exceeds 40 lines, any helper raises uncaught exception, render layout breaks Telegram Markdown rendering, ANY existing test in full suite regresses

**Evidence (filled by /phased-implementation):**
- 2026-05-23 — Automated checks:
  - `pytest tests/test_topdown_snapshot.py -v` → **69/69 passed** (was 41 Phase 1 + 28 new PR1 tests)
  - `pytest tests/ -x -q` → **1219 passed, 1 xpassed, 0 failed** (was 1191, +28 new)
  - `build_brief_text('SOL/USDT', mode='telegram')` → 27 lines, all features present
- 2026-05-23 — SOL E2E sample (real DB, demonstrates ALL 5 PR1 enhancements simultaneously):
  ```
  *SOL/USDT* — 2026-05-23 20:50 UTC (lag 6m ⚠️)
  Price: `86.63`
  *BIAS:* 🔴 SHORT — _medium_ — _PD conflict_ (3/5)
  *ICT STRENGTH:*
  • Displacement 4H: 🔴 _weak_ (bull, body x1.3)
  • PD Array 4H: 🟢 22.2% _discount_
    ⚠️ *PD-BIAS CONFLICT* — counter-PD trade, downgrade conviction
  • Last BOS: 🟢 _IDM confirmed_ (swept `85.91`)
    🟢 _London session (high quality)_
  • Killzone: 🟢 _Asian active_
  ...
  *PLAY:*
  Entry: `86.652`  (sweep, +0.03%)
  SL:    `86.9`  (0.29% risk)
  TP:    `85.19`  (1.69% reward)
  R:R:   `5.90`
  *INVALIDATION:* 4H close above `86.9`
  ```
- Manual checklist (pending user confirmation post-deploy):
  - [ ] `/topdown sol|btc|eth|xrp` in Telegram — explicit Entry/SL/TP/R:R visible
  - [ ] Conflict flag appears when applicable
  - [ ] BOS session quality annotated
  - [ ] Sweep too-far renders spectator message when present
- Rollback trigger fired: **no**
- Files changed (1 modified, 1 modified test, 2 new docs):
  - `M scripts/topdown_snapshot.py` (+~200 / -~50 LOC — 5 PR1 helpers + renderer wiring)
  - `M tests/test_topdown_snapshot.py` (+~270 LOC — 28 new tests)
  - `+ docs/grill/topdown-v2-context-simplicity-2026-05-23.md`
  - `+ docs/plans/topdown-v2-context-simplicity-2026-05-23.md` (this file)

---

## Phase 2 — PR2 Daily Context Memory
**Status:** pending (blocked until Phase 1 deployed + manual checks confirmed)
**Inputs:**
- Phase 1 PR1 merged to `feat/topdown-ict-enhancements-phase1` (or stacked if PR1 still open)
- `candles` table data inventory confirmed: 580+ daily candles per pair, continuous 4H/1H/15m/5m ingestion

**Outputs:**
- `scripts/topdown_snapshot.py` — 4 new helpers: `_compute_pdh_pdl()`, `_compute_pwh_pwl()`, `_daily_bias_chain()`, `_today_candle_status()`
- New `*DAILY CONTEXT:*` section in `_render_telegram_markdown` (3 lines max)
- Tests for each helper + integration

**Work:**
1. `_compute_pdh_pdl(daily_candles)` — return prev day high + prev day low + today_open from candles where timeframe='1d'. Use `candles[-2]` (yesterday) for PDH/PDL, `candles[-1]` for today_open. Trigger logic: PDH "untaken" if today_high < PDH; "swept" if today_high > PDH but today_close < PDH; "broken" if today_close > PDH.
2. `_compute_pwh_pwl(daily_candles, weeks_back=1)` — find most recent calendar-week range (Mon-Sun UTC), return high+low. If today_close inside [PWL, PWH] = inside week, else broken.
3. `_daily_bias_chain(daily_candles, n=5)` — last 5 daily candles, classify each as `bull` (close > open) / `bear` (close < open) / `doji` (|close-open|/open < 0.001). Return list + summary count.
4. `_today_candle_status(daily_candles)` — current daily candle is still forming. Return `{forming: True/False, side: bull|bear|inside, close_so_far, open}`.
5. Render section (3 lines tight):
   ```
   *DAILY CONTEXT:*
   Today: <emoji> <side> forming (PDH `X` <status>, PDL `Y` <status>)
   Daily chain (5d): <bear/bull arrows>  (<X>/5 <direction>)
   Weekly: <inside/broken> (PWH `Q`, PWL `Z`)
   ```
6. Tests: per helper + golden-file integration confirming section renders correctly + edge cases (insufficient candles, equal high, week boundary).

**Verification gate:**
- [ ] Automated: all new tests pass + existing 41+ Phase 1 tests still pass
- [ ] Automated: SOL/USDT E2E brief stays ≤38 lines with new DAILY CONTEXT section (was 35 cap, +3 lines)
- [ ] Automated: `_compute_pdh_pdl` correctness — feed synthetic 3-day candle series, verify computed PDH/PDL match by hand
- [ ] Manual: user spot-checks one pair's DAILY CONTEXT line against TradingView daily view — PDH/PDL values match
- [ ] Rollback if: aggregation produces wrong levels (off-by-one daily boundary), section adds >5 lines to brief

**Evidence:** _empty_

---

## Phase 3 — PR3 Adaptive TP via Daily ATR
**Status:** pending (blocked until Phase 1 + Phase 2 deployed)
**Inputs:**
- Phase 1 + Phase 2 in production
- User Q3 choice: Daily ATR(14) multiple, not R ratio
- Existing `_trade_triplet` helper from Phase 1

**Outputs:**
- `scripts/topdown_snapshot.py` — `_daily_atr()` + `_adaptive_tp()` helpers; `_trade_triplet` extended to optional scaled-TP variant
- Brief renders TP1 + TP2 when scaled, single TP when not

**Work:**
1. `_daily_atr(daily_candles, period=14)` — standard True Range averaged over last `period` daily candles. Returns ATR value in price units (e.g. SOL might have daily ATR = $2.50). Insufficient candles → return None.
2. `_adaptive_tp(entry, sl, final_target, daily_atr, multiple=2.0)` — compute target distance vs `multiple * daily_atr`. If distance ≥ threshold → scaled: TP1 at intermediate liquidity within `1× daily_atr` (find from liquidity pool), TP2 at final_target. Suggest 50/50 split. Else single TP at final_target.
3. Brief render — when scaled, replace single `TP: X` with `TP1: X (50%)` + `TP2: Y (50%)`. R:R line shows blended R:R: `R:R: N.N (TP1 R:R M, TP2 R:R K)`.
4. Tests: synthetic candles with known ATR; verify threshold behavior on edge cases (exactly 2× ATR, slightly under, far above).

**Verification gate:**
- [ ] Automated: ATR computation matches hand-calculated value on synthetic 14-day candle series (within 0.01)
- [ ] Automated: adaptive logic returns scaled when distance ≥ 2× ATR, single when <
- [ ] Automated: full suite still green
- [ ] Manual: user runs `/topdown` for a long-distance setup (any pair, when present) and confirms TP1/TP2 split shown; runs for a short-distance setup and confirms single TP only
- [ ] Rollback if: ATR computation diverges from TradingView ATR(14) by >5% on spot check, adaptive logic flips incorrectly at threshold boundary

**Evidence:** _empty_

---

## Out of scope (defended)
- **New collection cron / new tables** — `candles` table already has all closes for all TFs. Aggregation = pure derivation. Premature infra.
- **Cache table for daily context** — defer until measured latency >200ms p95. SQL aggregation over indexed `candles(pair, timeframe, timestamp)` is fast.
- **AI / Claude layer over brief** — user explicitly deferred; revisit after v2 ships + 30d usage if interpretation gaps remain.
- **`strategy_service/` changes** — FREEZE until 2026-06-08.
- **Macro cascade (1W→1D→4H→1H)** — original `topdown-ict-enhancements` plan Phase 3+ territory.
- **Push alerts on new BOS / sweep** — separate plan, stateful daemon required.
- **R ratio metric for adaptive TP** — user chose daily ATR multiple instead.

## Resolved decisions (no open questions)
1. ✅ Architecture: pure derivation, no cache, no new cron
2. ✅ Scope: 3 PRs sequenced under one plan
3. ✅ Adaptive TP metric: Daily ATR(14) multiple, threshold 2×
4. ✅ Daily Context section: 3 lines max (today, 5d chain, weekly)
5. ✅ Falsification: inherit Phase 1 gate, window reset on v2 PR1 deploy date
6. ✅ Timeline: 2 days total accepted (PR1 today, PR2 + PR3 over next 1-2 days)

## Changelog hook
On completion of each PR, append to `docs/SYSTEM_BASELINE.md` §9 changelog:
- PR1: `<date> — /topdown v2 PR1 shipped (PR #N). Quick wins: PD-bias conflict flag, sweep distance gate, R:R + Entry/SL/TP triplet, BOS session quality.`
- PR2: `<date> — /topdown v2 PR2 shipped (PR #N). Daily Context Memory: PDH/PDL/PWH/PWL + 5d bias chain over derived candle aggregation.`
- PR3: `<date> — /topdown v2 PR3 shipped (PR #N). Adaptive TP via daily ATR(14) — scaled when target ≥ 2× daily ATR.`
