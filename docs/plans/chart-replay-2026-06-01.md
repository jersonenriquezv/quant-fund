# Phased Plan: Dashboard Chart + Bar Replay + Bot-Detection Overlay

**Date:** 2026-06-01
**Source grill:** `docs/grill/chart-replay.md` (verdict: BUILD)
**Scope:** BTC + ETH only. Timeframes 5m/15m/1h/4h (1m dead, skip).
**Precondition status:** TradingView Charting Library GitHub access — **APPROVED** (no longer blocks Phase A).

## Goal

TradingView-style chart in the dashboard with:
1. Native **Bar Replay** + **Long/Short Position** drawing tool (practice positioning) — comes free with the Charting Library.
2. **Bot-detection overlay** — render the bot's own OB/FVG zones as-of any historical bar, to validate detectors visually.

Avoids the tradingview.com monthly plan: the Charting Library (Advanced Charts) is a distinct free, self-hosted product.

## Architecture decisions (locked in grill)

- **Library:** TradingView Charting Library (self-hosted), NOT lightweight-charts (render-only, no replay/position/drawing).
- **Piece B (practice) folds into Phase A** — replay + Long/Short Position are native. No DB, no scoring engine.
- **Phase C is the real custom work** — server-side detector-replay endpoint using the backtester's `SimulatedClock` pattern. Reuse `service.evaluate_all()` / detector `update()` under a simulated clock; do NOT re-implement detection.
- Dashboard stays **read-only on bot state** (dashboard/CLAUDE.md rule 1). New endpoints only READ candles + RUN detectors in-memory; never write `qf:bot:*` or bot tables.

---

## Phase A — Charting Library + Datafeed + native replay/position tool

**Outcome:** `/chart` route renders BTC/ETH at 5m/15m/1h/4h from our DB, with working Bar Replay and Long/Short Position tool. No overlay yet.

### A1. Vendor the Charting Library
- Pull approved repo files into `dashboard/web/public/charting_library/` (per TV license — self-host, no redistribution; keep out of git if license requires, else `.gitignore` the vendor dir and document the fetch step).
- Confirm license terms acceptable for personal/internal fund use. Document in `docs/context/06-dashboard.md`.

### A2. Backend Datafeed endpoints (FastAPI, `dashboard/api/routes/`)
The Datafeed protocol needs more than today's `GET /candles/{pair}/{tf}` (cap 500, count-only). Add a new router (e.g. `routes/chart.py`) — leave the existing sparkline `candles.py` untouched.

- `GET /chart/config` — DatafeedConfiguration: `supported_resolutions` = `["5","15","60","240"]`, `supports_marks`, `supports_time`, exchange/symbol-type lists.
- `GET /chart/symbols?symbol=BTC/USDT` — `resolveSymbol`: LibrarySymbolInfo (ticker, name, session `24x7`, timezone, pricescale, minmov, supported_resolutions).
- `GET /chart/search?query=...` — `searchSymbols`: restrict to BTC/USDT, ETH/USDT only.
- `GET /chart/history?symbol=&resolution=&from=&to=&countback=` — `getBars`: **range query by `from`/`to` ms** (new — current query is count-only). Returns `{s:"ok", t:[], o:[], h:[], l:[], c:[], v:[]}` or `{s:"no_data", nextTime}`.
  - Add `queries.get_candles_range(pair, timeframe, from_ms, to_ms, limit)` — `WHERE timestamp BETWEEN ... ORDER BY timestamp ASC`. Raise cap 500 → ~5000 for this path (keep sparkline cap at 500).
  - Resolution→timeframe map: `5→5m, 15→15m, 60→1h, 240→4h`.
  - **Pair validation mandatory** (dashboard/CLAUDE.md rule 4): regex reject anything outside the BTC/ETH allowlist before hitting DB.

### A3. Live wiring (`subscribeBars` / `unsubscribeBars`)
- `ws.py` emits only confirmed 5m candles (2s Redis poll). For replay-focused tool, **confirmed-bar updates are acceptable** — no intra-candle ticks.
- Datafeed `subscribeBars`: poll `/chart/history` tail (or reuse WS price for last-bar close) for the current resolution. Decision: **confirmed-bar only**; document the limitation in `docs/context/06-dashboard.md`. Higher TFs (15m/1h/4h) update on close — fine for replay.

### A4. Frontend `/chart` route (`dashboard/web/src/app/chart/`)
- Load `charting_library` script, instantiate `widget` with custom Datafeed adapter (`src/lib/datafeed.ts`) pointing at `/chart/*`.
- Enable features: `study_templates`, drawing toolbar, **Bar Replay** (`widgetbar` / `header_screenshot`...), Long/Short Position tool (native — no config needed beyond drawing toolbar enabled).
- Symbol switcher limited to BTC/ETH; resolution buttons 5m/15m/1h/4h.

### A5. Mobile (CLAUDE.md mandate)
- Charting Library is heavier than lightweight-charts. Test at **375px** (iPhone SE): chart usable, toolbar accessible, nothing overflows. Use `disabled_features`/`enabled_features` to trim toolbar on narrow screens if needed.

### Phase A verification gate
- `/chart` renders BTC 1h from DB, scrolls back through full history (range query works, not capped at 500).
- Bar Replay plays/pauses; Long/Short Position tool drags entry/SL/TP and shows R:R.
- 375px: usable, no overflow.
- `cd dashboard/web && npm run build` clean.

---

## Phase C — Bot-detection overlay (detector-replay via SimulatedClock)

**Outcome:** Toggle on `/chart` that overlays the bot's OB/FVG zones as-of the currently-viewed bar range, matching what the live bot would have detected. The detector-validation tool.

### C1. Detector-replay backend (`dashboard/api/routes/chart.py` or new module)
- `GET /chart/detections?symbol=&resolution=&from=&to=` → returns OB/FVG zones active at `to` (the as-of bar), with full geometry: `{high, low, body_top, body_bottom, direction, type, mitigated, filled_pct, timestamp}`.
- **Fidelity is the whole point** (grill Q2). Two non-negotiables:
  1. **Simulated clock** — OB/FVG expire via `time.time()`. Run under the backtester's `SimulatedClock` (`scripts/backtest.py:212`) so zones expire against the as-of bar time, NOT today. Patch `strategy_service.service.time.time` (and `setups.time.time` if exercised) exactly as backtest does (`backtest.py:2073`).
  2. **Incremental driving** — mitigation/retest state depends on call order. Feed candles bar-by-bar up to `to`, calling the detector chain each step (set clock per bar). Do NOT one-shot the whole window.
- **Reuse, don't reimplement:** drive `service.evaluate_all(pair, candle)` per bar (it runs the same state-update pass: MarketStructure → OB → FVG), then read active zones off the detector instances. Alternatively instantiate `MarketStructureAnalyzer` + `OrderBlockDetector` + `FVGDetector` directly and call `.update(...)` per bar with `current_time_ms = bar.timestamp`. Prefer the service path to stay identical to live.
- Performance: detector-replay over thousands of bars is CPU work in a request. Cap the replay window (e.g. last N bars before `to`), cache by `(symbol, resolution, to)`, and `log()` the cap. Do not block the event loop — run in threadpool / `run_in_executor`.

### C2. Frontend overlay
- Custom study or shapes layer rendering returned zones as colored boxes (bullish/bearish OB, FVG) across their time/price extent. Distinct styling: OB vs FVG, mitigated/filled dimmed.
- Toggle button: "Show bot detections". On bar-replay step or scroll, re-query `/chart/detections` for the new as-of bar.

### C3. Fidelity verification gate (CRITICAL)
- Pick a known historical setup from `ml_setups` / `trades` (a recorded OB/FVG with timestamp + geometry).
- Confirm the overlaid zone at that bar **matches** what the live bot recorded (same high/low/direction/mitigation). If it diverges → the replay harness is wrong; fix before shipping.
- This is the gate that proves the overlay isn't "a lie" (grill Q2).

### Phase C verification gate
- Overlay zones appear and move correctly as replay steps.
- Fidelity check (C3) passes against ≥1 known recorded setup per pair.
- No event-loop blocking (detector replay in executor).

---

## Cross-cutting

### Tests
- Backend: `tests/test_chart_datafeed.py` — config/symbols/search/history shape, range query, pair validation rejects non-BTC/ETH, resolution map.
- Backend: `tests/test_chart_detections.py` — SimulatedClock applied (zone NOT expired-by-today), incremental driving, fidelity against a fixture setup.
- Follow `feedback_tests_env_coupling`: patch settings explicitly, never trust dev `.env`.

### Docs (per `/doc-update` rules)
- `docs/SYSTEM_BASELINE.md` — note new `/chart` route + detector-replay endpoint in changelog.
- `docs/context/06-dashboard.md` (Spanish) — endpoints, Datafeed contract, replay/overlay behavior, confirmed-bar live limitation, Charting Library vendor/fetch step + license note.
- Update `dashboard/CLAUDE.md` "Never" note: it warns against adding a charting library without bundle-size check — record the decision + that `/chart` is a dedicated route, sparklines stay SVG.

### Security / safety
- All new endpoints READ-only on bot data (run detectors in-memory; never write `qf:bot:*` or bot tables).
- Pair validation regex on every DB/Redis-bound path (allowlist BTC/USDT, ETH/USDT).
- Tailscale-only access (no new internet exposure).

---

## Sequence

1. **Phase A** (A1→A5) — chart + Datafeed + native replay/position. Ship + verify.
2. **Phase C** (C1→C3) — detector overlay + fidelity gate. Ship + verify.

Phase A is independently useful (practice tool). Phase C is the fund-value detector validator.

**Next step:** `/phased-implementation chart-replay` (Phase A first) using this doc.
