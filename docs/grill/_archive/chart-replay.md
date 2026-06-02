# Grill: Dashboard Chart + Bar Replay + Bot-Detection Overlay

**Date:** 2026-06-01
**Topic:** TradingView-style chart in the dashboard with bar replay and overlay of the bot's own OB/FVG detections, to (a) practice positioning via replay and (b) validate the detectors visually — without paying TradingView's monthly plan.
**Verdict:** BUILD — tooling, not capital at risk; high training + detector-validation value; reuses the existing backtester replay harness and candle endpoints; an honest $0 path exists (TradingView Charting Library, self-hosted).

> Grill intensity reduced from default-KILL: this is an enhancement to already-working tooling (dashboard), not a new trading signal with capital at risk. Per memory `feedback_grill_intensity`.

## Context loaded

- Dashboard stack: Next.js 15 / React 19, FastAPI backend. Existing `GET /candles/{pair}/{tf}` (cap 500) and `ws.py` (polls Redis every 2s, 5m candles only, all 7 pairs).
- Detectors are stateful but re-runnable: `OrderBlockDetector.update(candles, structure_breaks, pair, tf, current_time_ms, ...)` and `FVGDetector` return active zones with full geometry (high/low/body/direction/mitigated/filled_pct).
- `scripts/backtest.py` already replays candles bar-by-bar (`visible = candles where timestamp <= current_time_ms`, line 127) and patches `time.time()` via `SimulatedClock` (line 209) so OB/FVG expiration is computed against simulated time, not wall clock.
- Candle history (measured, not assumed):
  - **1m**: only BTC/ETH/SOL/DOGE, dead window 2026-02-24 → 03-14. Unusable.
  - **5m**: BTC/ETH from Dec 2025 (~50k rows); XRP/LINK/AVAX only from March.
  - **30m**: XRP only 6 days. Unusable for XRP.
  - **15m / 1h / 4h**: solid (~3-6 months).
  - **1d**: Nov 2024 → today (deepest).
  - Conclusion: replay is solid only on **BTC/ETH at 5m/15m/1h/4h**. Scope to BTC/ETH.

## Decision tree

### Q1: What is replay FOR, and where is the feedback loop?
**My recommended answer:** Replay only teaches if the future is masked and the user records + grades a hypothetical entry. Naked scrubbing over your own DB = screensaver.
**User answer:** Two goals — (1) practice by placing a hypothetical entry and watching it play out; (2) overlay what the bot detects (OB/FVG/imbalances) to validate the detector is correct. Later clarified (1) needs no persistence/scoring — it is literally TradingView's Long/Short Position tool + Bar Replay: drag entry/SL/TP, press play, watch.
**Grade:** ✅ — clear purpose. Scope grew from "MVP chart+replay" into THREE pieces (A chart+replay, B position-tool practice, C detector overlay). C is the highest-value-for-the-fund piece.

### Q2: The detector-fidelity trap
**My recommended answer:** Overlaying bot detections on history requires re-running the stateful detector chain — and the detectors expire zones via `time.time()`. Run them in a normal request (real clock = today) and every historical zone looks expired/pruned → empty or wrong overlay → the validation is a lie.
**User answer:** (measured by me, not asked) — confirmed in code. The backtester already solves this with `SimulatedClock`.
**Grade:** ❌ against the original plan — it assumed raw `series.update` of candles and did not contemplate that the overlay requires driving the stateful pipeline under a simulated clock. Mitigation: reuse the backtester's `SimulatedClock` harness; do not re-implement detection.

### Q3: The library — original plan (lightweight-charts) cannot deliver the requested UX
**My recommended answer:** `lightweight-charts` is render-only: NO drawing tools, NO Long/Short Position tool, NO Bar Replay. The requested "TradingView with its tools" lives in a separate, also-free product: the **TradingView Charting Library (Advanced Charts)** — self-hosted, your own data, with native Replay + Long/Short Position + full drawing toolbar. The monthly plan being avoided is tradingview.com the website; the Charting Library is a distinct free product.
**User answer:** Chose **TradingView Charting Library**.
**Grade:** ❌ against the original plan (lightweight-charts) — it could not provide replay/position-tool/drawing without rebuilding half of TradingView. Resolved by switching library.

## Final verdict

BUILD. The idea survived once reframed:
- **Piece B collapses** — replay + Long/Short Position tool are NATIVE to the Charting Library. No DB, no scoring engine. It comes free with Piece A.
- **Piece A** = integrate Charting Library + write a Datafeed adapter pointing at the existing candle endpoints + live wiring. The library brings replay and position tool with it.
- **Piece C** = the real custom work: server-side detector-replay endpoint built on the backtester's `SimulatedClock` pattern, returning OB/FVG zones as-of a range, rendered as a custom overlay/study. This is the detector-validation tool and the highest fund value.

Scope locked to **BTC + ETH** (the only pairs with clean deep history, and the pairs actually traded). Timeframes **5m/15m/1h/4h** (1m is dead, skip it).

Effective sequence (A→C→B chosen, but B is native so it folds into A):
1. **Phase A** — Charting Library + Datafeed adapter + live + (native) replay + (native) position tool.
2. **Phase C** — bot-detection overlay via SimulatedClock harness + detector-fidelity verification.

## If BUILD: pre-conditions for /phased-plan

- **External dependency, start TODAY:** request access to the TradingView Charting Library GitHub repo (application form, ~1–3 day approval). This blocks Phase A. Confirm the license terms are acceptable for a personal/internal fund (free, non-redistribution).
- **Datafeed adapter contract:** the Charting Library's Datafeed protocol needs more than the current `/candles` endpoint — `getBars(from, to, countback)`, `resolveSymbol`, `searchSymbols`, a `/config` endpoint, and resolution→timeframe mapping. Raise the candle cap (500 → ~5000) and add a range query (`from`/`to` ms). Backend work scoped in plan.
- **Live wiring:** `ws.py` currently emits only confirmed 5m candles from Redis (2s poll). Decide whether intra-candle live ticks matter; for a replay-focused tool, confirmed-bar updates via `subscribeBars` are acceptable. Document the limitation.
- **Phase C fidelity:** the detector-replay endpoint MUST use the `SimulatedClock` pattern AND drive detectors incrementally (mitigation/retest state depends on call order). Verification gate: pick a known historical setup from `ml_setups`/`trades`, confirm the overlaid zone matches what the live bot recorded.
- **Mobile (CLAUDE.md mandate):** verify Charting Library at 375px — it is heavier than lightweight-charts. Confirm usable, nothing overflows.
- **Docs:** new `/chart` route + Datafeed + detector-overlay endpoint → update `docs/SYSTEM_BASELINE.md` and add `docs/context/` entry per `/doc-update` rules.

Next step: `/phased-plan chart-replay` using this doc as input.
