# Phased Plan: Dashboard Chart + Bar Replay + Bot-Detection Overlay

**Date:** 2026-06-01
**Source grill:** `docs/grill/_archive/chart-replay.md` (verdict: BUILD)
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
| A1 install klinecharts | ✅ **DONE — PR #57** |
| A3 live wiring | ✅ **DONE — PR #61** (forming-candle tick via `/chart/live`, 2s) |
| A4 `/chart` route | ✅ **DONE — PR #57** |
| A5 replay control (custom) | ✅ **DONE — PR #58** |
| A6 position tool (custom) | ✅ **DONE — `src/lib/positionTool.ts`** (draggable long/short, live R:R, 2026-06-03) |
| A7 mobile | ✅ **DONE — condensed toolbar at 375px, 0 overflow** |
| C2 overlay frontend | ✅ **DONE — PR #58** |
| C3 fidelity gate | ✅ **DONE — `scripts/chart_c3_fidelity.py`** (80 setups, 0 lies, 2026-06-03) |

**Post-merge fixes (PR #61):**
- Nav link to `/chart`; API proxied same-origin through Next (Tailscale reachability).
- **Detection perf — solved (not just mitigated).** `/chart/detections` is ~2.5s/call (O(n²) 600-bar replay), so per-bar requery during scrub/play piled up and flickered. New endpoint `GET /chart/detection_timeline` does ONE replay over the window and returns each zone's lifecycle (`born_ts`/`expire_ts`/`spent_ts`). Frontend fetches it once per symbol/resolution (and per new live bar), then `zonesAsOf()` filters the cached lifecycles client-side as the bar moves → zero per-bar server calls, instant scrub. Verified: 1 timeline call survives a full replay playthrough; active-as-of-newest count matches the old `/detections` endpoint exactly. Old `/detections` retained (single-shot use).
- **Overlay cosmetic.** Fixed klinecharts' default text style painting a blue chip behind every label (`backgroundColor`/`borderColor` = blue) — forced transparent. Direction-distinct labels (`OB↑/↓`, `FVG↑/↓`; previously both directions rendered "B"), labels anchored to the as-of (right) edge so they stay visible when the origin scrolls off-screen, overlays `lock: true`.
- **Zone curation — "Focus" toggle (too many zones to read).** MTF + unfiltered OBs + spent zones shown = clutter ("ni sé a cuál hacerle caso"). `curateZones()` (client-side, on when Focus is ON): hide spent (mitigated OB / filled FVG), drop weak OBs (`impulse_score < 0.5`), keep only zones within `MAX_DIST_PCT` (3%) of price PLUS the nearest per TF (HTF bias anchor stays even if farther), cap `PER_GROUP` (2) per (TF, type). Renamed the toggle Significant→Focus. Off = raw. Verified BTC 1h: 17 active → 11 → **4** clean zones. Knobs in `chartDatafeed.ts`.
- **Render perf (UI lag fix).** Was one klinecharts overlay PER zone — klinecharts re-runs every overlay's `createPointFigures` on each repaint (every 2s live tick AND every crosshair move), so ~20-26 MTF zones = 20-26 callbacks/frame = monitor jank. Collapsed to a SINGLE overlay with N*2 points (origin + as-of per zone) drawing all rects/labels in one pass → one callback per frame. Plus: the 2s live poll now skips `updateData` (full canvas repaint) when the forming candle's O/H/L/C is unchanged (idle market).
- **FVG de-noise — "Significant" toggle (chart-only).** Ported LuxAlgo's adaptive Auto-Threshold: an FVG is significant only if its displacement bar (`FVG.timestamp`==c2) moved more than `2× the running mean |body %|`. `detection_timeline` precomputes a `significant` flag per zone; the toggle (ON by default) filters with `zonesAsOf(..., significantOnly)`. Bot detector (`fvg.py`, fixed `FVG_MIN_SIZE_PCT`) is untouched — chart stays a validation tool; this is a visual filter only. Verified BTC 1h live: 10→7 zones; deferred decision to port into the detector as a real strategy change. Threshold multiplier (2×) is the tunable knob if more aggressive de-noising is wanted.
- **Added 1D timeframe.** `D`→`1d` (635 daily bars in DB since Nov 2024).
- **Added 1W timeframe (2026-06-04).** Weekly isn't stored — `queries.get_weekly_candles` aggregates the 1d candles into Monday-00:00-UTC weeks with tz-free integer math (epoch day 0 = Thursday → shift 3); `chart.py` routes resolution `W`→virtual `1w` via a `_fetch_candle_rows` helper at every call site (history, detections, detection_timeline). Frontend `periodStartMs()` uses the identical Monday math so the live forming weekly bar aligns with closed ones. **Also fixed a latent HTF overlay bug 1W exposed:** the live detection overlay filtered zones as-of the latest bar's *start* timestamp — fine on LTF, but up to 7 days stale on 1W (Detections showed 0 despite 261 active 1D/4H zones). Live mode now filters/fetches as-of `Date.now()` with a 60s heartbeat; cache key bounds refetch to `min(period, 1h)`. Replay unchanged (as-of pointer bar).
- **A3 real-time tick (fixed properly).** First cut polled `/history` (closed bars only) → looked like snapshots. Root cause: the bot **discards** OKX forming candles (`websocket_feeds` processes only `confirm="1"`), so nothing ticked intra-bar. Fix (bot-side, display-only): forming 5m candles now go through a new `on_candle_tick` callback → `RedisStore.set_live_candle` → `qf:livecandle:{pair}:5m` (30s TTL). New endpoint `GET /chart/live` reads it; the frontend polls every 2s and aggregates onto the forming bar (5m direct; HTF: close=price, open=prior bar's close). **Strategy/ML/pipeline untouched** — still consume only confirmed candles.
- **Multi-timeframe (MTF) overlay.** `detection_timeline` now replays the HTF bias TFs (1D, 4H) ALWAYS plus the chart's own TF (deduped, parallel via `asyncio.gather`), tagging every zone with `source_tf`. So a 5m chart shows the structural 1D/4H gaps (top-down) instead of dumping every gap of one TF — the user's real ask (LuxAlgo's `fairValueGapsTimeframe` idea, generalized to the strategy ladder). Significance threshold is computed per-TF (each TF vs its own volatility). Labels carry the TF (`FVG↓ 4H`); HTF zones get a heavier border. **Open-expiry fix:** zones still active on a replay's final bar get `expire_ts = ZONE_OPEN_TS` (sentinel) so an HTF zone (whose last HTF bar predates the current LTF bar) still renders as-of now on an LTF chart. Response shape changed `bars`→`timeframes`.

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

### A6. Long/Short Position tool (custom) ✅ DONE — `src/lib/positionTool.ts` (TradingView-style, 2026-06-03)
- **Click-to-place:** `+ Long` / `+ Short` arm the tool (crosshair cursor + hint); the next click on the
  chart drops the entry at that exact price/time (`chart.convertFromPixel`). Seeds default 1%-risk /
  2%-reward (2R); the R:R chip button clears it.
- Single klinecharts overlay (`positionTool`): green reward box (entry→TP) + red risk box (entry→SL) at
  starting at the anchor bar (where it was placed) and extending to the right edge — NOT the whole chart;
  lines + boxes share that left edge (entry dashed), right-anchored labels `TP/SL (±%)` + `Entry · R:R`.
  Handles are draggable dots (`needDefaultPointFigure` + enlarged `styles.point`) revealed on select.
- **Interaction (klinecharts model):** drag a *line* → whole position translates (entry+SL+TP move
  together via `performEventPressedMove`, R:R preserved); drag a *handle dot* → that level adjusts
  independently (SL/TP), R:R recomputes. Direction implied by geometry (crossing entry flips long↔short).
- `createPointFigures` reads live point values every repaint (R:R + box + labels, no React round-trip);
  `onPressedMoving`/`onPressedMoveEnd` mirror the R:R into the toolbar chip.
- No persistence / no order placement (grill: pure practice). Read-only — no bot/DB writes.
- **Verified in browser (Playwright, real DB):** click-to-place at the clicked price; line-drag moves the
  whole position (R:R held); SL-handle drag moves only SL (R:R 2.00→0.79); clear; re-place resets to 2.00;
  375px no overflow. Dev-only `window.__qfChart` / `__qfPosId` test handles (stripped from prod).

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

### C3. Fidelity verification gate (CRITICAL) ✅ DONE — `scripts/chart_c3_fidelity.py` (2026-06-03)
- Automated, repeatable (read-only on DB, no docker). Pulls recorded zone-derived setups
  (`setup_a/b/f/g/h`, `setup_d_bos/choch`) from `ml_setups`, then drives the **real overlay code**
  (`chart._replay_detections`) over the same 600-bar window as-of each setup's detection bar.
- Classifies each: **EXACT** (raw OB edge == recorded SL, <0.05% — byte-exact zone reproduction),
  **BAND** (entry inside a matching-direction zone), **CASCADE** (zones present but entry/SL synthesised
  off the raw edge by `_resolve_entry` — a setup-construction detail, not an overlay defect),
  **LIE** (no matching-direction zone in replay — the only true overlay failure).
- **Result (n=80, both pairs): EXACT 10, BAND 64, CASCADE 6, LIE 0. VERDICT PASS** — the overlay
  reproduces the live detector exactly; the 10 byte-exact OB-edge matches prove the replay harness
  (`current_time_ms`=bar.ts, incremental, 600-bar window) is faithful.
- **Scope note:** `engine1_trend_pullback`, `scalp_*`, `bench_*` are OUT of scope — they derive entry/SL
  from impulse-origin/ATR or random, not OB/FVG, so they never map to overlay zones (not a bug). The
  overlay only draws OB/FVG.
- Proves the overlay isn't "a lie" (grill Q2).

### Phase C verification gate
- Overlay zones appear and move correctly as replay steps. ✅
- C3 fidelity passes against ≥1 recorded setup per pair. ✅ (80 setups, 0 lies — `scripts/chart_c3_fidelity.py`)
- No event-loop blocking (already handled in C1 via `to_thread`). ✅

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
