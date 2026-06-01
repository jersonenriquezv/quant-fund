# Bybit Journal v2 — Redesign Plan (resumable)

**Started:** 2026-05-30
**Goal:** ML-grade manual-trade journaling that separates the *trading edge* from *behavioral noise*. Replaces the v1 free-text annotation system (unlearnable — rule-break trades mixed with clean ones poison any dataset).

**Status:** Phase 0+1 DONE (#46), Phase 2 DONE (#48), Phase 3 DONE (PR #49), Phase 4 DONE (this branch). Phases 5–7 pending.

---

## Design principle (the whole point)

Three labels carry all ML weight:
1. **Closed-vocab top-down chain** (what you saw) → learn the *edge*.
2. **`followed_process` y/n** (did you obey rules) → filter *clean samples*.
3. **MAE/MFE in R** (how price moved) → mechanical *cut-winner / held-loser* detector.

Free text (`thesis_pre`, `lesson_post`) stays — for the human, not ML. ML reads only enums + numbers.

**Reality check (C3):** Bybit manual = few trades/week. Per-setup `n` stays <15 for months. This is a **discipline + clean-data-collection** system first, ML edge-detection second. Don't over-build analytics before `n` has statistical power.

**Dataset wall:** Bybit rows are ML-grade for the **manual strategy only**. NEVER cross into `ml_setups` (bot edge). See SYSTEM_BASELINE §7.0.

---

## Locked decisions (grill + user)

- **Confluence floor:** 3 of 5 independent factors, **HTF + trigger mandatory**. Store the 5 booleans, derive count (`tf_aligned_count`). Data raises the floor later, don't guess.
  - 5 factors → cols: `conf_htf`, `conf_location` (PD + key_level merged), `conf_mtf`, `conf_trigger`, `conf_noconflict` (funding/CVD/session not fighting).
  - **Range branch:** `htf_bias=range` has no direction → mandatory becomes `sweep_reclaim at range edge` + `location`, NOT HTF-dir. (Else range fades blocked — same trap that froze the bot in lateral markets.)
- **MAE/MFE:** batch backfill script (mirror `scripts/classify_sl_failures.py`), re-runnable. **1m REST backfill** per trade window (only 5m/15m/1h/4h stored — no 1m).
- **Form UX:** auto-classifier pre-fills the chain from `context_snapshot`; user taps to confirm/correct. Error tags + `followed_process` stay **manual + blank-default** at REVIEW (honesty layer).
- **Old data:** v1/v2 split via `journal_schema_version`. v1 frozen (kept "por si acaso"), excluded from new edge math. Older un-annotated trades stay raw-PnL only (no SL/chain to recover) — clean slate from v2.
- **R unit:** `R_usd = |planned_entry − planned_sl| × size`; `realized_r = closed_pnl / R_usd`. `closed_pnl` already net of fees — do NOT re-deduct (memory `feedback_pnl_already_net_of_fees`).

---

## Taxonomy (closed enums — frozen vocab, already in schema)

| Field | Values |
|---|---|
| `htf_bias_daily` / `htf_bias_4h` | bullish / bearish / range |
| `htf_structure_reason` | HH_HL / LH_LL / range_bound / unclear |
| `location_pd` | premium / equilibrium / discount |
| `location_quality` | key_level / no_mans_land |
| `mtf_1h` | confirms / contradicts / neutral |
| `ltf_trigger` | sweep_reclaim / bos / choch / fvg / order_block / simple_break |
| `structure_type` | continuation / reversal / range |
| `entry_type` | at_level_limit / confirmation_shift |
| `technical_error` (JSONB array, multi) | misread_structure / sl_bad_placement / entered_against_htf / early_no_confirmation / wrong_invalidation / chased_extended |
| `behavioral_error` (JSONB array, multi) | outcome_bias / inconsistent_sizing / revenge_overtrade / not_in_plan / widened_sl / cut_winner_early / held_loser |

Error arrays: `[]` = reviewed, clean. `NULL` = not reviewed yet (don't conflate).
Generated `clean_sample` = `followed_process IS TRUE AND behavioral_error = '[]'`.
Generated `trade_quality` = good_win / good_loss / bad_win / bad_loss (the quadrant — kills outcome bias).

---

## Phases

### ✅ Phase 0+1 — version freeze + v2 schema (DONE, PR #46)
`data_service/bybit_sync.py` `ensure_tables()`: additive idempotent DDL on `bybit_trade_annotations` + `bybit_pending_orders`. `journal_schema_version` (default 1) froze v1 (25 annotations + 13 pending). All PLAN/REVIEW cols + 2 generated STORED cols per table. Docs synced (SYSTEM_BASELINE §7.0 + §8). Verified vs live DB; `pytest -k bybit` 15 pass; CI green.

### ⏳ Phase 2 — data sources (CRITICAL — C1 fix) **[NEXT]**
Without SL, R unit has no data source → entire stats layer is decorative.
- **Watcher** (`data_service/bybit_watcher.py`): at position open, call `get_positions()` → store actual SL in `position_sl_price`; call `get_wallet_balance()` → store `account_equity_at_open`. Neither captured today.
- **1D bias:** enable `candle1D` subscription (in `_TIMEFRAME_TO_CHANNEL` map in `websocket_feeds.py` but NOT subscribed — only 5m/15m/1h/4h stored). Backfill 1D candles. Extend `data_service/context_service.py` `_htf_bias` (currently only iterates 4h/1h) to add 1D.
- Set `journal_schema_version=2` on rows written by v2-aware watcher path.
- Tests: watcher captures SL + equity; `_htf_bias` returns daily key.

### ⏳ Phase 3 — auto-classifier v2 chain pre-fill
- Extend `strategy_service/trade_classifier.py` to emit the v2 top-down chain from `context_snapshot`: daily/4h/1h bias, VA-zone → PD proxy (`volume_profile.zone` ≈ premium/discount, rough — user corrects), `ltf_trigger` from `recent_breaks`/`recent_sweeps`, `structure_type`, 5 conf booleans.
- Bump `CONTEXT_CLASSIFIER_VERSION` (`context_service.py`).
- Writes `auto_*` AND pre-fills the human cols (user confirms in form). Keep both — disagreement (human bullish, machine bearish) IS the misread signal; never overwrite.

### ✅ Phase 4 — MAE/MFE batch backfill script (DONE)
- `scripts/compute_bybit_mae_mfe.py` (mirrors `scripts/classify_sl_failures.py`). Args `--days/--limit/--force/--dry-run`.
- 1m candles fetched on demand via Bybit REST (`get_kline interval="1"`, paginated, ±1m window pad) and discarded — not stored (`mae_mfe_tf='1m'`).
- Direction-aware excursions clamped (`mfe_r≥0`, `mae_r≤0`). Entry/SL anchor prefers `planned_*`, falls back to actual `entry_price` + `position_sl_price` so rows resolve before the form exists. `R_usd = R_price × size`; `realized_r = pnl_usd / R_usd` (pnl already net — no re-deduct); `exit_efficiency = realized_r / mfe_r` (NULL when `mfe_r≤0`); `entry_slippage_bps` direction-aware adverse (NULL without planned entry).
- Re-runnable + idempotent (only `mae_r IS NULL` unless `--force`); nightly-friendly.
- Tests: `tests/test_bybit_mae_mfe.py` (excursion math, planned-vs-actual anchor, slippage sign, pagination). 0 closed v2 rows live yet → populates as v2 trades close.

### ⏳ Phase 5 — mobile form rewrite (375px responsive)
- `dashboard/web/src/app/annotate/[id]/page.tsx` + backend `dashboard/api/routes/bybit.py` `AnnotationUpdate` model.
- **PLAN:** chain dropdowns pre-filled from auto-classifier; 5-box confluence checklist w/ live count + **3-of-5 gate (HTF+trigger mandatory, range branch)**.
- **REVIEW:** `followed_process` toggle + multi-select error chips (blank by default), lesson.
- Demote `grade_self` / `confidence` (keep optional). Mobile: nothing overflows at 375px.

### ⏳ Phase 6 — switch readers + queries/dashboard
- Migrate `scripts/weekly_review_bybit.py` + `scripts/explain_bot.py` to v2 cols.
- Add queries (n always col 1, `clean_sample` filter on edge math, `unnest` JSONB tags): expectancy + PF per setup; clean-vs-dirty cost; behavioral-leak ranked; R distribution; exit efficiency.
- Surface as stats endpoint + Grafana panel.
- **THEN** stop writing `confluences` / `grade_self` (demote, keep cols). Readers switch LAST so the watcher daemon never crashes mid-flight.

### ⏳ Phase 7 — docs + ML training filter
- SYSTEM_BASELINE final sync, memory update.
- ML training query: `WHERE journal_schema_version = 2 AND clean_sample`.

---

## Key queries (Phase 6 reference)

```sql
-- A. Expectancy + PF per setup (clean samples only), n first
SELECT ltf_trigger, structure_type, COUNT(*) AS n,
  ROUND(AVG(realized_r)::numeric,3) AS expectancy_r,
  ROUND(100.0*AVG((realized_r>0)::int),1) AS win_rate_pct,
  ROUND(SUM(CASE WHEN realized_r>0 THEN realized_r ELSE 0 END) /
        NULLIF(ABS(SUM(CASE WHEN realized_r<0 THEN realized_r ELSE 0 END)),0),2) AS profit_factor
FROM bybit_trade_annotations
WHERE status='closed' AND clean_sample
GROUP BY ltf_trigger, structure_type ORDER BY n DESC;

-- B. Cost of breaking rules (clean vs dirty)
SELECT clean_sample, COUNT(*) n, ROUND(AVG(realized_r)::numeric,3) expectancy_r,
       ROUND(SUM(pnl_usd)::numeric,2) net_usd
FROM bybit_trade_annotations WHERE status='closed' GROUP BY clean_sample;

-- C. Behavioral leak ranked (unnest multi-tag)
SELECT tag, COUNT(*) n, ROUND(SUM(pnl_usd)::numeric,2) net_usd
FROM bybit_trade_annotations, jsonb_array_elements_text(behavioral_error) tag
WHERE status='closed' GROUP BY tag ORDER BY net_usd ASC;

-- E. Exit efficiency (cut winners / held losers)
SELECT ltf_trigger, COUNT(*) n,
  ROUND(AVG(mfe_r)::numeric,2) avg_mfe, ROUND(AVG(mae_r)::numeric,2) avg_mae,
  ROUND(AVG(realized_r/NULLIF(mfe_r,0))::numeric,2) exit_eff
FROM bybit_trade_annotations
WHERE status='closed' AND clean_sample AND mfe_r>0
GROUP BY ltf_trigger ORDER BY n DESC;
```

---

## Gotchas / notes for whoever resumes

- `docs/context/` has only 00–07 — there is **NO** `09-bybit` file. Bybit docs live in SYSTEM_BASELINE §10 (auto-grade rubric) + §8 changelog + §9 side-plans, and `data_service/CLAUDE.md` (bybit_sync / bybit_watcher rows).
- Schema is inline DDL in `bybit_sync.py` `ensure_tables()` — NOT the `schema_version`-tracked migration system in `data_store.py` (that's at v21, separate). Bybit tables idempotent-ensured on watcher startup.
- Each phase = its own PR (workflow chain: grill → plan → implement → pr-creator → babysit). Additive throughout; readers switch LAST.
- Memory: `project_bybit_journal_v2.md`. v1 system context: `project_bybit_tradelog_system.md`.
- CI: `pytest` + `docs-truth` workflows on PRs. `python3 scripts/check_docs_truth.py` must pass before PR.
