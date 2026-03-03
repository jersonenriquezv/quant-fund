# Changelog — One-Man Quant Fund

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
