# ONE-MAN QUANT FUND — Personal Crypto Trading Bot

## What this project is

A personal automated trading system that uses Smart Money Concepts (SMC) to detect setups in crypto and execute trades 24/7. It is a micro-scale version of how institutional hedge funds like Citadel or Two Sigma operate.

**Core principle:** Deterministic bot detects → Claude filters → Risk approves → Execution executes.
If any layer says NO, the trade does NOT execute.

---

## How a Trade Opens (Simple Version)

The bot watches 5m and 15m candles on BTC and ETH. When a candle closes, it passes through 5 filters in sequence — if any one says NO, the trade does not execute.

### 1. Strategy detects an SMC pattern

The bot looks for 2 types of setup:

**Setup A (primary) — Liquidity Sweep + CHoCH + Order Block:**
- Price sweeps retail stops (liquidity sweep)
- Then reverses direction (CHoCH)
- And retraces to a fresh Order Block (zone where institutions bought/sold)
- Entry: 50% of the OB body

**Setup B — BOS + FVG + Order Block:**
- Price breaks structure (BOS) confirming the trend
- Leaves a gap (FVG) inside or near an OB
- Entry: 50% of the FVG or OB

**Mandatory rules:**
- Minimum 2 confluences (OB alone = no trade)
- Long only in discount (below 50% of the range)
- Short only in premium (above 50%)
- 4H/1H trend must align with trade direction

### 2. Pre-filter (free, no Claude)

3 instant checks that catch ~90% of bad trades:
- HTF bias against direction → reject (long + bearish trend = no)
- Extreme funding rate against direction → reject
- Strong CVD (volume) divergence → reject

### 3. Claude evaluates the context

Claude receives the setup + market data (funding, CVD, liquidations, whales, OI) and decides if the context supports the trade. It does not see the chart pattern — it evaluates whether conditions are favorable to execute now.

Requires: confidence >= 0.50 (aggressive) or 0.60 (default) + approved=true

### 4. Risk Service checks guardrails

- No more than 3 open positions
- Daily drawdown < 5%
- Weekly drawdown < 10%
- 15min cooldown after a loss
- Minimum R:R 1.2:1
- Calculates size: (capital x 2%) / distance to SL

### 5. Execution places orders

- Entry: limit order at the OB price
- SL: stop-market (guaranteed fill on crash)
- TP1: 50% of position at 1:1 R:R → SL moves to breakeven
- TP2: 30% at 2:1 R:R → SL moves to TP1
- TP3: remaining 20% at the next liquidity level

### The ideal setup

A perfect long looks like this:

1. ETH drops, sweeps the lows (liquidity sweep) — retailers get liquidated
2. Immediately reverses and breaks opposite structure (bullish CHoCH)
3. Leaves a fresh OB in the discount zone with 2x+ average volume
4. Price retraces to 50% of the OB — this is where the bot enters
5. Funding rate is negative (shorts overcrowded = fuel to go up)
6. CVD is positive (buyers dominating)
7. Whales withdrawing from exchanges (accumulating)
8. Claude says "yes, everything aligns" with 70%+ confidence
9. Risk approves: there is capital, no drawdown, R:R is good
10. Limit order at 50% of OB, SL below OB, scaled TPs above

**In short:** institutions hunt stops → confirmed reversal → bot enters where institutions entered → Claude validates macro is not against it → risk controls the size.

---
## Language Rule
ALL code, comments, variable names, docstrings, commit messages, and log messages MUST be in English. No exceptions. The docs/context/ files can be in Spanish (they are for the user). Agent instructions are in Spanish but all output code is in English.

## Mobile Responsiveness Rule
ALL dashboard UI changes MUST work on mobile (375px+). The dashboard uses 2 CSS breakpoints in `globals.css`: tablet (≤1023px, 2-column grid) and mobile (≤639px, single column). When adding or modifying any component in `dashboard/web/`:
* Test at 375px width (iPhone SE) — nothing should overflow or be unusable
* Use CSS classes instead of inline styles for any layout property that needs to change on mobile (grid-template-columns, font-size, flex-wrap, etc.)
* Hide low-priority table columns on mobile via `display: none` classes
* Tables must scroll horizontally on narrow screens (`.scroll-y` already handles this)

## Technical Stack

* **Language:** Python (entire system)
* **Exchange:** OKX via ccxt (REST) + native OKX WebSocket
* **Pairs:** BTC/USDT and ETH/USDT (linear perpetuals). Instrument IDs: BTC-USDT-SWAP, ETH-USDT-SWAP.
* **Analysis:** pandas + numpy
* **AI Filter:** Claude API (Sonnet)
* **Database:** PostgreSQL (historical) + Redis (real-time cache)
* **On-chain:** Etherscan API (ETH wallets) + mempool.space API (BTC wallets)
* **Dashboard:** FastAPI + Next.js
* **Containers:** Docker + Docker Compose
* **Server:** Acer Nitro 5 (i5-9300H, 16GB RAM, GTX 1050) running Ubuntu Server 24.04

---
## Infrastructure
- **Server:** Acer Nitro 5 (i5-9300H, 4 cores/8 threads, 16GB RAM, SSD 240GB) running Ubuntu Server 24.04 — dedicated 24/7 server, no GUI
- **IP:** 192.168.1.236 (static, local network)
- **Internet:** Direct connection, no proxy. Required for OKX WebSocket, Etherscan API, Claude API.
- **Docker + Docker Compose:** Installed and ready
- **Python:** 3.12 with venv at ~/quant-fund/venv
- **Node.js:** 20.x (for Claude Code only)
- **Development:** VS Code Remote SSH from main PC (ASUS). Claude Code runs directly on the server.
- **The bot runs ON this server 24/7.** All code, Docker containers, databases, and WebSocket connections live here. It's not a dev machine — it's the production server.

## Project Structure

```
quant-fund/
├── main.py                  # Entry point — starts all services, runs pipeline
├── config/
│   ├── settings.py          # All configuration (mode, API keys, pairs, thresholds)
│   └── .env                 # Secrets (OKX keys, Etherscan, Anthropic) — NOT in git
├── shared/
│   ├── models.py            # Dataclasses shared between all services
│   ├── logger.py            # Loguru setup (stdout + daily rotated files)
│   └── notifier.py          # Telegram push notifications (fire-and-forget)
├── data_service/
│   ├── __init__.py
│   ├── service.py           # DataService facade (only public interface)
│   ├── websocket_feeds.py   # OKX WebSocket (candles on /business)
│   ├── exchange_client.py   # OKX REST via ccxt (backfill, funding, OI)
│   ├── cvd_calculator.py    # CVD from OKX trade stream (/public)
│   ├── oi_liquidation_proxy.py # OI-based liquidation cascade detection
│   ├── binance_liq.py       # Binance Futures WebSocket (UNUSED — geo-blocked from Canada)
│   ├── etherscan_client.py  # ETH whale wallet monitoring (Etherscan API)
│   ├── btc_whale_client.py  # BTC whale wallet monitoring (mempool.space API)
│   └── data_store.py        # Redis (cache) + PostgreSQL (historical)
├── strategy_service/
│   ├── __init__.py
│   ├── service.py           # StrategyService facade
│   ├── market_structure.py  # BOS/CHoCH detection
│   ├── order_blocks.py      # OB detection + mitigation tracking
│   ├── fvg.py               # FVG detection + fill tracking
│   ├── liquidity.py         # Liquidity pools, sweeps, premium/discount
│   └── setups.py            # Setup A & B assembly + confluence counting
├── ai_service/
│   ├── __init__.py
│   ├── service.py           # AIService facade
│   ├── claude_client.py     # Anthropic API wrapper
│   └── prompt_builder.py    # Builds structured context for Claude
├── risk_service/
│   ├── __init__.py
│   ├── service.py           # RiskService facade
│   ├── guardrails.py        # Stateless checks (RR, cooldown, DD, etc.)
│   ├── position_sizer.py    # Position size + leverage calculation
│   └── state_tracker.py     # In-memory state (open positions, daily PnL)
├── execution_service/
│   ├── __init__.py
│   ├── models.py            # ManagedPosition — mutable position lifecycle state
│   ├── executor.py          # Order placement via ccxt
│   ├── monitor.py           # Fill monitoring, SL/TP lifecycle, slippage tracking
│   └── service.py           # ExecutionService facade
├── tests/
│   ├── conftest.py          # Shared fixtures (make_candle, make_snapshot)
│   ├── test_market_structure.py
│   ├── test_order_blocks.py
│   ├── test_fvg.py
│   ├── test_liquidity.py
│   ├── test_setups.py
│   ├── test_oi_proxy.py
│   ├── test_guardrails.py
│   ├── test_position_sizer.py
│   ├── test_state_tracker.py
│   ├── test_risk_service.py
│   ├── test_ai_service.py
│   ├── test_claude_client.py
│   ├── test_prompt_builder.py
│   ├── test_data_service.py
│   └── test_execution.py
├── dashboard/
│   ├── api/                 # FastAPI backend (read-only, port 8000)
│   └── web/                 # Next.js frontend (port 3000)
├── docs/
│   └── context/             # Auto-generated docs per service (Spanish)
├── logs/                    # Daily rotated log files (auto-created)
├── requirements.txt
└── CLAUDE.md
```

---

## ARCHITECTURE — 5 Layers

All 5 layers run in the **same Python process**. Communication is via direct function calls — no message queues, no pub/sub, no IPC.

```
Market Data → [1. Data Service] → [2. Strategy Service] → [3. AI Service] → [4. Risk Service] → [5. Execution Service] → Exchange
```

### Inter-layer Communication

Direct Python calls. The pipeline is triggered on every confirmed candle:

```python
# main.py — on_candle_confirmed callback
candle = ...  # from OKX WebSocket (confirmed=True)
setup = strategy_service.evaluate(candle.pair, candle)
if setup:
    snapshot = data_service.get_market_snapshot(candle.pair)
    decision = await ai_service.evaluate(setup, snapshot)
    if decision.approved:
        approval = risk_service.check(setup)
        if approval.approved:
            execution_service.execute(setup, approval)
```

**Redis is used ONLY for:**
* State caching (latest candles, OI, funding, orderbook depth)
* Persistence between restarts (portfolio state, daily PnL tracking)
* NOT for inter-module messaging

---

### Layer 1: Data Service (`data_service/`)

Receives real-time data from multiple sources:

* **OKX WebSocket**: Candles on `/business` (`wss://ws.okx.com:8443/ws/v5/business`), trades on `/public` (`wss://ws.okx.com:8443/ws/v5/public`)
* **OKX REST** (via ccxt): Candle backfill, funding rate (every 8 hours), open interest
* **Etherscan REST**: Whale ETH wallet movements (all large transfers, not just exchange)
* **mempool.space REST**: Whale BTC wallet movements (UTXO parsing, no API key needed)
* Funding rates: OKX charges every 8 hours (standard CEX schedule)
* Liquidations: OI drop proxy — detects cascades when OI drops >2% in 5min (Binance WebSocket geo-blocked from Canada)
* **Rate limits:** 20 requests/2s market data, 60 requests/2s trading
* **Instrument format:** `BTC-USDT-SWAP` (hyphens, not slashes — OKX convention)

Stores data in Redis (fast cache) and PostgreSQL (historical storage).

**Authentication:** API key + secret + passphrase. Store `OKX_API_KEY`, `OKX_SECRET`, `OKX_PASSPHRASE` in `.env`.
**Demo mode:** Set `OKX_SANDBOX=true` in `.env`. OKX demo uses `x-simulated-trading: 1` header.
**Note:** OKX website is geo-blocked in Canada, but the API works fine from Canadian servers. The bot only uses the API, never the website.

---

### Layer 2: Strategy Service (`strategy_service/`)

Deterministic Python engine that detects SMC patterns. No AI. Pure rules.

#### Patterns Detected

**1. Market Structure (BOS/CHoCH)**

* BOS (Break of Structure): Price closes 0.1%+ beyond previous swing high/low. Confirms continuation.
* CHoCH (Change of Character): Break in the opposite direction of the trend. Reversal signal.
* Requires full candle close (not wick only). Timeframe 15m+.
* Crypto note: Large liquidation wicks cause false BOS. The 0.1% filter is mandatory.

**2. Order Blocks (OB)**

* Bullish OB: Last red candle before bullish impulse + BOS.
* Bearish OB: Last green candle before bearish impulse + BOS.
* Entry: 50% of candle body.
* SL: Below/above entire OB.
* Crypto note: Only use fresh OBs (< 24–48 hours). Older OBs invalidate faster than in forex.

**3. Fair Value Gaps (FVG)**

* 3-candle gap where wick of candle 1 does not touch wick of candle 3.
* Minimum size: 0.1% of price.
* Expiration: 48 hours.
* Crypto note: FVG alone is insufficient. Always requires OB confluence.

**4. Liquidity Pools & Sweeps**

* BSL (Buy-Side Liquidity): Above swing highs (stop clusters).
* SSL (Sell-Side Liquidity): Below swing lows.
* Sweep = wick breaks level but closes back inside range.
* Crypto note: Most profitable pattern. Retail leverage + public liquidation data = institutions actively hunt stops.

**5. Premium/Discount Zones**

* Premium (>50% of range): Shorts only.
* Discount (<50% of range): Long only.
* Equilibrium (50%): Do not trade.
* Crypto note: Recalculate every 4–6 hours using 4H swing high/low.

**6. Volume & Institutional Indicators**

* OB volume: >1.5x average = valid. Low volume = ignore.
* Sweep volume: >2x average + visible liquidations = strong confirmation.
* CVD divergence = reversal signal.
* Open Interest rising + price rising = strong trend.
* Funding extreme positive = caution shorts. Extreme negative = long opportunity.
* Liquidation cascade during sweep = strong confirmation.

---

### Setup A (Primary) — Liquidity Sweep + CHoCH + Order Block

1. Confirm HTF trend (4H or 1H)
2. Identify liquidity buildup (equal highs/lows)
3. Liquidity sweep occurs
4. CHoCH confirms direction change
5. Fresh OB forms (<24–48h)
6. OB in discount (long) or premium (short)
7. Retrace to OB — entry at 50%
8. Volume spike >2x + liquidations visible
9. Claude approval (confidence ≥ 0.60)
10. Risk check passes

---

### Setup B (Secondary) — BOS + FVG + Order Block

1. HTF trend confirmed
2. BOS on LTF (5m/15m) with 0.1%+ close
3. Fresh OB
4. FVG inside or adjacent to OB
5. Premium/Discount aligned
6. Entry at 50% FVG or OB
7. Volume >1.5x average + CVD aligned
8. Claude + Risk approval

---

### Minimum Confluence Rule

* Minimum 2 confirmations per trade
* OB alone → DO NOT trade
* FVG alone → DO NOT trade
* Liquidity Sweep alone → DO NOT trade

---

### Exit Rules

* Stop Loss mandatory. Never move against trade.
* TP1: 50% at 1:1 R/R, move SL to breakeven
* TP2: 30% at 1:2 R/R
* TP3: 20% trailing stop or next liquidity level
* Max duration: 12 hours
* Invalidation: Close through full OB/FVG → exit immediately

---

### Layer 3: AI Service (`ai_service/`)

Claude API (Sonnet) as filter. Does not originate trades. **Claude has NO internet access** — all data must be provided as structured context.

**What Claude receives (built by `prompt_builder.py`):**
* The detected setup (type, pair, direction, entry, SL, TP levels, confluences)
* HTF market structure (4H/1H trend direction, recent BOS/CHoCH)
* Current funding rate + context (extreme or normal? threshold: ±0.03%)
* Open Interest trend (rising/falling + price correlation)
* CVD snapshot (divergence with price?)
* Recent liquidation cascades (OI proxy — estimated USD from OI drops)
* Whale movements (last 24h significant transfers)
* Recent price action (1H and 4H candle % change)

**What Claude does NOT have access to:**
* News, Twitter/X, Reddit, or any real-time internet data
* Fear & Greed Index (future phase)
* Macro economic data (Fed rates, CPI)

**Claude's output:** Structured JSON with:
* `confidence`: float 0.0–1.0 (minimum 0.60 to proceed)
* `approved`: bool (must be true AND confidence ≥ 0.60)
* `reasoning`: string explaining the decision
* `adjustments`: optional SL/TP modifications
* `warnings`: list of risk factors detected

**Fail-safe behavior:**
* If Claude API call fails → trade is REJECTED (never execute without filter)
* If ANTHROPIC_API_KEY not set → all trades auto-rejected
* If response can't be parsed → trade is REJECTED
* Timeout: 30 seconds max per API call

**Config:** Model `claude-sonnet-4-20250514`, temperature 0.3, max_tokens 500.

---

### Layer 4: Risk Service (`risk_service/`)

Non-negotiable guardrails:

* Risk per trade: 1–2%
* Max daily DD: 3%
* Max weekly DD: 5%
* Max open positions: 3
* Minimum R/R: 1:1.5
* Max leverage: 5x
* Cooldown after loss: 30 min
* Max trades/day: 5

Position size formula:

```
Position Size = (Capital × Risk%) / (Entry - Stop Loss)
```

---

### Layer 5: Execution Service (`execution_service/`)

Executes orders on OKX via ccxt (exchange id: `"okx"`).
Authentication: API key + secret + passphrase (`OKX_API_KEY`, `OKX_SECRET`, `OKX_PASSPHRASE` in `.env`).
Instrument format: `"BTC-USDT-SWAP"`, `"ETH-USDT-SWAP"` (OKX convention).

**Order flow:**
1. Receive approved trade from Risk Service (TradeSetup + RiskApproval)
2. Place limit entry order at calculated price (50% OB/FVG)
3. Attach SL as stop-market order (guaranteed fill on volatile moves)
4. Attach TP1/TP2/TP3 as limit orders with scaled sizes (50%/30%/20%)
5. Monitor fill status

**Order types:**
* Entry: Limit order. If not filled within 15 minutes → cancel.
* Stop Loss: Stop-market (not stop-limit — stop-limits can skip during crashes)
* Take Profit: Limit orders (TP1, TP2, TP3 as separate orders)

**Partial fills & edge cases:**
* Entry partially fills → keep order open, SL/TP scale to filled amount
* Entry fails (insufficient margin, API error) → log ERROR, do NOT retry automatically
* SL/TP placement fails after entry fills → EMERGENCY close at market immediately
* Slippage tracking: log expected vs actual fill price on every order

**Position management after entry:**
* TP1 hit (50% at 1:1 R/R) → move SL to breakeven
* TP2 hit (30% at 1:2 R/R) → trail SL to TP1 level
* TP3: trailing stop or next liquidity level for remaining 20%

**Trading Mode:**
* Demo first (4-week minimum): set `OKX_SANDBOX=true`, adds `x-simulated-trading: 1` header
* Live: set `OKX_SANDBOX=false`

**Graduation criteria (demo → live):**
* Minimum 50 paper trades executed
* Win rate >40% on paper
* No critical bugs in execution flow
* Drawdown stayed within limits
* Minimum 4 weeks elapsed

---

## Performance Metrics

| Metric           | Target |
| ---------------- | ------ |
| Win Rate         | >45%   |
| Avg Risk/Reward  | >1:1.5 |
| Max Drawdown     | <10%   |
| Sharpe Ratio     | >1.0   |
| Profit Factor    | >1.5   |
| Monthly Return   | 5–10%  |
| Trades per Week  | 5–15   |
| AI Approval Rate | 30–60% |

---

## Build Order

1. Data Service
2. Strategy Service
3. Risk Service
4. AI Service
5. Execution Service (paper trading first)
6. Dashboard

Initial validation capital: $50–100 USD on OKX (demo mode first, then live).

---

## Shared Data Models (`shared/models.py`)

All inter-service communication uses typed frozen dataclasses. No raw dicts between layers.

**Layer 1 (Data) outputs:**
* `Candle` — OHLCV with `confirmed` flag. Only process when `confirmed=True`.
* `FundingRate` — current rate + next estimated rate + next funding time
* `OpenInterest` — OI in contracts, base currency, and USD
* `CVDSnapshot` — cumulative volume delta at 5m/15m/1h windows + buy/sell volume
* `LiquidationEvent` — single liquidation (pair, side, size_usd, source)
* `WhaleMovement` — ETH/BTC transfer (wallet, action, amount, exchange, significance, chain). Actions: `exchange_deposit`, `exchange_withdrawal`, `transfer_out`, `transfer_in`
* `MarketSnapshot` — aggregates all of the above for a single pair at a point in time

**Layer 2 (Strategy) outputs:**
* `TradeSetup` — detected setup with entry/SL/TP1-3, confluences list, htf_bias, ob_timeframe

**Layer 3 (AI) outputs:**
* `AIDecision` — confidence (0-1), approved (bool), reasoning, adjustments (dict), warnings (list)

**Layer 4 (Risk) outputs:**
* `RiskApproval` — approved (bool), position_size, leverage, risk_pct, reason

---

## Database Schema (PostgreSQL)

```sql
candles (
    id SERIAL PRIMARY KEY,
    pair VARCHAR(20),
    timeframe VARCHAR(5),
    timestamp BIGINT,         -- Unix ms
    open FLOAT, high FLOAT, low FLOAT, close FLOAT,
    volume FLOAT,
    volume_quote FLOAT,
    UNIQUE(pair, timeframe, timestamp)
)

trades (
    id SERIAL PRIMARY KEY,
    pair VARCHAR(20),
    direction VARCHAR(5),     -- "long" or "short"
    setup_type VARCHAR(10),   -- "setup_a" or "setup_b"
    entry_price FLOAT,
    sl_price FLOAT,
    tp1_price FLOAT, tp2_price FLOAT, tp3_price FLOAT,
    actual_entry FLOAT,       -- Real fill price (slippage tracking)
    actual_exit FLOAT,
    exit_reason VARCHAR(20),  -- "tp1", "tp2", "tp3", "sl", "timeout", "invalidation"
    position_size FLOAT,
    pnl_usd FLOAT,
    pnl_pct FLOAT,
    ai_confidence FLOAT,
    opened_at TIMESTAMP,
    closed_at TIMESTAMP,
    status VARCHAR(15)        -- "open", "closed", "cancelled"
)

ai_decisions (
    id SERIAL PRIMARY KEY,
    trade_id INT REFERENCES trades(id),
    confidence FLOAT,
    reasoning TEXT,
    adjustments JSONB,
    warnings JSONB,
    created_at TIMESTAMP
)

risk_events (
    id SERIAL PRIMARY KEY,
    event_type VARCHAR(30),   -- "daily_dd_limit", "weekly_dd_limit", "cooldown", "max_positions"
    details JSONB,
    created_at TIMESTAMP
)
```

---

## Startup Sequence (`main.py`)

1. **Validate config** — check API keys, log trading mode (demo/live), log pairs and risk params
2. **Initialize services** — DataService (with pipeline callback), StrategyService, AIService, RiskService
3. **DataService.start():**
   - Connect Redis + PostgreSQL
   - Backfill last 500 candles per pair/timeframe via OKX REST
   - Store backfilled candles in memory + PostgreSQL
   - Start OKX WebSocket (candles on `/business`)
   - Start OKX trades WebSocket (for CVD on `/public`)
   - Start Etherscan polling loop (ETH whales, every 5 min)
   - Start mempool.space polling loop (BTC whales, every 5 min)
   - Start funding rate polling (every 8 hours)
   - Start OI polling (every 5 min)
   - Start health check loop (every 30 seconds)
4. **Main loop** — WebSocket callbacks trigger `on_candle_confirmed` → pipeline runs
5. **Graceful shutdown** on SIGINT/SIGTERM — close AI client, stop DataService, cancel tasks

---

## Logging

All services use `loguru` via `shared/logger.py`:

```
{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | {name}:{function}:{line} | {message}
```

* **stdout**: All levels (for Docker logs). No colors for clean output.
* **File**: `logs/{service}_{date}.log` — daily rotation at midnight, 30-day retention, gzip compression
* **Thread-safe**: `enqueue=True` for async contexts
* **Levels**: DEBUG (development), INFO (normal ops), WARNING (recoverable), ERROR (requires attention)
* **Usage**: `from shared.logger import setup_logger` then `logger = setup_logger("service_name")`

---

## Error Recovery & Resilience

**WebSocket disconnects (OKX):**
* Automatic reconnection with exponential backoff
* Initial delay: 1s → doubles each retry → max 60s
* Configured in `settings.py`: `RECONNECT_INITIAL_DELAY`, `RECONNECT_MAX_DELAY`, `RECONNECT_BACKOFF_FACTOR`
* Logs WARNING on disconnect, INFO on successful reconnect

**Redis/PostgreSQL down:**
* Data Service health check runs every 30 seconds
* If Redis fails: bot continues with in-memory data only, logs ERROR
* If PostgreSQL fails: candles not persisted historically, real-time still works, logs ERROR
* Neither failure kills the bot — it degrades gracefully

**Data validation (before publishing to pipeline):**
* Price ≤ 0 → discard, log ERROR
* Volume = 0 on BTC/ETH → discard, log WARNING
* Timestamp >60s in the future → discard, log WARNING

**Claude API failure:**
* Timeout (30s) or network error → trade REJECTED (fail-safe)
* Never execute without AI filter passing

**Open positions on restart:**
* Risk Service state (open positions, daily PnL) is in-memory — resets on restart
* SL/TP orders live on the exchange — they persist independently of the bot
* Future: persist Risk state to Redis for recovery

**Exchange API errors:**
* Rate limit hit → ccxt built-in throttling handles backoff
* Order placement failure → log ERROR, do NOT retry automatically
* Authentication failure → log CRITICAL, disable trading

---

## Environment Variables (`.env`)

```
OKX_API_KEY=
OKX_SECRET=
OKX_PASSPHRASE=
OKX_SANDBOX=true

ETHERSCAN_API_KEY=

ANTHROPIC_API_KEY=

TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DB=quant_fund
POSTGRES_USER=jer
POSTGRES_PASSWORD=

REDIS_HOST=localhost
REDIS_PORT=6379
```
