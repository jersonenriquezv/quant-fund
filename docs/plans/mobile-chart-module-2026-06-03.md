# Plan: Mobile app — Chart module (V2 add-on)

**Slug:** mobile-chart-module-2026-06-03
**Source grill:** inline (this conversation, 2026-06-03) — medium grill (chart is a working web tool; adding to a planned app)
**Created:** 2026-06-03
**Status:** APPROVED, pending implementation. Depends on the Journal app (V1) shipping first.
**Parent plan:** `docs/MOBILE_APP_PLAN.md` (Journal/Bybit app, Expo/RN). This resolves
that doc's open question #5 ("include the OKX bot chart?") = YES, as a V2 module via WebView.

## Decision summary (grilled 2026-06-03)
- **Render approach = WebView** of the existing Next.js `/chart` route. klinecharts is
  a canvas/DOM web library with **no native React Native port** — a 1:1 RN rewrite of
  the overlay + replay + position tool is disproportionate. WebView reuses 100% of the
  shipped chart. Rest of the app stays native (Expo/RN).
- **"Analysis" = all three layers:** (1) bot OB/FVG overlay (already in `/chart`),
  (2) `/topdown` brief, (3) a new on-demand AI analysis.
- **Exposure = yes**, serve `/chart/*` + candle endpoints publicly behind JWT (same
  Cloudflare tunnel + auth as the Journal app). Read-only.
- **Sequencing = V2**, after the Journal app (V1) is stable. Chart ~doubles frontend
  complexity; ship journal first.

## What already exists (reuse)
- `/chart` route: klinecharts replay + position tool + OB/FVG overlay (PRs #55/#56,
  frontend in progress — see [[project_chart_replay]] / `chart-replay-2026-06-01.md`).
- Detections overlay endpoint: `dashboard/api/routes/chart.py:178+` (`_replay_detections`, OB/FVG).
- Candle history: `GET /api/chart/history` (reads `candles`, resolutions 5/15/60/240/D).
- `/topdown` brief: **scripts only today** (`scripts/topdown_snapshot.py`, `topdown_push.py`
  → Telegram). NO API endpoint — must be wrapped (CV2). `topdown_brief_used` flag already
  exists in `bybit_trade_annotations`.

## Dependency gate
Requires `docs/MOBILE_APP_PLAN.md` **P0** done: Cloudflare Tunnel + JWT auth + `app_users`.
The chart module extends that surface; it does not stand alone.

## Phase CV1 — Expose chart endpoints behind auth
**Goal:** chart-side endpoints reachable through the public tunnel, safely.
**Work:** add `Depends(current_user)` to `/chart/*` + candle routes; rate-limit;
confirm CORS/origin for the WebView; keep pair-format validation (already enforced).
Read-only — no new mutations.
**Gate:** authed request returns chart data through the tunnel; unauthed = 401.

## Phase CV2 — `/topdown` brief endpoint
**Goal:** serve the brief as structured data to the app (today it's CLI→Telegram).
**Work:** extract the brief logic from `scripts/topdown_snapshot.py` into a callable;
new `GET /mobile/brief?symbol=` (auth) → structured brief (bias, key zones, entry/SL/TP,
flags). Follow brief output prefs (memory: [[feedback_brief_output_preferences]],
[[feedback_pure_smc_no_classic_indicators]] — no RSI/ADX/Stoch in the user-facing brief).
**Gate:** endpoint returns a brief matching the Telegram version for BTC/ETH.

## Phase CV3 — Embed mode + WebView screen
**Goal:** the chart renders inside the Expo app.
**Work:**
- Next.js `/chart`: add `?embed=1` mode — hide dashboard chrome, mobile-first layout,
  must NOT break the existing desktop `/chart`.
- Expo: new "Chart" screen/tab with a WebView → `/chart?symbol=&embed=1`.
- Auth: inject JWT into the WebView via header/`postMessage` (NOT token-in-URL — avoid
  logs/leaks).
- Deep link: journal trade row → open chart at that symbol/time.
**Gate:** chart loads in-app through the tunnel, authed, at 375px.

## Phase CV4 — Analysis layers
**Goal:** the three analysis surfaces around the chart.
**Work:**
- OB/FVG overlay: free via the WebView (already rendered).
- Brief: native RN panel below the WebView consuming CV2 (`/mobile/brief`). Native panel
  > embedding for mobile UX.
- AI analysis: new `GET /mobile/analysis?symbol=` (auth) → Claude analyzes the current
  setup on demand. New prompt design + Claude API cost + latency handling (spinner,
  cache last result). On-demand button, not auto.
**Gate:** all three render; AI analysis returns a coherent read for a live symbol.

## Phase CV5 — Mobile UX
**Goal:** touch UX that doesn't fight the app.
**Work:** resolve WebView pan/zoom vs app-scroll gesture conflict; decide replay +
position-tool scope on mobile (recommend **read-only live view for chart-V1**; replay/
position-tool as a later increment — touch UX for scrubbing/drawing is its own design).
**Gate:** chart usable one-handed at 375px; no gesture deadlock.

## Phase CV6 — Verify
- `cd dashboard/web && npm run build` (embed mode + bundle; klinecharts stays lazy).
- Auth enforced on `/chart/*` + `/mobile/brief` + `/mobile/analysis`.
- WebView loads through Cloudflare tunnel on a real device, 375px.
- Brief + AI endpoints return for BTC/ETH; AI latency acceptable.
- Existing desktop `/chart` (Tailscale) unaffected by embed mode.

## Docs
- Update `docs/MOBILE_APP_PLAN.md`: resolve question #5, add Chart module as V2 track.
- `docs/context/06-dashboard.md`: embed mode + new mobile endpoints (`/doc-update`).

## Risks
- **WebView auth leakage** → header/postMessage injection, never token-in-URL.
- **Exposing OKX-side data** → auth + rate-limit + read-only + pair validation.
- **Gesture conflict** (chart pan vs scroll) → CV5 explicit.
- **klinecharts perf in mobile WebView** on low-end phones → test; lazy-load.
- **AI analysis cost/latency** → on-demand only, cache last result.
- **`/topdown` extraction** → scripts are CLI/Telegram-shaped; CV2 may need a refactor to
  separate brief computation from the Telegram sink.
- **Embed mode regressing desktop chart** → CV3/CV6 guard.

## Rollback
Chart module is additive: a screen + 3 endpoints + an embed flag. Remove the screen and
gate the endpoints to disable. Journal app (V1) unaffected.
