# Dashboard — FastAPI + Next.js

## Arquitectura

Dos contenedores separados del bot:
- **api** (FastAPI, puerto 8000) — Lee PostgreSQL + Redis, endpoints read-only
- **web** (Next.js, puerto 3000) — Dashboard UI, se conecta al API

Si el dashboard crashea, el bot sigue operando normalmente.

## API — Endpoints

| Endpoint | Fuente | Devuelve |
|----------|--------|----------|
| `GET /api/health` | Redis ping + PG ping + env | Estado del sistema + `sandbox` boolean |
| `GET /api/market/{pair}` | Redis (candle, funding, OI) | Precio live, funding, OI |
| `GET /api/trades?status=&limit=50` | PostgreSQL trades | Lista de trades paginada |
| `GET /api/trades/{id}` | PG trades + ai_decisions | Detalle de trade con AI reasoning |
| `GET /api/ai/decisions?limit=20` | PG ai_decisions | Evaluaciones recientes de Claude |
| `GET /api/risk` | Redis + PG risk_events | DD, cooldown, eventos recientes. Filters `pending_entry` from open position count (only counts filled positions). |
| `GET /api/candles/{pair}/{tf}?count=100` | PG candles | OHLCV + volume_quote para sparklines y liquidation estimator |
| `GET /api/stats` | PG trades (closed) | Win rate, P&L, profit factor |
| `GET /api/whales?hours=24` | Redis (whale_movements) | Whale movements last N hours |
| `WS /api/ws` | Redis poll cada 2s | Precio live + posiciones |
| `GET /api/strategy/order-blocks` | Redis (`qf:bot:order_blocks`) | OBs activos (ambos pares, LTF) |
| `GET /api/strategy/htf-bias` | Redis (`qf:bot:htf_bias`) | HTF bias por par |
| `GET /api/sentiment` | Redis (`qf:bot:news:fear_greed`) | Fear & Greed score + label |
| `GET /api/headlines` | Redis (`qf:bot:news:headlines:{BTC,ETH}`) | Recent news headlines (CryptoCompare) |
| `POST /api/trades/{pair}/cancel` | Redis write (`qf:cancel_request:{pair}`) | Solicita cancelación de posición (TTL 60s) |
| `GET /api/liquidation/heatmap/{pair}` | PG candles + Redis OI + cache | Estimated liquidation levels (bins con long/short USD) |
| `POST /api/manual/calculate` | Pure math | Position sizing & R:R calculator (linear + inverse) |
| `POST /api/manual/trades` | PG manual_trades | Create manual trade (planned) |
| `GET /api/manual/trades` | PG manual_trades | List trades (filter by status/pair) |
| `GET /api/manual/trades/{id}` | PG manual_trades + partials | Trade detail + partial closes |
| `PATCH /api/manual/trades/{id}` | PG manual_trades | Update trade (status transitions, PnL auto-calc) |
| `DELETE /api/manual/trades/{id}` | PG manual_trades | Hard delete trade |
| `POST /api/manual/trades/{id}/partial-close` | PG manual_partial_closes | Record partial close (auto-closes at 100%) |
| `GET /api/manual/balances` | PG manual_balances | Per-pair balance tracking |
| `PUT /api/manual/balances/{pair}` | PG manual_balances | Set/update balance |
| `GET /api/manual/price/{pair}` | Redis candle cache | Current price (maps USD→USDT) |
| `GET /api/manual/analytics` | PG manual_trades + partials | Win rate, avg R, PnL, TP hit rates, breakdowns |
| `GET /manual` | HTML file | Manual trading UI (standalone page, no /api prefix) |

## Frontend — Layout

```
HEADER: Status dot + "QF" + LIVE/DEMO pill + F&G pill (colored) + UTC clock (time only)
├── BTC/USDT panel (gradient bg, HTF bias badge) | ETH/USDT panel (gradient bg) | Risk gauges (arcos con glow)
├── Open Positions (rich cards: TP2/TP3/leverage/AI confidence/time open/cancel) | Equity curve
├── Trade Log (tabla, hover rows) | AI Decision Log (mini-cards con confidence ring)
├── Active Order Blocks (full width)
├── Estimated Liquidation Levels (full width, canvas heatmap, BTC/ETH tabs, 30s polling)
├── Whale Movements Log (full width)
└── System Health: Redis + PG + API status dots
```

## Estilo — Apple-inspired (black/white + glassmorphism)

- Fondo negro puro (`#000000` / `#0a0a0a`), cards con `backdrop-filter: blur(20px)` y fondo semitransparente (`rgba(255,255,255,0.04)`)
- Borders sutiles: `rgba(255,255,255,0.08)`, border-radius 12px en cards
- Gap entre cards: 8px (antes 1px), padding exterior 8px
- Verde para longs/positivo (#10b981), rojo para shorts/negativo (#ef4444)
- Azul accent (#3b82f6), amarillo warnings (#f59e0b)
- Font monospace (JetBrains Mono / system fallback)
- Números right-aligned, tabular-nums
- LIVE/DEMO: pill badge con borde coloreado (verde=live, ámbar=demo)
- Badges: border-radius 100px (pill shape)
- Hover effects: cards y table rows cambian a `rgba(255,255,255,0.06)`

## Trade Persistence (Prerrequisito)

Para que el dashboard muestre datos, el bot ahora escribe a PostgreSQL:

- **`data_store.py`** — Métodos: `insert_trade()`, `update_trade()`, `insert_ai_decision()`, `insert_risk_event()`
- **`monitor.py`** — Entry fill → `insert_trade()`, position close → `update_trade()`
- **`main.py`** — AI evaluation → `insert_ai_decision()`, risk rejection → `insert_risk_event()`
- **`risk_service/service.py`** — Guardrail hit → `insert_risk_event()`
- **Redis** — `qf:bot:positions` → JSON de posiciones abiertas actuales
- **Redis** — `qf:bot:whale_movements` → JSON de whale movements (TTL 600s, actualizado cada poll de Etherscan)
- **Redis** — `qf:bot:order_blocks` → JSON de OBs activos (TTL 600s, actualizado en cada candle confirmada)
- **Redis** — `qf:bot:htf_bias` → JSON de HTF bias por par (TTL 600s, actualizado en cada candle confirmada)
- **Redis** — `qf:bot:news:fear_greed` → JSON `{score, label}` (TTL 1800s, actualizado cada 5 min por NewsClient)

## Docker

```yaml
# docker-compose.yml (nuevos servicios)
api:
  build: {context: ., dockerfile: dashboard/api/Dockerfile}
  network_mode: host
  depends_on: [postgres, redis]

web:
  build: {context: ./dashboard/web, args: {NEXT_PUBLIC_API_URL: "http://192.168.1.238:8000"}}
  network_mode: host
  depends_on: [api]
```

## Archivos

```
dashboard/
├── __init__.py
├── api/
│   ├── __init__.py
│   ├── main.py          # FastAPI app, CORS, lifespan
│   ├── database.py      # asyncpg pool + redis.asyncio
│   ├── models.py        # Pydantic response schemas
│   ├── queries.py       # SQL queries centralizadas
│   ├── routes/
│   │   ├── health.py    # GET /api/health
│   │   ├── market.py    # GET /api/market/{pair}
│   │   ├── trades.py    # GET /api/trades
│   │   ├── ai.py        # GET /api/ai/decisions
│   │   ├── risk.py      # GET /api/risk
│   │   ├── candles.py   # GET /api/candles/{pair}/{tf}
│   │   ├── stats.py     # GET /api/stats
│   │   ├── whales.py    # GET /api/whales
│   │   ├── strategy.py     # GET /api/strategy/order-blocks, /api/strategy/htf-bias
│   │   ├── sentiment.py    # GET /api/sentiment
│   │   ├── liquidation.py  # GET /api/liquidation/heatmap/{pair}
│   │   └── manual_routes.py # Manual trading API + HTML page
│   ├── manual/
│   │   ├── calculator.py  # Position sizing math (linear + inverse), no external deps
│   │   ├── trade_manager.py # CRUD, partial closes, balance tracking (asyncpg)
│   │   ├── analytics.py   # Win rate, R multiples, TP hit rates, breakdowns
│   │   └── schema.sql     # CREATE TABLE for manual_trades, manual_partial_closes, manual_balances
│   ├── templates/
│   │   └── manual.html    # Standalone manual trading UI
│   ├── ws.py            # WS /api/ws
│   ├── requirements.txt
│   └── Dockerfile
└── web/
    ├── src/
    │   ├── app/          # Next.js app router (/ = bot dashboard, /manual = manual trading)
    │   ├── components/   # 13 bot components + 5 manual components (manual/ subdir)
    │   └── lib/          # API client, hooks, types
    ├── package.json
    ├── Dockerfile
    └── next.config.ts
```

## Responsive — Mobile/Tablet

CSS-first approach con 2 breakpoints en `globals.css`:

- **Tablet (≤1023px):** Grid 2 columnas, sidebar items (risk, equity, AI log) pasan a full-width
- **Mobile (≤639px):** Grid 1 columna, header wrap centrado, precios font reducido (28→22px), position cards 2×2, tablas scroll horizontal, columnas de baja prioridad ocultas (Type/P&L$/Exit en TradeLog, Significance/wallet addr en WhaleLog, Range/VolRatio en OrderBlockPanel), health grid wrap

Clases CSS añadidas a componentes para permitir override de inline styles via `!important`:
- `header-inner` (Header), `price-value` (PricePanel), `position-grid` (PositionCard), `health-inner` (HealthGrid)
- `col-type`, `col-pnl-usd`, `col-exit` (TradeLog), `col-sig`, `wallet-addr` (WhaleLog), `col-range`, `col-vol` (OrderBlockPanel)

## Cancel desde Dashboard

Mecanismo seguro y desacoplado:
1. Dashboard API escribe `qf:cancel_request:{pair}` en Redis con TTL 60s
2. El PositionMonitor del bot verifica cancel requests en cada poll cycle (antes de procesar cada posición)
3. Si encuentra uno, consume la key y ejecuta:
   - Pending entry → cancela orden de entrada
   - Active position → cancela SL/TPs + market close
4. Dashboard no habla directamente con OKX

**Redis key:** `qf:cancel_request:{pair}` (TTL 60s, consumida al leer)
**Backend:** `POST /api/trades/{pair}/cancel` → `queries.set_cancel_request()`
**Bot:** `monitor._check_cancel_request()` → `redis.pop_cancel_request()`

## PositionCard — Redesign

Cada posición muestra:
- Row 1: Pair + direction badge + setup type + phase + time open (e.g. "2h 14m")
- Row 2: P&L % (grande) + P&L USD estimado
- Row 3: 6-col grid — Entry, SL (rojo), TP1, TP2, TP3 (verde), Leverage
- Row 4: AI Confidence bar + botón Cancel (rojo, con diálogo de confirmación inline)

Mobile: 6-col grid → 3-col. Cancel button full width. Footer stacks.

## AILog — Redesign

- Mini-cards (no flat list items)
- Confidence ring SVG (círculo con porcentaje)
- Reasoning expandible (click para ver texto completo)
- Setup type badge visible
- Warnings como pills coloreados
- Empty state: "No AI evaluations yet — decisions appear when the bot detects a setup"

## Fear & Greed Pill

Pill en el Header que muestra el Fear & Greed Index en tiempo real:
- **Componente:** `FearGreedPill.tsx` — polls `GET /api/sentiment` cada 60s
- **API:** `sentiment.py` — lee `qf:bot:news:fear_greed` de Redis
- **Colores:** Rojo (0-25 Extreme Fear/Fear), naranja (26-45 Fear), gris (46-55 Neutral), verde-amarillo (56-75 Greed), verde (76-100 Extreme Greed)
- **Formato:** `F&G: 23` con tooltip completo ("Fear & Greed: 23/100 (Extreme Fear)")
- **Graceful:** Si no hay datos en Redis → no renderiza (returns null)
- **Mobile:** Pill compacto, no wrap

## Liquidation Heatmap

Estimated liquidation level chart — DIY approximation of Coinglass-style heatmap using OI + candle data.

**Backend:** `data_service/liquidation_estimator.py`
- Takes last 200 5m candles + current OI in USD
- Projects liquidation prices for 5 leverage tiers (5x/10x/25x/50x/100x) with industry-average weights (0.30/0.30/0.20/0.15/0.05)
- OI distributed across candles weighted by `volume_quote` (not uniform)
- Bins: $50 for BTC, $2 for ETH, $0.50 for SOL, $0.002 for DOGE (configurable via `LIQ_BIN_SIZE_*`)
- Result cached in Redis (`qf:liq_heatmap:{pair}`, TTL 30s via `LIQ_CACHE_TTL`)

**API:** `GET /api/liquidation/heatmap/{pair}` -> `LiqHeatmapResponse {pair, current_price, bins[]}`

**Frontend:** `LiquidationHeatmap.tsx`
- Canvas-based horizontal bar chart (no new dependencies)
- Y-axis: price, X-axis: estimated USD
- Long liquidations (red) extend left from center, short (green) extend right
- Dashed blue line for current price
- BTC/ETH/SOL/DOGE tab selector
- 30s polling via `usePolling`
- `devicePixelRatio` scaling for retina
- Mobile: 200px height (vs 300px desktop)

**Limitations vs Coinglass:** Assumed leverage distribution (not real), OKX only, candle close as entry proxy, snapshot only (no time dimension). Labeled "Estimated Liquidation Levels" to be transparent.

**Settings:** `LIQ_CANDLE_COUNT` (200), `LIQ_BIN_SIZE_BTC` (50), `LIQ_BIN_SIZE_ETH` (2), `LIQ_BIN_SIZE_SOL` (0.5), `LIQ_BIN_SIZE_DOGE` (0.002), `LIQ_CACHE_TTL` (30)

## Manual Trading Module

Módulo independiente para trades manuales — completamente separado del bot automático. No importa strategy/risk/execution services.

**Margin types:** Linear (USDT-margined, size en base asset) e Inverse (coin-margined, size en USD contracts). Calculator soporta ambos con PnL correcto.

**TP strategy:** 50/50 split automático. TP1 cierra 50% + mueve SL a breakeven. TP2 cierra el resto. Si no se proveen TPs, sugiere 1R y 2R automáticamente.

**Status flow:** `planned` → `active` (activated_at auto) → `closed` (closed_at auto, PnL auto-calc, balance auto-update)

**Partial closes:** Registra cierres parciales con porcentaje. Auto-cierra trade si total >= 100%. Auto-actualiza balance del par con PnL.

**Analytics:** Win rate, avg R multiple, total PnL, TP1/TP2 hit rates, breakdowns by pair/setup/direction, streak tracking.

**Tablas PostgreSQL:** `manual_trades`, `manual_partial_closes`, `manual_balances` — schema en `dashboard/api/manual/schema.sql`.

**Next.js dashboard (`/manual`):** Página dedicada con 5 componentes: ManualStats (balance, PnL, WR, streak), QuickCalculator (sizing + crear trade), ActiveTrades (cards con PnL live cada 10s, progreso TP1, botón close), TradeHistory (tabla expandible con thesis/mistakes/partials), ManualAnalytics (WR, avg R, profit factor, breakdown por par/dirección). Header con nav Bot/Manual.

**Validaciones:** Pair format regex en price endpoint (previene Redis key injection), leverage >= 1 (Pydantic Field).

## Bugs Conocidos (resueltos)

- **`queries.py` — `db.db.pg_pool`**: La función `get_trades()` usaba `db.db.pg_pool` en vez de `db.pg_pool`, causando `AttributeError` en cada request a `/api/trades`. Las demás queries (`get_trade_by_id`, `get_ai_decisions`, etc.) usaban `db.pg_pool` correctamente. Corregido.

## Limitaciones v1

- Sin charting library (TradingView, etc.) — sparklines SVG
- Sin modificación de SL/TP desde el dashboard — solo cancel completo
- Sin autenticación — localhost detrás del router
- Sin backtesting UI o alertas en el dashboard (notificaciones push via Telegram — `shared/notifier.py`)
