# Dashboard — FastAPI + Next.js

## Arquitectura

Dos contenedores separados del bot:
- **api** (FastAPI, puerto 8000) — Lee PostgreSQL + Redis, endpoints read-only
- **web** (Next.js, puerto 3000) — Dashboard UI, se conecta al API

Si el dashboard crashea, el bot sigue operando normalmente.

## API — Endpoints

| Endpoint | Fuente | Devuelve |
|----------|--------|----------|
| `GET /api/health` | Redis ping + PG ping | Estado del sistema |
| `GET /api/market/{pair}` | Redis (candle, funding, OI) | Precio live, funding, OI |
| `GET /api/trades?status=&limit=50` | PostgreSQL trades | Lista de trades paginada |
| `GET /api/trades/{id}` | PG trades + ai_decisions | Detalle de trade con AI reasoning |
| `GET /api/ai/decisions?limit=20` | PG ai_decisions | Evaluaciones recientes de Claude |
| `GET /api/risk` | Redis + PG risk_events | DD, cooldown, eventos recientes |
| `GET /api/candles/{pair}/{tf}?count=100` | PG candles | OHLCV para sparklines |
| `GET /api/stats` | PG trades (closed) | Win rate, P&L, profit factor |
| `GET /api/whales?hours=24` | Redis (whale_movements) | Whale movements last N hours |
| `WS /api/ws` | Redis poll cada 2s | Precio live + posiciones |
| `GET /api/profile` | Redis + definiciones locales | Perfil activo + perfiles disponibles |
| `POST /api/profile` | Redis write | Cambiar perfil de estrategia |

## Frontend — Layout

```
HEADER: Bot status + Mode (DEMO) + Profile Selector (dropdown) + UTC clock
├── BTC/USDT panel | ETH/USDT panel | Risk gauges (DD arcos)
├── Open Positions (cards)        | Equity curve (SVG sparkline)
├── Trade Log (tabla, últimos 20) | AI Decision Log (barras de confianza)
├── Whale Movements Log (full width, últimas 24h, 4 badge types: deposit/withdrawal/transfer out/transfer in)
└── System Health: Redis + PG + API status dots
```

## Estilo — "VAULT"

- Fondo oscuro (#0a0e17), borders 1px, sin sombras
- Verde para longs/positivo (#10b981), rojo para shorts/negativo (#ef4444)
- Azul accent (#3b82f6), amarillo warnings (#f59e0b)
- Font monospace (JetBrains Mono / system fallback)
- Números right-aligned, tabular-nums
- Demo mode: banner ámbar

## Trade Persistence (Prerrequisito)

Para que el dashboard muestre datos, el bot ahora escribe a PostgreSQL:

- **`data_store.py`** — Métodos: `insert_trade()`, `update_trade()`, `insert_ai_decision()`, `insert_risk_event()`
- **`monitor.py`** — Entry fill → `insert_trade()`, position close → `update_trade()`
- **`main.py`** — AI evaluation → `insert_ai_decision()`, risk rejection → `insert_risk_event()`
- **`risk_service/service.py`** — Guardrail hit → `insert_risk_event()`
- **Redis** — `qf:bot:positions` → JSON de posiciones abiertas actuales
- **Redis** — `qf:bot:whale_movements` → JSON de whale movements (TTL 600s, actualizado cada poll de Etherscan)

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
│   │   └── profile.py   # GET/POST /api/profile
│   ├── ws.py            # WS /api/ws
│   ├── requirements.txt
│   └── Dockerfile
└── web/
    ├── src/
    │   ├── app/          # Next.js app router
    │   ├── components/   # 10 componentes UI (incl. ProfileSelector)
    │   └── lib/          # API client, hooks
    ├── package.json
    ├── Dockerfile
    └── next.config.ts
```

## Responsive — Mobile/Tablet

CSS-first approach con 2 breakpoints en `globals.css`:

- **Tablet (≤1023px):** Grid 2 columnas, sidebar items (risk, equity, AI log) pasan a full-width
- **Mobile (≤639px):** Grid 1 columna, header wrap centrado, precios font reducido (28→22px), position cards 2×2, tablas scroll horizontal, columnas de baja prioridad ocultas (Type/P&L$/Exit en TradeLog, Significance/wallet addr en WhaleLog), health grid wrap

Clases CSS añadidas a componentes para permitir override de inline styles via `!important`:
- `header-inner` (Header), `price-value` (PricePanel), `position-grid` (PositionCard), `health-inner` (HealthGrid)
- `col-type`, `col-pnl-usd`, `col-exit` (TradeLog), `col-sig`, `wallet-addr` (WhaleLog)

## Profile Selector

El dashboard incluye un dropdown para cambiar el perfil de estrategia del bot en tiempo real:
- **GET /api/profile** — devuelve perfil activo + lista de perfiles disponibles con label, description, color
- **POST /api/profile** — escribe nuevo perfil a Redis (`qf:bot:strategy_profile`)
- El bot lee el perfil desde Redis al inicio de cada pipeline cycle (`main.py: _sync_profile_from_redis()`)
- **CORS** actualizado a `GET + POST` (antes solo GET)
- Color indicators: verde (default), amarillo (aggressive), rojo (scalping)
- Warning badge pulsa cuando no está en default

## Limitaciones v1

- Sin charting library (TradingView, etc.) — sparklines SVG
- Sin ejecución de trades desde el dashboard — mayormente read-only (excepto profile switch)
- Sin autenticación — localhost detrás del router
- Sin backtesting UI o alertas en el dashboard (notificaciones push via Telegram — `shared/notifier.py`)
