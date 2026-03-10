# Data Service
> Last updated: 2026-03-10
> Status: implemented (complete, running in Docker). Audited — 4 CRITICAL fixes applied. Whale tracking with USD enrichment, 3-tier Telegram notifications, new whale wallets (Trump, Jump Trading, a16z, FTX/Alameda, UK Gov BTC). News sentiment (Fear & Greed + headlines) as new data layer.

## What it does (30 seconds)
The Data Service is the bot's eyes and ears. It connects to OKX 24/7, collecting price data (candles), trade flow (CVD), market indicators (funding rate, open interest), liquidation cascades (via OI proxy), and whale movements. Every other service gets clean, validated, typed data through here.

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
| OI Proxy | Liquidation cascades | OI drop >2% in 5min = cascade | Every 5 minutes (fed by OI poll) |
| Etherscan REST | ETH whale movements | Transaction polling, 5 calls/sec limit | Every 5 minutes |
| mempool.space REST | BTC whale movements | UTXO transaction polling, no API key | Every 5 minutes |
| alternative.me REST | Fear & Greed Index | `GET /fng/?limit=1`, no API key | Every 5 minutes (cached 30min) |
| cryptocurrency.cv REST | Crypto news headlines | `GET /api/news?asset=BTC&limit=5`, User-Agent required | Every 5 minutes (cached 5min) |

### Pipeline Flow
```
1. Startup: connect Redis + PostgreSQL
2. Backfill 500 candles per pair/timeframe via OKX REST (ccxt) → store in PostgreSQL + memory
3. Connect OKX WebSocket → candle channels (8 total: 2 pairs × 4 timeframes)
4. Connect OKX WebSocket → trades channel (2 pairs) for CVD calculation
5. Start Etherscan polling loop (whale wallets every 5 min)
6. Start news sentiment polling (F&G + headlines every 5 min)
7. Start funding rate polling (every 8 hours) and OI polling (every 5 min)
8. On confirmed candle (confirm="1"):
   → Store in memory + Redis + PostgreSQL
   → Trigger pipeline callback: Strategy → AI → Risk → Execution
```

### Communication Model
All 5 layers run in the same Python process. The Data Service exposes methods that other services call directly via import. No pub/sub, no message queues.

## Implemented Files

### `shared/models.py` — Typed Dataclasses
12 frozen dataclasses shared across all layers:
- **Candle** — OHLCV with pair, timeframe, confirmed flag
- **FundingRate** — current rate + next estimated + next funding time
- **OpenInterest** — in contracts, base currency, and USD
- **CVDSnapshot** — cumulative volume delta for 5m, 15m, 1h windows + buy/sell volume
- **LiquidationEvent** — from OI proxy (OI drop >2% = cascade), with side and size_usd
- **WhaleMovement** — Whale transfers (ETH via Etherscan, BTC via mempool.space). 4 action types: `exchange_deposit` (bearish), `exchange_withdrawal` (bullish), `transfer_out` (neutral), `transfer_in` (neutral). Fields: `amount` (ETH or BTC), `chain` ("ETH" or "BTC"), `exchange` (exchange name or truncated address), `wallet_label` (human-readable name from settings, e.g., "Vitalik Buterin"), `amount_usd` (USD value at detection time), `market_price` (asset price in USD when detected). USD fields default to 0.0 if price provider unavailable.
- **NewsHeadline** — single news headline (title, source, timestamp, category)
- **NewsSentiment** — Fear & Greed score (0-100) + label + recent headlines + fetched_at
- **MarketSnapshot** — wraps funding, OI, CVD, liquidations, whales, news_sentiment for a pair
- **TradeSetup** — detected setup from Strategy Service
- **AIDecision** — Claude's evaluation with confidence score
- **RiskApproval** — final risk check with position size and leverage

All frozen (immutable) except MarketSnapshot which has optional fields.

### `shared/logger.py` — Loguru Configuration
- **Test isolation**: `_is_testing()` detecta pytest via `sys.modules`. Bajo pytest, `setup_logger()` solo agrega stderr (WARNING+), sin file sinks. Previene que Mock objects y fixture data contaminen los logs de producción.
- Format: `{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | {name}:{function}:{line} | {message}`
- stdout: all levels (for Docker logs)
- File: `logs/{service}_{date}.log` — daily rotation, 30-day retention, gzip compressed

### `data_service/exchange_client.py` — OKX REST via ccxt
Six methods:
- `fetch_usdt_balance()` → `float | None`
  - Fetches USDT available balance from exchange via `fetch_balance()`
  - Returns `None` on any failure (logged as warning)
  - Used by `main.py` at startup to set initial capital for Risk Service
- `backfill_candles(pair, timeframe, count=500)` → `list[Candle]`
  - OKX returns max 100 candles per request — ccxt paginates automatically
  - All returned candles are confirmed (historical)
- `fetch_funding_rate(pair)` → `FundingRate | None`
- `fetch_open_interest(pair)` → `OpenInterest | None`
- `fetch_funding_rate_history(pair, since_ms, limit)` → `list[dict]`
  - Historical funding rates via ccxt. OKX provides ~3 months of history. Max 100 per request.
  - Returns `{timestamp, rate, next_rate}` dicts. Used by `fetch_history.py` for backtest backfill.
- `fetch_open_interest_history(pair, since_ms, limit, timeframe)` → `list[dict]`
  - Historical OI via ccxt. OKX limits: 1h goes back ~30 days, 1D ~99 days.
  - Returns `{timestamp, oi_contracts, oi_base, oi_usd}` dicts. Only `oi_usd` is populated (OKX limitation).

Auth: API key + secret + passphrase via ccxt. Market data is public, but auth is needed for trading.
Instrument format: `BTC-USDT-SWAP` (hyphens). ccxt translates `BTC/USDT:USDT` internally.
Data validation on every candle: price ≤ 0 → ERROR, volume = 0 → WARNING, future timestamp → WARNING. All invalid data discarded.

### `data_service/websocket_feeds.py` — OKX Candle WebSocket
- Connects to `wss://ws.okx.com:8443/ws/v5/business` (candle channels live here, NOT on `/public`)
- Subscribes to 8 channels: 2 instIds × 4 timeframes (candle5m, candle15m, candle1H, candle4H)
- **Candle confirmation:** OKX sends `confirm="1"` when candle is closed — only these are processed
- **Volume units:** Uses `candle_data[6]` (volCcy = base currency) instead of `candle_data[5]` (vol = contracts). This matches ccxt REST backfill which returns volume in base currency. OKX candle format: `[ts, o, h, l, c, vol(contracts), volCcy(base), volCcyQuote(quote), confirm]`. Fix applied 2026-03-09 — previously used contracts, causing 100x volume mismatch for BTC (ctVal=0.01) and 10x for ETH (ctVal=0.1), which broke OB volume filter detection.
- Stores last 600 candles per pair/timeframe in memory
- Public methods: `get_latest_candle(pair, tf)`, `get_candles(pair, tf, count)`
- `store_candles()` accepts backfilled candles with deduplication
- **Pipeline serialization:** Per-pair `asyncio.Lock` prevents concurrent pipeline runs on the same pair. Exception logging via `task.add_done_callback()`.
- Callback `on_candle_confirmed` triggers the main pipeline
- Handles OKX text "pong" keepalive messages
- Reconnection: exponential backoff 1s → 2s → 4s → ... → 60s max
- **Metrics callback:** Optional `metrics_callback` parameter. Emits `ws_reconnection` metric on each disconnect (for Grafana System Health dashboard).

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

### `data_service/oi_liquidation_proxy.py` — OI-Based Liquidation Proxy
- Detects liquidation cascades from OI drops (>2% in 5 minutes)
- Fed by DataService's `_oi_loop()` — no separate async task needed
- Ring buffer of last 12 OI snapshots per pair (1 hour of history)
- When OI drops ≥ `OI_DROP_THRESHOLD_PCT` in `OI_DROP_WINDOW_SECONDS`, generates `LiquidationEvent(source="oi_proxy")`
- Same public API as the old Binance feed: `get_recent_liquidations()`, `get_aggregated_stats()`, `is_connected`
- **Why:** Binance WebSocket is geo-blocked from Canada. OI proxy detects the same cascades indirectly.
- **Limitation:** Cannot detect individual liquidations — only aggregate cascades. No directional info (defaults to "long").
- Auto-prunes events older than 1 hour

### `data_service/etherscan_client.py` — ETH Whale Wallet Monitor
- Polls 46 configured wallets every `ETHERSCAN_CHECK_INTERVAL` seconds (default 300)
- Wallet categories: individual whales, institutional funds, trading firms (Jump Trading ×2), VC (a16z), political/insider (Trump ×2, WLFI), FTX/Alameda court liquidations (FTX ×2, Alameda), Ethereum Foundation, unlabeled mega-wallets
- Constructor accepts `price_provider` callback (returns current ETH price in USD) for USD enrichment
- Detects ALL large transfers from monitored wallets (not just exchange transfers)
- Whale → exchange = `exchange_deposit` (bearish signal)
- Exchange → whale = `exchange_withdrawal` (bullish signal)
- Whale → non-exchange = `transfer_out` (neutral, `exchange` = truncated address)
- Non-exchange → whale = `transfer_in` (neutral, `exchange` = truncated address)
- Significance: >100 ETH = "high", >10 ETH = "medium", <10 ETH ignored
- Rate limit enforced: max 4.5 calls/sec (safely under Etherscan's 5/sec)
- Creates `WhaleMovement(chain="ETH", wallet_label=label, amount_usd=..., market_price=...)` — USD computed at detection time
- Log format: `BEARISH|BULLISH|NEUTRAL Whale {action}: {label} → {dest} {amount} ETH (~$USD) [{significance}]`

### `data_service/btc_whale_client.py` — BTC Whale Wallet Monitor
- Polls 11 configured wallets every `MEMPOOL_CHECK_INTERVAL` seconds (default 300)
- Wallet categories: government seizures (US Gov Silk Road, UK Government ~61K BTC ×2 wallets), exchange hack recovery (Bitfinex), individual mega-wallets
- Removed (2026-03-09): El Salvador, Mt. Gox ×2, Block.one, Unknown 79K — mempool.space returns HTTP 400 "Invalid Bitcoin address" for these high-UTXO addresses
- Constructor accepts `price_provider` callback (returns current BTC price in USD) for USD enrichment
- API: `https://mempool.space/api/address/{addr}/txs` — no API key needed
- Parses BTC UTXO model: `vin[].prevout.scriptpubkey_address` (senders) and `vout[].scriptpubkey_address` (recipients)
- Values in satoshis (÷ 1e8 = BTC)
- Detects ALL large transfers from monitored wallets:
  - Wallet → exchange = `exchange_deposit` (bearish)
  - Exchange → wallet = `exchange_withdrawal` (bullish)
  - Wallet → non-exchange = `transfer_out` (neutral, sums non-self outputs)
  - Non-exchange → wallet = `transfer_in` (neutral)
- Significance: >100 BTC = "high", >10 BTC = "medium", <10 BTC ignored
- Rate limit: 0.5s between calls (~10 req/min, safe for public instance)
- Creates `WhaleMovement(chain="BTC", wallet_label=label, amount_usd=..., market_price=...)` — USD computed at detection time
- Log format: `BEARISH|BULLISH|NEUTRAL BTC whale {action}: {label} → {dest} {amount} BTC (~$USD) [{significance}]`

### `data_service/news_client.py` — News Sentiment Client
- Class: `NewsClient(redis_store=None)`
- Two data sources:
  - **alternative.me** — Fear & Greed Index (0-100 score, free, no API key, running since 2018)
  - **cryptocurrency.cv** — Crypto news headlines (free, requires `User-Agent` header for Cloudflare)
- Methods:
  - `fetch_fear_greed()` → `tuple[int, str] | None` — score + label ("Extreme Fear", "Fear", "Neutral", "Greed", "Extreme Greed")
  - `fetch_headlines(asset, limit=5)` → `list[NewsHeadline]` — recent headlines for BTC or ETH
  - `fetch_sentiment()` → `NewsSentiment | None` — combines F&G + headlines (BTC 3 + ETH 2)
- Redis caching via `set_bot_state`/`get_bot_state`:
  - `news:fear_greed` — TTL 30min (`NEWS_FEAR_GREED_CACHE_TTL`)
  - `news:headlines:{asset}` — TTL 5min (`NEWS_HEADLINES_CACHE_TTL`)
- HTTP via `aiohttp` with 15s timeout, `User-Agent: QuantFundBot/1.0`
- Graceful degradation: F&G failure → `None` (pre-filter skipped). Headlines failure → empty list (Claude context omitted).
- `close()` shuts down aiohttp session

### `data_service/data_store.py` — Redis + PostgreSQL
**Redis (real-time cache):**
- Key pattern: `qf:{category}:{pair}:{detail}` (e.g., `qf:candle:BTC/USDT:5m`)
- Stores latest: candle, funding rate, OI, bot state (drawdown, cooldowns)
- TTLs prevent stale data: candles 24h, funding 9h, OI 10min
- `set_latest_candle()`, `get_latest_candle()`, `pop_cancel_request()`, etc.

**PostgreSQL (historical):**
- 8 tables: `candles`, `trades`, `ai_decisions`, `risk_events`, `bot_metrics`, `funding_rate_history`, `open_interest_history`, `cvd_history`
- `store_candles()` with batch insert + ON CONFLICT DO NOTHING (dedup)
- `load_candles()` returns oldest-first ordering
- Index on `(pair, timeframe, timestamp DESC)` for fast lookups
- **Funding rate history:** `store_funding_rate(fr)`, `store_funding_rates_batch(records)`, `load_funding_rates(pair, since_ms, until_ms)`. Populated by live polling + `fetch_history.py` backfill. Used by backtester for MarketSnapshot.
- **OI history:** `store_open_interest(oi)`, `store_open_interest_batch(records)`, `load_open_interest(pair, since_ms, until_ms)`. Same pattern. OKX 1h resolution, ~30 days back.
- **CVD history:** `store_cvd_snapshot(cvd)`, `load_cvd_snapshots(pair, since_ms, until_ms)`. Persisted on every confirmed candle. Accumulates from 2026-03-10 onwards. Needed for Setup C/E backtesting.
- **Auto-reconnection:** `_ensure_connected()` checks connection health (sends `SELECT 1`). All DB methods (`store_candles`, `load_candles`, `insert_trade`, `update_trade`, `insert_ai_decision`, `insert_risk_event`, `insert_metric`) retry once on `psycopg2.OperationalError` / `InterfaceError` — sets `_conn = None` and reconnects.
- **Operational metrics (Grafana):** `insert_metric(name, value, pair, labels)` writes to `bot_metrics` table. `cleanup_old_metrics(retention_days=30)` deletes old rows. Both fire-and-forget.

### `data_service/service.py` — DataService Facade
- Wires all 9 sub-modules into a single interface (including NewsClient)
- Public methods: `get_latest_candle()`, `get_candles()`, `get_market_snapshot()`, `get_cvd()`, `fetch_usdt_balance()`, etc.
- Manages startup (backfill → WebSockets → polling loops) and graceful shutdown
- On confirmed candle: stores to Redis + PostgreSQL, triggers pipeline callback
- **Funding/OI/CVD persistence:** Polling loops persist funding rates and OI snapshots to PostgreSQL on every poll. CVD snapshots persisted on every confirmed candle. Building historical data for backtesting Setup C/E.
- Health check loop every 30 seconds — emits `health_status` metric (1.0=OK, 0.0=degraded) to `bot_metrics`
- **Metrics cleanup:** Every ~50 min (100 health checks), calls `cleanup_old_metrics(30)` to prune metrics older than 30 days
- **`_emit_metric()`:** Fire-and-forget metric writer to PostgreSQL `bot_metrics` table. Passed to WebSocket feeds as callback.
- Uses `asyncio.get_running_loop()` (not deprecated `get_event_loop()`)
- **Whale notification stability:** Uses `id()` snapshot before polling to detect new movements, preventing index instability if pruning occurs during poll
- **Price providers:** `_get_eth_price()` and `_get_btc_price()` return latest 5m candle close, passed to whale clients for USD conversion
- **Whale notification tiering (3-tier):**
  - Tier 1 (always notify): Exchange deposits/withdrawals — actionable trading signals
  - Tier 2 (notify if large): Non-exchange transfers with `significance == "high"` OR `amount_usd >= $500K` — likely unrecognized exchange addresses
  - Tier 3 (log only): Small non-exchange transfers — too noisy for Telegram
- **Whale notification format:** Compact single-line format with USD shorthand ($1.2M, $500K). Wallet label preferred, fallback to truncated address.
- **Market maker filtering:** `MARKET_MAKER_WALLETS` set (Cumberland, Galaxy, Wintermute, etc.) — only notify on `significance == "high"`. Data still collected for AI context.
- **News sentiment polling:** `_news_sentiment_loop()` fetches F&G + headlines every `NEWS_POLL_INTERVAL` (5min). Stores latest `NewsSentiment` in `_latest_sentiment`. Included in `get_market_snapshot()`. Initial fetch on startup, then periodic.

### `main.py` — Entry Point
- Single process, handles SIGINT/SIGTERM for graceful shutdown
- Creates DataService with pipeline callback
- **Capital at startup:** Fetches USDT balance from exchange via `data_service.fetch_usdt_balance()`. Falls back to `INITIAL_CAPITAL` setting if fetch fails or returns 0.
- Pipeline completo: Data → Strategy → Pre-filter → AI → Risk → Execution (5 capas wired)
- Pre-filter determinístico antes de Claude: funding extreme + F&G extreme + CVD divergencia
- AI filter obligatorio en todas las profiles (sin bypass)
- 4H OB summary: cuando cierra la vela 4H, envía resumen de OBs activos via Telegram
- **Pipeline metrics:** `_emit_metric()` helper writes to `bot_metrics`. Emits `pipeline_latency_ms` (per candle) and `claude_latency_ms` (per AI evaluation).

## Configuration (`config/settings.py`)

| Setting | Default | Used by |
|---|---|---|
| `INITIAL_CAPITAL` | `100` (env) | Fallback capital if exchange balance fetch fails |
| `TRADE_CAPITAL_PCT` | `0.15` (15%) | % of capital as notional per trade (replaces FIXED_TRADE_MARGIN) |
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
| `WHALE_MIN_BTC` | `10.0` | BTC whale filter threshold |
| `WHALE_HIGH_BTC` | `100.0` | BTC significance threshold |
| `MEMPOOL_CHECK_INTERVAL` | `300` (5min) | BTC polling schedule |
| `RECONNECT_INITIAL_DELAY` | `1.0` | Backoff start |
| `RECONNECT_MAX_DELAY` | `60.0` | Backoff ceiling |
| `OI_DROP_THRESHOLD_PCT` | `0.02` (2%) | OI proxy cascade threshold |
| `OI_DROP_WINDOW_SECONDS` | `300` (5min) | OI proxy measurement window |
| `RECONNECT_BACKOFF_FACTOR` | `2.0` | Backoff multiplier |
| `NEWS_SENTIMENT_ENABLED` | `True` | Enable/disable news sentiment fetching |
| `NEWS_FEAR_GREED_URL` | `https://api.alternative.me/fng/` | Fear & Greed API endpoint |
| `NEWS_HEADLINES_URL` | `https://cryptocurrency.cv/api/news` | Headlines API endpoint |
| `NEWS_POLL_INTERVAL` | `300` (5min) | News sentiment polling interval |
| `NEWS_FEAR_GREED_CACHE_TTL` | `1800` (30min) | Redis cache TTL for F&G score |
| `NEWS_HEADLINES_CACHE_TTL` | `300` (5min) | Redis cache TTL for headlines |
| `NEWS_EXTREME_FEAR_THRESHOLD` | `15` | F&G < 15 → reject longs (pre-filter) |
| `NEWS_EXTREME_GREED_THRESHOLD` | `85` | F&G > 85 → reject shorts (pre-filter) |

## FAQ

**Why OKX if the website is blocked in Canada?**
The OKX website is geo-blocked, but the API works without issues. We tested from our server — `https://www.okx.com/api/v5/public/instruments` returns data. The bot only uses the API, never the website.

**Why an OI proxy instead of Binance for liquidations?**
Binance Futures WebSocket (`forceOrder`) is geo-blocked from Canada where our server is located. Instead, we detect liquidation cascades indirectly: when OI drops >2% in 5 minutes, significant positions were force-closed. Not as granular as individual events, but sufficient for the Strategy Service's cascade detection.

**Why 500 candles for backfill?**
Strategy needs history for swing highs/lows, pattern detection, volume averages. 500 × 5min = ~42 hours. 500 × 4H = ~83 days. OKX returns max 100 per request, so we paginate (5 requests per pair/timeframe).

**Why frozen dataclasses?**
Immutability prevents accidental mutation. A candle that was valid at creation should never change.

**Why batch CVD every 5 seconds instead of per-trade?**
Trades can reach 50-100/sec during volatility. Processing each individually would burn CPU for marginal benefit. 5-second batches give smooth, usable CVD values.

**How does candle confirmation work?**
OKX sends candle updates with a `confirm` field. `confirm="0"` means the candle is still forming. `confirm="1"` means it's closed. We only store and process candles with `confirm="1"`.
