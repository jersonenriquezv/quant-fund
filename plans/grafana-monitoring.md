# Grafana + PostgreSQL — Monitoreo Operacional

**Status:** Pendiente
**Prioridad:** Media
**Esfuerzo:** ~6-8 horas

## What

Agregar Grafana como capa de monitoreo operacional. Grafana lee directamente de PostgreSQL (ya existente) y una nueva tabla `bot_metrics` donde el bot escribe metricas de runtime. Sin Prometheus -- PostgreSQL ya existe y el volumen de datos es bajo.

## Why

El dashboard Next.js muestra estado actual (posiciones, ultimo trade, health dots) pero NO cubre:
- **Historico temporal**: como cambia la latencia del pipeline, uptime del WebSocket, fill rate a lo largo de dias/semanas
- **Correlaciones**: slippage vs volatilidad, AI approval rate vs win rate por periodo
- **Performance del bot como sistema**: latencia de OKX API, Claude API, tiempos de respuesta
- **Tendencias**: equity curve, win rate rolling, drawdown over time

Grafana es la herramienta estandar para esto. Ya tenemos PostgreSQL con datos historicos.

## Current State (verificado leyendo codigo)

### Lo que Grafana puede consumir directamente (ya existe):

1. **Tabla `trades`** -- `opened_at`, `closed_at`, `pnl_usd`, `pnl_pct`, `exit_reason`, `actual_entry`, `entry_price` (slippage), `ai_confidence`, `setup_type`, `pair`, `direction`
2. **Tabla `ai_decisions`** -- `confidence`, `approved`, `created_at`, `pair`, `direction`, `setup_type`
3. **Tabla `risk_events`** -- `event_type`, `details` (JSONB), `created_at`
4. **Tabla `candles`** -- OHLCV historico

### Lo que NO existe y hay que crear:

- **Metricas operacionales** (pipeline latency, Claude latency, OKX latency, WS reconnections, health status)
- **Grafana** como container Docker
- **Dashboards provisioned**

### Recursos del servidor:
- 12GB RAM disponibles (Grafana usa ~200MB)
- 75GB disco libres
- 5 containers corriendo (bot, api, web, postgres, redis)
- `network_mode: host` en bot/api/web

## Steps

### Paso 1: Crear tabla `bot_metrics` en PostgreSQL → `data_service/data_store.py`

```sql
CREATE TABLE IF NOT EXISTS bot_metrics (
    id SERIAL PRIMARY KEY,
    metric_name VARCHAR(50) NOT NULL,
    value DOUBLE PRECISION NOT NULL,
    pair VARCHAR(20),
    labels JSONB,
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_metrics_name_time ON bot_metrics(metric_name, created_at DESC);
```

Agregar metodo `insert_metric(name, value, pair=None, labels=None)` a `PostgresStore`.

**Done when:** La tabla existe y tiene indices.

### Paso 2: Instrumentar el pipeline con metricas

| metric_name | Donde se mide | Frecuencia |
|---|---|---|
| `pipeline_latency_ms` | `main.py:on_candle_confirmed` | Cada candle (~5min) |
| `claude_latency_ms` | `main.py:_evaluate_with_claude` | Cada evaluacion |
| `okx_order_latency_ms` | `executor.py` (place/fetch) | Cada llamada OKX |
| `ws_reconnection` | `websocket_feeds.py:start()` | Cada reconexion |
| `health_status` | `service.py:_health_check_loop` | Cada 30s |

Patron: `time.monotonic()` antes/despues, luego `postgres.insert_metric(...)`. Fire-and-forget.

**Done when:** El bot escribe metricas durante operacion normal.

### Paso 3: Agregar Grafana a docker-compose.yml

```yaml
grafana:
  image: grafana/grafana-oss:11.5-alpine
  restart: unless-stopped
  network_mode: host
  environment:
    GF_SERVER_HTTP_PORT: "3001"
    GF_SECURITY_ADMIN_USER: admin
    GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_ADMIN_PASSWORD:-admin}
    GF_AUTH_ANONYMOUS_ENABLED: "true"
    GF_AUTH_ANONYMOUS_ORG_ROLE: Viewer
  volumes:
    - grafana_data:/var/lib/grafana
    - ./monitoring/provisioning:/etc/grafana/provisioning:ro
    - ./monitoring/dashboards:/var/lib/grafana/dashboards:ro
```

Puerto 3001 (3000 es Next.js). Anonymous viewer (localhost, detras del router).

**Done when:** `docker compose up -d` levanta Grafana en :3001.

### Paso 4: Provisioning de datasource → `monitoring/provisioning/datasources/postgres.yml`

```yaml
apiVersion: 1
datasources:
  - name: PostgreSQL
    type: postgres
    url: localhost:5432
    database: quant_fund
    user: jer
    secureJsonData:
      password: ${POSTGRES_PASSWORD}
    jsonData:
      sslmode: disable
    isDefault: true
```

**Done when:** Grafana se conecta a PostgreSQL automaticamente.

### Paso 5: Crear dashboards provisioned → `monitoring/dashboards/`

**Dashboard 1: Trading Performance**
- Equity curve (SUM pnl_usd OVER TIME)
- Win rate rolling 7 dias
- Slippage promedio por par
- Exit reason breakdown (pie chart)
- Setup type win rate comparison
- AI confidence vs outcome (scatter)
- Trades per day

**Dashboard 2: System Health**
- Pipeline latency over time
- Claude API latency over time
- OKX API latency over time
- WebSocket reconnections timeline
- Health status grid (uptime % last 24h)
- Candles received per hour

**Dashboard 3: AI & Risk Analytics**
- AI approval rate over time
- Confidence distribution histogram
- Risk events timeline
- Guardrail trigger frequency

**Done when:** 3 dashboards visibles al abrir Grafana.

### Paso 6: Documentar → `docs/context/`

**Done when:** Docs actualizados con nueva infra.

## Estructura de archivos nuevos

```
monitoring/
├── provisioning/
│   ├── datasources/
│   │   └── postgres.yml
│   └── dashboards/
│       └── default.yml
└── dashboards/
    ├── trading-performance.json
    ├── system-health.json
    └── ai-risk-analytics.json
```

## Risks

| Riesgo | Impacto | Mitigacion |
|--------|---------|------------|
| **PostgreSQL load** por metricas | Bajo -- ~12 rows/min | Retention policy: DELETE bot_metrics older than 30 days |
| **Grafana memory** con queries pesadas | Bajo -- 12GB disponibles | Limitar `max_data_points` a 1000 por panel |
| **Puerto 3001 en host network** | Bajo -- red local | Mismo modelo que dashboard (3000) y API (8000) |

## Out of Scope

- **Prometheus** -- Agrega container + time-series DB separado. Para <20 metricas, PostgreSQL es suficiente
- **Alerting via Grafana** -- Las alertas criticas ya van por Telegram
- **Loki (logs)** -- Los logs ya estan en archivos rotatados. Fase futura
- **Node Exporter / cAdvisor** -- Metricas de CPU/RAM/Docker. Se puede agregar despues en 10 minutos
- **Modificar dashboard Next.js** -- Grafana complementa, no reemplaza
