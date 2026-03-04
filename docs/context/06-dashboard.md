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
| `WS /api/ws` | Redis poll cada 2s | Precio live + posiciones |

## Frontend — Layout

```
HEADER: Bot status + Mode (DEMO) + UTC clock
├── BTC/USDT panel | ETH/USDT panel | Risk gauges (DD arcos)
├── Open Positions (cards)        | Equity curve (SVG sparkline)
├── Trade Log (tabla, últimos 20) | AI Decision Log (barras de confianza)
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
│   │   └── stats.py     # GET /api/stats
│   ├── ws.py            # WS /api/ws
│   ├── requirements.txt
│   └── Dockerfile
└── web/
    ├── src/
    │   ├── app/          # Next.js app router
    │   ├── components/   # 8 componentes UI
    │   └── lib/          # API client, hooks
    ├── package.json
    ├── Dockerfile
    └── next.config.ts
```

## Limitaciones v1

- Sin charting library (TradingView, etc.) — sparklines SVG
- Sin ejecución de trades desde el dashboard — read-only
- Sin autenticación — localhost detrás del router
- Sin responsive mobile — 1080p+
- Sin backtesting UI o alertas en el dashboard (notificaciones push via Telegram — `shared/notifier.py`)
