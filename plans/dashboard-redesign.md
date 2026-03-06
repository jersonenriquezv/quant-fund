## What

Visual overhaul of the dashboard — Apple-inspired design (black/white, glassmorphism, transparency). Richer position cards with cancel functionality. AI decisions log with meaningful data.

## Why

Current dashboard is functional but basic terminal-style. Missing key info on open trades (leverage, AI confidence, TP2/TP3, time open). No way to cancel trades from UI. AI decision log shows raw data without actionable context.

## Current State

VERIFIED by reading all 14 frontend components + API + CSS:
- **Colors**: Navy theme (`#0a0e17`, `#111827`, `#151c2c`) — not true black/white
- **PositionCard**: Shows pair, direction, P&L%, entry, SL, TP1, size. Missing: TP2/TP3, leverage, AI confidence, setup type, time open. No cancel button
- **AILog**: Component exists, renders decisions from `/ai/decisions`. Shows time, pair, direction, approved/rejected badge, confidence bar, reasoning (truncated 40px), warnings. But user says empty — likely no AI decisions in DB yet + UI doesn't surface enough when data exists
- **TradeLog**: Basic table — time, pair, direction, type, entry, P&L, exit reason, status
- **Cancel**: Dashboard is 100% read-only. No POST/DELETE for trades. Monitor has `_close_all_orders_and_market_close()` but no API endpoint exposes it
- **Responsive**: 3 breakpoints work (desktop 3-col, tablet 2-col, mobile 1-col)
- **WebSocket**: Sends positions + prices every 2s from Redis cache

## Steps

### 1. Apple-inspired color scheme + glassmorphism → `globals.css`
**Done when**: Colors are true black (`#000`/`#0a0a0a`) and white, cards use `backdrop-filter: blur()` with semi-transparent backgrounds, subtle borders with `rgba(255,255,255,0.06)`, no navy tones remaining.

New CSS variables:
```css
--bg-primary: #000000;
--bg-secondary: #0a0a0a;
--bg-card: rgba(255, 255, 255, 0.04);
--bg-card-hover: rgba(255, 255, 255, 0.06);
--border: rgba(255, 255, 255, 0.08);
--text-primary: #f5f5f7;
--text-secondary: rgba(255, 255, 255, 0.6);
--text-muted: rgba(255, 255, 255, 0.35);
```

Card class gets:
```css
.card {
  backdrop-filter: blur(20px);
  -webkit-backdrop-filter: blur(20px);
  border-radius: 12px;
  border: 1px solid rgba(255,255,255,0.08);
}
```

Gap between cards: `1px` → `8px` (breathing room).
Font stays monospace (fits the quant terminal vibe).

### 2. Richer PositionCard → `components/PositionCard.tsx`
**Done when**: Open positions show ALL this info in a clean layout:

Row 1: Pair + direction badge + setup type + phase label + **time open** (e.g. "2h 14m")
Row 2: P&L % (large) + P&L USD estimate
Row 3: 6-column grid — Entry, SL (red), TP1, TP2, TP3 (green), Leverage
Row 4: AI Confidence bar (same style as AILog) + **CANCEL button** (red, with confirm)

Mobile: 6-col grid → 3-col (2 rows). Cancel button full width.

Also needs: `PositionData` type already has `tp2_price`, `tp3_price`, `leverage`, `ai_confidence`, `created_at` — all available from WebSocket, just not rendered.

### 3. Cancel trade endpoint → `dashboard/api/routes/trades.py` + `dashboard/api/queries.py`
**Done when**: `POST /api/trades/{pair}/cancel` exists and writes a cancel request to Redis that the bot's monitor reads.

**Mechanism** (safe, decoupled):
- Dashboard API writes `qf:cancel_request:{pair}` key in Redis with TTL 60s
- Bot's PositionMonitor checks for cancel requests each poll cycle
- On cancel request: calls `_close_all_orders_and_market_close(pos)` (already exists)
- Dashboard doesn't talk directly to OKX — it requests, bot executes

New files/changes:
- `dashboard/api/routes/trades.py`: Add `POST /trades/{pair}/cancel`
- `dashboard/api/queries.py`: Add `set_cancel_request(pair)` + `get_cancel_request(pair)`
- `execution_service/monitor.py`: In `_check_all_positions()`, check Redis for cancel request before processing each position
- `dashboard/api/main.py`: Allow `DELETE` method in CORS

### 4. Cancel button UI → `components/PositionCard.tsx`
**Done when**: Red "Cancel" button on each position card. Click shows inline confirm ("Cancel this trade? [Yes] [No]"). On confirm, POSTs to API. Shows "Cancelling..." state. Position disappears on next WS update.

### 5. Better AI decision display → `components/AILog.tsx`
**Done when**:
- Each decision is a mini-card (not a flat list item)
- Confidence shown as a colored circle/ring (not just a bar)
- Reasoning is expandable (click to show full text, not truncated)
- Setup type badge visible (setup_a/setup_b)
- If approved: show entry price from linked trade
- Warnings as colored pills (not pipe-separated text)
- Empty state: helpful message "No AI evaluations yet — decisions appear when the bot detects a setup"

### 6. Header + component polish → all components
**Done when**:
- Header: cleaner layout, LIVE/DEMO badge redesigned as pill, clock smaller
- PricePanel: subtle gradient background based on change direction
- RiskGauge: arc gauges use Apple-style colors (white fill, subtle glow)
- TradeLog: row hover effect, alternating subtle backgrounds
- Tables: slightly more padding, cleaner borders
- All inline styles that affect layout moved to CSS classes (mobile-responsive)

### 7. Responsive verification → all components
**Done when**: Every change tested at 375px (iPhone SE). No overflow, no unusable elements. Cancel button works on mobile. AI cards stack properly. 6-col position grid becomes 3-col or 2-col.

## Risks

| Risk | Mitigation |
|------|-----------|
| Cancel request race condition (bot processes during cancel) | Redis TTL 60s + monitor checks before each position check. At worst, cancel is ignored if position just closed naturally |
| Cancel on filled position causes market close | Expected behavior — same as timeout close. SL/TP are cancelled, remaining closed at market |
| Glassmorphism not supported (old browsers) | Fallback: solid `--bg-card` if `backdrop-filter` not supported. Bot runs on local network, modern browsers only |
| Cancel endpoint security | Same network (192.168.1.x). No auth needed for now. If exposed externally later, add API key header |

## Out of Scope

- **Modifying SL/TP from dashboard** — too risky for V1. Cancel is sufficient for emergency control
- **Manual trade placement** — the bot should decide, not the user
- **Real-time P&L in USD on positions** — would need current price × size calculation. Can add later
- **Dark/light mode toggle** — user wants black. Done
- **Charts/candlestick view** — separate feature, much larger scope
