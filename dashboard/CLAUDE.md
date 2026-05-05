# Dashboard — CLAUDE.md

Operational rules for Claude when modifying `dashboard/`. Read-only on bot state — if dashboard crashes, bot keeps running.

## Purpose
Two services (separate from bot):
- `dashboard/api/` — FastAPI on :8000. Reads PostgreSQL + Redis. Read-only on bot data; CRUD on `manual_trades`/`manual_balances` (manual trading module is self-contained)
- `dashboard/web/` — Next.js on :3000. Apple-inspired dark UI. Tailscale-only access

## Source of truth (read before editing)
- **Detailed behavior:** `docs/context/06-dashboard.md` (Spanish, deep — endpoints, layout, components, manual trading, liquidation heatmap)
- **API auth:** `docs/OPERATIONS.md` §7 — Bearer token on write endpoints
- **Mobile rules:** root `CLAUDE.md` §"Mobile Responsiveness" — non-negotiable
- **Security model:** `docs/OPERATIONS.md` §1 — Tailscale mesh, no internet exposure

## Files
```
dashboard/
├── api/
│   ├── main.py              # FastAPI app, CORS, lifespan
│   ├── database.py          # asyncpg pool + redis.asyncio
│   ├── models.py            # Pydantic response schemas
│   ├── queries.py           # SQL queries (centralized)
│   ├── ws.py                # WS /api/ws (price + positions, 2s poll)
│   ├── routes/              # health, market, trades, ai, risk, candles, stats,
│   │                        # whales, strategy, sentiment, liquidation, manual_routes
│   ├── manual/
│   │   ├── calculator.py    # Position sizing math (linear + inverse), no external deps
│   │   ├── trade_manager.py # CRUD, partial closes, balance tracking (asyncpg)
│   │   ├── analytics.py     # WR, R multiples, TP hit rates, breakdowns
│   │   └── schema.sql       # manual_trades, manual_partial_closes, manual_balances
│   └── templates/manual.html # Standalone manual trading UI (no /api prefix)
└── web/
    ├── src/app/             # Next.js app router (/ = bot, /manual = manual trading)
    ├── src/components/      # 13 bot + 5 manual (manual/ subdir)
    └── src/lib/             # API client, hooks, types
```

## Rules — API endpoints
1. **Read-only on bot state.** `GET /api/trades`, `GET /api/risk`, `GET /api/strategy/*` etc. NEVER mutate bot tables (`trades`, `ai_decisions`, `risk_events`, `ml_setups`) from the API.
2. **Mutating endpoints (POST/PATCH/DELETE/PUT) require Bearer auth.** Use `Authorization: Bearer <DASHBOARD_API_KEY>`. Read endpoints are open behind Tailscale.
3. **Cancel from dashboard is decoupled.** `POST /api/trades/{pair}/cancel` writes `qf:cancel_request:{pair}` to Redis (TTL 60s). Bot's `PositionMonitor._check_cancel_request()` consumes it. **Dashboard never talks to OKX directly.**
4. **Pair format validation is mandatory** on any path/query that hits Redis. Regex must reject anything outside `[A-Z]+/[A-Z]+`. Past bug: Redis key injection via crafted pair.
5. **`get_trades()` and friends use `db.pg_pool`, NOT `db.db.pg_pool`.** Past bug: `AttributeError` on every request. Do not regress.
6. **Risk endpoint filters `pending_entry`** from open position count. Only filled positions count as "open" for the dashboard gauge.

## Rules — manual trading module
1. **Self-contained.** `dashboard/api/manual/` does NOT import strategy/risk/execution services. It is a separate accounting tool.
2. **Tables are separate:** `manual_trades`, `manual_partial_closes`, `manual_balances`. Schema in `manual/schema.sql`. Never write to these from the bot.
3. **Linear vs Inverse margin types.** Calculator must support both. PnL formula differs.
4. **Status flow:** `planned` → `active` (sets `activated_at`) → `closed` (sets `closed_at`, auto-calc PnL, auto-update balance).
5. **Partial closes auto-close at 100%** total. Auto-update balance with PnL.
6. **TP defaults to 50/50 split** at TP1/TP2. If no TPs provided, suggest 1R / 2R.
7. **Authoritative key for write ops** is `DASHBOARD_API_KEY` from `.env`. Frontend reads `NEXT_PUBLIC_DASHBOARD_API_KEY` from `.env.local`. Manual HTML uses `?key=` URL param → localStorage.

## Rules — frontend
1. **MOBILE RESPONSIVE IS MANDATORY.** Test at 375px (iPhone SE). Nothing overflows or breaks. Two breakpoints in `globals.css`: tablet ≤1023px (2-col grid), mobile ≤639px (1-col).
2. **Use CSS classes for responsive overrides**, not inline styles. Responsive `!important` overrides target classes like `header-inner`, `price-value`, `position-grid`, `col-type`, `col-pnl-usd`, `col-exit`, `col-sig`, `wallet-addr`, `col-range`, `col-vol`.
3. **Hide low-priority columns on mobile** via `display: none` classes. Tables scroll horizontally with `.scroll-y`.
4. **Apple-inspired dark UI.** Black bg, glassmorphism cards (`backdrop-filter: blur(20px)`, `rgba(255,255,255,0.04)`). Border-radius 12px on cards, 100px on pills. Green/red for long/short, blue accent, amber warnings. JetBrains Mono / system mono.
5. **Numbers right-aligned with `tabular-nums`.** Always.
6. **Polling intervals:** market 2s (WS), liquidation heatmap 30s, sentiment 60s. Do not poll faster — Redis is shared with the bot.
7. **Empty states are required.** Never show a blank panel. "No AI evaluations yet — decisions appear when the bot detects a setup."

## Rules — Redis dashboard keys
| Key | Purpose | Writer | TTL |
|---|---|---|---|
| `qf:bot:positions` | Open positions JSON | bot monitor | live |
| `qf:bot:whale_movements` | Whale events | data service | 600s |
| `qf:bot:order_blocks` | Active OBs | strategy service | 600s |
| `qf:bot:htf_bias` | HTF bias per pair | strategy service | 600s |
| `qf:bot:news:fear_greed` | F&G score+label | data service | 1800s |
| `qf:bot:news:headlines:{BTC,ETH}` | Headlines | data service | 300s |
| `qf:liq_heatmap:{pair}` | Heatmap cache | dashboard API | 30s |
| `qf:cancel_request:{pair}` | Cancel from UI | dashboard API | 60s, consumed by bot |

Dashboard reads from these keys. **Never write to `qf:bot:*` from the dashboard** — those are bot-owned.

## Never
- Mutate `trades`, `ai_decisions`, `risk_events`, `ml_setups`, `campaigns` from the API. Bot owns those.
- Talk to OKX directly from the dashboard. Cancel goes through Redis → bot.
- Add inline styles for responsive layout. Use CSS classes.
- Skip Pair format validation on Redis-bound endpoints.
- Expose write endpoints without Bearer auth.
- Add a charting library (TradingView etc.) without checking bundle size impact. Sparklines stay SVG.

## Verify after changes
```bash
# Backend
python -m pytest tests/test_manual_trading.py -v --tb=short

# Frontend (build check + manual mobile test)
cd dashboard/web && npm run build
# Open in browser, resize to 375px, verify nothing overflows on /, /manual
```

For UI changes, **start the dev server and use the feature in a browser** before reporting done. Type checking ≠ feature correctness.
