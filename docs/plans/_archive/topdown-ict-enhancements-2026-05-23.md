# Plan: /topdown ICT Enhancements

> **⛔ ABANDONED — NO EDGE.** PRs #37–42 never merged; backtest verdict **NO EDGE** (`backtest_results/TRACKER.md`). Disregard the in-review status below — archived for decision rationale only.

**Slug:** topdown-ict-enhancements-2026-05-23
**Source grill:** docs/grill/_archive/topdown-ict-enhancements-2026-05-23.md
**Created:** 2026-05-23
**Status:** pending
**Tracer bullet:** Can 3 ICT helpers (displacement / PD array / inducement) be computed purely from `MarketStructureState` + raw candles already loaded by `_build_snapshot`, AND does the resulting Telegram-Markdown brief render under one mobile screen (~25 lines) for SOL/USDT?

## Context summary
Extend existing `/topdown` brief (branch `feat/manual-edge-discipline-phase1`, commit `bb7de40`, deployed in container `quant-fund-explain-bot-1`) with pure-SMC reads named per ICT Top-Down Analysis vocabulary. Bug fix on `_play_idea` target distance (SOL incident: target 84.123 vs entry sweep 84.12 = noise). Telegram-Markdown reformat for mobile readability. NO `strategy_service/` changes — analyzers are read-only consumers. NO ML feature bump. FREEZE-safe per SYSTEM_BASELINE §9.

## Phase 1 — Tracer bullet: bug fix + 3 ICT helpers + Telegram Markdown reformat
**Status:** in-review (work + gate complete 2026-05-23, awaiting user manual validation)
**Branch:** `feat/topdown-ict-enhancements-phase1` off `feat/manual-edge-discipline-phase1`
**Inputs:**
- Grill doc `docs/grill/_archive/topdown-ict-enhancements-2026-05-23.md` (BUILD verdict)
- Existing `scripts/topdown_snapshot.py` (cascade + reconcile + render)
- Existing analyzers on `feat/manual-edge-discipline-phase1`:
  - `MarketStructureState.swing_highs / swing_lows / structure_breaks / latest_break / trend`
  - `LiquidityLevel.swept / touch_count / level_type / price`
  - `OrderBlock.mitigated / impulse_score / body_high / body_low / entry_price`
  - `shared.ml_features.trading_session()` (v14 — asia/europe/us/overlap)
- Sample brief mockup from grill doc

**Outputs:**
- `scripts/topdown_snapshot.py` extended with 4 new pure helpers + Markdown renderer
- `scripts/explain_bot.py` — `parse_mode="Markdown"` already used, no change needed
- `docs/topdown_brief_reference.md` — mapping table brief element → ICT concept → public source
- Unit tests covering each new helper + golden-file render test
- One successful end-to-end Telegram brief delivery for SOL/USDT showing all new sections

**Work:**

1. **Bug fix `_play_idea` target distance** (`scripts/topdown_snapshot.py:344-404`)
   - Add helper `_min_target_distance(sweep_level, invalidation_level)` returning `abs(sweep - invalidation) * 1.5` (1.5R floor)
   - When picking `liq_below` (short) or `liq_above` (long) as target, filter to only liquidity at distance ≥ floor
   - If no valid target found: render "No clean liquidity target at ≥1.5R — find manually" instead of noise level
   - Carry `min_target_distance` value into render so user can see why a level was skipped

2. **New helper `_displacement_read(candles, lookback_n=3, baseline_n=30)`** — ICT Displacement Candle
   - Input: last N candles of one TF
   - Compute: avg body size last 3 candles vs avg body size prior 30 candles
   - Compute: direction consistency (all same color?), close-to-extreme ratio (avg (close - low) / (high - low) for bull, inverted for bear)
   - Return: `{strength: "strong"|"moderate"|"weak", body_ratio: float, direction_consistent: bool, close_to_extreme_pct: float}`
   - Strong = body_ratio ≥ 2.0 AND direction_consistent AND close_to_extreme_pct ≥ 0.80
   - Moderate = body_ratio ≥ 1.5 AND direction_consistent
   - Weak = anything else
   - Reference comment: `# ICT Displacement Candle — see ICT Mentorship 2022 "Market Maker Models"`

3. **New helper `_pd_array_position(liq_analyzer, snap)`** — ICT PD Array / Dealing Range (THIN WRAPPER over existing)
   - Reuse `LiquidityAnalyzer.update_premium_discount(htf_candles, htf_swing_highs, htf_swing_lows, pair, current_price, current_time_ms)` — already implemented in `strategy_service/liquidity.py`. Returns `PremiumDiscountZone` with `range_high`, `range_low`, `equilibrium`, `zone` ("premium"|"discount"|"equilibrium").
   - Compute extra: `position_pct = (current_price - range_low) / (range_high - range_low) * 100`
   - Return: `{position_pct: float, zone: str, range_low: float, range_high: float}`
   - Reference comment: `# ICT PD Array / Dealing Range — wraps LiquidityAnalyzer.update_premium_discount. See ICT "Premium and Discount Arrays".`

4. **New helper `_inducement_check(latest_break, liquidity_levels, lookback_candles=10)`** — ICT IDM
   - Input: `MarketStructureState.latest_break` + all `LiquidityLevel` for this TF + N candles to scan back
   - Logic: scan liquidity_levels for any swept level within `lookback_candles` BEFORE the BOS timestamp, AND in opposite direction (bearish BOS → swept BSL above; bullish BOS → swept SSL below)
   - Return: `{has_idm: bool, idm_level: float|None, idm_swept_at: int|None}`
   - Reference comment: `# ICT Inducement (IDM) — sweep of liquidity opposite to BOS direction, often institutional bait`

5. **New helper `_killzone_now(timestamp_ms)`** — ICT Killzones (exact ICT windows, not session buckets)
   - Constants: `ICT_KILLZONES = [("Asian", 20, 24), ("London", 2, 5), ("NY AM", 12, 15), ("NY PM", 18, 20)]` (start, end in UTC hours)
   - Compute UTC hour from timestamp_ms
   - If hour is inside any killzone window → `{name: str, active: True, next_name: None, minutes_to_next: 0}`
   - Else → find nearest upcoming killzone, return `{name: None, active: False, next_name: str, minutes_to_next: int}`
   - Do NOT reuse `ml_features.trading_session` — hour buckets are too wide (Asian 00-07 UTC ≠ ICT Asian 20-00). Keep `trading_session` intact for ML use.
   - Reference comment: `# ICT Killzones — exact windows per ICT "Killzones" series. NY AM = London/NY overlap.`

6. **New renderer `_render_telegram_markdown(snap)`** replacing `_render_short` for Telegram output
   - Use Markdown syntax: `*bold*`, `_italic_`, `` `code` ``, emoji flags 🟢🔴⚠️✅
   - Section order: Header (pair + time + lag) → BIAS → ICT STRENGTH (4 lines) → KEY ZONES → MAGNETS BELOW/ABOVE → PLAY → INVALIDATION
   - Add freshness flag: compute `lag_min = (now - current_time_ms) / 60000`. ✅ if <5min for 5m TF, ⚠️ otherwise.
   - Keep `_render_short` and `_render` (full mode) untouched — additive only
   - Total target: ≤30 rendered lines

7. **Wire into `build_brief_text`** — add new `mode="telegram"` option that calls `_render_telegram_markdown`. Default `/topdown <pair>` switches to this mode. `/topdown <pair> full` keeps `_render`.

7b. **New table `topdown_brief_renders`** — usage tracking for Phase 3 falsification (no annotation form change).
   - DDL: `CREATE TABLE IF NOT EXISTS topdown_brief_renders (id BIGSERIAL PRIMARY KEY, pair VARCHAR(20) NOT NULL, rendered_at TIMESTAMPTZ DEFAULT NOW(), brief_mode VARCHAR(20))`
   - Index: `CREATE INDEX IF NOT EXISTS idx_topdown_renders_pair_time ON topdown_brief_renders(pair, rendered_at DESC)`
   - Create via new `ensure_topdown_renders_table()` function in `topdown_snapshot.py` (called once on first use)
   - `scripts/explain_bot.py` `/topdown` handler inserts a row after successful `build_brief_text` call (symbol = canonical pair, mode = "short"|"full"|"telegram"). Failure to insert MUST NOT block the user-facing brief reply (try/except, log warning).

8. **Reference doc `docs/topdown_brief_reference.md`**
   - Header: purpose + how to read
   - Table: brief element → ICT concept name → 1-line description → public source (ICT YouTube series, Maven Trading curriculum, TradingView SMC Lux indicator)
   - Section per ICT concept used (displacement, PD array, IDM, killzones, mitigation, FVG, BSL/SSL, top-down cascade)

**Verification gate:**
- [ ] Automated: `python -m pytest tests/test_topdown_snapshot.py -v` — all unit tests for new helpers + bug fix pass. Golden-file test confirms Markdown render contains all 6 expected sections (BIAS, ICT STRENGTH, KEY ZONES, MAGNETS, PLAY, INVALIDATION).
- [ ] Automated: `PYTHONPATH=. venv/bin/python scripts/topdown_snapshot.py snapshot SOL/USDT` runs without error and produces brief with all sections in <30 lines.
- [ ] Automated: target distance bug — assert `_play_idea` never emits a target level closer than 1.5R from the sweep entry (parameterized test on synthetic snapshots).
- [ ] Manual: user runs `/topdown sol` in Telegram, confirms brief renders cleanly on mobile (one screen, no overflow, emoji + bold + code-tick all show correctly), and confirms the ICT reads (displacement / PD position / IDM flag / killzone) match what they see on TradingView.
- [ ] Manual: user runs `/topdown btc` and `/topdown eth` — confirms helpers work across all 4 pairs (BTC/ETH/XRP/SOL).
- [ ] Rollback if: any helper raises uncaught exception on any of the 4 supported pairs, OR rendered brief exceeds 35 lines, OR user reports the ICT reads contradict TradingView on >1 of 3 spot checks.

**Evidence (filled by /phased-implementation):**
- 2026-05-23 — Automated checks:
  - `PYTHONPATH=. venv/bin/python -m pytest tests/test_topdown_snapshot.py -v` → **41/41 passed** (0.38s)
  - `PYTHONPATH=. venv/bin/python -m pytest tests/ -x --tb=short -q` (full suite regression) → **1191 passed, 1 xpassed, 0 failed** (28.58s)
  - `PYTHONPATH=. venv/bin/python -c "from scripts.topdown_snapshot import build_brief_text; print(build_brief_text('SOL/USDT', mode='telegram'))"` → renders 24 lines with all 6 required section headers (`*BIAS:*`, `*ICT STRENGTH:*`, `*KEY ZONES:*`, `*MAGNETS BELOW:*`, `*PLAY:*`, `*INVALIDATION:*`)
  - Telegram brief output for SOL captured during gate run — sample (08m lag flagged ⚠️ correctly):
    ```
    *SOL/USDT* — 2026-05-23 20:00 UTC (lag 8m ⚠️)
    Price: `84.9`
    *BIAS:* 🔴 SHORT — _medium_ (3/5)
    *ICT STRENGTH:*
    • Displacement 4H: 🔴 _weak_ (bull, body x1.3)
    • PD Array 4H: 🟢 10.7% _discount_
    • Last BOS: 🟢 _IDM confirmed_ (swept `85.91`)
    • Killzone: 🟢 _Asian active_
    *KEY ZONES:*
    🔴 4H OB `86.65` PRISTINE (+2.06%)
    *MAGNETS BELOW:*
    🟢 SSL `84.475` × 2 (-0.50%)
    🟢 SSL `84.1233` × 3 (-0.91%)
    *PLAY:*
    Wait for sweep above 85.025 (+0.15%) then short on rejection back below.
    Invalidate: 4H close above 84.95.
    Target / partial: 84.475 (-0.50%, sell-stops below).
    *INVALIDATION:* 4H close above `84.95`
    ```
- Manual checklist (pending user confirmation):
  - [ ] Run `/topdown sol`, `/topdown btc`, `/topdown eth`, `/topdown xrp` in Telegram on iPhone — confirm one mobile screen, no overflow, emoji + bold + code-tick render correctly.
  - [ ] Spot-check 3 of the ICT reads (Displacement / PD Array / IDM / Killzone) against TradingView on one pair — flag if any contradicts TV.
  - [ ] Confirm bug fix: `/topdown sol` no longer produces a noise target like sweep entry ≈ target.
  - [ ] After first usage, query `psql -c "SELECT COUNT(*), pair, brief_mode FROM topdown_brief_renders GROUP BY pair, brief_mode"` to verify usage-tracking rows are landing.
- Rollback trigger fired: **no**
- Files changed (2 modified, 4 new):
  - `M scripts/topdown_snapshot.py` (+581 / -14 LOC — 4 ICT helpers, bug fix, Telegram renderer, ensure/log table helpers, Snapshot.raw_candles, mode wiring)
  - `M scripts/explain_bot.py` (+30 / -7 LOC — telegram-mode routing, brief-render logging, help text update)
  - `+ tests/test_topdown_snapshot.py` (~430 LOC — 41 tests across 6 classes)
  - `+ docs/topdown_brief_reference.md` (ICT concept → public source mapping)
  - `+ docs/grill/_archive/topdown-ict-enhancements-2026-05-23.md` (grill verdict)
  - `+ docs/plans/_archive/topdown-ict-enhancements-2026-05-23.md` (this plan)
- LOC delta: +606 / -21 in tracked files; +430 tests; ~620 in new docs.

---

## Phase 2 — OB mitigation + FVG magnets + killzone overlay + engineered liquidity
**Status:** pending (blocked until Phase 1 in production ≥2 weeks)
**Inputs:**
- Phase 1 shipped, deployed in `quant-fund-explain-bot-1`, used for ≥2 weeks
- User feedback log on Phase 1 brief readability + usefulness
- `ml_features.trading_session()` reuse from Phase 1

**Outputs:**
- `scripts/topdown_snapshot.py` extended with 4 additional sections in `_render_telegram_markdown`
- New helpers: `_ob_mitigation_status()`, `_unfilled_fvg_magnets()`, `_equal_levels_engineered_liquidity()`
- Killzone overlay section added to brief header
- Updated `docs/topdown_brief_reference.md` covering new sections

**Work:**
1. **OB pristine/mitigated status per TF** — surface `OrderBlock.mitigated` already tracked. List up to 3 nearest aligned OBs per TF with status flag (PRISTINE / MITIGATED %x / INVALIDATED). Rank by `impulse_score`.
2. **Unfilled FVG magnets section** — already detected by `FVGDetector`. Filter `filled_pct < 0.5`. Show closest above + closest below current price with distance + age.
3. **Killzone overlay** — render in header: `🟢 London active` / `🟡 Pre-London (45m to open)` / `🔴 Dead zone`. Use `trading_session()` mapping.
4. **Equal highs/lows engineered liquidity** — scan `LiquidityLevel.touch_count ≥ 3` clusters. Flag as "engineered" + "magnet for stop-runs".

**Verification gate:**
- [ ] Automated: existing tests still pass + new helper tests pass
- [ ] Automated: rendered brief stays ≤40 lines after additions
- [ ] Manual: user spot-checks 4 pairs over 3 days, confirms new sections add usable signal not noise
- [ ] Rollback if: brief becomes too long to scan in <10s on mobile, OR new sections produce false flags on >20% of spot checks

**Evidence:**
_empty_

---

## Phase 3 — Falsification window + decision gate
**Status:** pending (blocked until Phase 1+2 shipped AND ≥30d of usage AND ≥20 trades joinable to a brief render)
**Inputs:**
- Phase 1+2 in production
- Table `topdown_brief_renders` populated by Phase 1's Telegram handler
- ≥20 `bybit_trade_annotations` rows that JOIN to a `topdown_brief_renders` row by `symbol = pair AND opened_at BETWEEN rendered_at AND rendered_at + INTERVAL '30 min'` ("brief-informed" bucket), and ≥20 rows that do NOT join (control bucket)
- Bybit Rules taxonomy v3 Rule 13 N=30 journal gate

**Outputs:**
- Audit doc `docs/audits/topdown-brief-falsification-<date>.md`
- Decision: KEEP, EXTEND (Phase 4 macro/scalp cascades), or KILL (remove command, archive)

**Work:**
1. SQL query bucketing trades: brief-informed = JOIN match within 30min window after a brief render; control = no such join. Per bucket compute WR, PF, avg R, avg holding time.
2. Statistical test: bootstrap CI on WR delta. Threshold: brief-informed WR ≥ control WR + 5pp with 80% CI not crossing zero.
3. Qualitative: user writes 1-page reflection on whether brief changed decision quality vs just confirming existing reads.
4. Decision tree:
   - WR delta ≥5pp + CI clean → KEEP + greenlight Phase 4 (macro 1W→1D→4H→1H, scalp 1H→30m→15m→5m cascades + push alerts on new BOS/sweep)
   - WR delta 0-5pp or CI noisy → KEEP as-is, no Phase 4 (insufficient evidence to expand)
   - WR delta negative or brief made decisions worse → KILL command + archive learning

**Verification gate:**
- [ ] Automated: SQL query produces N≥20 in BOTH brief-informed and control buckets
- [ ] Automated: bootstrap CI script runs and emits decision label
- [ ] Manual: user writes the 1-page reflection BEFORE seeing the WR numbers (prevents post-hoc rationalization)
- [ ] Rollback if: N<20 by 30d mark → extend window 30d more; if N<20 by 60d → KILL command (insufficient adoption = not useful enough)

**Evidence:**
_empty_

---

## Out of scope (deliberately)
- **Macro (1W→1D→4H→1H) and scalp (1H→30m→15m→5m) cascades** — original grill 2026-05-20 deferred these to Phase 5 post-falsification. Same here. Don't expand surface before measuring current surface.
- **Push alerts on new BOS/sweep** — requires stateful Redis subscription daemon. Bigger architecture lift. Defer to Phase 4 only if Phase 3 verdict = KEEP+EXTEND.
- **`strategy_service/` changes** — FREEZE forbids until 2026-06-08. All work here is read-only analyzer consumption + render-layer.
- **ML feature version bump** — not changing detector behavior, not changing feature semantics.
- **Auto-generating Bybit orders from brief** — brief = decision support, not signal. User stays in driver seat.
- **Classic indicators (RSI / ADX / EMA-as-bias)** — user rejected per `feedback_pure_smc_no_classic_indicators.md`. Pure SMC only.
- **Replacing reconciled bias logic** — current `_reconcile` works and user likes it. Additions complement, not replace.

## Resolved decisions (locked before /phased-implementation)
1. ✅ **Branch base** — Phase 1 branches off `feat/manual-edge-discipline-phase1`. New branch name: `feat/topdown-ict-enhancements-phase1`.
2. ✅ **Brief usage tracking** — Do NOT add column to `bybit_trade_annotations`. Instead, create new tiny table `topdown_brief_renders(id BIGSERIAL, pair VARCHAR(20), rendered_at TIMESTAMPTZ DEFAULT NOW(), brief_mode VARCHAR(20))`. Phase 1 inserts a row inside the Telegram handler each time `build_brief_text` is called. Phase 3 falsification query joins on `symbol = pair AND opened_at BETWEEN rendered_at AND rendered_at + INTERVAL '30 min'`. Zero UI change, zero user-discipline requirement, captures real usage automatically.
3. ✅ **PD Array swing selection** — Reuse existing `LiquidityAnalyzer.update_premium_discount()` + `PremiumDiscountZone` dataclass (already implemented in `strategy_service/liquidity.py` on feat branch). Helper `_pd_array_position` becomes a thin wrapper that calls `update_premium_discount(htf_candles, htf_swing_highs, htf_swing_lows, pair, current_price, current_time_ms)` and computes `position_pct = (current - range_low) / (range_high - range_low) * 100`. ICT-compliant because the existing method already uses HTF swings as range anchors. No new swing-selection logic.
4. ✅ **Killzone boundaries** — Implement exact ICT windows in new pure helper `_killzone_now(timestamp_ms)`. Do NOT reuse `ml_features.trading_session` (hour buckets too wide — Asian 00-07 UTC vs ICT Asian 20-00, etc — keep `trading_session` intact for ML use). ICT spec:
   - Asian killzone: 20:00 – 00:00 UTC
   - London killzone: 02:00 – 05:00 UTC
   - NY AM killzone: 12:00 – 15:00 UTC
   - NY PM killzone: 18:00 – 20:00 UTC
   Helper returns `{name: str|None, active: bool, next_name: str, minutes_to_next: int}`. ~15 LOC, pure function, easy unit test.

## Changelog hook
On completion, append to `docs/SYSTEM_BASELINE.md` §9 changelog:
- One line: `<date> — /topdown ICT enhancements Phase 1 shipped (PR #N). Impact: bug fix on target distance + 3 ICT reads (displacement/PD array/IDM) + Telegram-Markdown reformat. Read-only, no strategy_service touches, FREEZE-safe.`
