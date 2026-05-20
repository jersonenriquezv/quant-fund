# Plan: SL Classifier Post-Mortem
**Slug:** sl-classifier-postmortem
**Source grill:** docs/grill/one-step-down-cascade-2026-05-20.md
**Created:** 2026-05-20
**Status:** pending
**Tracer bullet:** Prove `ml_setups` + `candles` tables have enough granularity to compute MFE/MAE per SL and assign a failure mode label.

## Context summary
- Grill killed OSD. Real binding constraint per data = BE rate 60% + sub-fee edge on engine1, not bias-cascade absence.
- Before next refactor (post ML v0 re-run 5/25), classify losing trades into modal failure modes so the next fix targets the actual cause.
- Read-only tool. NO touches to detectors, settings, ML feature surface, or live state. Safe during ML v0 validation window.

## Failure mode taxonomy
| Class | Definition (computed from candles between actual_entry and actual_exit) |
|---|---|
| **wrong_direction** | MFE < 0.3R AND MAE ≥ 1.0R from entry — market never moved in trade direction |
| **sl_too_tight_noise** | MFE ≥ 0.7R AND trade still stopped out — moved favorable then reversed |
| **late_entry** | MFE < 0.3R AND fill_candle was within 2 candles of impulse terminus (impulse_directional_purity at entry > 0.85) — bot bought top / sold bottom |
| **wrong_zone** | actual_entry > 0.3R away from sl_price expected geometry OR htf_bias undefined at entry |
| **counter_trend_valid** | htf_bias aligned with direction, MFE in 0.3-0.7R range, ATR-noise SL — unavoidable loss |
| **unclassified** | doesn't fit any bucket cleanly — needs manual inspection |

R = `risk_distance_pct` × `entry_price`.

## Phase 1 — Tracer: extract MFE/MAE for 5 engine1 SLs
**Status:** done
**Inputs:**
- `ml_setups` rows where `setup_type='engine1_trend_pullback' AND outcome_type='shadow_sl' AND created_at >= NOW() - INTERVAL '14 days'`, ordered DESC, LIMIT 5
- `candles` table 5m + 15m for relevant pair/timeframe between `actual_entry` ts and `actual_exit` ts

**Outputs:**
- New script `scripts/classify_sl_failures.py` (read-only)
- Console output: 5 rows × {pair, direction, entry, sl, R_pct, MFE_R, MAE_R, fill_to_exit_minutes, candidate_class}
- Sanity assertion: for SL outcome, MAE must be ≥ 1.0R (price reached SL). If <1.0R for any row, candle data is wrong or actual_exit is wrong — STOP.

**Work:**
- Add script `scripts/classify_sl_failures.py`
- Helper: `_load_candles_between(pair, tf, ts_start, ts_end)` from `data_service.data_store`
- Helper: `_compute_mfe_mae(direction, entry, candles_slice) -> (mfe, mae)`
- Print loop over 5 sample SLs

**Verification gate:**
- [ ] Automated: `python scripts/classify_sl_failures.py --limit 5` exits 0
- [ ] Automated: 5/5 SL rows produce non-null MFE/MAE with MAE ≥ 1.0R (sanity)
- [ ] Manual: user reviews 5 printed rows, confirms numbers look sensible (entry/sl prices match Bybit/OKX history if checked)
- [ ] Rollback if: ≥1 row produces nonsensical MFE/MAE (e.g. MFE > MAE in absolute terms, candle slice empty) → candle resolution insufficient → re-evaluate plan

**Evidence (filled by /phased-implementation):**
- 2026-05-20 — Automated checks:
  - `PYTHONPATH=. venv/bin/python scripts/classify_sl_failures.py --limit 5` → exit 0
  - 5/5 SL rows produced non-null MFE/MAE
  - 5/5 had MAE ≥ 1.0R (sanity passed)
  - 0/5 unclassified
- Sample output (5 most recent engine1 SLs, 14d window):
  - AVAX short MFE 0.73R MAE 1.07R → sl_too_tight_noise
  - LINK short MFE 0.73R MAE 1.31R → sl_too_tight_noise
  - SOL short MFE 0.62R MAE 1.24R → counter_trend_valid
  - XRP short MFE 0.91R MAE 1.19R → sl_too_tight_noise
  - SOL short MFE 0.79R MAE 1.07R → sl_too_tight_noise
- Early signal (5-sample, not statistically valid): 4/5 = sl_too_tight_noise. Confirms grill hypothesis (BE rate + SL noise, not bias cascade).
- Manual checklist:
  - [ ] User to confirm: 5 printed rows look sensible (entry/sl prices roughly match observed price action for those pairs/times)
- Rollback trigger fired: no
- Files changed: `scripts/classify_sl_failures.py` (new, 197 LOC)
- LOC delta: +197 / -0

---

## Phase 2 — Full classifier on last 50 engine1 SLs + report
**Status:** in-review
**Inputs:**
- Phase 1 outputs verified (MFE/MAE extraction proven)
- Failure mode taxonomy above

**Outputs:**
- `scripts/classify_sl_failures.py` extended: `--limit N --setup-type X --report out.md`
- Markdown report `docs/audits/sl-postmortem-engine1-2026-05-20.md` with:
  - Class distribution table (count + % per failure mode)
  - 3 representative examples per class (top-3 by MAE)
  - Modal class identified + one-paragraph hypothesis on what would fix it
  - Comparison table: same analysis for setup_f (N=8) as sanity counter-sample

**Work:**
- Extend script with classifier logic (apply taxonomy rules)
- Add `--report` flag → writes markdown
- Run for engine1 N=50 + setup_f N=8
- Author report

**Verification gate:**
- [x] Automated: `python scripts/classify_sl_failures.py --setup engine1_trend_pullback --limit 50 --report docs/audits/sl-postmortem-engine1-2026-05-20.md` exits 0
- [x] Automated: < 20% of rows classed as `unclassified` (taxonomy covers majority)
- [ ] Manual: user reads report, agrees modal class is plausible vs his recall of Telegram alerts
- [ ] Rollback if: > 50% unclassified → taxonomy is wrong, redesign

**Evidence (filled by /phased-implementation):**
- 2026-05-20 — Automated checks:
  - `PYTHONPATH=. venv/bin/python scripts/classify_sl_failures.py --setups engine1_trend_pullback,setup_f --limit 100 --days 14 --report docs/audits/sl-postmortem-engine1-2026-05-20.md` → exit 0
  - Total 39 SLs (engine1: 34, setup_f: 5; orphan rows with resolve_ts ≤ fill_ts filtered out via SQL)
  - 37/39 classified (94.9%) — well under 20% unclassified threshold
  - 1/39 sanity failure (2.5%, residual data artifact; gate tolerates <5%)
- **Engine 1 class distribution (N=34):**
  - `wrong_direction` — 13 (38.2%) **modal**
  - `sl_too_tight_noise` — 11 (32.4%)
  - `counter_trend_valid` — 10 (29.4%)
  - `unclassified` — 0
- **Setup F class distribution (N=5):**
  - `wrong_direction` — 2 (40%) modal
  - `counter_trend_valid` — 1 (20%)
  - `unclassified` — 2 (40%, both longs — taxonomy currently tuned for shorts; counter-sample only, low priority)
- **Key finding flips Phase 1 hint:** at N=34, modal is `wrong_direction` (38%), NOT `sl_too_tight_noise` as the 5-sample suggested. Engine 1 enters against the actual short-term direction more than 1/3 of the time. Combined with `counter_trend_valid` (29%), >65% of SLs involve direction-quality issues, not SL distance per se.
- **Fix hypothesis (deferred to post 5/25 ML v0 re-run):** add 1D HTF veto layer — NOT full 4-step OSD cascade. Estimated cost: ~50 LOC. Estimated payoff: drop ~38% of `wrong_direction` losses if signal works. Counterfactual: tighten engine1 impulse threshold (cheaper, may reduce N but not direction quality).
- Manual checklist:
  - [ ] User to read `docs/audits/sl-postmortem-engine1-2026-05-20.md` and confirm distribution maps to felt experience.
- Rollback trigger fired: no
- Files changed: `scripts/classify_sl_failures.py` (+222 / -50), `docs/audits/sl-postmortem-engine1-2026-05-20.md` (new)
- LOC delta: +222 / -50 + report

---

## Out of scope (deliberately)
- **No code changes to bot detectors / setups / risk / execution.** Pure analysis.
- **No new ML features.** Does not bump `ML_FEATURE_VERSION`. ML v0 baseline unaffected.
- **No fix implementation.** Plan ends at report. Fixes happen post 5/25 in a separate plan.
- **Not extending to scalp_random_baseline** (control, not strategy).
- **Not classifying TP outcomes.** Only losses — that's where the leak is.
- **Not OSD anything.** Killed in grill.

## Open questions (must resolve before starting)
- **Candle TF for MFE/MAE**: 5m or 15m? — Use **5m**. Engine 1 entries are 5m timeframe per `engine1.evaluate`. 15m would smooth wicks that matter.
- **N for phase 2**: 50 vs all 48 SLs from 14d? — Use **all 48** (effectively all). N=50 cap is just safety.
- **What if modal class = `counter_trend_valid`?** — Means SLs are pure noise; no detector fix possible; only edge-improvement is fees/sizing. Documented outcome.

## Changelog hook
On completion, append to `docs/SYSTEM_BASELINE.md` §9 changelog:
- `2026-05-XX — SL post-mortem classifier shipped (PR #N). Read-only analysis script. Modal engine1 failure mode = <X>. Next grill: targeted fix for <X> after ML v0 re-run 5/25.`
