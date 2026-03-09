# Arquitectura del Sistema
> Última actualización: 2026-03-09
> Estado: **5/5 capas implementadas** — pipeline completo Data → Strategy → AI → Risk → Execution. 2 perfiles (default/aggressive). AI filter obligatorio en todas las profiles. Sandbox usa limit orders con tolerancia. News sentiment (F&G + headlines) como nuevo data layer y pre-filter.

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
        │  TELEGRAM NOTIFIER          │ ← Push al celular
        │  (observador silencioso)    │
        └─────────────────────────────┘
          ↑ Notifica en cada evento clave:
          │ setup detectado, AI aprobó/rechazó,
          │ risk rechazó, trade abierto/cerrado,
          │ emergencias
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
9. Execution Service coloca la orden limit en OKX, con SL (stop-market) y 3 TPs (limit). **En sandbox**: limit al ask/bid actual + 0.05% tolerancia (evita slippage de market orders).
10. PositionMonitor gestiona el ciclo de vida: entry fill → TP1 (SL→breakeven) → TP2 (SL→TP1) → TP3/SL

**Regla clave:** Si CUALQUIER servicio dice NO, el trade se descarta. No hay "pero" ni "tal vez".

**Notificaciones Telegram:** En cada paso del pipeline (setup detectado, AI pre-filtered, AI decision, risk rejection, trade abierto/cerrado, emergencias, whale exchange deposits/withdrawals con USD y nombre de wallet, resumen de OBs cada 4H), el bot envía push notification al celular via Telegram Bot API. Whale transfers neutrales (transfer_in/transfer_out) se loguean pero no generan notificación. Fire-and-forget — si Telegram falla, el bot sigue operando.

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
| **Total** | **5/5 completas + backtester** | **420** | `main.py` (pipeline completo) |

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
  - TP1 (50% @ 1:1 R:R) → SL a breakeven → TP2 (30% @ 2:1) → TP3 (20%)
  - Timeout: `MAX_TRADE_DURATION_SECONDS`
  - Position sizing: `(equity * RISK_PER_TRADE) / |entry - sl|`, cap MAX_LEVERAGE
- **Métricas**: win rate, avg R:R, PnL, max drawdown, Sharpe, profit factor, trades/week
- **Breakdowns**: por setup type, par, dirección, exit reasons
- **Export CSV**: `--csv` genera archivo con todas las trades

```bash
python scripts/backtest.py --days 60 --profile aggressive --capital 10000 --csv
```

**Tests:** 21 tests en `tests/test_backtest.py` (SL, TP1-3, breakeven, timeout, sizing, métricas).

## Roadmap v2

### Trailing stop para TP3
Actualmente TP3 usa una limit order fija al siguiente nivel de liquidez. CLAUDE.md especifica "trailing stop or next liquidity level". La implementación v2:
- OKX soporta trailing stops via API (`trigger-order` con `callbackRatio`)
- Cuando `phase == "tp2_hit"`, cancelar el TP3 limit y colocar trailing stop
- Nuevo setting: `TRAILING_STOP_CALLBACK_PCT` (e.g., 0.5% = $250 en BTC a $50k)
- Nuevo estado en máquina de estados: `tp2_hit` → trailing en vez de limit fijo
- Requiere testing extensivo en sandbox — trailing stops se comportan diferente a limits en volátil

### Otras mejoras v2
- Persistencia de estado del monitor en Redis (sobrevivir restarts)
- Detección de posiciones huérfanas al reiniciar
- Aplicar `AIDecision.adjustments` a SL/TP antes de ejecutar
- Reconstruir estado de Risk Service desde PostgreSQL al arrancar
- Ver `docs/to-fix.md` para backlog completo (~30 IMPORTANT + 29 MINOR issues)

## Cambios recientes
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