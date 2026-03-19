# Data Service
> Last updated: 2026-03-11 (news headlines switched from CryptoPanic to CryptoCompare — free, no API key)
> Status: implemented (complete, running in Docker). Audited — 4 CRITICAL fixes applied. Whale tracking with USD enrichment, 3-tier Telegram notifications, new whale wallets (Trump, Jump Trading, a16z, FTX/Alameda, UK Gov BTC). News sentiment (Fear & Greed + headlines) as new data layer. HTF campaigns: 1D candle support + campaigns table.

## What it does (30 seconds)
The Data Service is the bot's eyes and ears. It connects to OKX 24/7, collecting price data (candles), trade flow (CVD), market indicators (funding rate, open interest), liquidation cascades (via OI proxy), and whale movements. Every other service gets clean, validated, typed data through here.

## Why it exists
Without real-time market data, the Strategy Service has nothing to analyze. Without historical candles, it can't detect patterns. The Data Service ensures every layer gets typed dataclasses from `shared/models.py` — never raw dicts, never stale prices.

## How it works (5 minutes)

### Data Sources

| Source | What | How | Frequency |
|---|---|---|---|
| OKX WebSocket | Candles (5m, 15m, 1h, 4h, 1d when HTF campaigns enabled) | `candle{tf}` channel per instId | Real-time, on candle close (confirm="1") |
| OKX WebSocket | Trades (for CVD) | `trades` channel per instId, batched every 5 seconds | Real-time |
| OKX REST (ccxt) | Historical candles | `fetch_ohlcv()` via ccxt | On startup (backfill 500) |
| OKX REST (ccxt) | Funding rate | `fetch_funding_rate()` via ccxt | Every 8 hours |
| OKX REST (ccxt) | Open Interest | `fetch_open_interest()` via ccxt | Every 5 minutes |
| OI Proxy | Liquidation cascades | OI drop >2% in 5min = cascade | Every 5 minutes (fed by OI poll) |
| Etherscan REST | ETH whale movements | Transaction polling, 5 calls/sec limit | Every 5 minutes |
| mempool.space REST | BTC whale movements | UTXO transaction polling, no API key | Every 5 minutes |
| alternative.me REST | Fear & Greed Index | `GET /fng/?limit=1`, no API key | Every 5 minutes (cached 30min) |
| CryptoCompare REST | Crypto news headlines | `GET /data/v2/news/?lang=EN&categories={BTC,ETH}`, free, no API key | Every 5 minutes (cached 5min) |

### Pipeline Flow
```
1. Startup: connect Redis + PostgreSQL
2. Backfill 500 candles per pair/timeframe via OKX REST (ccxt) → store in PostgreSQL + memory
3. Connect OKX WebSocket → candle channels (16 total: 4 pairs × 4 timeframes)
4. Connect OKX WebSocket → trades channel (4 pairs) for CVD calculation
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
- **FundingRate** — current rate + next estimated + next funding time + `fetched_at` (actual fetch time, default 0 for backward compat)
- **OpenInterest** — in contracts, base currency, and USD
- **CVDSnapshot** — cumulative volume delta for 5m, 15m, 1h windows + buy/sell volume
- **OIFlushEvent** — from OI flush detector (OI drop >2% = cascade), with side and size_usd
- **WhaleMovement** — Whale transfers (ETH via Etherscan, BTC via mempool.space). 4 action types: `exchange_deposit` (bearish), `exchange_withdrawal` (bullish), `transfer_out` (neutral), `transfer_in` (neutral). Fields: `amount` (ETH or BTC), `chain` ("ETH" or "BTC"), `exchange` (exchange name or truncated address), `wallet_label` (human-readable name from settings, e.g., "Vitalik Buterin"), `amount_usd` (USD value at detection time), `market_price` (asset price in USD when detected). USD fields default to 0.0 if price provider unavailable.
- **NewsHeadline** — single news headline (title, source, timestamp, category, sentiment). Sentiment is optional ("bullish"/"bearish"/None) derived from CryptoCompare community votes (upvotes vs downvotes).
- **NewsSentiment** — Fear & Greed score (0-100) + label + recent headlines + fetched_at
- **SourceFreshness** — per-source freshness status (name, priority, age_ms, is_stale)
- **SnapshotHealth** — aggregate snapshot health (completeness_pct, critical_sources_healthy, stale/missing sources, `redis_healthy`, `service_state`)
- **MarketSnapshot** — wraps funding, OI, CVD, oi_flushes, whales, news_sentiment, health for a pair
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
Supported pairs: BTC-USDT-SWAP, ETH-USDT-SWAP, SOL-USDT-SWAP, DOGE-USDT-SWAP.
Contract sizes: BTC=0.01, ETH=0.1, SOL=1.0, DOGE=1000.
Data validation on every candle: price ≤ 0 → ERROR, volume = 0 → WARNING, future timestamp → WARNING. All invalid data discarded.

### `data_service/websocket_feeds.py` — OKX Candle WebSocket
- Connects to `wss://ws.okx.com:8443/ws/v5/business` (candle channels live here, NOT on `/public`)
- Subscribes to 16 channels (base): 4 instIds × 4 timeframes (candle5m, candle15m, candle1H, candle4H). When HTF campaigns enabled, adds candle1D (+4 channels).
- **Candle confirmation:** OKX sends `confirm="1"` when candle is closed — only these are processed
- **Volume units:** Uses `candle_data[6]` (volCcy = base currency) instead of `candle_data[5]` (vol = contracts). This matches ccxt REST backfill which returns volume in base currency. OKX candle format: `[ts, o, h, l, c, vol(contracts), volCcy(base), volCcyQuote(quote), confirm]`. Fix applied 2026-03-09 — previously used contracts, causing 100x volume mismatch for BTC (ctVal=0.01) and 10x for ETH (ctVal=0.1), which broke OB volume filter detection.
- Stores last 600 candles per pair/timeframe in memory
- Public methods: `get_latest_candle(pair, tf)`, `get_candles(pair, tf, count)`
- `store_candles()` accepts backfilled candles with deduplication
- **Candle dedup:** `_last_confirmed_ts` dict tracks last confirmed candle timestamp per (pair, timeframe). Prevents duplicate pipeline runs if OKX sends the same candle twice.
- **Pipeline serialization:** Per-pair `asyncio.Lock` prevents concurrent pipeline runs on the same pair. Exception logging via `task.add_done_callback()`.
- Callback `on_candle_confirmed` triggers the main pipeline
- Handles OKX text "pong" keepalive messages
- **OHLC sanity:** Rejects candles where `low > min(open,close)` or `high < max(open,close)`. Tracks `_bad_candle_counts` per pair/tf.
- **Live candle tracking:** `_live_candle_count` resets to 0 on each connect. Warmup requires ≥1 live candle before `RUNNING`.
- **Reconnect callback:** `_on_reconnect_cb` fires after WS reconnect (set by DataService for gap backfill).
- Reconnection: exponential backoff 1s → 2s → 4s → ... → 60s max
- **Metrics callback:** Optional `metrics_callback` parameter. Emits `ws_reconnection` metric on each disconnect (for Grafana System Health dashboard).

### `data_service/cvd_calculator.py` — OKX Trades WebSocket + CVD
- Connects to `wss://ws.okx.com:8443/ws/v5/public` (trades are on `/public`, separate connection from candle feed on `/business`)
- Subscribes to `trades` channel for BTC-USDT-SWAP, ETH-USDT-SWAP, SOL-USDT-SWAP, and DOGE-USDT-SWAP
- Side: `"buy"` or `"sell"` directly from OKX (no mapping needed)
- **Batching:** Accumulates raw trades in deques, recalculates every 5 seconds
- Rolling windows: 5 minutes, 15 minutes, 1 hour
- CVD formula: `sum(size if buy, -size if sell)` per window
- Tracks total buy_volume and sell_volume (1h window)
- Auto-prunes trades older than 1 hour
- **Contract size normalization:** Trades from OKX are in contracts. Normalized to base currency using `CONTRACT_SIZES` from `data_integrity.py` (BTC: ×0.01, ETH: ×0.1, etc.).
- **CVD state machine:** Per-pair `CVDState` (WARMING_UP → VALID → INVALID on disconnect → WARMING_UP on reconnect). On reconnect, trade buffer is flushed to prevent stale trades contaminating CVD windows. `get_cvd()` returns `None` when state ≠ VALID.
- **Per-window progressive warmup:** 5m window valid after 5 min of trades (transitions to VALID immediately — unblocks setups), 15m after 15 min, 1h after 60 min. Each milestone logged. `get_warm_windows(pair)` returns set of warm windows. Replaces single `CVD_WARMUP_SECONDS` (was 3600s = blocked all trading for 1h on startup).
- Public methods: `get_cvd(pair)` → `CVDSnapshot | None`, `get_cvd_state(pair)` → `CVDState`, `get_warm_windows(pair)` → `set[str]`

### `data_service/oi_flush_detector.py` — OI Flush Detector
- Detects liquidation cascades from OI drops (>2% in 5 minutes)
- Fed by DataService's `_oi_loop()` — no separate async task needed
- Ring buffer of last 12 OI snapshots per pair (1 hour of history)
- When OI drops ≥ `OI_DROP_THRESHOLD_PCT` in `OI_DROP_WINDOW_SECONDS`, generates `OIFlushEvent(source="oi_proxy")`
- Public API: `get_recent_oi_flushes()`, `get_aggregated_stats()`, `is_connected`
- **Why:** Binance WebSocket is geo-blocked from Canada. OI proxy detects the same cascades indirectly.
- **Side attribution:** Uses price change to infer which side was liquidated (price drop >0.5% + OI drop → `"long"`, rise → `"short"`, else `"unknown"`). `update()` accepts `current_price` from latest candle.
- **Snapshot age validation:** Rejects stale snapshots older than `window_ms × OI_SNAPSHOT_MAX_AGE_FACTOR` (default 2.0).
- **Limitation:** Cannot detect individual liquidations — only aggregate cascades.
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
- **First-poll baseline:** On first poll for each wallet (`_last_seen_tx` is None), seeds the tx hash without generating events. Prevents false whale alerts on startup.
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
- **First-poll baseline:** Same as Etherscan — seeds `_last_seen_tx` on first poll, no false events on startup.
- Rate limit: 0.5s between calls (~10 req/min, safe for public instance)
- Creates `WhaleMovement(chain="BTC", wallet_label=label, amount_usd=..., market_price=...)` — USD computed at detection time
- Log format: `BEARISH|BULLISH|NEUTRAL BTC whale {action}: {label} → {dest} {amount} BTC (~$USD) [{significance}]`

### `data_service/news_client.py` — News Sentiment Client
- Class: `NewsClient(redis_store=None)`
- Two data sources:
  - **alternative.me** — Fear & Greed Index (0-100 score, free, no API key, running since 2018)
  - **CryptoCompare** — News headlines. Endpoint: `https://min-api.cryptocompare.com/data/v2/news/`. Filters by asset category (BTC, ETH). Community votes (upvotes/downvotes) provide per-headline sentiment (bullish/bearish/None). **Nota:** API ahora requiere API key (antes era free). Sin key, `Data` viene como `None` — handler defensivo devuelve `[]` con warning limpio. F&G score (alternative.me) sigue funcionando sin key.
- Methods:
  - `fetch_fear_greed()` → `tuple[int, str] | None` — score + label ("Extreme Fear", "Fear", "Neutral", "Greed", "Extreme Greed")
  - `fetch_headlines(asset, limit=5)` → `list[NewsHeadline]` — recent headlines for BTC or ETH from CryptoCompare. Returns `[]` if API key missing or response malformed.
  - `fetch_sentiment()` → `NewsSentiment | None` — combines F&G + headlines (BTC 3 + ETH 2)
- Sentiment derivation: `_extract_sentiment(upvotes, downvotes)` — more upvotes = "bullish", more downvotes = "bearish", tied or no votes = None.
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
- 9 tables: `candles`, `trades`, `ai_decisions`, `risk_events`, `bot_metrics`, `funding_rate_history`, `open_interest_history`, `cvd_history`, `campaigns`
- `store_candles()` with batch insert + ON CONFLICT DO NOTHING (dedup)
- `load_candles()` returns oldest-first ordering
- Index on `(pair, timeframe, timestamp DESC)` for fast lookups
- **Funding rate history:** `store_funding_rate(fr)`, `store_funding_rates_batch(records)`, `load_funding_rates(pair, since_ms, until_ms)`. Populated by live polling + `fetch_history.py` backfill. Used by backtester for MarketSnapshot.
- **OI history:** `store_open_interest(oi)`, `store_open_interest_batch(records)`, `load_open_interest(pair, since_ms, until_ms)`. Same pattern. OKX 1h resolution, ~30 days back.
- **CVD history:** `store_cvd_snapshot(cvd)`, `load_cvd_snapshots(pair, since_ms, until_ms)`. Persisted on every confirmed candle. Accumulates from 2026-03-10 onwards. Needed for Setup C/E backtesting.
- **Campaigns:** `insert_campaign(campaign)` → DB id, `update_campaign(campaign)` on close. Stores HTF campaign lifecycle (campaign_id, pair, direction, initial/weighted entry, total size/margin, adds detail as JSONB, SL, PnL, close reason, timestamps).
- **Auto-reconnection:** `_ensure_connected()` checks connection health (sends `SELECT 1`). All DB methods (`store_candles`, `load_candles`, `insert_trade`, `update_trade`, `insert_ai_decision`, `insert_risk_event`, `insert_metric`) retry once on `psycopg2.OperationalError` / `InterfaceError` — sets `_conn = None` and reconnects.
- **Operational metrics (Grafana):** `insert_metric(name, value, pair, labels)` writes to `bot_metrics` table. `cleanup_old_metrics(retention_days=30)` deletes old rows. Both fire-and-forget.

### `data_service/data_integrity.py` — Data Integrity Module (NEW)
Central hub for data quality types and gating logic:
- **`DataServiceState`** enum: `RECOVERING` (startup/reconnect), `RUNNING` (all checks pass), `DEGRADED` (circuit breaker tripped)
- **`CVDState`** enum: `VALID`, `WARMING_UP`, `INVALID`
- **`CONTRACT_SIZES`** dict: single source of truth for OKX contract sizes (used by CVD + exchange_client)
- **`SETUP_DATA_DEPS`** dict: per-setup data dependencies (setup_c needs candles+funding+cvd, setup_e needs candles+oi, rest only need candles)
- **`can_trade_setup()`**: checks service state + per-setup deps. Returns `(allowed, reason)`.
- **`validate_candle_continuity()`**: checks timestamps are sequential per timeframe (tolerance 1.5×). Returns `(is_continuous, gap_count)`.
- **`CircuitBreaker`**: sliding window of reconnect events. Trips after `CIRCUIT_BREAKER_MAX_RECONNECTS` in `CIRCUIT_BREAKER_WINDOW_SECONDS`. Auto-resets after `CIRCUIT_BREAKER_STABLE_SECONDS` of stability.

### `data_service/service.py` — DataService Facade
- Wires all sub-modules into a single interface
- **Global state machine:** `_state` = RECOVERING → RUNNING → DEGRADED. Starts in RECOVERING. `_check_warmup()` evaluates transition to RUNNING every 10s.
- **RUNNING requires:** (1) WS connected, (2) ≥`STARTUP_WARMUP_CANDLE_MIN` candles per pair/tf, (3) ≥1 live WS candle, (4) candle continuity validated, (5) circuit breaker not tripped.
- **Reconnect handler (`_on_ws_reconnect`):** state→RECOVERING, circuit breaker event, gap backfill (up to 500 candles, paginated). If gap > backfill capacity, stays RECOVERING with CRITICAL log.
- **Backfill idempotency:** `_backfill_in_progress` flag prevents duplicate backfills on rapid reconnects.
- **Snapshot health:** `_compute_health()` uses `FundingRate.fetched_at` (actual fetch time, not exchange event time). `fetched_at` persisted to Redis (round-trips correctly). Redis health checked — if down, `critical_sources_healthy=False`. `SnapshotHealth` includes `redis_healthy` and `service_state`.
- **OI loop:** passes current price to `oi_flush_detector.update()` for side attribution.
- Public methods: `get_latest_candle()`, `get_candles()`, `get_market_snapshot()`, `get_cvd()`, `get_cvd_state()`, `fetch_usdt_balance()`, `state` property
- Health check loop every 30 seconds — emits `health_status` + `asyncio_tasks` metrics, logs current `state`. Warns if asyncio task count > 25 (expected ~15, leak detection). Includes `_cvd_health_summary()`: shows CVD warmup progress per pair. `health()` dict includes `asyncio_tasks` count for dashboard visibility.
- **Per-pair candle staleness:** `_check_candle_staleness()` runs in health check loop. Detects when a specific pair/tf stops receiving candles while WS is "connected" (3x expected interval = stale). Catches silently dead subscriptions.
- **State transition alerts:** `_fire_state_alert()` / `_alert_state_transition()` send Telegram alerts via AlertManager when state changes (RECOVERING→RUNNING=INFO, →DEGRADED=CRITICAL). Safe from both sync and async contexts.
- **Metrics:** `data_service_state` (on transition to RUNNING), `ws_reconnect`, `circuit_breaker_tripped`, `gap_backfill_unrecoverable`

### `main.py` — Entry Point
- Single process, handles SIGINT/SIGTERM for graceful shutdown
- Creates DataService with pipeline callback
- **Data integrity gate:** After setup detection, before dedup: checks `_data_service.state == RUNNING` and `can_trade_setup()` per-setup deps. Blocked setups logged as `data_blocked` ML outcome. Position Guardian and HTF pipeline also gated on RUNNING state.
- Pipeline completo: Data → **Data Gate** → Strategy → AI (bypass/filter) → Risk → Execution
- AI filter currently bypassed for all active setups (setup_a in AI_BYPASS_SETUP_TYPES, setup_d variants in QUICK_SETUP_TYPES)
- Pipeline dedup cache at entry covers ALL setup types. Risk rejections for structural reasons also cached.
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
| `STARTUP_WARMUP_CANDLE_MIN` | `50` | Min candles per pair/tf before RUNNING |
| `CVD_WARMUP_SECONDS` | `3600` | CVD trade span before VALID (60 min) |
| `CIRCUIT_BREAKER_MAX_RECONNECTS` | `5` | Reconnects before DEGRADED |
| `CIRCUIT_BREAKER_WINDOW_SECONDS` | `300` | Sliding window for circuit breaker |
| `CIRCUIT_BREAKER_STABLE_SECONDS` | `120` | Stability before auto-reset |
| `OI_SNAPSHOT_MAX_AGE_FACTOR` | `2.0` | Max age multiplier for OI snapshots |
| `NEWS_SENTIMENT_ENABLED` | `True` | Enable/disable news sentiment fetching |
| `NEWS_FEAR_GREED_URL` | `https://api.alternative.me/fng/` | Fear & Greed API endpoint |
| `NEWS_HEADLINES_URL` | `https://min-api.cryptocompare.com/data/v2/news/` | CryptoCompare news API endpoint (free, no key) |
| `NEWS_POLL_INTERVAL` | `300` (5min) | News sentiment polling interval |
| `NEWS_FEAR_GREED_CACHE_TTL` | `1800` (30min) | Redis cache TTL for F&G score |
| `NEWS_HEADLINES_CACHE_TTL` | `300` (5min) | Redis cache TTL for headlines |
| `NEWS_EXTREME_FEAR_THRESHOLD` | `5` | F&G < 5 → reject longs (only systemic crashes) |
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
