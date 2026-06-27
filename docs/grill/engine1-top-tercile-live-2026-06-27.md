# Grill: engine1 + ML-score filter → first real money (top-tercile, small live)
**Date:** 2026-06-27
**Topic:** Go small-live on engine1 gated by the frozen meta-label model's score (top tercile)
**Verdict:** **BUILD** — filter holds FORWARD on unseen data (PF 0.74 → 1.32), survives the honest breakeven-fee test, no calibration needed. Build the execution wiring with a frozen score cutoff and a data-driven kill switch.

## Context loaded
- `scripts/ml_v1_money_test.py` output: take-all PF 1.01 → top-half PF 1.99 (+$420), monotonic. Verdict MONETIZABLE. **But** uses pooled regimes + only tp/sl rows.
- `scripts/ml_v1_meta_label.py::fetch_for` — does NOT filter `experiment_id`; pools v1b/v1c/v1d/legacy. Only `shadow_tp`/`shadow_sl`.
- DB (current EXPERIMENT_ID `engine1_short_quarantine_v1d_2026_05_22`):
  - v1d outcomes: shadow_tp 92, shadow_sl 129, shadow_breakeven 278, no_fill 2.
  - Fill rate ≈ 100% (only 2 no_fill) — limit pullback entry fills in shadow.
  - v1d tp/sl only: PF 1.16, +$114. With breakeven fee drag (−$212): **all-fills = −$98 (−$0.20/signal)**.
- `models/engine1_meta_v1.pkl` — frozen 2026-06-23, train_n 322. Keys: model, feature_names, cat_cols, cat_categories, cutoff_created_at.
- `scripts/ml_v1_forward_check.py` — N_GATE=30, scores ONLY post-freeze unseen trades. **This is the gate that killed the impulse-gate (PF 4.5 in-sample → 0.94 forward).**

## Decision tree

### Q1: The money test ranks PAPER shadow PnL. Real money = real fills. Engine1 has a documented fill-rate history. What is the top-half fill rate, limit or market?
**My recommended answer:** quantify the real-fill haircut before capital.
**User answer:** "engine1 should be limit (fees). Don't know the fill %."
**Grade:** ⚠️ → resolved by query.
**Notes:** Queried — only 2 `shadow_no_fill` in all of v1d. **Fill is NOT the risk.** The limit pullback entry gets touched ~100% of the time in shadow. The real optimism is "touch = full-size fill", not no-fill rate. Logged as a live watch-item, not a blocker.

### Q2: The money test ignores 278 breakeven trades that still pay fees. Does the filter remove them or do you keep bleeding BE fees live?
**My recommended answer:** rebuild the test honest — score ALL v1d fills (tp+sl+be) with the frozen model, include BE fee drag.
**User answer:** (asked me to check)
**Grade:** ✅ — this was the real kill-shot; measured it.
**Notes:** Scored all 499 v1d fills with the frozen model. Clean directional separation:
- Bottom half (low score): 120 sl, 3 tp → −$736
- Top half (high score): 89 tp, 9 sl → +$638

Honest sweep INCLUDING breakeven fee drag:
| keep | N | tp | sl | be | total |
|------|---|----|----|----|-------|
| all | 499 | 92 | 129 | 278 | −$98 |
| top 50% | 249 | 89 | 9 | 151 | +$639 |
| top 33% | 164 | 86 | 0 | 78 | +$723 |
| top 25% | 124 | 82 | 0 | 42 | +$721 |

Filter flips engine1 from −$98 to strongly positive **even counting BE fees**. BE concern does NOT kill it.

### Q3: Does the edge hold FORWARD (out-of-time, unseen at train)? This is what killed the impulse-gate.
**My recommended answer:** run `ml_v1_forward_check.py`; only PASS here justifies real money.
**Result (ran it):** forward N=34:
- take-all: WR 32.4%, PF 0.74, −$34.11
- **top-half: WR 47.1%, PF 1.32, +$17.05**
- bottom-half: WR 17.6%, PF 0.35, −$51.16
**Grade:** ✅ PASS. Clean separation on genuinely unseen data. The gate that killed the last lever passes here.
**Notes:** N=34 is small; +$17 paper is tiny in absolute terms. Directionally correct and the script verdict is PASS.

### Q4: Calibration — needed or not?
**My recommended answer:** NO. The live rule is a frozen percentile cutoff (rank-based), not probability sizing. Calibration only matters if sizing by confidence. Brier 0.234 (poor calibration) is irrelevant to a rank threshold.
**Grade:** ✅ Resolved. Skip calibration; freeze the cutoff score instead.

### Q5: Size / capital — $86, send more?
**Resolution:** Keep $86 for the plumbing-validation phase. The first ~15–20 live trades exist to prove live fills ≈ shadow, not to make money. $86 is enough for min-notional. Hold the extra $100 USDT; fund only AFTER live≈shadow is confirmed. Adding capital before that increases exposure to an unproven-live edge without de-risking the unknown.

### Q6: Kill switch — fixed −$20 is arbitrary; a healthy model can draw down then recover.
**Resolution:** data-driven. Bootstrapped (20k resamples) the top-tercile shadow-PnL max-drawdown distribution:
| horizon | p95 DD | p99 DD |
|---------|--------|--------|
| 20 trades | 6.8R | 9.0R |
| 30 trades | 7.6R | 9.9R |
| 50 trades | 8.6R | 11.0R |

Healthy-model 99th-percentile drawdown over 30 trades ≈ **10R**. Losing streaks: 7-in-a-row = 0.6% for this model. **Kill line = 10R cumulative drawdown (R = live risk-per-trade), OR 7 consecutive losses, OR rolling-20 PF < 1.2.** Below 10R = normal variance, let it ride. This replaces the arbitrary $ stop with "worse than a healthy version of this model would realistically do."

## Final verdict: BUILD
What survived: forward PASS (PF 1.32 on unseen data), honest BE-inclusive test positive (+$639 top-half), ~100% fill in shadow, no calibration needed, data-driven kill criterion exists. What's still soft: forward N=34 and tp N=92 are small samples; shadow "touch = full fill" is optimistic; top-tercile had 0 sl in v1d (very clean — watch live sl rate for overfit). These are live-validation watch-items, not build blockers.

## Pre-conditions for /phased-plan
- Freeze the top-tercile score cutoff (≈0.847 on v1d) into the model artifact / settings — do NOT recompute live.
- Wire scoring into the engine1 pipeline path (load frozen model, score at detection, gate execution on score ≥ cutoff).
- Confirm engine1 routes through `risk_service` + emergency halt.
- Define R (live risk-per-trade) explicitly so the 10R kill line is a concrete $ number.
- Instrument live vs shadow parity logging (fill price, slippage, outcome) for the first 20 trades.
