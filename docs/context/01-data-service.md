# Data Service
> Last updated: 2026-03-03
> Status: implemented (core complete, ready for integration)

## What it does (30 seconds)
The Data Service is the bot's eyes and ears. It connects to OKX and Binance 24/7, collecting price data (candles), trade flow (CVD), market indicators (funding rate, open interest), liquidation events, and whale movements. Every other service gets clean, validated, typed data through here.

## Why it exists
Without real-time market data, the Strategy Service has nothing to analyze. Without historical candles, it can't detect patterns. The Data Service ensures every layer gets typed dataclasses from `shared/models.py` — never raw dicts, never stale prices.

## How it works (5 minutes)

### Data Sources

| Source | What | How | Frequency |
|---|---|---|---|
| OKX WebSocket | Candles (5m, 15m, 1h, 4h) | `candle{tf}` channel per instId | Real-time, on candle close (confirm="1") |
| OKX WebSocket | Trades (for CVD) | `trades` channel per instId, batched every 5 seconds | Real-time |
| OKX REST (ccxt) | Historical candles | `fetch_ohlcv()` via ccxt | On startup (backfill 500) |
| OKX REST (ccxt) | Funding rate | `fetch_funding_rate()` via ccxt | Every 8 hours |
| OKX REST (ccxt) | Open Interest | `fetch_open_interest()` via ccxt | Every 5 minutes |
| Binance Futures WS | Liquidations | `forceOrder` channel, no auth | Real-time |
| Etherscan REST | Whale movements | Transaction polling, 5 calls/sec limit | Every 5 minutes |

### Pipeline Flow
```
1. Startup: connect Redis + PostgreSQL
2. Backfill 500 candles per pair/timeframe via OKX REST (ccxt) → store in PostgreSQL + memory
3. Connect OKX WebSocket → candle channels (8 total: 2 pairs × 4 timeframes)
4. Connect OKX WebSocket → trades channel (2 pairs) for CVD calculation
5. Connect Binance Futures WS → forceOrder (liquidations)
6. Start Etherscan polling loop (whale wallets every 5 min)
7. Start funding rate polling (every 8 hours) and OI polling (every 5 min)
8. On confirmed candle (confirm="1"):
   → Store in memory + Redis + PostgreSQL
   → Trigger pipeline callback: Strategy → AI → Risk → Execution
```

### Communication Model
All 5 layers run in the same Python process. The Data Service exposes methods that other services call directly via import. No pub/sub, no message queues.

## Implemented Files

### `shared/models.py` — Typed Dataclasses
10 frozen dataclasses shared across all layers:
- **Candle** — OHLCV with pair, timeframe, confirmed flag
- **FundingRate** — current rate + next estimated + next funding time
- **OpenInterest** — in contracts, base currency, and USD
- **CVDSnapshot** — cumulative volume delta for 5m, 15m, 1h windows + buy/sell volume
- **LiquidationEvent** — from Binance forceOrder or OI proxy, with side and size_usd
- **WhaleMovement** — Etherscan whale transfers to/from exchanges
- **MarketSnapshot** — wraps funding, OI, CVD, liquidations, whales for a pair
- **TradeSetup** — detected setup from Strategy Service
- **AIDecision** — Claude's evaluation with confidence score
- **RiskApproval** — final risk check with position size and leverage

All frozen (immutable) except MarketSnapshot which has optional fields.

### `shared/logger.py` — Loguru Configuration
- Format: `{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | {name}:{function}:{line} | {message}`
- stdout: all levels (for Docker logs)
- File: `logs/{service}_{date}.log` — daily rotation, 30-day retention, gzip compressed

### `data_service/exchange_client.py` — OKX REST via ccxt
Three methods:
- `backfill_candles(pair, timeframe, count=500)` → `list[Candle]`
  - OKX returns max 100 candles per request — ccxt paginates automatically
  - All returned candles are confirmed (historical)
- `fetch_funding_rate(pair)` → `FundingRate | None`
- `fetch_open_interest(pair)` → `OpenInterest | None`

Auth: API key + secret + passphrase via ccxt. Market data is public, but auth is needed for trading.
Instrument format: `BTC-USDT-SWAP` (hyphens). ccxt translates `BTC/USDT:USDT` internally.
Data validation on every candle: price ≤ 0 → ERROR, volume = 0 → WARNING, future timestamp → WARNING. All invalid data discarded.

### `data_service/websocket_feeds.py` — OKX Candle WebSocket
- Connects to `wss://ws.okx.com:8443/ws/v5/business` (candle channels live here, NOT on `/public`)
- Subscribes to 8 channels: 2 instIds × 4 timeframes (candle5m, candle15m, candle1H, candle4H)
- **Candle confirmation:** OKX sends `confirm="1"` when candle is closed — only these are processed
- Stores last 600 candles per pair/timeframe in memory
- Public methods: `get_latest_candle(pair, tf)`, `get_candles(pair, tf, count)`
- `store_candles()` accepts backfilled candles with deduplication
- Callback `on_candle_confirmed` triggers the main pipeline
- Handles OKX text "pong" keepalive messages
- Reconnection: exponential backoff 1s → 2s → 4s → ... → 60s max

### `data_service/cvd_calculator.py` — OKX Trades WebSocket + CVD
- Connects to `wss://ws.okx.com:8443/ws/v5/public` (trades are on `/public`, separate connection from candle feed on `/business`)
- Subscribes to `trades` channel for BTC-USDT-SWAP and ETH-USDT-SWAP
- Side: `"buy"` or `"sell"` directly from OKX (no mapping needed)
- **Batching:** Accumulates raw trades in deques, recalculates every 5 seconds
- Rolling windows: 5 minutes, 15 minutes, 1 hour
- CVD formula: `sum(size if buy, -size if sell)` per window
- Tracks total buy_volume and sell_volume (1h window)
- Auto-prunes trades older than 1 hour
- Public method: `get_cvd(pair)` → `CVDSnapshot | None`

### `data_service/binance_liq.py` — Binance Liquidation WebSocket
- Connects to `wss://fstream.binance.com/ws/!forceOrder@arr` (public, no auth)
- **Independent of trading exchange** — Binance liquidation data is exchange-independent
- Filters only BTCUSDT and ETHUSDT, ignores all other pairs
- Maps to our pairs: BTCUSDT → BTC/USDT, ETHUSDT → ETH/USDT
- Parses: SELL side = long liquidated, BUY side = short liquidated
- `get_recent_liquidations(pair, minutes)` → `list[LiquidationEvent]`
- `get_aggregated_stats(pair, minutes)` → `{total_usd, long_usd, short_usd, count}`
- Auto-prunes events older than 1 hour

### `data_service/etherscan_client.py` — Whale Wallet Monitor
- Polls configured wallets every `ETHERSCAN_CHECK_INTERVAL` seconds (default 300)
- **Unchanged** — Etherscan monitoring is independent of trading exchange
- Detects transfers to/from known exchange deposit addresses
- Whale → exchange = `exchange_deposit` (bearish signal)
- Exchange → whale = `exchange_withdrawal` (bullish signal)
- Significance: >100 ETH = "high", >10 ETH = "medium", <10 ETH ignored
- Rate limit enforced: max 4.5 calls/sec (safely under Etherscan's 5/sec)

### `data_service/data_store.py` — Redis + PostgreSQL
**Redis (real-time cache):**
- Key pattern: `qf:{category}:{pair}:{detail}` (e.g., `qf:candle:BTC/USDT:5m`)
- Stores latest: candle, funding rate, OI, bot state (drawdown, cooldowns)
- TTLs prevent stale data: candles 24h, funding 9h, OI 10min
- `set_latest_candle()`, `get_latest_candle()`, etc.

**PostgreSQL (historical):**
- 4 tables matching CLAUDE.md schema: `candles`, `trades`, `ai_decisions`, `risk_events`
- `store_candles()` with batch insert + ON CONFLICT DO NOTHING (dedup)
- `load_candles()` returns oldest-first ordering
- Index on `(pair, timeframe, timestamp DESC)` for fast lookups

### `data_service/service.py` — DataService Facade
- Wires all 7 sub-modules into a single interface
- Public methods: `get_latest_candle()`, `get_candles()`, `get_market_snapshot()`, `get_cvd()`, etc.
- Manages startup (backfill → WebSockets → polling loops) and graceful shutdown
- On confirmed candle: stores to Redis + PostgreSQL, triggers pipeline callback
- Health check loop every 30 seconds

### `main.py` — Entry Point
- Single process, handles SIGINT/SIGTERM for graceful shutdown
- Creates DataService with pipeline callback
- Pipeline stub ready for Strategy → AI → Risk → Execution wiring

## Configuration (`config/settings.py`)

| Setting | Default | Used by |
|---|---|---|
| `OKX_SANDBOX` | `true` | exchange_client — demo vs live |
| `OKX_API_KEY` | `""` | exchange_client — auth |
| `OKX_SECRET` | `""` | exchange_client — auth |
| `OKX_PASSPHRASE` | `""` | exchange_client — auth |
| `TRADING_PAIRS` | `["BTC/USDT", "ETH/USDT"]` | All modules |
| `HTF_TIMEFRAMES` | `["4h", "1h"]` | WS subscriptions |
| `LTF_TIMEFRAMES` | `["15m", "5m"]` | WS subscriptions |
| `FUNDING_RATE_INTERVAL` | `28800` (8h) | Polling schedule |
| `OI_CHECK_INTERVAL` | `300` (5min) | Polling schedule |
| `ETHERSCAN_CHECK_INTERVAL` | `300` (5min) | Polling schedule |
| `WHALE_MIN_ETH` | `10.0` | Etherscan filter threshold |
| `WHALE_HIGH_ETH` | `100.0` | Significance threshold |
| `RECONNECT_INITIAL_DELAY` | `1.0` | Backoff start |
| `RECONNECT_MAX_DELAY` | `60.0` | Backoff ceiling |
| `RECONNECT_BACKOFF_FACTOR` | `2.0` | Backoff multiplier |

## FAQ

**Why OKX if the website is blocked in Canada?**
The OKX website is geo-blocked, but the API works without issues. We tested from our server — `https://www.okx.com/api/v5/public/instruments` returns data. The bot only uses the API, never the website.

**Why Binance for liquidations if we don't trade there?**
OKX has no public liquidation endpoint. Binance's `forceOrder` is public, free, and real-time. No API key or account needed. BTC/ETH liquidations are correlated across exchanges within seconds.

**Why 500 candles for backfill?**
Strategy needs history for swing highs/lows, pattern detection, volume averages. 500 × 5min = ~42 hours. 500 × 4H = ~83 days. OKX returns max 100 per request, so we paginate (5 requests per pair/timeframe).

**Why frozen dataclasses?**
Immutability prevents accidental mutation. A candle that was valid at creation should never change.

**Why batch CVD every 5 seconds instead of per-trade?**
Trades can reach 50-100/sec during volatility. Processing each individually would burn CPU for marginal benefit. 5-second batches give smooth, usable CVD values.

**How does candle confirmation work?**
OKX sends candle updates with a `confirm` field. `confirm="0"` means the candle is still forming. `confirm="1"` means it's closed. We only store and process candles with `confirm="1"`.
