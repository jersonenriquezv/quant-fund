# Grill: replace signal_scanner engine with /topdown edge-triplet

**Date:** 2026-05-25
**Topic:** The classifier-graded `signal_scanner` (grade A/B from `trade_classifier`) emits SMC setups that have no out-of-sample edge. Replace its engine with the `/topdown` edge-triplet logic — the same mechanical sweep+invalidation triplet that measured +0.13R maker on BTC/ETH (`docs/audits/topdown-edge-expectancy-2026-05-25.md`). Keep the scanner shell (timer, dedup table, Telegram format). Goal: fewer alerts, but each carrying a validated edge.
**Verdict:** **BUILD** — low risk. This emits an already-measured strategy through existing infra; it is instrumentation, not a new hypothesis.

## Why not KILL (grill-intensity note)
Per `feedback_grill_intensity`: drop default-KILL when grilling additions to an already-working tool. Here the "addition" is wiring a strategy whose edge is already measured + OOS-validated. The risk is not "does it have edge" (settled) but execution + frequency + forward decay.

## Decision tree

### Q1 — Is the edge in the triplet or in manual judgment? ✅
The +0.13R maker / +0.35R-over-random was measured on the **mechanical triplet** (entry/SL/TP derived from sweep + 4H invalidation), over *every* backtest emission. The edge is in the automatable logic, not the user's eye. A scanner emitting that triplet inherits the measured edge.

### Q2 — Does "fewer but good" actually hold? ✅ (resolved with data)
Raw triplet emits ~8.5/day on BTC/ETH at sweep ≤0.5% — a flood, and +0.13R is the *average*, not "the good ones". Tested per-setup dedup (first tap per pair+direction within a rolling window) on run `topdown_20260525_220604`:

| Dedup | signals/day | WR | maker E |
|---|---|---|---|
| none | 8.50 | 30.9% | +0.122R |
| **6h** | **3.06** | **33.3%** | **+0.197R** |
| 12h | 2.13 | 33.8% | +0.205R |
| 24h | 1.48 | 33.2% | +0.157R |

Dedup *raises* per-signal expectancy (first tap is highest quality; re-taps dilute). 6h is the sweet spot (best total R, ~3/day) and **matches the existing `signal_scanner` 6h dedup** — no infra change needed.

### Q3 — Maker or death 🔴
Edge exists only maker (taker −0.16R). The alert MUST specify a **LIMIT order at the sweep/entry level**, never market. The triplet already yields that price (entry = sweep level). Non-negotiable in the alert text.

### Q4 — Single-TP only 🔴
Scaled-TP = 0 TP ever (both runs). Scanner emits single-TP only (the triplet's final target).

### Q5 — Anti-signal trap ✅ (avoided)
Cannot gate on "more confluences" — the confluence-reliability study proved SMC tags do NOT generalize OOS (best in-sample stack collapses below baseline on holdout). The only mechanically-justified quality gate is **sweep tightness (≤0.5%) + pair (BTC/ETH)**.

### Q6 — Forward decay ⚠️ (accepted, mitigated)
The edge is from the in-sample window (150d to 2026-05-25). No forward proof yet. Mitigants: manual execution at small size (user already trades Bybit by hand), and the first N live alerts ARE the forward validation. Tie alerts to a journal flag (reuse/extend `topdown_brief_used`, or a `signal_source` tag) so live WR is measurable.

## Resolved design parameters
- **Engine:** replace `trade_classifier.classify` call in `signal_scanner` with the `/topdown` triplet path (reuse `topdown_snapshot` helpers; do NOT modify `/topdown` itself).
- **Pairs:** BTC/USDT, ETH/USDT only.
- **Gate:** actionable sweep ≤0.5%, valid triplet geometry (SL correct side), reconciled bias defined.
- **TP:** single-TP only.
- **Dedup:** 6h per pair+direction (existing).
- **Alert text:** explicit LIMIT entry price, SL, single TP, R:R, bias, sweep distance. State "limit order" clearly.
- **Expected:** ~3 alerts/day, WR ~33%, maker E ~+0.20R/trade.

## Falsification
Forward: tag every emitted signal; after N≥30 closed manual trades taken from these alerts, live WR should hold ≥30% AND realized R-expectancy (maker, after real fills) > 0. If live WR collapses to <25% or realized expectancy ≤0 over N≥30 → the in-sample edge did not transfer → revert / kill.

## Out of scope
- No change to `/topdown` brief logic or its push (Phase 4b).
- No live bot execution — alert is a manual heads-up, user sizes + places limit on Bybit.
- No ML feature / strategy_service detector changes (FREEZE-respect; this reuses read-only analytics).
