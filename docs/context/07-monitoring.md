# Monitoring (Grafana + bot_metrics)
> Última actualización: 2026-03-10
> Estado: **Implementado**. Grafana en puerto 3001, 3 dashboards provisioned, 5 métricas operacionales.

## Qué hace
Grafana lee directamente de PostgreSQL (existente) y muestra dashboards de rendimiento de trading, salud del sistema, y analítica de AI/Risk. Sin Prometheus — PostgreSQL ya existe y el volumen de datos es bajo (~12 rows/min).

## Por qué existe
El dashboard Next.js muestra estado actual (posiciones, último trade, health dots) pero NO cubre histórico temporal (latencia, uptime, win rate rolling), correlaciones (slippage vs volatilidad, AI confidence vs outcome), ni tendencias (equity curve, drawdown over time).

## Infraestructura

### Container Docker
```yaml
grafana:
  image: grafana/grafana-oss:latest
  network_mode: host
  port: 3001
  anonymous viewer: enabled (red local)
```

Depende de `postgres` (healthcheck). Volume `grafana_data` persiste configuración.

### Estructura de archivos
```
monitoring/
├── provisioning/
│   ├── datasources/
│   │   └── postgres.yml       # Auto-conecta a quant_fund DB
│   └── dashboards/
│       └── default.yml        # Carga JSON desde /var/lib/grafana/dashboards/
└── dashboards/
    ├── trading-performance.json   # 11 panels — trades existentes
    ├── system-health.json         # 8 panels — bot_metrics
    └── ai-risk-analytics.json     # 7 panels — ai_decisions + risk_events
```

## Tabla `bot_metrics`

```sql
bot_metrics (
    id SERIAL PRIMARY KEY,
    metric_name VARCHAR(50) NOT NULL,
    value DOUBLE PRECISION NOT NULL,
    pair VARCHAR(20),
    labels JSONB,
    created_at TIMESTAMP DEFAULT NOW()
)
-- Index: idx_metrics_name_time ON (metric_name, created_at DESC)
```

Creada automáticamente por `_create_tables()` en `data_service/data_store.py`.

### Métricas emitidas

| metric_name | Dónde | Frecuencia | Labels |
|---|---|---|---|
| `pipeline_latency_ms` | `main.py` (on_candle_confirmed) | Cada candle (~5min) | pair |
| `claude_latency_ms` | `main.py` (_evaluate_with_claude) | Cada evaluación | pair |
| `okx_order_latency_ms` | `executor.py` (place_limit/stop/tp) | Cada orden | pair, type |
| `ws_reconnection` | `websocket_feeds.py` (start loop) | Cada reconexión | feed |
| `health_status` | `data_service/service.py` (health check) | Cada 30s | — |

### Retention
- Cleanup automático cada ~50 min (100 health checks × 30s)
- Borra métricas con `created_at > 30 días`
- Método: `PostgresStore.cleanup_old_metrics(retention_days=30)`

### Patrón de instrumentación
Fire-and-forget: `_emit_metric(name, value, pair, labels)`. Nunca bloquea el pipeline. Errores silenciados.

```python
# En main.py / data_service / execution_service:
def _emit_metric(name, value, pair=None, labels=None):
    try:
        postgres.insert_metric(name, value, pair=pair, labels=labels)
    except Exception:
        pass  # Never block
```

## Dashboards

### 1. Trading Performance (11 panels)
Fuente: tablas `trades` y `ai_decisions` (existentes, sin código nuevo).

| Panel | Tipo | Query principal |
|---|---|---|
| Equity Curve | timeseries | SUM(pnl_usd) OVER (ORDER BY closed_at) |
| Summary Stats | stat | COUNT, wins, win rate %, total PnL |
| Win Rate by Setup | bargauge | Win rate % GROUP BY setup_type |
| PnL by Setup | barchart | SUM(pnl_usd) GROUP BY setup_type |
| Exit Reasons | piechart | COUNT GROUP BY exit_reason |
| Entry Slippage | bargauge | AVG(ABS(actual_entry - entry_price) / entry_price) |
| Trades per Day | bar timeseries | COUNT GROUP BY day |
| AI Confidence vs Outcome | scatter | confidence vs win/loss |
| Win Rate Rolling 7d | timeseries | 7-day rolling window |
| PnL by Direction | barchart | SUM(pnl_usd) GROUP BY direction |
| Recent Trades | table | Last 50 closed trades |

### 2. System Health (8 panels)
Fuente: tabla `bot_metrics`.

| Panel | Tipo | Métrica |
|---|---|---|
| Health Status | stat | Último health_status (OK/DEGRADED) |
| Uptime % (24h) | stat | % de health_status=1 en 24h |
| WS Reconnections (24h) | stat | COUNT ws_reconnection |
| Pipeline Latency | timeseries | pipeline_latency_ms over time |
| Claude API Latency | timeseries | claude_latency_ms over time |
| OKX Order Latency | timeseries | okx_order_latency_ms over time |
| WS Reconnections Timeline | bar timeseries | COUNT por hora |
| Health Timeline | state-timeline | OK/DOWN over time |

### 3. AI & Risk Analytics (7 panels)
Fuente: tablas `ai_decisions` y `risk_events`.

| Panel | Tipo | Query |
|---|---|---|
| AI Approval Rate | timeseries | % approved GROUP BY day |
| AI Stats | stat | Total evaluations, approved, avg confidence |
| Confidence Distribution | histogram | Buckets de 0.05 |
| Approval by Setup | bargauge | % approved GROUP BY setup_type |
| Risk Events Timeline | stacked bars | COUNT GROUP BY event_type, day |
| Guardrail Frequency | piechart | COUNT GROUP BY event_type |
| Recent AI Decisions | table | Last 50 decisions |

## Acceso
- **URL:** `http://192.168.1.236:3001`
- **Anonymous viewer:** habilitado (red local, detrás del router)
- **Admin:** `admin` / `GRAFANA_ADMIN_PASSWORD` env var (default: "admin")

## Comandos
```bash
docker compose up -d grafana       # Levantar Grafana
docker compose logs grafana -f     # Ver logs
docker compose restart grafana     # Restart (recarga dashboards)
```
