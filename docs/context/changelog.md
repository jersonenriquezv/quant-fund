# Changelog — One-Man Quant Fund

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
