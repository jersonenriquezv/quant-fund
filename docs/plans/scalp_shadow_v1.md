# Scalp Shadow Signals — v1 Plan

**Status:** in progress
**Branch:** `feat/scalp-shadow-signals`
**Owner:** jer
**Started:** 2026-05-04
**Target exit:** ≥100 resolved outcomes per signal OR 4 weeks elapsed

---

## Goal

Test microstructural scalping signals in **shadow mode only** (zero real capital) to determine whether any has edge worth promoting to live execution. No changes to existing live or shadow setups (A/B/D/F/engine1) — this is additive.

## Hypothesis

Crypto retail accounts cannot compete on latency with colocated HFT, but **specific microstructural events** (forced liquidations, stop hunts, flow/price divergence, crowded funding) leave readable footprints in 1-min data. A simple time-stop scalper around these events may produce >50% WR with 1.5–3R R:R post-fees, despite ~100ms execution latency.

## Non-goals

- Live execution. Not in v1. Live only after shadow validation.
- Changing live setups. A/B/D/F/engine1 untouched.
- Modifying AI service or risk service. Scalping bypasses both (treated as autonomous shadow track).
- Tick-data or orderbook-snapshot features beyond what's already in `data_service` (Redis-cached).

---

## Signals under test

All signals share infra: detection in `strategy_service`, routed to `shadow_monitor` with TP/SL/time-stop. Direction strictly defined per signal. `experiment_id = "scalp_v1_2026_05"`.

### Signal 1 — Liquidation reclaim (`scalp_liq_reclaim_v1`)
- **Trigger:** OI drop ≥2% in 5min (existing `oi_liquidation_proxy`)
- **Confirmation:** 1m candle with wick ≥0.5% but close back inside prior 20-candle range
- **Direction:** counter to wick (wick down → long, wick up → short)
- **TP:** 0.40% | **SL:** 0.20% | **Time stop:** 3 min
- **Edge thesis:** forced sellers exhausted, MM rebid

### Signal 2 — Sweep + 1m CHoCH (`scalp_sweep_choch_v1`)
- **Trigger:** price takes high/low of last 20×1m candles
- **Confirmation:** next 1m candle closes back inside range, body ≥60% of candle range
- **Direction:** counter to sweep
- **TP:** 0.30% | **SL:** 0.15% | **Time stop:** 5 min
- **Edge thesis:** stop-hunt completion, smart-money fade

### Signal 3 — Volume Z-score + CVD divergence (`scalp_vol_cvd_div_v1`)
- **Trigger:** 1m volume ≥3σ over 20-period mean AND CVD direction opposite to candle direction
- **Confirmation:** orderbook spread ≤2bps (no chaos)
- **Direction:** dirección de CVD (no del precio)
- **TP:** 0.50% | **SL:** 0.20% | **Time stop:** 4 min
- **Edge thesis:** price move without supporting flow → mean reversion

### Signal 4 — Funding extreme + flat price (`scalp_funding_extreme_v1`)
- **Trigger:** funding rate ≥0.05% (8h) AND price range last 30min ≤0.3%
- **Direction:** counter to funding
- **TP:** 0.80% | **SL:** 0.30% | **Time stop:** 15 min
- **Edge thesis:** crowded long/short trade ready to flush

### Control — Random baseline (`scalp_random_baseline_v1`)
- **Trigger:** uniform random per pair, capped at expected combined frequency of S1–S4
- **Direction:** random 50/50
- **TP/SL/time stop:** matched to whichever signal it's compared against (per-pair rotation)
- **Purpose:** any "winning" signal must beat this baseline by ≥15pp WR to be considered real edge

---

## Validation rules

For each signal to graduate to live consideration:

1. **N ≥ 100** resolved outcomes (TP, SL, or time_stop)
2. **WR > 50%** post-fees (subtract 0.11% round-trip from each outcome's PnL)
3. **Profit factor > 1.5** post-fees
4. **Beats `scalp_random_baseline_v1`** by ≥15pp WR on matched config
5. **Frequency ≥ 5 trades/day** (otherwise live validation takes >6 months)

If 0 signals pass → kill experiment, document negative result, no live work.
If 1+ pass → design v2 with parameter sweep around best signal, still shadow.
Live execution only after v2 confirms.

## Hard rules

- No changes to live trading thresholds, sizing, gating.
- No execution path. Detector → shadow_monitor only.
- No new dependencies. Build on Redis state + existing `shared/models.py` types.
- Mandatory cross-signal dedup: if S1 and S2 fire on same pair within 30s, keep first only (avoid double-counting).
- All signal handlers must respect `TRADING_HALTED` even though no execution — for audit trail.

---

## Implementation plan (commits on `feat/scalp-shadow-signals`)

| # | Commit | Scope |
|---|--------|-------|
| 1 | `docs(plan): scalp shadow v1` | This document. PR opens here. |
| 2 | `feat(scalp): scaffold experiment id + setup_type registry` | Add `scalp_v1_2026_05` experiment id constant, register 5 setup_types in `SHADOW_MODE_SETUPS`, settings flags. No detection logic. |
| 3 | `feat(scalp): signal 1 — liquidation reclaim detector` | `strategy_service/scalp_setups.py` with S1. Wire to shadow_monitor. Tests. |
| 4 | `feat(scalp): signal 2 — sweep + 1m CHoCH` | S2 detector + tests. |
| 5 | `feat(scalp): signal 3 — vol z-score + cvd divergence` | S3 detector + tests. |
| 6 | `feat(scalp): signal 4 — funding extreme + flat` | S4 detector + tests. |
| 7 | `feat(scalp): random baseline control` | S5 baseline emitter. |
| 8 | `feat(scalp): cross-signal dedup` | 30s window dedup across scalp_* setup_types. |
| 9 | `feat(report): scripts/report_scalp_shadow.py` | Per-signal WR/PF/freq/time-to-resolution, fees-adjusted, decision rule output. |
| 10 | `docs: SYSTEM_BASELINE update + README of experiment` | Document open experiment in §9 of baseline. |

PR stays open until experiment exits (pass or kill). Each merge = checkpoint.

---

## Risks / things that could invalidate the experiment

- **Stale tick data** — 1m candles arrive after close; signals fire ~1s late → shadow assumes mid-candle entry that retail can't hit. Mitigation: report_script discounts entries by realistic slippage (5bps).
- **Look-ahead bias in detection** — using close-of-candle confirmations is OK for 1m if we never reference future candles. Add unit test per signal that asserts no future bar is touched.
- **Signal collision** — multiple signals firing on same wick → inflated frequency. Cross-signal dedup mandatory.
- **CVD/OI source lag** — confirm Redis writes are <1s old before triggering. Add health check.
- **OKX rate limits** — scalping increases query rate. Verify shadow path doesn't hit OKX (price comes from existing WS stream).

## Open questions (to resolve as data comes in)

- Should TP/SL be in % or in ATR multiples? v1 = %. If WR is regime-dependent, switch to ATR.
- Per-pair behavior: BTC vs DOGE microstructure differ. Report must break down by pair.
- Time-of-day effect: scalp may only work in US/EU overlap. Report must include `trading_session` feature already in v14+.

## Exit criteria

Experiment ends when:
- ≥100 outcomes for each of S1–S4, OR
- 4 weeks elapsed (whichever first), OR
- Pipeline health degrades (e.g. shadow_monitor backlog >1h on scalp setups) → pause, fix infra, resume.

Final deliverable: 1-page summary in `docs/audits/` with verdict per signal + recommendation.
