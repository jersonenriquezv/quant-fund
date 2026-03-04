# Changelog — One-Man Quant Fund

## [2026-03-04] — Full Audit — 12 CRITICAL fixes
**Qué cambió:**
Auditoría completa de las 5 capas. 12 issues CRITICAL corregidos, 28 IMPORTANT + 29 MINOR documentados en `docs/to-fix.md`.

**Fixes aplicados:**
1. **Wallet dedup** (`config/settings.py`) — Removidas 5 ETH exchange hot wallets de `WHALE_WALLETS` que también estaban en `EXCHANGE_ADDRESSES` (Binance, Kraken, Bithumb, Crypto.com, Gate.io). Removidos 3 BTC duplicados (MicroStrategy/Bitfinex Hack Recovery, Bitfinex exchange, Binance).
2. **PostgreSQL reconnection** (`data_service/data_store.py`) — `_ensure_connected()` con `SELECT 1` health check. Todos los métodos DB retry una vez en `OperationalError`/`InterfaceError`.
3. **Pipeline serialization** (`data_service/websocket_feeds.py`) — Per-pair `asyncio.Lock` previene pipelines concurrentes en el mismo par. `task.add_done_callback()` para logging de excepciones.
4. **asyncio.get_running_loop()** (`data_service/service.py`, `execution_service/executor.py`) — Reemplaza todas las llamadas a `get_event_loop()` (deprecated).
5. **OKX algo orders** (`execution_service/executor.py`) — `ordType: "conditional"` para SL stop-market orders. `fetch_order()` con fallback a `_fetch_algo_order()`.
6. **Emergency close retry** (`execution_service/monitor.py`) — Verifica return value de `close_position_market()`. Fase `emergency_pending` con máximo 3 reintentos. Tras 3 fallos → `emergency_failed`.
7. **Cancelled entries** (`execution_service/monitor.py`) — Entries canceladas (timeout sin fill) no notifican a Risk ni envían Telegram de trade cerrado.
8. **Sweep temporal guard** (`strategy_service/liquidity.py`) — Solo evalúa candles con timestamp > `max(level.timestamps)` para prevenir sweeps falsos.
9. **OB break_timestamp** (`strategy_service/order_blocks.py`) — Campo `break_timestamp` en OrderBlock. Mitigación solo evalúa candles posteriores a la vela de ruptura.
10. **Setup A documentation** (`strategy_service/setups.py`) — Comentario explicando que Setup A es patrón de CONTINUACIÓN (CHoCH alineado con HTF bias) — decisión intencional, no bug.
11. **Whale notification stability** (`data_service/service.py`) — Usa `id()` snapshot antes del polling para detectar nuevos movimientos sin depender de índices de lista.

**Nuevos campos:**
- `ManagedPosition.emergency_retries: int = 0`
- `OrderBlock.break_timestamp: int = 0`

**Tests:** 280/280 passing. 3 tests actualizados (PG no-connection mocking, cancelled entry assertion).

**Por qué:** Auditoría pre-producción para eliminar bugs críticos antes de activar trading en sandbox.
**Impacto:** config/, data_service/, strategy_service/, execution_service/, tests/, docs/

## [2026-03-04] — BTC Whale Movement Tracking
**Qué cambió:**
- `shared/models.py` — WhaleMovement generalizado: `amount_eth` → `amount`, nuevo campo `chain` ("ETH" o "BTC"). Soporta ambas cadenas.
- `config/settings.py` — 15 BTC whale wallets (mega-wallets, gobiernos, Mt. Gox, MicroStrategy, etc.), 11 exchange addresses (Binance, Robinhood, Bitfinex, OKX, Kraken, etc.), `WHALE_MIN_BTC=10`, `WHALE_HIGH_BTC=100`, `MEMPOOL_CHECK_INTERVAL=300`.
- `data_service/btc_whale_client.py` — NUEVO. Cliente mempool.space REST API. Parsea modelo UTXO (vin/vout) para detectar deposits/withdrawals a exchanges conocidos. Rate limit 0.5s entre calls. Misma API pública que EtherscanClient.
- `data_service/etherscan_client.py` — Usa `amount=` y `chain="ETH"`. `serialize_movements()` incluye `chain` en JSON.
- `data_service/service.py` — Integra `BtcWhaleClient`. `get_whale_movements()` y `get_market_snapshot()` mergean ETH+BTC. Nuevo `_btc_whale_loop()`. `_publish_whale_movements()` centraliza publicación a Redis (ETH+BTC combinados).
- `ai_service/prompt_builder.py` — Sección whale dinámica: "150.0 ETH" o "10.5 BTC" según chain.
- `dashboard/api/models.py` — `WhaleMovementRecord`: `amount_eth` → `amount`, nuevo `chain`.
- `dashboard/web/src/lib/api.ts` — Interface actualizada: `amount`, `chain`.
- `dashboard/web/src/components/WhaleLog.tsx` — Título "Whale Movements (24h)", columna Amount muestra "500.00 ETH" o "10.5000 BTC", decimales dinámicos por chain.
- `tests/` — Fixtures actualizadas en test_data_service.py y test_prompt_builder.py.

**Por qué:** ETH whale tracking estaba funcionando pero BTC no se monitoreaba. BTC es el par principal del bot. mempool.space es gratis, sin API key, y cubre Bitcoin mainnet.
**Impacto:** shared/, config/, data_service/, ai_service/, dashboard/, tests/

## [2026-03-04] — Dashboard — Whale Movements Section
**Qué cambió:**
- `config/settings.py` — `WHALE_WALLETS` cambiado de `List[str]` a `dict[str, str]` (address → label). Permite mostrar nombres legibles en el dashboard.
- `data_service/etherscan_client.py` — Itera `.keys()` del dict. Nuevo método `serialize_movements()` que incluye label de cada wallet en el JSON.
- `data_service/data_store.py` — 2 métodos nuevos en RedisStore: `set_whale_movements()` / `get_whale_movements()`. Key: `qf:bot:whale_movements`, TTL 600s.
- `data_service/service.py` — Etherscan ya no se lanza como tarea independiente. Nuevo `_etherscan_loop()` que ejecuta el poll y publica a Redis después de cada ciclo.
- `dashboard/api/models.py` — Nuevo modelo `WhaleMovementRecord` (timestamp, wallet, label, action, amount_eth, exchange, significance).
- `dashboard/api/routes/whales.py` — Nuevo endpoint `GET /api/whales?hours=24`. Lee Redis, filtra por timestamp.
- `dashboard/api/main.py` — Router de whales registrado.
- `dashboard/web/src/lib/api.ts` — Interface `WhaleMovement` TypeScript.
- `dashboard/web/src/components/WhaleLog.tsx` — Tabla: Time, Wallet (label + truncated addr), Action (deposit=red, withdrawal=green badge), Amount ETH, Exchange, Significance. Polls cada 30s.
- `dashboard/web/src/app/page.tsx` — WhaleLog agregado al grid entre trade/AI logs y health bar.
- `dashboard/web/src/app/globals.css` — Grid row 5 para whale-log (full width), health bar movido a row 6.
- `tests/test_data_service.py` — Tests Etherscan actualizados para usar dict en vez de list.

**Por qué:** Whale movements solo vivían en memoria del bot. El dashboard (proceso separado) necesita leerlos via Redis.
**Impacto:** config/, data_service/, dashboard/, tests/

## [2026-03-04] — Telegram Notifications
**Qué cambió:**
- `shared/notifier.py` — Nuevo módulo `TelegramNotifier`. Envía mensajes via Telegram Bot API (httpx POST). Fire-and-forget: si Telegram falla, el bot continúa. 6 métodos: `notify_setup_detected`, `notify_ai_decision`, `notify_risk_rejected`, `notify_trade_opened`, `notify_trade_closed`, `notify_emergency`.
- `config/settings.py` — 2 settings nuevos: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (desde .env).
- `main.py` — Inicializa `TelegramNotifier` al startup. Notifica en pipeline: setup detected, AI decision (approved/rejected), risk rejected.
- `execution_service/service.py` — Acepta `notifier` parameter, pasa a `PositionMonitor`.
- `execution_service/monitor.py` — Acepta `notifier` parameter. Notifica: trade opened (entry fill), trade closed (SL/TP/timeout), emergency close (SL placement failure).

**Por qué:** Monitorear el bot desde el celular sin revisar el dashboard constantemente. Push notifications instantáneas en Telegram para cada evento clave.
**Impacto:** shared/notifier.py, config/settings.py, main.py, execution_service/

## [2026-03-04] — Dashboard — Phase 6
**Qué cambió:**
- **Trade persistence** — Bot ahora escribe a PostgreSQL: `insert_trade()`, `update_trade()`, `insert_ai_decision()`, `insert_risk_event()` en `data_store.py`. Wired en `monitor.py` (entry fill/close), `main.py` (AI decisions), `risk_service/service.py` (guardrail events). Redis key `qf:bot:positions` para posiciones abiertas.
- **FastAPI backend** (`dashboard/api/`) — 8 endpoints read-only + WebSocket. asyncpg + redis.asyncio. Pydantic schemas. Health check con PG + Redis ping.
- **Next.js frontend** (`dashboard/web/`) — Single-page "VAULT" theme: dark (#0a0e17), monospace, tabular-nums. 8 componentes: Header, PricePanel, RiskGauge (SVG arc DD gauges), PositionCard, PnLChart (SVG sparkline), TradeLog, AILog (confidence bars), HealthGrid.
- **Docker** — 2 nuevos servicios en `docker-compose.yml`: `api` (python:3.12-slim, port 8000) y `web` (node:20-alpine, port 3000). `network_mode: host`.
- `execution_service/models.py` — Campo `db_trade_id` para tracking en PostgreSQL
- `execution_service/monitor.py` — `data_store` parameter, `_persist_trade_open/close()`, `_update_positions_cache()`
- `execution_service/service.py` — Acepta `data_service` parameter, pasa a PositionMonitor
- `risk_service/service.py` — Acepta `data_service` parameter, `_persist_risk_event()` en guardrail hits
- `data_service/service.py` — Properties `postgres` y `redis` para acceso directo
- `data_service/data_store.py` — 4 métodos nuevos en PostgresStore, `set/get_positions` en RedisStore
- `main.py` — Pasa `data_service` a Risk y Execution. Persiste AI decisions y risk events en pipeline.
- `docs/context/06-dashboard.md` — Documentación completa

**Por qué:** Sexto paso — monitorear el bot sin `docker compose logs`. Dashboard en http://192.168.1.238:3000.
**Impacto:** dashboard/, data_service/, execution_service/, risk_service/, main.py, docker-compose.yml, docs/

## [2026-03-04] — Docker Compose deployment
**Qué cambió:**
- `.dockerignore` — Excluye .git, venv, tests, docs, .env del build context
- `Dockerfile` — python:3.12-slim, pip install, `python -u main.py`, healthcheck via pgrep
- `docker-compose.yml` — 3 servicios: postgres:16-alpine, redis:7-alpine, bot. Healthchecks, named volumes (`pgdata`, `redisdata`), `restart: unless-stopped`, `stop_grace_period: 30s`
- `.env` (root) — Variable `POSTGRES_PASSWORD` para Docker Compose interpolation
- `config/.env` — Template con todos los secrets del bot (montado read-only en container)
- `docs/context/00-architecture.md` — Sección "Docker Compose — Deployment" con tabla de servicios, archivos, configuración, y comandos
- `docs/context/01-data-service.md` — Status actualizado de "ready for integration" a "running in Docker"
- `main.py` — Docstring actualizado (ya no dice "stub")
- `docs/to-fix.md` — Issues pendientes documentados

**Nota técnica:** Bot usa `network_mode: host` porque Docker bridge no tiene NAT configurado en el server. Build usa `network: host` para pip install. Dos `.env` necesarios: root para Compose interpolation, `config/.env` para el bot Python.

**Por qué:** Sexto paso — containerizar el bot con sus dependencias (PostgreSQL, Redis) para correr 24/7.
**Impacto:** Dockerfile, docker-compose.yml, .dockerignore, .env, config/.env, docs/

## [2026-03-04] — Docs sync + trailing stop v2 roadmap
**Qué cambió:**
- `docs/context/00-architecture.md` — Estado actualizado a "5/5 capas implementadas". Removido Coinglass. Tabla de estado por capa con conteo de tests (280 total). Sección roadmap v2 con trailing stop y otras mejoras.
- `docs/context/01-data-service.md` — main.py ya no es "stub", pipeline completo.
- `docs/context/03-ai-filter.md` — "adjustments" actualizado: Execution Service ya existe, aplicar ajustes es para v2.
- `docs/context/04-risk.md` — 3 referencias a "Execution Service (futuro)" actualizadas a "(implementado)". Max trade duration ahora es enforceado por PositionMonitor.
- `docs/context/05-execution.md` — Roadmap v2 detallado para trailing stop: API de OKX, settings nuevos, cambios en monitor/executor, consideraciones de testing y fallback.

**Por qué:** Los docs referenciaban Execution Service como futuro cuando ya está implementado. El usuario pidió documentar trailing stop para v2.
**Impacto:** docs/context/ (6 archivos)

## [2026-03-04] — Execution Service — Layer 5 implementado
**Qué cambió:**
- `execution_service/models.py` — ManagedPosition: estado mutable del ciclo de vida de cada trade (phase, order IDs, fills, PnL)
- `execution_service/executor.py` — OrderExecutor: wrapper ccxt para OKX (limit, stop-market, TP, cancel, fetch, emergency close, fetch_position)
- `execution_service/monitor.py` — PositionMonitor: loop async polling cada 5s, máquina de estados (pending_entry → active → tp1_hit → tp2_hit → closed)
- `execution_service/service.py` — ExecutionService facade: execute(), start(), stop(), health()
- `execution_service/__init__.py` — Exporta ExecutionService
- `config/settings.py` — 4 settings nuevos: ENTRY_TIMEOUT_SECONDS, ORDER_POLL_INTERVAL, MARGIN_MODE, MAX_TRADE_DURATION_SECONDS
- `main.py` — Pipeline completo 5 capas: Data → Strategy → AI → Risk → Execution. Variable scoping fix para decision/approval. Graceful shutdown del Execution Service.
- `tests/test_execution.py` — 20 tests (facade, entry fill/timeout, TP1/TP2/SL lifecycle, emergency close, slippage, PnL, health)
- `docs/context/05-execution.md` — Documentación completa del servicio

**Por qué:** Quinto y último paso del build order. Sin Execution Service, los trades aprobados nunca se ejecutaban en OKX.
**Impacto:** execution_service/, config/settings.py, main.py, tests/, docs/context/

## [2026-03-03] — Strategy Service review — 6 fixes
**Qué cambió:**
- `strategy_service/market_structure.py` — Solo un break por candle. Si una vela grande rompe múltiples swing levels, solo se registra el más significativo (mayor distancia). Los demás se marcan como consumidos.
- `strategy_service/setups.py` — 3 fixes:
  - **Orden temporal en Setup A**: sweep debe ocurrir ANTES del CHoCH, con proximidad máxima configurable (`SETUP_A_MAX_SWEEP_CHOCH_GAP=20` candles).
  - **R:R blended**: validación ponderada real (50%×TP1 + 30%×TP2 + 20%×TP3) en vez de check muerto contra TP2.
  - **Proximidad OB**: margen basado en % del precio (`OB_PROXIMITY_PCT=0.3%`) en vez de 50% del body del OB.
- `strategy_service/liquidity.py` — 2 fixes:
  - **Equilibrium band**: zona equilibrium es ahora 48%-52% (±`PD_EQUILIBRIUM_BAND`), no exacto 50%.
  - **Swept persistence**: niveles de liquidez mantienen su estado `swept` entre llamadas via merge por proximidad de precio.
- `config/settings.py` — 3 settings nuevos: `PD_EQUILIBRIUM_BAND`, `OB_PROXIMITY_PCT`, `SETUP_A_MAX_SWEEP_CHOCH_GAP`
- `tests/` — 12 tests nuevos: blended R:R, OB proximity, temporal ordering, equilibrium band, swept persistence, single break per candle
- `docs/context/02-strategy.md` — Documentación actualizada con todos los cambios

**Por qué:** Revisión del @planner encontró 6 issues (3 críticos, 2 significativos, 1 menor). 192/192 tests passing.
**Impacto:** strategy_service/, config/settings.py, tests/, docs/context/

## [2026-03-03] — Binance → OI Proxy migration
**Qué cambió:**
- `data_service/oi_liquidation_proxy.py` — Nuevo módulo: detecta cascadas de liquidación via OI drops >2% en 5min. Ring buffer de 12 snapshots por par. Misma API pública que BinanceLiquidationFeed.
- `data_service/service.py` — Reemplaza `BinanceLiquidationFeed` por `OILiquidationProxy`. OI proxy alimentado desde `_oi_loop()` y `_fetch_initial_indicators()`. Removido Binance async task.
- `config/settings.py` — 2 settings nuevas: `OI_DROP_THRESHOLD_PCT` (0.02), `OI_DROP_WINDOW_SECONDS` (300)
- `ai_service/prompt_builder.py` — Prompt actualizado: "OI Proxy" en vez de "Binance feed offline". Sección de liquidaciones muestra source y disclaimer.
- `tests/test_oi_proxy.py` — 13 tests nuevos (detección, aislamiento por par, stats, edge cases, threshold custom)
- Tests existentes actualizados: `source="binance_forceOrder"` → `source="oi_proxy"` en test_setups.py, test_liquidity.py, test_prompt_builder.py
- `CLAUDE.md` — 6 referencias a Binance actualizadas. oi_liquidation_proxy.py agregado a estructura.
- `docs/context/01-data-service.md` — Sección Binance reemplazada por OI proxy. FAQ actualizada.

**Por qué:** Binance Futures WebSocket (`forceOrder`) está geo-bloqueado desde Canadá. OI proxy detecta las mismas cascadas indirectamente via OKX Open Interest (ya polled cada 5 min).
**Impacto:** data_service/, ai_service/, config/, tests/, CLAUDE.md, docs/

## [2026-03-03] — Risk Service review + settings cleanup
**Qué cambió:**
- `risk_service/state_tracker.py` — Warning log cuando `record_trade_closed()` no encuentra el pair en posiciones abiertas. Previene bugs silenciosos en el pipeline.
- `config/settings.py` — Todos los comentarios y docstrings traducidos de español a inglés (cumplimiento de CLAUDE.md language rule).
- `docs/context/04-risk.md` — Actualizado: conteo correcto de tests (14 en test_risk_service.py, no 10), sección de limitaciones conocidas, FAQ sobre warning log, cambios recientes.

**Por qué:** Revisión del @planner encontró 2 issues menores. Ningún bug crítico — 69/69 tests passing, 100% alineado con CLAUDE.md.
**Impacto:** risk_service/state_tracker.py, config/settings.py, docs/context/04-risk.md

## [2026-03-03] — AI Service — Layer 3 implementado
**Qué cambió:**
- `ai_service/prompt_builder.py` — System + evaluation prompts para Claude. Interpreta funding, OI, CVD, liquidaciones, whales.
- `ai_service/claude_client.py` — Wrapper async de Anthropic SDK. Timeout 30s, 2 retries, JSON parsing, code fence stripping.
- `ai_service/service.py` — Facade AIService.evaluate(setup, snapshot) → AIDecision. Double check confidence >= 0.60. Fail-safe en todo error.
- `ai_service/__init__.py` — Exporta AIService
- `config/settings.py` — 4 settings nuevas: AI_TIMEOUT_SECONDS, AI_TEMPERATURE, AI_MAX_TOKENS, FUNDING_EXTREME_THRESHOLD
- `requirements.txt` — Agregado anthropic==0.84.0
- `main.py` — Pipeline completo Data→Strategy→AI→Risk. data_service como variable module-level. Graceful shutdown de AI client. Validación de ANTHROPIC_API_KEY.
- `tests/test_prompt_builder.py` — 14 tests (system prompt, eval prompt, datos faltantes, funding extremo)
- `tests/test_claude_client.py` — 8 tests (JSON parsing, API errors, timeout, rate limit)
- `tests/test_ai_service.py` — 12 tests (approval/rejection, confidence clamping, API failure, disabled mode)
- `docs/context/03-ai-filter.md` — Documentación completa del servicio

**Por qué:** Cuarto paso del build order. El AI filter evalúa contexto de mercado que las reglas determinísticas no capturan.
**Impacto:** ai_service/, tests/, main.py, config/, docs/context/

## [2026-03-03] — Risk Service — Layer 4 implementado
**Qué cambió:**
- `risk_service/position_sizer.py` — Calculadora: (capital × risk%) / |entry - sl|, cap a MAX_LEVERAGE (5x)
- `risk_service/guardrails.py` — 6 checks puros: R:R, cooldown, max trades/día, max posiciones, DD diario, DD semanal
- `risk_service/state_tracker.py` — Estado in-memory: trades hoy, posiciones abiertas, P&L, cooldown, auto-reset diario/semanal
- `risk_service/service.py` — Facade RiskService.check(setup) → RiskApproval. Fail fast.
- `risk_service/__init__.py` — Exporta RiskService
- `main.py` — Integrado en pipeline: setup detectado → risk.check() → log approval/rejection. Capital $100 demo.
- `tests/test_position_sizer.py` — 10 tests (fórmula, leverage cap, edge cases)
- `tests/test_guardrails.py` — 17 tests (cada regla pass/fail/boundary)
- `tests/test_state_tracker.py` — 18 tests (lifecycle, DD, cooldown, date reset)
- `tests/test_risk_service.py` — 10 tests (check() integración completa)
- `docs/context/04-risk.md` — Documentación completa del servicio
- `docs/context/02-strategy.md` — Actualizado de "pendiente" a "implementado"

**Por qué:** Tercer paso del build order. Sin Risk Service, no hay protección del capital.
**Impacto:** risk_service/, tests/, main.py, docs/context/

## [2026-03-03] — Exchange Revert — Hyperliquid → OKX
**Qué cambió:**
- Reverted ALL files from Hyperliquid back to OKX. Same files as the migration entry below, but in reverse.
- `CLAUDE.md` — OKX via ccxt + native WebSocket. Pairs: BTC/USDT, ETH/USDT. API key auth. Demo mode. Rate limits 20req/2s.
- `config/settings.py` — WALLET_PRIVATE_KEY/WALLET_ADDRESS/HYPERLIQUID_TESTNET → OKX_API_KEY/SECRET/PASSPHRASE/SANDBOX. TRADING_PAIRS back to USDT. FUNDING_RATE_INTERVAL 1h→8h.
- `shared/models.py` — All docstrings: Hyperliquid → OKX, USDC → USDT, 1h → 8h funding.
- `shared/logger.py` — Example log messages back to OKX.
- `data_service/exchange_client.py` — Full rewrite: ccxt.hyperliquid → ccxt.okx. API key/secret/passphrase auth. BTC-USDT-SWAP format. 100 candles/req.
- `data_service/websocket_feeds.py` — Full rewrite: Hyperliquid WS → OKX WS (wss://ws.okx.com:8443/ws/v5/public). OKX subscription format. Candle confirmation via confirm="1".
- `data_service/cvd_calculator.py` — Full rewrite: Hyperliquid trades WS → OKX trades WS. Side "buy"/"sell" directly.
- `data_service/binance_liq.py` — Pair mapping: BTC/USDC:USDC → BTC/USDT.
- `data_service/service.py` — HyperliquidWebSocketFeed → OKXWebSocketFeed. Task names reverted.
- `data_service/data_store.py` — Redis key examples back to USDT pairs.
- `main.py` — Config validation for OKX API key / sandbox.
- `.claude/agents/data-engineer.md` — Full rewrite: Hyperliquid API → OKX API section.
- `.claude/agents/architect.md` — Hyperliquid → OKX context.
- `.claude/agents/risk-guard.md` — Hyperliquid → OKX. DEX risk → CEX risk.
- `.claude/agents/smc-engine.md` — Hyperliquid OI proxy → OKX OI proxy.
- `docs/context/01-data-service.md` — Full rewrite for OKX.

**Por qué:** Reverted Hyperliquid migration — API geo-blocked from Canada. OKX API confirmed working from server. OKX website is blocked in Canada but the API works fine, and the bot only uses the API.
**Impacto:** Todo el proyecto excepto etherscan_client.py.

## [2026-03-03] — Exchange Migration — OKX → Hyperliquid
**Qué cambió:**
- `CLAUDE.md` — Exchange changed to Hyperliquid (DEX). Pairs: BTC/USDC:USDC, ETH/USDC:USDC. Settlement in USDC. Wallet auth. Funding hourly. Rate limits updated.
- `config/settings.py` — OKX_API_KEY/SECRET/PASSPHRASE/SANDBOX → WALLET_PRIVATE_KEY/WALLET_ADDRESS/HYPERLIQUID_TESTNET. TRADING_PAIRS to USDC. FUNDING_RATE_INTERVAL 8h→1h.
- `shared/models.py` — All docstrings updated: OKX → Hyperliquid, USDT → USDC, 8h → 1h funding.
- `data_service/exchange_client.py` — Full rewrite: ccxt.okx → ccxt.hyperliquid. Wallet key auth. Symbol format BTC/USDC:USDC.
- `data_service/websocket_feeds.py` — Full rewrite: OKX WS → Hyperliquid WS (wss://api.hyperliquid.xyz/ws). New subscription format. Candle confirmation via open-time tracking (no confirm flag).
- `data_service/cvd_calculator.py` — Full rewrite: OKX trades WS → Hyperliquid trades WS. Side mapping B/A → buy/sell.
- `data_service/binance_liq.py` — Pair mapping updated (BTCUSDT → BTC/USDC:USDC). Binance WS itself unchanged (public, no account needed).
- `data_service/service.py` — OKXWebSocketFeed → HyperliquidWebSocketFeed. Comments updated.
- `data_service/data_store.py` — Redis key examples updated to USDC pairs.
- `main.py` — Config validation updated for wallet key / testnet.
- `.claude/agents/data-engineer.md` — Entire OKX API section → Hyperliquid API section. File structure updated.
- `.claude/agents/architect.md` — OKX section → Hyperliquid section. DEX context added.
- `.claude/agents/risk-guard.md` — OKX refs → Hyperliquid. Exchange risk updated for DEX.
- `.claude/agents/smc-engine.md` — OKX OI proxy → Hyperliquid OI proxy.
- `docs/context/01-data-service.md` — Full rewrite for Hyperliquid.

**Por qué:** Migrated from OKX to Hyperliquid — Canada restricts all major CEX derivatives (OKX, Binance, Bybit, Kraken). Hyperliquid is a DEX with no KYC and no geographic restrictions.
**Impacto:** Todo el proyecto excepto etherscan_client.py y shared/logger.py.

## [2026-03-03] — Data Service — CVD, Etherscan, Data Store
**Qué cambió:**
- `data_service/cvd_calculator.py` — OKX trades WebSocket with 5-second batching, CVD for 5m/15m/1h windows
- `data_service/etherscan_client.py` — Whale wallet monitoring, exchange deposit/withdrawal detection
- `data_service/data_store.py` — Redis (real-time state cache with TTLs) + PostgreSQL (historical candles, 4 tables from CLAUDE.md schema)
- `config/settings.py` — Added WHALE_MIN_ETH, WHALE_HIGH_ETH, WHALE_WALLETS, EXCHANGE_ADDRESSES
- `docs/context/01-data-service.md` — Complete documentation of all 7 data service modules

**Por qué:** Complete the Data Service with all data sources before building Strategy Service.
**Impacto:** data_service/, config/, docs/

## [2026-03-03] — Data Service — Core implementation
**Qué cambió:**
- `shared/models.py` — 10 dataclasses (Candle, FundingRate, OpenInterest, CVDSnapshot, LiquidationEvent, WhaleMovement, MarketSnapshot, TradeSetup, AIDecision, RiskApproval)
- `shared/logger.py` — loguru setup with stdout + daily rotated files, 30-day retention
- `data_service/exchange_client.py` — OKX REST via ccxt (backfill 500 candles, funding rate, OI)
- `data_service/websocket_feeds.py` — OKX WebSocket for real-time candles (8 channels, confirm="1" only)
- `data_service/binance_liq.py` — Binance Futures WebSocket for liquidation data (forceOrder channel)
- `config/settings.py` — removed Coinglass references (deferred to future phase)
- `docs/context/01-data-service.md` — full documentation of implemented modules

**Por qué:** First step in build order. Data Service must work before Strategy Service can detect patterns.
**Impacto:** shared/, data_service/, config/, docs/

## [2026-03-03] — Proyecto — Setup inicial
**Qué cambió:** Estructura completa del proyecto creada con 5 servicios, agentes, documentación.
**Por qué:** Primer día de construcción del bot.
**Impacto:** Todo el proyecto.
