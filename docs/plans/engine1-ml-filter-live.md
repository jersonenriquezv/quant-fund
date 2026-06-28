# Plan: engine1 + ML-score filter → small live (top tercile)
**Slug:** engine1-ml-filter-live
**Source grill:** docs/grill/engine1-top-tercile-live-2026-06-27.md
**Created:** 2026-06-27
**Status:** Phase 1 + 2 done (wiring shipped, flag default OFF); Phase 3 (go-live) pending Q2 min-notional
**Tracer bullet:** Can the bot reproduce the frozen model's score IN-PROCESS at detection time, matching the offline script? If live score ≠ offline score, the whole filter is wrong.

## Context summary
Engine1 (trend-pullback) is shadow-only and unprofitable ungated (forward PF 0.74). The frozen meta-label model (`models/engine1_meta_v1.pkl`) ranks its trades well enough that the top tercile by score is profitable FORWARD (PF 1.32, +$17 on unseen N=34) and survives the honest breakeven-fee test (+$639). This plan wires the model's score into the live execution gate so engine1 trades **only** when `score ≥ frozen cutoff (≈0.847)`. It does NOT retrain the model, change features, or touch other setups. First live capital since shadow-only (2026-04-15).

**The live rule (simple, frozen):** at detection, score the engine1 setup with the frozen model → if score ≥ 0.847 → execute small live; else → shadow only. No calibration. No probability sizing. One fixed cutoff.

## Phase 1 — Live scoring parity (tracer bullet)
**Status:** done (merged PR #110, 2026-06-27)
**Inputs:** frozen model `models/engine1_meta_v1.pkl`; engine1 detection path in `main.py` (~line 191 `evaluate_all`, ~274 shadow branch); offline scorer in `scripts/ml_v1_money_test.py` / `prepare_features`.
**Outputs:** a reusable in-process scorer module (e.g. `strategy_service/engines/engine1_scorer.py`) that loads the frozen model once and returns `score` + `passes_cutoff` for a live engine1 `TradeSetup`; a `ENGINE1_LIVE_SCORE` log line per engine1 emission (still shadow, no execution change).
**Work:**
- Build scorer: load artifact, extract the SAME features `prepare_features` uses, return `predict_proba[:,1]`.
- Freeze cutoff in settings: `ENGINE1_SCORE_CUTOFF = 0.847` (from v1d top-tercile).
- Hook into engine1 emission in `main.py` shadow branch — log score + decision, change NOTHING about execution.
- Backfill check: re-score the last N resolved engine1 shadow rows offline AND in-process; compare.

**Verification gate:**
- [ ] Automated: in-process score matches offline `ml_v1_money_test` score within ±0.001 on ≥10 recent engine1 rows.
- [ ] Manual: `ENGINE1_LIVE_SCORE` lines appear in bot logs after deploy, with sane score distribution (not all 0/1).
- [ ] Rollback if: live score diverges from offline (feature mismatch) → do not proceed; fix feature parity first.

**Evidence (filled by /phased-implementation):**
- 2026-06-27 — Automated checks:
  - `python scripts/engine1_scorer_parity.py` → **PASS**. N=40 recent engine1 rows, max |batch − per_row| = **0.00e+00** (tol 1e-3), 0 rows over tol. Per-row (live path) scoring exactly reproduces batch (offline) scoring. Score range 0.001..0.996; cutoff 0.847 → 6/40 eligible.
  - `python -m pytest tests/ -q` → **1414 passed, 1 xpassed** (incl. new `tests/test_engine1_scorer.py`, 5 tests; 0 failures).
  - Syntax + import: `engine1_scorer` imports clean, `settings.ENGINE1_SCORE_CUTOFF = 0.847`.
- Manual checklist:
  - [x] `ENGINE1_LIVE_SCORE` emission — closed by automated test `tests/test_engine1_scorer.py::test_engine1_score_log_fires_and_logs` (feeds an engine1 setup through `_engine1_score_log`, asserts the log fires). No need to wait for a live emission. (Live log will also self-confirm when engine1 next emits on an active pair.)
- Rollback trigger fired: no (live score == offline score, exact).
- Files changed:
  - `config/settings.py` (+`ENGINE1_SCORE_CUTOFF = 0.847`, frozen rank cutoff)
  - `strategy_service/engines/engine1_scorer.py` (NEW — frozen-model in-process scorer, reuses `prepare_features`, forces training categories)
  - `scripts/engine1_scorer_parity.py` (NEW — Phase 1 gate)
  - `main.py` (`_ml_log_setup` now returns the feature dict; new `_engine1_score_log`; log-only hook after dedup — NO execution change)
- LOC delta: ~+135 / −3
- **Parity design note:** scorer reuses `scripts.ml_v0_engine1.prepare_features` (one transform path, no drift) and overrides categorical categories from the frozen artifact, so single-row live scoring cannot diverge from batch scoring. Proven by 0.00 max diff.
- 2026-06-27 — **DEPLOY (tracer caught 2 production blockers the offline gate could not):**
  - First deploy crash-looped 8× — the OKX `id=None` ccxt crash. The fix `harden_okx_markets` (`fc15ca5`) had never been merged to this line. Cherry-picked → committed separately (PR #108, MERGED `ab9be03`). Confirmed live: `harden_okx_markets: dropped 1 market(s) with id=None`; real balance restored ($86.30 vs $100 fallback).
  - In-container scoring failed: `ModuleNotFoundError: lightgbm` then `OSError: libgomp.so.1`. The bot runtime image lacked the model's deps. Fixed: `requirements.txt` +`lightgbm==4.6.0`, +`scikit-learn==1.8.0` (pinned to freeze version — kills `InconsistentVersionWarning`); `Dockerfile` +`apt install libgomp1`. **Without the tracer this would have silently thrown on every engine1 emission (caught by try/except) — ENGINE1_LIVE_SCORE would never have appeared.**
  - In-container scoring now verified: model loads clean (no version warning), 5 recent rows score identically host vs container (0.8292 / 0.0045 / 0.0048 / 0.8320 / 0.8165) — **host==container exact**.
  - Bot healthy post-deploy: `Restarts=0`, shadow-only (`LIVE: []`), scoring is log-only.
- Manual check status: `ENGINE1_LIVE_SCORE` not yet observed — engine1 (short-only, 5 pairs) has not emitted on an active pair since deploy (only BTC scope-filtered + setup_a/b/scalp emits seen). Hook is reachable + scoring works in-container; awaiting a real engine1 emission to close the manual check.

---

## Phase 2 — Execution wiring behind a flag (default OFF)
**Status:** done (2026-06-28, flag default OFF — zero behaviour change until flipped)
**Inputs:** Phase 1 scorer + frozen cutoff, proven parity.
**Outputs:** an engine1 live-gated execution path that routes `score ≥ cutoff` setups through `risk_service.check` → `execution_service.execute` at min-notional; gated by a new master flag `ENGINE1_LIVE_GATED_ENABLED` (default `False`); explicit `R` (risk-per-trade) wired so the kill line is a concrete $.
**Work:**
- New branch in `main.py`: if `setup_type == engine1_trend_pullback` AND `ENGINE1_LIVE_GATED_ENABLED` AND `score ≥ cutoff` → live path; else shadow (unchanged).
- Position sizing: smallest viable (min-notional), risk-per-trade `R` fixed in settings (e.g. `ENGINE1_RISK_USD = 1.5`).
- Confirm engine1 routes through `risk_service` guardrails + `TRADING_HALTED` / `/emergency` halt.
- Kill-switch instrumentation: track cumulative DD in R, consecutive losses, rolling-20 PF; emit Telegram alert at thresholds (10R / 7 losses / PF<1.2).

**Verification gate:**
- [ ] Automated: `/test` green (0 new failures); a unit/integration test proves a high-score engine1 setup reaches `execution_service.execute` and a low-score one does NOT.
- [ ] Manual: with flag OFF, behavior identical to today (engine1 still pure shadow). With flag ON in sandbox, a high-score setup places one min order and stops at SL/TP correctly.
- [ ] Rollback if: flag ON changes any NON-engine1 setup behavior, or risk guardrails bypassed.

**Evidence:**
- 2026-06-28 — Automated checks:
  - `python -m pytest tests/ -q` → **1432 passed, 1 xpassed** (was 1414 in Phase 1; +18 new). 0 failures.
  - New `tests/test_engine1_live_gate.py` (5) proves routing: flag ON + score≥cutoff + no-kill → `execution_service.execute` called once, `shadow_monitor.add_shadow` NOT called, and `risk.check` receives `risk_usd=ENGINE1_RISK_USD`; flag ON + score<cutoff → shadow; flag OFF + score≥cutoff → shadow; kill verdict True → reverts to shadow (no execute); non-engine1 setup untouched.
  - New `tests/test_engine1_kill_switch.py` (13) covers all three kill conditions (DD-in-R peak-to-trough, trailing consecutive losses, rolling-window PF) + ordering + zero-R safety.
  - `main` imports clean; `risk.check` signature back-compat verified (existing `test_main_pipeline` updated to expect `risk_usd=None` for non-engine1).
- Design:
  - **Routing** (`main.py` `_process_pipeline_setup`): engine1 score computed once via `_engine1_score_log` (now returns the score). `engine1_live = ENGINE1_LIVE_GATED_ENABLED and score >= ENGINE1_SCORE_CUTOFF and not kill`. The shadow gate gains `and not engine1_live`, so an eligible engine1 setup falls through to the live path; every other engine1 emission stays shadow. Flag OFF → `engine1_live` always False → identical to Phase 1.
  - **AI bypass**: engine1_live setups get a synthetic approved `AIDecision` (frozen model replaces the AI filter) — no Claude call.
  - **Sizing**: new `risk_service.check(..., risk_usd=)` param. When set, `risk_pct = risk_usd / capital` (else `RISK_PER_TRADE`). engine1 live passes `ENGINE1_RISK_USD=$1.5` → concrete 10R=$15 kill line. Still bounded by `MAX_MARGIN_PCT_OF_CAPITAL` (0.25). All standard guardrails (`MIN_RISK_DISTANCE`, min-order-size, portfolio heat, `TRADING_HALTED`, live-slot guard) still run — engine1 is NOT a special execution path, only a special *gate*.
  - **Kill switch** (`strategy_service/engines/engine1_kill_switch.py`, pure): `evaluate_kill(pnls, …)` over closed engine1 trade PnL (oldest-first). Checked at the live-gate decision (`_engine1_kill_check` queries `fetch_recent_closed_trades`, filters `setup_type='engine1_trend_pullback'`). On breach: engine1 reverts to shadow automatically + throttled (1h) CRITICAL Telegram alert prompting the operator to flip the flag off. Fail-safe: any DB/scoring error → `(False, None)`, standalone guardrails still protect capital.
- Files changed:
  - `config/settings.py` (+`ENGINE1_LIVE_GATED_ENABLED`=False, +`ENGINE1_RISK_USD`=1.5, +`ENGINE1_KILL_DD_R`=10, +`ENGINE1_KILL_CONSEC_LOSSES`=7, +`ENGINE1_KILL_ROLLING_PF`=1.2, +`ENGINE1_KILL_ROLLING_WINDOW`=20)
  - `risk_service/service.py` (+`risk_usd` override param)
  - `strategy_service/engines/engine1_kill_switch.py` (NEW — pure kill-metric module)
  - `main.py` (`_engine1_score_log` returns score; live-gate routing branch; `_engine1_kill_check` + throttled `_engine1_emit_kill_alert`; engine1 in AI-bypass branch; `risk_usd` wired; `AlertPriority` import)
  - `tests/test_engine1_live_gate.py` (NEW, 5), `tests/test_engine1_kill_switch.py` (NEW, 13), `tests/test_main_pipeline.py` (1 assertion updated)
- Manual check (flag OFF = identical to today): proven by `test_flag_off_high_score_stays_shadow` + full-suite green. Flag-ON sandbox order placement is deferred to Phase 3 (real OKX, money decision).
- Open: min-notional per OKX pair (Q2) still unconfirmed — blocks Phase 3, NOT Phase 2. With `risk_usd=$1.5` a tight SL may size below OKX min; `risk_service` rejects gracefully (`risk_rejected`) — no crash, just no fill until Q2 sizing is confirmed.

---

## Phase 3 — Go live small (plumbing validation)
**Status:** pending
**Inputs:** Phase 2 wiring, flag ready, $86 funded (no top-up yet).
**Outputs:** first ~15–20 REAL engine1 top-tercile trades; a live-vs-shadow parity log (fill price, slippage, outcome) proving live ≈ shadow.
**Work:**
- Flip `ENGINE1_LIVE_GATED_ENABLED = True` (real OKX, `OKX_SANDBOX=false`), min size, top-tercile only.
- Log per trade: intended entry vs actual fill, slippage, outcome vs shadow-predicted outcome.
- Watch-items from grill: live SL rate (shadow top-tercile had 0 SL — confirm not overfit), fill rate, BE rate.

**Verification gate (this is the money decision):**
- [ ] Quantitative: over first ~15–20 live trades — fill rate ≥ 80%, slippage within tolerance, live top-tercile WR ≥ 45%, rolling-20 PF ≥ 1.2.
- [ ] Manual: live fills visibly match shadow assumptions; no surprise SL cluster.
- [ ] **Kill / rollback (data-driven, from grill Q6):** flip flag OFF + return to shadow if ANY of:
  - cumulative drawdown > **10R** (p99 of a healthy model over 30 trades), OR
  - **7 consecutive losses**, OR
  - rolling-20 PF < 1.2 after ≥20 trades.
  - (Below 10R = normal variance — let it ride, do NOT stop.)

**Evidence:**
<empty>

---

## Phase 4 — Scale (only if Phase 3 passes)
**Status:** pending (conditional)
**Inputs:** Phase 3 live ≈ shadow confirmed.
**Outputs:** capital top-up (+$100 USDT held ready) and R increased proportionally; same cutoff, same kill rules scaled in R.
**Work:** fund, raise `ENGINE1_RISK_USD`, keep everything else identical. Re-freeze model + cutoff only if a new EXPERIMENT_ID regime is adopted.

## Out of scope (deliberately)
- **Calibration** — not needed; the rule is a frozen rank cutoff, not probability sizing (grill Q4).
- **Retraining / feature changes** — would bump ML_FEATURE_VERSION and invalidate the frozen model. Use the model as-is.
- **Other setups (A/B/D/F, scalp)** — untouched. This is engine1-only.
- **Topping up capital before Phase 3 passes** — adding $ before live≈shadow is confirmed increases exposure to an unproven-live edge without de-risking the unknown.

## Open questions (must resolve before starting)
- Exact `R` (risk-per-trade $) → sets the 10R kill line as a real number. **RESOLVED 2026-06-27: `R = $1.5` (10R = $15 kill line).**
- Min-notional per OKX pair for the 5 v1d pairs (ETH/SOL/LINK/AVAX/XRP short) — confirm $86 covers min size on all.
- Re-freeze cadence: when does the cutoff get recomputed? Proposal: never during this live test; only on a deliberate re-freeze + new forward window.

## Changelog hook
On completion append to `docs/SYSTEM_BASELINE.md` §9:
- `2026-06-?? — engine1 ML-score filter live-small shipped (PR #N). Impact: first live capital since shadow-only; engine1 top-tercile (score≥0.847) routes to real OKX execution at min size, gated by ENGINE1_LIVE_GATED_ENABLED. Kill = 10R DD / 7 losses / rolling-PF<1.2.`
