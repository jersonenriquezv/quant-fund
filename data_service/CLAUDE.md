# Data Service — CLAUDE.md

Operational rules for Claude when modifying `data_service/`. The eyes and ears — every other layer reads typed data from here.

## Purpose
24/7 connection to OKX (WebSocket + REST) for candles, trades, funding, OI. Whale tracking (ETH via Etherscan, BTC via mempool.space). News sentiment (Fear & Greed + headlines). Storage layer (Redis cache + PostgreSQL history).

## Source of truth (read before editing)
- **Detailed behavior:** `docs/context/01-data-service.md` (Spanish, deep — all sources, pipeline flow, every file, FAQ)
- **Active config:** `docs/SYSTEM_BASELINE.md` §1 — TRADING_PAIRS, timeframes, intervals
- **Schema:** `docs/OPERATIONS.md` §schema — current migration version + table list
- **Models:** `shared/models.py` — ALL inter-service data is typed dataclasses (frozen). Never raw dicts

## Files
| File | Role |
|---|---|
| `service.py` | Facade `DataService`. State machine RECOVERING → RUNNING → DEGRADED. Wires all sub-modules. Health loop, reconnect handler, alerts |
| `exchange_client.py` | OKX REST via ccxt. Backfill, funding, OI, balance, orderbook |
| `websocket_feeds.py` | OKX candle WS (`/business`). 35 channels (7 pairs × 5 tf). Confirms only |
| `cvd_calculator.py` | OKX trades WS (`/public`). CVD per 5m/15m/1h with progressive warmup |
| `oi_flush_detector.py` | OI drop >2% in 5min = liquidation cascade. Replaces Binance forceOrder (geo-blocked) |
| `etherscan_client.py` | ETH whale polling (33 wallets) |
| `btc_whale_client.py` | BTC whale polling via mempool.space (8 wallets) |
| `news_client.py` | Fear & Greed (alternative.me) + headlines (CryptoCompare) |
| `liquidation_estimator.py` | Heatmap estimator from OI + candles. Approximation only |
| `data_integrity.py` | State enums, CVDState, CONTRACT_SIZES re-export, CircuitBreaker, can_trade_setup() |
| `metadata.py` | OKX instrument IDs + contract sizes. Single source. `assert_supported_trading_pairs()` fails fast |
| `data_store.py` | Redis (cache, TTL'd) + PostgreSQL (history, 11 tables). VALID_OUTCOMES + NON_MARKET_OUTCOMES + ml_market_outcome_filter_sql |

## Rules — modifying data sources
1. **All inter-service data is a typed frozen dataclass from `shared/models.py`.** Never return raw dicts to callers. Add new fields to the dataclass, not parallel kwargs.
2. **OKX instrument format is `BTC-USDT-SWAP` (hyphens).** ccxt uses `BTC/USDT:USDT` internally. Live in `metadata.py` — never hardcode elsewhere.
3. **`active_okx_instruments()` derives from `settings.TRADING_PAIRS`.** When adding a pair, update `OKX_SWAP_INSTRUMENTS` + `CONTRACT_SIZES` in `metadata.py` first or `assert_supported_trading_pairs()` fails fast on startup.
4. **Volume comes from `candle_data[6]` (volCcy = base currency), NOT `candle_data[5]` (vol = contracts).** Past bug: 100x BTC volume mismatch broke OB volume filter. Do not regress.
5. **Trades are normalized to base currency via `CONTRACT_SIZES`.** OKX returns contracts; CVD calculator must multiply.
6. **Side comes from OKX `"buy"`/`"sell"` directly.** No mapping needed.
7. **Validation on every candle.** Price ≤ 0 → ERROR drop. Volume = 0 → WARNING. Future timestamp → WARNING. Invalid candles never reach storage.
8. **OHLC sanity:** Reject candles where `low > min(open,close)` or `high < max(open,close)`. Track `_bad_candle_counts` per pair/tf.

## Rules — modifying CVD / state machines
1. **CVDState transitions:** `WARMING_UP → VALID → INVALID` (on disconnect) → `WARMING_UP` (on reconnect). On reconnect, **flush trade buffer** to prevent stale trades contaminating windows.
2. **Per-window progressive warmup.** 5m valid after 5min, 15m after 15min, 1h after 60min. Each `CVDSnapshot` carries `warm_windows`. Strategy only consumes 15m/1h CVD when those windows are warm.
3. **`get_cvd(pair)` returns `None` when state ≠ VALID.** Never return stale or partial data — callers rely on None to mean "skip CVD-based logic".
4. **`DataServiceState`:** RECOVERING (startup/reconnect) → RUNNING (all checks) → DEGRADED (circuit breaker tripped). Pipeline gate uses `can_trade_setup()` from `data_integrity.py`.
5. **RUNNING requires ALL:** WS connected, ≥`STARTUP_WARMUP_CANDLE_MIN` candles per pair/tf, ≥1 live WS candle, candle continuity validated, circuit breaker not tripped.

## Rules — modifying storage
1. **Redis is cache only.** Bot must work if Redis dies (degraded). Never use Redis for inter-service messaging — direct function calls only.
2. **Redis key pattern:** `qf:{category}:{pair}:{detail}`. TTLs MUST be set: candles 24h, funding 9h, OI 10min.
3. **PostgreSQL is source of truth for history.** All inserts must use `ON CONFLICT DO NOTHING` for dedup. `_ensure_connected()` retries once on `OperationalError`/`InterfaceError`.
4. **Schema migrations are idempotent.** `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`. Use `_apply_migration(cur, N, "description")` to record version. Bump `ML_FEATURE_VERSION` if adding ML feature columns. Update `docs/OPERATIONS.md` §schema.
5. **`VALID_OUTCOMES`** in `data_store.py` is the authoritative whitelist for `ml_setups.outcome_type`. Drift emits WARNING in `update_ml_setup_outcome`. New outcome types must be added there first.
6. **`NON_MARKET_OUTCOMES`** is the subset to exclude from training/edge queries. Use `ml_market_outcome_filter_sql()` helper in new scripts to avoid drift.
7. **Aggregated trades queries filter `exit_reason IS DISTINCT FROM 'orphaned_restart'`.** Synthetic restart-orphans must not contaminate DD reconcile or dashboard stats.

## Rules — whale tracking
1. **First-poll baseline.** Both Etherscan and BTC clients seed `_last_seen_tx` on first poll without generating events. Prevents false alerts on startup.
2. **Significance thresholds:** ETH >100 high, >10 medium, <10 ignored. Same for BTC. Configurable via `WHALE_MIN_*`/`WHALE_HIGH_*`.
3. **USD enrichment is best-effort.** Constructor accepts `price_provider` callback. Falls back to `0.0` if provider unavailable.
4. **4 action types:** `exchange_deposit` (bearish), `exchange_withdrawal` (bullish), `transfer_out` (neutral), `transfer_in` (neutral).
5. **Rate limits:** Etherscan ≤4.5 calls/sec (under 5/sec limit), mempool.space 0.5s between calls.

## Never
- Use Binance WebSocket for liquidations — geo-blocked from Canada. OI proxy is the replacement.
- Hardcode instrument IDs or contract sizes outside `metadata.py`.
- Return raw dicts between services. Use `shared/models.py` dataclasses.
- Skip the `confirmed=True` candle filter — only confirmed candles trigger pipeline.
- Persist a candle with invalid OHLC. Validation is a hard gate.
- Bypass `_ensure_connected()` retry on DB ops.

## Verify after changes
```bash
python -m pytest tests/test_data_service.py tests/test_data_integrity.py tests/test_data_store_filters.py -v --tb=short
```

## Telemetry
- `data_service_state` (on transition to RUNNING)
- `ws_reconnect`, `ws_reconnection`, `circuit_breaker_tripped`, `gap_backfill_unrecoverable`
- `pipeline_latency_ms`, `claude_latency_ms`, `health_status`, `asyncio_tasks`
