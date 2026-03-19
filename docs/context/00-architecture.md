# Arquitectura del Sistema
> Última actualización: 2026-03-15
> Estado: **5/5 capas implementadas** — pipeline completo Data → Strategy → Risk → Execution. AI filter currently bypassed for all active setups (setup_a in AI_BYPASS_SETUP_TYPES, setup_d variants in QUICK_SETUP_TYPES). ENABLED_SETUPS: setup_a, setup_d_choch. Setup B (0-7.7% WR), D_bos (20-33% WR) and F (34.8% WR) disabled. OB selector upgraded with composite scoring. PnL tracking con fee deduction (0.05% per side). Signal mode disponible (`SIGNAL_ONLY=true`). **ML instrumentation** active: `ml_setups` table captures structured features at detection + outcomes at close.

## Qué hace (para entenderlo rápido)
El sistema es un bot de trading que funciona como una línea de ensamblaje. Los datos entran por un lado, pasan por 5 filtros en orden, y si todos dicen "sí", se ejecuta el trade. Si cualquier filtro dice "no", el trade se descarta.

## Por qué existe
Sin esta arquitectura, tendríamos un solo programa gigante donde todo está mezclado. Si algo falla, todo falla. Con 5 servicios separados, si el AI Service se cae, el Risk Service sigue protegiendo el capital. Cada pieza hace una sola cosa bien.

## Diagrama del sistema

```
                    ┌─────────────┐
                    │   OKX API   │ ← Exchange de crypto
                    │  Etherscan  │ ← Datos on-chain ETH
                    └──────┬──────┘
                           │ datos en tiempo real
                           ▼
                ┌──────────────────┐
                │  1. DATA SERVICE │ ← Recoge y limpia datos
                │  (el periodista) │
                └────────┬─────────┘
                         │ OHLCV, volumen, OI, funding, on-chain
                         ▼
              ┌────────────────────┐
              │ 2. STRATEGY SERVICE│ ← Detecta patrones SMC
              │  (el detective)    │
              └────────┬───────────┘
                       │ "Encontré un Setup A/B en BTC/USDT"
                       ▼
              ┌────────────────────┐
              │  3. AI SERVICE     │ ← Claude evalúa contexto
              │  (el consultor)    │
              └────────┬───────────┘
                       │ "Aprobado, confianza 0.75"
                       ▼
              ┌────────────────────┐
              │  4. RISK SERVICE   │ ← Verifica guardrails
              │  (el guardián)     │
              └────────┬───────────┘
                       │ "Aprobado, position size = 0.05 ETH"
                       ▼
              ┌────────────────────┐
              │ 5. EXECUTION       │ ← Ejecuta la orden
              │  (el ejecutor)     │
              └────────┬───────────┘
                       │ orden de compra/venta
                       ▼
                ┌──────────────┐
                │   OKX API    │
                └──────────────┘

        ┌─────────────────────────────┐
        │  ALERT MANAGER              │ ← Push al celular
        │  (observador inteligente)   │
        └─────────────────────────────┘
          ↑ Solo 4 notificaciones:
          │ 1. ORDER PLACED (limit enviada)
          │ 2. TRADE CLOSED (SL/TP/timeout)
          │ 3. CAMPAIGN CLOSED (HTF position trade)
          │ 4. EMERGENCY (fallo SL, etc.)
```

## Cómo se comunican los servicios
1. Data Service recoge datos de OKX, Etherscan (liquidaciones via OI proxy, no Binance)
2. Cuando hay una vela nueva (cada 5m/15m), manda los datos al Strategy Service
3. Strategy Service analiza los datos buscando patrones SMC
4. Si encuentra un setup completo (Setup A swing, o D_choch quick), lo pasa al siguiente filtro
5. **Pipeline dedup**: cache at pipeline entry covers ALL setup types (key: pair+direction+setup_type, TTL 1h). `entry_price` excluded from key — same setup with slightly different entry should not bypass dedup. Risk rejections for structural reasons ("SL too close") also cached.
6. **AI filter currently bypassed for ALL active setups:**
   - Setup A: in `AI_BYPASS_SETUP_TYPES` (89.6% approval = no value). Synthetic AIDecision(confidence=1.0).
   - Setup D variants: in `QUICK_SETUP_TYPES`. Synthetic AIDecision(confidence=1.0).
   - Setup B, F: disabled entirely (not in ENABLED_SETUPS).
7. **WebSocket candle dedup**: `_last_confirmed_ts` dict in websocket_feeds.py prevents duplicate pipeline runs if OKX sends same candle twice.
8. Risk Service verifica TODOS los guardrails y calcula el position size
9. Execution Service coloca la orden limit en OKX, con SL (stop-market) y TP (limit al tp2 3:1 R:R, 100% close). **En sandbox**: limit al ask/bid actual + 0.05% tolerancia (evita slippage de market orders).
10. PositionMonitor gestiona el ciclo de vida: entry fill → breakeven (SL→entry al cruzar tp1 1:1) → trailing SL (SL→tp1 al cruzar midpoint 2:1) → TP/SL. **Progressive trailing** opcional (`TRAILING_TP_ENABLED=false` por defecto): trails SL en pasos de 0.5 R:R en vez de los dos saltos fijos.

**HTF Campaign path** (cuando `HTF_CAMPAIGN_ENABLED=true`):
- Velas 4H disparan evaluación HTF separada: Strategy (`evaluate_htf`) → AI → Risk → CampaignMonitor
- Daily candles (1D) determinan bias (en vez de 4H/1H)
- CampaignMonitor gestiona posiciones multi-día con pyramid adds (hasta 3) y trailing SL en swing levels de 4H
- Intraday bloqueado en el par mientras haya campaña activa (y viceversa)
- Sin TP orders — la campaña sale solo via trailing SL o timeout (7 días)

**Data integrity gate (antes de dedup):** DataService tiene un estado global (`RECOVERING`/`RUNNING`/`DEGRADED`). Ningún setup pasa mientras no sea `RUNNING`. Además, cada setup tiene dependencias de datos específicas (setup_c necesita CVD válido, setup_e necesita OI). Si un dep falta, el setup se bloquea y se registra como `data_blocked` en ML. Position Guardian y HTF campaigns también se bloquean durante RECOVERING.

**Regla clave:** Si CUALQUIER servicio dice NO, el trade se descarta. No hay "pero" ni "tal vez".

**Sistema de alertas (`shared/alert_manager.py`):** AlertManager envuelve TelegramNotifier con prioridades (INFO/WARNING/CRITICAL/EMERGENCY), rate limiting, auto-silenciamiento, y escalamiento EMERGENCY con retry+backoff. Infraestructura de routing completa, pero solo 3 tipos de notificación activos. Configuración en `config/settings.py` (ALERT_*).

**Notificaciones Telegram (actualizado 2026-03-11) — MINIMALISTAS:**
- **ORDER PLACED** — cuando se envía la limit order al exchange (par, dirección, entry, SL, TP, size, leverage). CRITICAL priority. También usado para initial campaign entry.
- **TRADE CLOSED** — cuando la posición intraday cierra (SL, TP, trailing SL, breakeven SL, timeout, invalidation). CRITICAL priority.
- **CAMPAIGN CLOSED** — cuando una campaña HTF cierra (trailing SL, timeout, emergency). Incluye P&L USD, % y cantidad de adds. CRITICAL priority.
- **SIGNAL** — (solo en signal mode) setup aprobado con entry/SL/TP/R:R/size/confluences/AI reasoning. CRITICAL priority.
- **EMERGENCY** — fallo de SL placement, emergency market close. EMERGENCY priority con retry.
- **Removidos:** OB summary, AI decisions, whale movements, daily summary, bot started, breakeven/trailing SL, entry expired, DD warning, health down/recovered. Todo eso sigue disponible en logs + Grafana.

**Signal Mode (`SIGNAL_ONLY=true`):**
Modo semi-manual donde el bot detecta setups, pasa por todos los filtros (Strategy → AI → Risk), pero en vez de ejecutar manda una señal por Telegram con toda la info para abrir manualmente:
- Par, dirección, setup type, R:R
- Entry, SL (con distancia %), TP (con distancia %)
- Position size y leverage calculados
- Confluencias detectadas
- AI confidence + reasoning (o "bypass" para quick setups)
El usuario abre el trade manualmente en OKX. Activar: `SIGNAL_ONLY=true` en `.env` o como env var.

## Detalles técnicos

### Comunicación entre servicios
Por ahora: llamadas directas entre módulos Python (simple, sin overhead). Si el bot crece, se puede migrar a Redis pub/sub o message queues.

### Almacenamiento
- **Redis:** Cache de datos en tiempo real. Último precio, última vela, estado del bot.
- **PostgreSQL:** Histórico de trades, velas pasadas, logs de decisiones, HTF campaigns.

### Infraestructura
- **Servidor:** Acer Nitro 5 (i5-9300H, 16GB RAM) con Ubuntu Server 24.04
- **IP:** 192.168.1.238
- **Contenedores:** Docker Compose (bot + PostgreSQL + Redis)
- **Desarrollo:** VS Code Remote SSH desde PC principal

### Docker Compose — Deployment

El bot corre en 3 containers via `docker-compose.yml`:

| Servicio | Imagen | Puerto | Propósito |
|----------|--------|--------|-----------|
| `postgres` | postgres:16-alpine | 127.0.0.1:5432 | Almacenamiento histórico (candles, trades, AI decisions) |
| `redis` | redis:7-alpine | 127.0.0.1:6379 | Cache en tiempo real (último precio, OI, funding, estado) |
| `bot` | python:3.12-slim (build local) | — | Bot de trading (5 capas) |
| `grafana` | grafana/grafana-oss:11.5-alpine | 3001 (host) | Monitoreo operacional (dashboards SQL sobre PostgreSQL) |

**Archivos Docker:**
- `.dockerignore` — Excluye .git, venv, tests, docs, .env del build
- `Dockerfile` — python:3.12-slim, pip install, `python -u main.py`, healthcheck via pgrep
- `docker-compose.yml` — 3 servicios con healthchecks, named volumes, `restart: unless-stopped`

**Configuración clave:**
- **Bot usa `network_mode: host`** — Docker bridge no tiene NAT configurado en el server. Con host network, el bot accede a Postgres/Redis en localhost directamente y tiene acceso a internet para OKX/Etherscan/Claude API.
- **Build usa `network: host`** — Para que `pip install` pueda descargar paquetes de PyPI durante el build.
- **Dos archivos `.env`:**
  - Root `.env` — Docker Compose variable interpolation (`${POSTGRES_PASSWORD}`)
  - `config/.env` — Secrets del bot (OKX, Anthropic, Etherscan). Montado read-only en `/app/config/.env`
- **Volumes:** `pgdata`, `redisdata` y `grafana_data` persisten datos entre restarts.
- **Puertos:** Solo `127.0.0.1` (no expuestos a la red externa).
- **Redis persistence:** `--appendonly yes` para durabilidad.
- **Graceful shutdown:** `stop_grace_period: 30s` para que el bot cierre WebSockets y cancele entries pendientes. All 6 background tasks (`data_service`, `daily_summary`, `liquidation_alerts`, `session_alerts`, `dry_spell_alerts`, `market_monitor`) are cancelled on SIGINT/SIGTERM.

**Comandos:**
```bash
docker compose up -d          # Arrancar todo
docker compose logs bot -f    # Ver logs del bot en vivo
docker compose down           # Parar (volumes se preservan)
docker compose down -v        # Parar y borrar volumes (reset total)
docker compose build --no-cache  # Rebuild después de cambios en código
```

## Monitoreo Operacional (Grafana)

Grafana corre en `http://192.168.1.236:3001` como container Docker. Lee directamente de PostgreSQL — sin Prometheus (volumen bajo, <20 métricas).

### Dashboards provisioned (`monitoring/dashboards/`)

| Dashboard | Fuente de datos | Qué muestra |
|-----------|----------------|-------------|
| **Trading Performance** | `trades`, `ai_decisions` | Equity curve, win rate rolling 7d, PnL por setup/dirección, exit reasons, slippage, AI confidence vs outcome |
| **System Health** | `bot_metrics` | Pipeline/Claude/OKX latency, WS reconnections, health status timeline, uptime % |
| **AI & Risk Analytics** | `ai_decisions`, `risk_events` | Approval rate over time, confidence distribution, guardrail trigger frequency |

### Tabla `bot_metrics` (métricas operacionales)

```sql
bot_metrics (metric_name VARCHAR(50), value FLOAT, pair VARCHAR(20), labels JSONB, created_at TIMESTAMP)
```

| metric_name | Dónde se mide | Frecuencia |
|---|---|---|
| `pipeline_latency_ms` | `main.py` (on_candle_confirmed) | Cada candle (~5min) |
| `claude_latency_ms` | `main.py` (_evaluate_with_claude) | Cada evaluación Claude |
| `okx_order_latency_ms` | `executor.py` (place_limit/stop/tp) | Cada orden |
| `ws_reconnection` | `websocket_feeds.py` (start loop) | Cada reconexión |
| `health_status` | `data_service/service.py` (health check) | Cada 30s |

**Retention:** Cleanup automático cada ~50 min (100 health checks). Borra métricas >30 días.

### Provisioning (`monitoring/provisioning/`)

- `datasources/postgres.yml` — PostgreSQL datasource (auto-conecta al arrancar)
- `dashboards/default.yml` — Carga dashboards JSON desde `/var/lib/grafana/dashboards/`

### Acceso

- URL: `http://192.168.1.236:3001`
- Anonymous viewer habilitado (red local, detrás del router)
- Admin: `admin` / password en `GRAFANA_ADMIN_PASSWORD` env var

## Glosario
- **BOS:** Break of Structure. Cuando el precio rompe un máximo/mínimo anterior, confirmando la tendencia.
- **CHoCH:** Change of Character. Cuando el precio rompe en dirección opuesta — posible cambio de tendencia.
- **OB:** Order Block. Zona donde las instituciones acumularon órdenes grandes. Es como una "huella" que dejan.
- **FVG:** Fair Value Gap. Un "hueco" en el precio que el mercado tiende a llenar después.
- **Sweep:** Cuando el precio barre los stop losses de otros traders y regresa. Las instituciones "cazan" la liquidez.
- **CVD:** Cumulative Volume Delta. Muestra quién está comprando más vs vendiendo más en un periodo.
- **OI:** Open Interest. Cuántos contratos de futuros están abiertos. Indica flujo de capital nuevo.
- **HTF/LTF:** Higher/Lower Time Frame. Timeframes grandes (4H, 1H) vs pequeños (15m, 5m).
- **SMC:** Smart Money Concepts. Teoría de trading que estudia cómo operan las instituciones para seguir sus movimientos.
- **Setup:** Una combinación de patrones que indica una oportunidad de trade.
- **Confluencia:** Múltiples señales apuntando en la misma dirección. Más confluencia = más confianza.

## Estado actual de cada capa

| Capa | Estado | Tests | Archivo principal |
|------|--------|-------|-------------------|
| 1. Data Service | Implementado + auditoría | 82 | `data_service/service.py` |
| 2. Strategy Service | Implementado + auditoría | 76 | `strategy_service/service.py` |
| 3. AI Service | Implementado | 41 + 26 news | `ai_service/service.py` |
| 4. Risk Service | Implementado | 72 | `risk_service/service.py` |
| 5. Execution Service | Implementado + auditoría | 32 | `execution_service/service.py` |
| Backtester | Implementado (fase 1) | 21 | `scripts/backtest.py` |
| Alert Manager | Implementado | 26 | `shared/alert_manager.py` |
| **Total** | **5/5 completas + backtester + alerts** | **478** | `main.py` (pipeline completo) |

## Backtesting (`scripts/`)

### `scripts/fetch_history.py`
Descarga velas históricas de OKX REST (vía ExchangeClient.backfill_candles()) y las almacena en PostgreSQL. Soporta `--days 90`, `--pair`, `--timeframe`. ON CONFLICT maneja dedup.

### `scripts/backtest.py`
Backtester completo con simulación de fills:
- **BacktestDataService** — mock del DataService con cursor temporal
- **SimulatedClock** — patchea `time.time()` para expiración de OBs/FVGs
- **TradeSimulator** — simula fills candle-by-candle:
  - Entry: limit order, fill cuando candle toca el precio. Timeout configurable.
  - SL: stop-market, prioridad máxima (check primero en cada candle).
  - Single TP at tp2 (3:1 R:R) → 100% close. Breakeven at tp1 (1:1). Trailing SL at midpoint(tp1,tp2) (2:1) → SL to tp1.
  - Timeout: `MAX_TRADE_DURATION_SECONDS`
  - Position sizing: `(equity * RISK_PER_TRADE) / |entry - sl|`, cap MAX_LEVERAGE
- **Timeframe-detail mode** (`--detail`): carga velas 1m para resolver ambigüedad SL/TP. Cuando una vela contiene tanto SL como TP en su rango, replay 1m sub-candles para determinar cuál se tocó primero. Fallback a SL-first si no hay data 1m. Requiere velas 1m en PostgreSQL (via `fetch_history.py --timeframe 1m`).
- **Fill probability** (`--fill-prob 0.8`): aplica probabilidad de fill después de que el precio alcanza entry. Simula fill rates realistas de limit orders. `--seed` para reproducibilidad. Default 1.0 (siempre fill).
- **Risk guardrails** (matching live RiskService): MIN_RISK_DISTANCE_PCT, R:R check, cooldown after loss, max trades/day, daily DD, weekly DD. Tracks `risk_rejections` dict.
- **Setup dedup cache**: key=(pair, direction, setup_type, round(entry_price, 2)), TTL=1h. **Nota:** main.py live dedup ya no incluye entry_price en la key — backtest aún usa la key vieja.
- **Métricas**: win rate, avg R:R, PnL, max drawdown, Sharpe, profit factor, trades/week
- **Breakdowns**: por setup type, par, dirección, exit reasons, risk rejections
- **Export CSV**: `--csv` genera archivo con todas las trades
- **JSON persistence**: cada run guarda automáticamente un resumen JSON en `backtest_results/` con métricas, breakdowns y metadata. Filename: `{timestamp}_{days}d.json`. No requiere flag — siempre se guarda.
- **Settings overrides**: `run_backtest(overrides={"PARAM": value})` permite override temporal de settings para optimización automática.

```bash
python scripts/backtest.py --days 60 --capital 10000 --csv
python scripts/backtest.py --days 60 --detail              # timeframe-detail (1m resolution)
python scripts/backtest.py --days 60 --fill-prob 0.8        # 80% fill probability
```

### `scripts/optimize.py`
Optuna parameter optimizer — automated strategy parameter tuning:
- **Wraps `run_backtest()`** como función objetivo de Optuna
- **10 parámetros tuneables**: SETUP_A_ENTRY_PCT, OB_PROXIMITY_PCT, MIN_RISK_DISTANCE_PCT, OB_MAX_AGE_HOURS, SETUP_A_MAX_SWEEP_CHOCH_GAP, OB_MIN_VOLUME_RATIO, OB_MIN_BODY_PCT, OB_MAX_DISTANCE_PCT, MIN_ATR_PCT, MIN_TARGET_SPACE_R
- **Métricas**: profit_factor (default), sharpe, pnl, win_rate, composite
- **Walk-forward validation** (`--walk-forward`): 70% train / 30% test. Detecta overfitting comparando optimized vs baseline en test period.
- **Parallel trials**: `--jobs 2-4` (limitado por cores del Nitro 5)
- **Output**: JSON con best params, top 5, parameter importance. Guardado en `backtest_results/`

```bash
python scripts/optimize.py --days 60 --trials 100 --metric profit_factor
python scripts/optimize.py --days 60 --trials 50 --walk-forward --jobs 2
```

**Tests:** 21 tests en `tests/test_backtest.py` (SL, single TP, breakeven, trailing SL, timeout, sizing, métricas).

## Roadmap v2

### Mejoras v2
- Persistencia de estado del monitor en Redis (sobrevivir restarts)
- Detección de posiciones huérfanas al reiniciar
- Aplicar `AIDecision.adjustments` a SL/TP antes de ejecutar
- Reconstruir estado de Risk Service desde PostgreSQL al arrancar
- Ver `docs/to-fix.md` para backlog completo (~30 IMPORTANT + 29 MINOR issues)

## ML Instrumentation

Desde 2026-03-13, el bot registra cada setup detectado con features estructurados en la tabla `ml_setups` de PostgreSQL. Objetivo: recolectar datos limpios (sin data leakage) para entrenar modelos ML futuros.

### Dos modelos objetivo
1. **Fill probability** — ¿este setup se va a llenar? Entrena con `filled_*` (positivo) + `unfilled_timeout` + `replaced` (negativo).
2. **Trade quality** — si se llena, ¿será rentable? Entrena solo con `filled_*`. Label: `pnl_pct`.

### Qué se captura

**Al detectar (después de dedup, antes de risk):**
- Geometría del setup (entry, SL, TP, R:R, risk_distance)
- Confluences descompuestas (has_sweep, has_choch, ob_volume_ratio, pd_aligned, etc.)
- Graduated signal tiers (sweep_tier, funding_tier, oi_rising_tier, dominance_tier)
- Market snapshot (funding, OI delta, buy_dominance, fear_greed)
- `daily_vol` — AFML Ch.3 getDailyVol(): EWMA std of close-to-close log-returns (span=100). Para normalizar barrier widths en análisis ML.
- Stale/late entry features (entry_distance_pct, sl_distance_pct, setup_age_minutes)
- Temporal features (hour_of_day, atr_pct)
- Missingness flags (has_funding, has_oi, has_cvd, has_news, has_whales)
- Risk context (capital, open_positions, daily_dd, weekly_dd)
- Guardian shadow triggers (counter, momentum, stall, cvd) — set during trade lifetime for feature importance
- `feature_version` (incrementar en `ML_FEATURE_VERSION` cuando cambien params de estrategia)

**Al resolver:**
- `outcome_type`: filled_tp, filled_sl, filled_trailing, filled_timeout, filled_guardian, unfilled_timeout, risk_rejected, deduped, replaced, data_blocked
- PnL, actual entry/exit, exit_reason, fill_duration_ms, trade_duration_ms

### Non-stationary features (AFML Ch.5) — excluir del training
- Precios absolutos (entry_price, sl_price, tp1/tp2_price, current_price_at_detection) → usar risk_distance_pct, entry_distance_pct, rr_ratio
- `oi_usd` → usar `oi_delta_pct`
- `cvd_5m/15m/1h` → usar `buy_dominance`

### Triple-barrier labels (AFML Ch.3)
- filled_tp/trailing → +1 (upper barrier)
- filled_sl → -1 (lower barrier)
- filled_timeout/guardian → sign(pnl_pct) (vertical barrier)

### Feature importance script
`scripts/feature_importance.py` — AFML Ch.7-8 compliant:
- `max_features=1` for unbiased MDI (AFML Ch.8)
- `neg_log_loss` scoring for MDA (AFML Ch.9)
- Sample uniqueness weighting (AFML Ch.4) — concurrent trades share info
- `--label barrier` mode for triple-barrier labels
- Kendall tau triangulation between MDI and SFI rankings

### Archivos
- `shared/ml_features.py` — extraction functions + `_get_daily_vol()` (AFML Ch.3)
- `shared/models.py` — `TradeSetup.setup_id` (uuid auto-generated)
- `data_service/data_store.py` — `ml_setups` table, `insert_ml_setup()`, `update_ml_setup_outcome()`, `update_ml_guardian_shadow()`
- `execution_service/models.py` — `ManagedPosition.setup_id`
- `main.py` — `_ml_log_setup()`, `_ml_resolve_outcome()` (fire-and-forget)
- `execution_service/monitor.py` — `_ml_resolve_close()` (outcome on trade close/cancel/replace)
- `scripts/feature_importance.py` — MDI/MDA/SFI with purged k-fold CV
- `config/settings.py` — `ML_FEATURE_VERSION` (currently 6)

### Leakage safety
- Features capturados DESPUÉS de dedup, ANTES de risk check (strategy-time)
- Risk context separado (safe para fill model, caution para quality model)
- Guardian shadow flags set DURING trade lifetime (outcome-dependent — exclude from quality model)
- No se usa información de outcome en features

### Timeline
- Fill model (~200 samples): ~5 meses a 10 trades/semana
- Quality model (~500 samples): ~12 meses
- Hasta entonces: solo recolección de datos

## Cambios recientes
- 2026-03-19: **ML v6 — daily_vol feature + AFML training pipeline.** `_get_daily_vol()` (AFML Ch.3 getDailyVol) computes EWMA std of close-to-close log-returns (span=100). New `daily_vol` column in `ml_setups` with safe ALTER TABLE migration. `scripts/feature_importance.py` rewritten: `max_features=1` for MDI, `neg_log_loss` for MDA, sample uniqueness weighting (AFML Ch.4), triple-barrier labels (`--label barrier`), non-stationary feature exclusion (AFML Ch.5), Kendall tau triangulation. Guardian shadow columns added for future feature importance.
- 2026-03-19: **Setup H disabled** — 27 trades, 11% WR, PF 0.10. Entry at impulse completion = adverse selection. Code kept for recalibration.
- 2026-03-19: **Guardian → shadow mode** — All guardian checks log-only (no closes, no SL changes). Triple barrier handles all exits for clean ML labels. Shadow triggers stored in ml_setups for AFML Ch.8 feature importance.
- 2026-03-15: **Optuna parameter tuning** — Walk-forward validated parameter optimization (PF 1.05→2.65). Key changes: OB_MIN_VOLUME_RATIO 1.2→1.3, OB_MAX_AGE_HOURS 72→84, OB_MIN_BODY_PCT 0.001→0.0015, OB_PROXIMITY_PCT 0.008→0.007, OB_MAX_DISTANCE_PCT 0.08→0.04 (biggest improvement), SETUP_A_ENTRY_PCT 0.50→0.65, SETUP_A_MAX_SWEEP_CHOCH_GAP 40→45, MIN_ATR_PCT 0.0025→0.0045, MIN_TARGET_SPACE_R 1.2→1.4. New `scripts/optimize.py` for automated Optuna optimization.
- 2026-03-15: **Periodic SL verification** — `_verify_sl_exists()` in PositionMonitor confirms SL algo order exists on exchange every `SL_VERIFY_INTERVAL_SECONDS` (60s) via `find_pending_algo_orders()`. Catches silent SL drops missed by `fetch_order()`. Re-places SL if missing. New `last_sl_verified_ms` field in ManagedPosition.
- 2026-03-15: **Backtest enhancements** — Timeframe-detail mode (`--detail`) loads 1m candles to resolve ambiguous SL/TP ordering within a candle. Fill probability model (`--fill-prob 0.8`) simulates realistic limit order fill rates with `--seed` for reproducibility. `run_backtest(overrides={})` for Optuna parameter optimization. `_candle_duration_ms()` + binary search for 1m sub-candle lookup. Returns metrics for programmatic use.
- 2026-03-15: **Liquidation heatmap** — DIY approximation of Coinglass-style heatmap. `data_service/liquidation_estimator.py` projects liquidation prices for 5 leverage tiers (5x-100x) using OI + candle volume distribution. Dashboard: canvas-based horizontal bar chart with BTC/ETH tabs. Settings: `LIQ_CANDLE_COUNT`, `LIQ_BIN_SIZE_BTC/ETH`, `LIQ_CACHE_TTL`.
- 2026-03-15: **Logger resilience** — `shared/logger.py` wraps file sink creation in try/catch for PermissionError (e.g. bot running as root, script as user). Falls back to stdout-only logging.
- 2026-03-15: **ExchangeClient 1m/1d timeframes** — `_timeframe_to_ms()` now supports 1m and 1d for backtest detail mode and daily candle backfill.
- 2026-03-13: **ML Instrumentation** — New `ml_setups` PostgreSQL table captures structured features for every detected setup (before dedup/risk). `TradeSetup.setup_id` (uuid) tracks each setup across detection→risk→execution→close. Features decomposed at write time: confluences (has_sweep, has_choch, ob_volume_ratio, cvd_aligned, pd_aligned), market snapshot (funding, OI, CVD, buy_dominance), stale/late-entry (entry_distance_pct, setup_age_minutes), missingness flags (has_funding, has_oi, has_cvd, has_news, has_whales). Outcomes resolved: filled_tp/sl/trailing/timeout, unfilled_timeout, risk_rejected, deduped, replaced. `ML_FEATURE_VERSION=5` (v5: graduated signals — sweep tiers, CVD magnitude, OI delta numeric, funding tiers, dominance tiers). New file: `shared/ml_features.py`. Pipeline instrumented in `main.py`, outcome resolution in `execution_service/monitor.py`. 14 new tests. Zero impact on trading logic — pure data collection.
- 2026-03-11: **Single mode + PnL fix + Institutional AI** — Removed dual profile system (default/aggressive). Aggressive values merged as new defaults: AI_MIN_CONFIDENCE 0.50, MAX_DAILY_DRAWDOWN 5%, MAX_WEEKLY_DRAWDOWN 10%, COOLDOWN_MINUTES 15, MAX_TRADES_PER_DAY 10, MIN_RISK_REWARD 1.2, OB_PROXIMITY_PCT 0.008, PD_EQUILIBRIUM_BAND 0.01, ALLOW_EQUILIBRIUM_TRADES True, HTF_BIAS_REQUIRE_4H False, ENTRY_TIMEOUT_SECONDS 21600. PnL tracking now deducts trading fees (TRADING_FEE_RATE 0.05% per side) and stores actual_exit_price. All exit paths compute PnL before closing. AI prompt rewritten with scoring rubric (4 dimensions: setup_quality, market_support, contradiction, data_sufficiency). Deleted: ProfileSelector component, profile API route, profile sync in main.py. 478 tests pass.
- 2026-03-11: **HTF Campaign Trading** — Position trades en 4H con Daily bias. CampaignMonitor gestiona ciclo de vida: initial entry → pyramid adds (hasta 3, margen decreciente: $30/$15/$10/$5) → trailing SL en 4H swing levels → timeout 7 días. Sin TP — sale solo via trailing SL. Intraday bloqueado en par con campaña activa. Nuevos modelos: `CampaignAdd`, `PositionCampaign`. Nuevo archivo: `execution_service/campaign_monitor.py`. Nueva tabla PostgreSQL: `campaigns`. Daily candles (1D) backfill + WebSocket. Settings: `HTF_CAMPAIGN_*`. Pipeline HTF wired en `main.py`. Alert: `notify_campaign_closed()`.
- 2026-03-11: **Signal Mode** — Nuevo modo semi-manual (`SIGNAL_ONLY=true`). Bot detecta setups y pasa todos los filtros pero NO ejecuta — manda señal por Telegram con entry/SL/TP/R:R/size/confluences/AI reasoning. Para validar calidad de señales antes de confiar en ejecución automática. `config/settings.py` (SIGNAL_ONLY flag), `shared/alert_manager.py` (notify_signal), `main.py` (pipeline conditional). 5 tests nuevos.
- 2026-03-10: **Telegram minimalista** — Solo 3 notificaciones activas: ORDER PLACED (nueva, al enviar limit order), TRADE CLOSED (SL/TP), EMERGENCY. Removidos: OB summary, AI decisions, whale movements, daily summary, bot started, breakeven/trailing SL, entry expired, DD warning, health down/recovered. Métodos siguen existiendo en AlertManager para re-enable futuro. Datos de whale/health siguen colectándose para AI context, dashboard y logs.
- 2026-03-10: **Notification overhaul** — Whale alerts filtradas: solo exchange deposits/withdrawals ≥$1M (`WHALE_NOTIFY_EXCHANGE_ONLY`, `WHALE_NOTIFY_MIN_USD`). Formato con señal BEARISH/BULLISH. Status horario eliminado, reemplazado por resumen diario 00:00 UTC. Nuevas notificaciones: BOT STARTED, breakeven SL, trailing SL, entry expired, DD warning (66% del límite). `notify_hourly_status` removido de notifier.py y alert_manager.py.
- 2026-03-10: **Grafana Monitoring** — Grafana OSS container en puerto 3001 con 3 dashboards provisioned (Trading Performance, System Health, AI & Risk Analytics). Nueva tabla `bot_metrics` para métricas operacionales. Pipeline instrumentado: pipeline_latency_ms, claude_latency_ms, okx_order_latency_ms, ws_reconnection, health_status. Retention automático 30d. Trading Performance dashboard funciona sin código nuevo (queries sobre tables existentes).
- 2026-03-10: **AlertManager** — `shared/alert_manager.py` reemplaza llamadas directas a TelegramNotifier. Prioridades (INFO/WARNING/CRITICAL/EMERGENCY), rate limiting por prioridad, auto-silenciamiento por categoría (3 en 5min → 15min silence), whale batching (2min digest), EMERGENCY retry con backoff. Health check alerta por Telegram cuando infra cae/recupera. `trade_lifecycle` y `emergency` nunca se silencian. 26 tests nuevos. Todos los callers migrados: `main.py`, `execution_service/monitor.py`, `data_service/service.py`.
- 2026-03-10: **ENABLED_SETUPS gate** — New setting `ENABLED_SETUPS` (default `["setup_b", "setup_f"]`) controls which setup types can trade. Gate in `strategy_service/service.py` checks after detection. Backtest showed B=56.8% WR, F=48.4% WR; A/G not profitable and disabled.
- 2026-03-10: **MIN_RISK_DISTANCE_PCT 0.1%→0.2%** — Filters micro-SL noise trades (especially Setup B shorts with 0.16-0.45% SL distances). Backtest: 54.5% WR, 0.95 PF. Tested 0.05%, 0.1%, 0.2%, 0.3%, 0.5% — 0.2% optimal.
- 2026-03-10: **Backtester risk guardrails** — TradeSimulator now applies same checks as live RiskService: MIN_RISK_DISTANCE_PCT, R:R, cooldown, max trades/day, daily DD, weekly DD. Setup dedup cache added (matching main.py). Reports risk rejection counts.
- 2026-03-10: **Whale log format** — ETH/BTC whale logs now prefix BEARISH|BULLISH|NEUTRAL, show wallet label first, include USD value consistently, show significance in brackets.
- 2026-03-10: **WebSocket volume fix** — `websocket_feeds.py` uses `candle_data[6]` (base currency) instead of `[5]` (contracts). Fixes 100x volume mismatch for BTC, 10x for ETH vs REST backfill.
- 2026-03-10: **Notifier returns bool** — `TelegramNotifier.send()` now returns `True`/`False` for success/failure.
- 2026-03-10: **Trailing SL + TP3 cleanup** — Simplified exit management from 3-tier partial closes (TP1 50%, TP2 30%, TP3 20%) to single TP at tp2 (3:1 R:R, 100% close) + progressive SL: breakeven at tp1 (1:1), trailing SL at midpoint(tp1,tp2) (2:1) → SL moves to tp1. Removed `tp3_price` from TradeSetup, ManagedPosition, SimulatedTrade, settings, all setups, AI prompt, and tests. Backtester rewritten to match live execution. R:R simplified from weighted blended to simple `abs(tp2-entry)/abs(entry-sl)`. PostgreSQL `tp3_price` column kept for historical data (param default 0.0). 450 tests pass.
- 2026-03-10: **PricePanel hides undefined bias** — Dashboard hides the bias badge when HTF bias is "undefined" instead of showing "undefined" text.
- 2026-03-09: **Phantom fill debug logging** — `monitor.py` logea WARNING con campos raw de OKX (avgPx, px, state) cuando fill price difiere >0.5% del limit price. Diagnostica caso donde buy-limit a $1937 reportó fill a $1990.
- 2026-03-09: **Test log isolation** — `shared/logger.py` detecta pytest y skipea file sinks. Previene que Mock objects contaminen logs de producción.
- 2026-03-09: **5 BTC whale wallets removidos** — mempool.space retorna HTTP 400 "Invalid Bitcoin address" para 1LQoWist8K, 32ixEdpwzG, 1HeKStJGY, 1AsHPP7Wc, 3MfN5to5K. Eliminaba 1130+ warnings/día.
- 2026-03-09: **Etherscan timeout 10s→15s** — Reduce timeouts transitorios (14/día). Alineado con btc_whale_client.
- 2026-03-12: **AI disabled + ENABLED_SETUPS trimmed + OB scoring + Setup D split** — AI filter bypassed for ALL active setups: setup_a added to `AI_BYPASS_SETUP_TYPES` (89.6% approval = no value), setup_d variants already in `QUICK_SETUP_TYPES`. Zero Claude API calls in pipeline. ENABLED_SETUPS changed from [A, B, D, F] to [A, D_bos, D_choch] — Setup B (0% WR live) and F (34.8% WR) disabled. Setup D split into `setup_d_bos` and `setup_d_choch` variants for per-variant measurement. OB selector upgraded: composite `_score_ob()` using volume (35%), freshness (30%), proximity (20%), body size (15%); `OB_MIN_BODY_PCT` (0.1%) filters micro-OBs. `MIN_RISK_DISTANCE_PCT` check moved to Strategy layer (evaluate_setup_a/d) as early filter. `SETUP_A_ENTRY_PCT` (0.50, env override) makes entry depth configurable. WebSocket `_last_confirmed_ts` dict deduplicates candles. Pipeline dedup cache moved to pipeline entry covering ALL setup types. Dashboard /api/risk filters `pending_entry` from open position count.
- 2026-03-12: **SL validation + PD override + risk dedup** — `_validate_sl_direction()` en setups A/B/F/G rechaza si SL está del lado incorrecto del entry (bug: Setup B con FVG encima del OB generaba SL invertido). PD alignment ahora diferido: setups con 5+ confluencias (`PD_OVERRIDE_MIN_CONFLUENCES`) pueden operar contra zona PD, evitando lockout total en bearish+discount. main.py cachea risk rejections estructurales ("SL too close") en dedup para no re-evaluar con Claude. 507 tests.
- 2026-03-09: **News Sentiment Analysis** — Fear & Greed Index (alternative.me) + crypto headlines (cryptocurrency.cv) como nuevo data layer. Pre-filter rechaza longs en Extreme Fear (F&G<15) y shorts en Extreme Greed (F&G>85). Claude recibe F&G score + headlines como factor 8. `data_service/news_client.py` nuevo con Redis caching. 26 tests nuevos. 420 tests totales.
- 2026-03-09: **SL vs market validation** — Execution Service ahora verifica que el SL no esté ya "adentro" del mercado antes de colocar la orden. Short con SL < market → skip (OKX 51053). Previene fallos en Setup G cuando precio se movió más allá del breaker block.
- 2026-03-09: **Market maker noise filter** — `MARKET_MAKER_WALLETS` set en settings filtra notificaciones de Cumberland, Galaxy, Wintermute, etc. (solo notifica significancia alta). Data sigue colectándose para AI context.
- 2026-03-09: **Backtester con simulación de fills** — `scripts/backtest.py` ahora simula entry/SL/TP fills candle-by-candle con position sizing real, 3-tier TP exits, breakeven SL, timeout. Produce métricas completas (win rate, Sharpe, max DD, profit factor). `scripts/fetch_history.py` nuevo para descargar 90 días de velas históricas. 21 tests nuevos. 394 tests totales.
- 2026-03-06: **Dynamic capital + fixed margin sizing** — `main.py` ahora busca USDT balance del exchange al arrancar (fallback a `INITIAL_CAPITAL`). `FIXED_TRADE_MARGIN` reemplaza `SANDBOX_MARGIN_PER_TRADE` — cuando > 0, position sizing usa margen fijo en ambos modos (sandbox y live). `fetch_usdt_balance()` añadido a ExchangeClient y DataService.
- 2026-03-06: **Profile cleanup + slippage fix** — Scalping eliminado. Aggressive rediseñado: PD/HTF alignment ON, AI obligatorio, DD 5%/10%, R:R 1.2. `FORCE_MAX_LEVERAGE` eliminado. Sandbox usa limit orders con tolerancia 0.05% (era market orders con 13.8% slippage). Setup dedup cache evita re-evaluar mismo setup en Claude.
- 2026-03-05: **Whale notifications + 4H OB summary** — Notificaciones whale ahora muestran nombre de wallet (campo `wallet_label`). Nuevo `notify_ob_summary()` envía resumen de OBs activos cada 4H.
- 2026-03-05: **Algo order fetch rewrite** — `_fetch_algo_order` usa OKX native API (`privateGetTradeOrdersAlgoPending`/`History`) en vez de ccxt `fetch_open_orders` que causaba 6,871 errores repetidos.
- 2026-03-05: **Pre-filter** — Pre-filter determinístico (HTF bias conflict, funding extreme, CVD divergencia) rechaza setups obvios antes de llamar a Claude.
- 2026-03-04: **Strategy profiles** — 3 perfiles (default/aggressive/scalping) switcheables desde dashboard o env var. Backtester en `scripts/backtest.py` para replay histórico.
- 2026-03-04: **Whale tracking completo** — Todas las transferencias grandes se trackean (no solo exchange). 4 acciones: deposit (bearish), withdrawal (bullish), transfer_out (neutral), transfer_in (neutral). ETH + BTC.
- 2026-03-04: **Auditoría completa** — 12 CRITICAL corregidos (PG reconnection, pipeline locks, OKX algo orders, emergency close retry, sweep temporal guard, OB break_timestamp, etc.). 28 IMPORTANT + 29 MINOR documentados en `docs/to-fix.md`.
- 2026-03-04: BTC whale tracking via mempool.space.
- 2026-03-04: Telegram notifications — push al celular en cada evento clave del pipeline (`shared/notifier.py`).
- 2026-03-04: Docker Compose deployment — bot + PostgreSQL + Redis containerizados.
- 2026-03-04: Las 5 capas implementadas. Pipeline completo Data → Strategy → AI → Risk → Execution.
- 2026-03-03: Documento inicial creado con arquitectura de 5 capas.