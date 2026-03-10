# Arquitectura del Sistema
> Última actualización: 2026-03-10
> Estado: **5/5 capas implementadas** — pipeline completo Data → Strategy → AI → Risk → Execution. AlertManager con prioridades, rate limiting, silenciamiento, whale batching, y EMERGENCY escalation. 2 perfiles (default/aggressive). AI filter obligatorio en todas las profiles.

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
          ↑ Rutea alertas con prioridad:
          │ EMERGENCY: retry con backoff
          │ CRITICAL: 1 retry, trade_lifecycle
          │ WARNING: AI decisions, health check
          │ INFO: OB summary, whale digest
          │ + rate limiting, auto-silencing,
          │   whale batching (2 min digest)
```

## Cómo se comunican los servicios
1. Data Service recoge datos de OKX, Etherscan (liquidaciones via OI proxy, no Binance)
2. Cuando hay una vela nueva (cada 5m/15m), manda los datos al Strategy Service
3. Strategy Service analiza los datos buscando patrones SMC
4. Si encuentra un setup completo (Setup A/B swing, o C/D/E quick si no hay swing), lo pasa al siguiente filtro
5. **Swing setups (A/B):** Pre-filter determinístico (funding extreme, Fear & Greed extreme, CVD divergencia) → Claude evalúa → confianza ≥ 0.60 (0.50 aggressive)
6. **Quick setups (C/D/E):** Skip Claude AI filter (los datos SON la señal). Setup C también skipea funding pre-filter. Se genera `AIDecision` sintético con confidence=1.0
7. **AI filter obligatorio para swing setups** — quick setups lo bypasean por diseño (data-driven).
8. Risk Service verifica TODOS los guardrails y calcula el position size
9. Execution Service coloca la orden limit en OKX, con SL (stop-market) y TP (limit al tp2, 100% close). **En sandbox**: limit al ask/bid actual + 0.05% tolerancia (evita slippage de market orders).
10. PositionMonitor gestiona el ciclo de vida: entry fill → breakeven (SL→entry al cruzar tp1) → trailing SL (SL→tp1 al cruzar midpoint) → TP/SL

**Regla clave:** Si CUALQUIER servicio dice NO, el trade se descarta. No hay "pero" ni "tal vez".

**Sistema de alertas (`shared/alert_manager.py`):** AlertManager envuelve TelegramNotifier con prioridades (INFO/WARNING/CRITICAL/EMERGENCY), rate limiting por prioridad (INFO: 10/h, WARNING: 5/15m, CRITICAL: 20/h), auto-silenciamiento por categoría (3 alertas en 5 min → 15 min silence), whale batching (2 min digest), y escalamiento EMERGENCY con retry+backoff (4 intentos: 0s/5s/15s/30s). Las categorías `trade_lifecycle` y `emergency` NUNCA se silencian. Health check de infra ahora alerta por Telegram cuando componentes se caen/recuperan. Configuración en `config/settings.py` (ALERT_*).

## Detalles técnicos

### Comunicación entre servicios
Por ahora: llamadas directas entre módulos Python (simple, sin overhead). Si el bot crece, se puede migrar a Redis pub/sub o message queues.

### Almacenamiento
- **Redis:** Cache de datos en tiempo real. Último precio, última vela, estado del bot.
- **PostgreSQL:** Histórico de trades, velas pasadas, logs de decisiones.

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
- **Volumes:** `pgdata` y `redisdata` persisten datos entre restarts.
- **Puertos:** Solo `127.0.0.1` (no expuestos a la red externa).
- **Redis persistence:** `--appendonly yes` para durabilidad.
- **Graceful shutdown:** `stop_grace_period: 30s` para que el bot cierre WebSockets y cancele entries pendientes.

**Comandos:**
```bash
docker compose up -d          # Arrancar todo
docker compose logs bot -f    # Ver logs del bot en vivo
docker compose down           # Parar (volumes se preservan)
docker compose down -v        # Parar y borrar volumes (reset total)
docker compose build --no-cache  # Rebuild después de cambios en código
```

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
| **Total** | **5/5 completas + backtester + alerts** | **450** | `main.py` (pipeline completo) |

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
  - Single TP at tp2 (2:1 R:R) → 100% close. Breakeven at tp1 (1:1). Trailing SL at midpoint(tp1,tp2) → SL to tp1.
  - Timeout: `MAX_TRADE_DURATION_SECONDS`
  - Position sizing: `(equity * RISK_PER_TRADE) / |entry - sl|`, cap MAX_LEVERAGE
- **Risk guardrails** (matching live RiskService): MIN_RISK_DISTANCE_PCT, R:R check, cooldown after loss, max trades/day, daily DD, weekly DD. Tracks `risk_rejections` dict.
- **Setup dedup cache**: key=(pair, direction, setup_type, round(entry_price, 2)), TTL=1h. Matches main.py live dedup.
- **Métricas**: win rate, avg R:R, PnL, max drawdown, Sharpe, profit factor, trades/week
- **Breakdowns**: por setup type, par, dirección, exit reasons, risk rejections
- **Export CSV**: `--csv` genera archivo con todas las trades

```bash
python scripts/backtest.py --days 60 --profile aggressive --capital 10000 --csv
```

**Tests:** 21 tests en `tests/test_backtest.py` (SL, single TP, breakeven, trailing SL, timeout, sizing, métricas).

## Roadmap v2

### Mejoras v2
- Persistencia de estado del monitor en Redis (sobrevivir restarts)
- Detección de posiciones huérfanas al reiniciar
- Aplicar `AIDecision.adjustments` a SL/TP antes de ejecutar
- Reconstruir estado de Risk Service desde PostgreSQL al arrancar
- Ver `docs/to-fix.md` para backlog completo (~30 IMPORTANT + 29 MINOR issues)

## Cambios recientes
- 2026-03-10: **AlertManager** — `shared/alert_manager.py` reemplaza llamadas directas a TelegramNotifier. Prioridades (INFO/WARNING/CRITICAL/EMERGENCY), rate limiting por prioridad, auto-silenciamiento por categoría (3 en 5min → 15min silence), whale batching (2min digest), EMERGENCY retry con backoff. Health check alerta por Telegram cuando infra cae/recupera. `trade_lifecycle` y `emergency` nunca se silencian. 26 tests nuevos. Todos los callers migrados: `main.py`, `execution_service/monitor.py`, `data_service/service.py`.
- 2026-03-10: **ENABLED_SETUPS gate** — New setting `ENABLED_SETUPS` (default `["setup_b", "setup_f"]`) controls which setup types can trade. Gate in `strategy_service/service.py` checks after detection. Backtest showed B=56.8% WR, F=48.4% WR; A/G not profitable and disabled.
- 2026-03-10: **MIN_RISK_DISTANCE_PCT 0.1%→0.2%** — Filters micro-SL noise trades (especially Setup B shorts with 0.16-0.45% SL distances). Backtest: 54.5% WR, 0.95 PF. Tested 0.05%, 0.1%, 0.2%, 0.3%, 0.5% — 0.2% optimal.
- 2026-03-10: **Backtester risk guardrails** — TradeSimulator now applies same checks as live RiskService: MIN_RISK_DISTANCE_PCT, R:R, cooldown, max trades/day, daily DD, weekly DD. Setup dedup cache added (matching main.py). Reports risk rejection counts.
- 2026-03-10: **Whale log format** — ETH/BTC whale logs now prefix BEARISH|BULLISH|NEUTRAL, show wallet label first, include USD value consistently, show significance in brackets.
- 2026-03-10: **WebSocket volume fix** — `websocket_feeds.py` uses `candle_data[6]` (base currency) instead of `[5]` (contracts). Fixes 100x volume mismatch for BTC, 10x for ETH vs REST backfill.
- 2026-03-10: **Notifier returns bool** — `TelegramNotifier.send()` now returns `True`/`False` for success/failure.
- 2026-03-10: **Trailing SL + TP3 cleanup** — Simplified exit management from 3-tier partial closes (TP1 50%, TP2 30%, TP3 20%) to single TP at tp2 (2:1 R:R, 100% close) + progressive SL: breakeven at tp1 (1:1), trailing SL at midpoint(tp1,tp2) (1.5:1) → SL moves to tp1. Removed `tp3_price` from TradeSetup, ManagedPosition, SimulatedTrade, settings, all setups, AI prompt, and tests. Backtester rewritten to match live execution. R:R simplified from weighted blended to simple `abs(tp2-entry)/abs(entry-sl)`. PostgreSQL `tp3_price` column kept for historical data (param default 0.0). 450 tests pass.
- 2026-03-10: **PricePanel hides undefined bias** — Dashboard hides the bias badge when HTF bias is "undefined" instead of showing "undefined" text.
- 2026-03-09: **Phantom fill debug logging** — `monitor.py` logea WARNING con campos raw de OKX (avgPx, px, state) cuando fill price difiere >0.5% del limit price. Diagnostica caso donde buy-limit a $1937 reportó fill a $1990.
- 2026-03-09: **Test log isolation** — `shared/logger.py` detecta pytest y skipea file sinks. Previene que Mock objects contaminen logs de producción.
- 2026-03-09: **5 BTC whale wallets removidos** — mempool.space retorna HTTP 400 "Invalid Bitcoin address" para 1LQoWist8K, 32ixEdpwzG, 1HeKStJGY, 1AsHPP7Wc, 3MfN5to5K. Eliminaba 1130+ warnings/día.
- 2026-03-09: **Etherscan timeout 10s→15s** — Reduce timeouts transitorios (14/día). Alineado con btc_whale_client.
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