# Plan: engine1 + ML-score filter → small live (top tercile)
**Slug:** engine1-ml-filter-live
**Source grill:** docs/grill/engine1-top-tercile-live-2026-06-27.md
**Created:** 2026-06-27
**Status:** pending
**Tracer bullet:** Can the bot reproduce the frozen model's score IN-PROCESS at detection time, matching the offline script? If live score ≠ offline score, the whole filter is wrong.

## Context summary
Engine1 (trend-pullback) is shadow-only and unprofitable ungated (forward PF 0.74). The frozen meta-label model (`models/engine1_meta_v1.pkl`) ranks its trades well enough that the top tercile by score is profitable FORWARD (PF 1.32, +$17 on unseen N=34) and survives the honest breakeven-fee test (+$639). This plan wires the model's score into the live execution gate so engine1 trades **only** when `score ≥ frozen cutoff (≈0.847)`. It does NOT retrain the model, change features, or touch other setups. First live capital since shadow-only (2026-04-15).

**The live rule (simple, frozen):** at detection, score the engine1 setup with the frozen model → if score ≥ 0.847 → execute small live; else → shadow only. No calibration. No probability sizing. One fixed cutoff.

## Phase 1 — Live scoring parity (tracer bullet)
**Status:** pending
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
<empty>

---

## Phase 2 — Execution wiring behind a flag (default OFF)
**Status:** pending
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
<empty>

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
- Exact `R` (risk-per-trade $) → sets the 10R kill line as a real number. Recommend `R = $1.5` (10R = $15) at $86 capital. **User to confirm.**
- Min-notional per OKX pair for the 5 v1d pairs (ETH/SOL/LINK/AVAX/XRP short) — confirm $86 covers min size on all.
- Re-freeze cadence: when does the cutoff get recomputed? Proposal: never during this live test; only on a deliberate re-freeze + new forward window.

## Changelog hook
On completion append to `docs/SYSTEM_BASELINE.md` §9:
- `2026-06-?? — engine1 ML-score filter live-small shipped (PR #N). Impact: first live capital since shadow-only; engine1 top-tercile (score≥0.847) routes to real OKX execution at min size, gated by ENGINE1_LIVE_GATED_ENABLED. Kill = 10R DD / 7 losses / rolling-PF<1.2.`
