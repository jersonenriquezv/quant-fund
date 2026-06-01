# Phased Plan: Dashboard Chart + Bar Replay + Bot-Detection Overlay

**Date:** 2026-06-01
**Source grill:** `docs/grill/chart-replay.md` (verdict: BUILD)
**Scope:** BTC + ETH only. Timeframes 5m/15m/1h/4h (1m dead, skip).

**Library decision (revised 2026-06-01):** **klinecharts** (open-source, MIT, `npm install`), NOT the TradingView Charting Library. TV was the grill's pick but its access is gated behind a private GitHub repo (application form → 1–3 day approval → invite); the account never got access. klinecharts has no gatekeeping, ships native drawing/overlay primitives (so the OB/FVG overlay — the highest-value piece — is *easier* than under TV), and the backend already built is library-agnostic. Trade-off: bar-replay + long/short-position tool are **not native** in klinecharts and are built by hand (we control the datafeed, so replay is a data-slice; the position tool is a custom overlay). ~1 extra day of frontend vs TV's days-of-waiting.

## Goal

A chart in the dashboard with:
1. **Bar Replay** + **Long/Short Position** tool (practice positioning) — built on klinecharts.
2. **Bot-detection overlay** — render the bot's own OB/FVG zones as-of any historical bar, to validate detectors visually. (Highest fund value.)

## Architecture decisions

- **Library:** klinecharts (frontend). Backend is a plain JSON candle/detection API — library-agnostic, already done.
- **Replay + position tool are custom** (not native, unlike TV). Replay = step a "visible up-to" pointer over our own history (the `/history` range query already supports it). Position tool = a klinecharts custom overlay drawing entry/SL/TP + R:R.
- **Phase C (overlay) is the real custom work and the fund-value piece.** The detector-replay endpoint reuses the live detectors directly.
- **Fidelity needs NO SimulatedClock** (correction to the original assumption): `order_blocks.py`/`fvg.py` expire zones via the `current_time_ms` **parameter**, never wall-clock `time.time()` (only `service.py` reads the clock). Driving the detectors directly with `current_time_ms = bar.timestamp` reproduces exactly what the live bot saw — no monkeypatch.
- Dashboard stays **read-only on bot state** (dashboard/CLAUDE.md rule 1): endpoints only READ candles + RUN detectors in-memory; never write `qf:bot:*` or bot tables.

---

## STATUS (2026-06-01)

| Piece | State |
|---|---|
| A2 Datafeed backend | ✅ **DONE — PR #55** |
| C1 detector-replay endpoint | ✅ **DONE — PR #56** (stacked on #55) |
| A1 install klinecharts | ⏭️ next |
| A3 live wiring | ⏭️ |
| A4 `/chart` route | ⏭️ |
| A5 replay control (custom) | ⏭️ |
| A6 position tool (custom) | ⏭️ |
| A7 mobile | ⏭️ |
| C2 overlay frontend | ⏭️ |
| C3 fidelity gate | ⏭️ (manual, needs DB) |

Backend (A2 + C1) is **complete and library-agnostic** — survives the TV→klinecharts switch unchanged.

---

## Phase A — Chart + Datafeed + replay + position tool

**Outcome:** `/chart` route renders BTC/ETH at 5m/15m/1h/4h from our DB, with working bar replay and a long/short position tool. No overlay yet.

### A1. Install klinecharts ⏭️
- `npm install klinecharts` in `dashboard/web/`. Public MIT package — no vendoring, no license gate.
- Check bundle-size impact (dashboard/CLAUDE.md "Never": don't add a charting lib without checking bundle; sparklines stay SVG). klinecharts is light (~tens of KB). Lazy-load only on `/chart`.

### A2. Backend Datafeed endpoints ✅ DONE (PR #55)
`dashboard/api/routes/chart.py`:
- `GET /api/chart/config` — supported resolutions 5/15/60/240.
- `GET /api/chart/symbols?symbol=` — resolveSymbol (BTC/ETH allowlist).
- `GET /api/chart/search?query=` — searchSymbols (allowlist).
- `GET /api/chart/history?symbol=&resolution=&from=&to=` — getBars, range query by from/to (UDF seconds), cap 5000, `no_data`+`nextTime`.
- `queries.get_candles_range()` added; resolution map 5/15/60/240 → 5m/15m/1h/4h; pair allowlist enforced.
- Note: the response shape was written to the TradingView UDF spec. klinecharts uses a simpler `{timestamp,open,high,low,close,volume}` array — the frontend datafeed adapter maps UDF→klinecharts (trivial), OR add a thin klinecharts-native variant. Decide in A4; mapping client-side is fine.

### A3. Live wiring ⏭️
- `ws.py` emits confirmed candles only (2s Redis poll). For a replay tool, **confirmed-bar updates are acceptable** — no intra-candle ticks.
- klinecharts `setLoadMoreDataCallback` / `updateData`: poll `/api/chart/history` tail for the current resolution; append/replace the last bar on close. Document the confirmed-bar-only limitation in `docs/context/06-dashboard.md`.

### A4. Frontend `/chart` route ⏭️ (`dashboard/web/src/app/chart/`)
- `src/lib/chartDatafeed.ts` — fetch `/api/chart/{config,symbols,search,history}`, map UDF response → klinecharts kline array.
- Init klinecharts on mount; symbol switcher limited to BTC/ETH; resolution buttons 5m/15m/1h/4h.
- Apple-dark styling to match the dashboard (globals.css tokens).

### A5. Bar replay control (custom) ⏭️
- A "Replay" mode: pick a start bar, then a play/pause/step control advances a `visibleTo` pointer; feed klinecharts only candles `<= visibleTo`. Speed selector. This is a data-slice over history we already serve — no special backend.

### A6. Long/Short Position tool (custom) ⏭️
- klinecharts custom overlay: drag entry, SL, TP handles; render the box + live R:R label (reward/risk from the three prices). Long vs short coloring. No persistence/scoring (grill: pure practice).

### A7. Mobile (CLAUDE.md mandate) ⏭️
- Test at **375px**: chart usable, replay + position controls reachable, nothing overflows. Trim/condense the toolbar on narrow screens.

### Phase A verification gate
- `/chart` renders BTC 1h from DB, scrolls back through full history (range query, not capped at 500).
- Replay plays/pauses/steps; position tool drags entry/SL/TP and shows R:R.
- 375px usable, no overflow.
- `cd dashboard/web && npm run build` clean.

---

## Phase C — Bot-detection overlay

**Outcome:** Toggle on `/chart` that overlays the bot's OB/FVG zones as-of the currently-viewed bar, matching what the live bot would have detected. The detector-validation tool.

### C1. Detector-replay backend ✅ DONE (PR #56)
`GET /api/chart/detections?symbol=&resolution=&to=` → OB/FVG zones active as-of bar `to`, full geometry.
- Drives detectors **incrementally** (OB/FVG mitigation/retest/fill depend on call order).
- Expiration via `current_time_ms = bar.timestamp` **param** — **no SimulatedClock/monkeypatch** (detectors don't read wall-clock; see Architecture note).
- CPU replay off the event loop (`asyncio.to_thread`), window capped 600 bars.
- 6 unit tests cover the replay-harness contract + endpoint shape.

### C2. Frontend overlay ⏭️
- klinecharts custom overlay/figure layer: render returned zones as colored boxes (bullish/bearish OB, FVG) across their time/price extent. OB vs FVG distinct; mitigated/filled dimmed.
- Toggle "Show bot detections". On replay step or scroll, re-query `/api/chart/detections?to=<as-of bar>`.

### C3. Fidelity verification gate (CRITICAL) ⏭️ (manual, needs DB)
- Pick a known historical setup from `ml_setups` / `trades` (recorded OB/FVG with timestamp + geometry).
- Confirm the overlaid zone at that bar **matches** what the live bot recorded (high/low/direction/mitigation). Divergence → harness bug, fix before shipping.
- Proves the overlay isn't "a lie" (grill Q2).

### Phase C verification gate
- Overlay zones appear and move correctly as replay steps.
- C3 fidelity passes against ≥1 recorded setup per pair.
- No event-loop blocking (already handled in C1 via `to_thread`).

---

## Cross-cutting

### Tests
- ✅ `tests/test_chart_datafeed.py` — config/symbols/search/history shape, range query, allowlist, resolution map (10 tests).
- ✅ `tests/test_chart_detections.py` — incremental driving + `current_time_ms`=bar contract, endpoint shape (6 tests).
- Frontend: build check + manual mobile test (no JS unit harness in repo).
- Follow `feedback_tests_env_coupling`: patch settings explicitly, never trust dev `.env`.

### Docs (per `/doc-update` rules)
- ✅ `docs/SYSTEM_BASELINE.md` §8 — A2 + C1 changelog entries.
- ✅ `docs/context/06-dashboard.md` — chart endpoints + files tree.
- ⏭️ On frontend ship: update `docs/context/06-dashboard.md` with `/chart` route + replay/position/overlay behavior + confirmed-bar live limitation + klinecharts dependency.
- ⏭️ Update `dashboard/CLAUDE.md` "Never" note: record that `/chart` is a dedicated route adding klinecharts (lazy-loaded), sparklines stay SVG.

### Security / safety
- All endpoints READ-only on bot data (detectors in-memory; never write `qf:bot:*` or bot tables). ✅
- Pair allowlist (BTC/USDT, ETH/USDT) on every DB-bound path. ✅
- Tailscale-only access (no new internet exposure).

---

## Sequence

1. **Backend** — A2 + C1. ✅ DONE (PR #55, #56).
2. **Phase A frontend** — A1 install → A3 live → A4 route → A5 replay → A6 position → A7 mobile. Ship + verify.
3. **Phase C frontend** — C2 overlay → C3 fidelity gate. Ship + verify.

**Next step:** A1 — `npm install klinecharts`, then build the `/chart` route (A4) wired to the existing Datafeed.
