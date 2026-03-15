# ONE-MAN QUANT FUND — Personal Crypto Trading Bot

## What this project is

A personal automated trading system that uses Smart Money Concepts (SMC) to detect setups in crypto and execute trades 24/7. It is a micro-scale version of how institutional hedge funds like Citadel or Two Sigma operate.

**Core principle:** Deterministic bot detects → Risk approves → Execution executes.
If any layer says NO, the trade does NOT execute.
**Note:** Claude AI filter is currently bypassed for all active setups (setup_a in AI_BYPASS_SETUP_TYPES, setup_d variants in QUICK_SETUP_TYPES). Code remains for re-enable when recalibrated.

---

## How a Trade Opens (Simple Version)

The bot watches 5m and 15m candles on BTC and ETH. When a candle closes, it passes through 5 filters in sequence — if any one says NO, the trade does not execute.

### 1. Strategy detects an SMC pattern

The bot looks for 3 active setup types (others exist but are disabled):

**Setup A (primary) — Liquidity Sweep + CHoCH + Order Block:** (ENABLED, AI bypassed)
- Price sweeps retail stops (liquidity sweep)
- Then reverses direction (CHoCH)
- And retraces to a fresh Order Block (zone where institutions bought/sold)
- Entry: configurable depth into OB body (`SETUP_A_ENTRY_PCT`, default 65% — Optuna optimized 2026-03-15)
- AI filter bypassed (89.6% approval rate = no value added)

**Setup B (secondary) — BOS + FVG + Order Block:** (DISABLED)
- Disabled: 0-7.7% WR in backtests, destroys PnL in every run.
- Code remains for future re-enable.

**Setup D_choch — LTF CHoCH Scalp:** (ENABLED, quick setup)
- CHoCH on 5m + fresh OB near price
- No sweep or FVG required. HTF bias + PD zone aligned.
- Entry: 50% of OB. Quick setup (1h entry timeout, 4h max duration).
- 75% WR in backtests.

**Disabled setups:** Setup B (0-7.7% WR). Setup D_bos (20-33% WR, net negative). Setup F (only profitable 90d+). Setup C, E, G pending validation.

**Mandatory rules:**
- Minimum 2 confluences (OB alone = no trade)
- Long only in discount (below 50% of the range), short only in premium (above 50%)
- PD override: setups with 5+ confluences can trade against PD zone
- OB selection: composite scoring (volume 35%, freshness 30%, proximity 20%, body size 15%). OB_MIN_BODY_PCT (0.15%) filters micro-OBs.
- SL-too-close filter in Strategy layer (MIN_RISK_DISTANCE_PCT 0.2%) before building TradeSetup
- Expectancy filters: MIN_ATR_PCT (0.45% — skip dead markets), MIN_TARGET_SPACE_R (1.4 — require room to target)

### 2. AI filter (currently bypassed)

All active setups bypass Claude:
- **Setup A**: in `AI_BYPASS_SETUP_TYPES` — synthetic AIDecision(confidence=1.0). AI v2 had 89.6% approval = no value.
- **Setup B**: in `AI_BYPASS_SETUP_TYPES` — AI v1 destroyed it (49% WR → 21.4% WR). Bypass until recalibrated.
- **Setup D variants**: in `QUICK_SETUP_TYPES` — data-driven, skip AI by design.
- Pre-filter (funding extreme, F&G extreme, CVD divergence) and Claude evaluation code remain for future re-enable.
- Pipeline dedup cache at entry prevents re-evaluating identical setups (1h TTL).

### 4. Risk Service checks guardrails

- No more than 5 open positions
- Daily drawdown < 5%
- Weekly drawdown < 10%
- 15min cooldown after a loss
- Minimum R:R 1.2:1
- Calculates size: fixed $20 margin × leverage

### 5. Execution places orders

- Entry: limit order at the OB price
- SL: stop-market (guaranteed fill on crash)
- Single TP at tp2 (2:1 R:R) — 100% close
- Breakeven: price crosses tp1 (1:1 R:R) → SL moves to entry
- Trailing SL: price crosses midpoint(tp1,tp2) (1.5:1 R:R) → SL moves to tp1

### The ideal setup

A perfect long looks like this:

1. ETH drops, sweeps the lows (liquidity sweep) — retailers get liquidated
2. Immediately reverses and breaks opposite structure (bullish CHoCH)
3. Leaves a fresh OB in the discount zone with 2x+ average volume
4. Price retraces to 50% of the OB — this is where the bot enters
5. Funding rate is negative (shorts overcrowded = fuel to go up)
6. CVD is positive (buyers dominating)
7. Whales withdrawing from exchanges (accumulating)
8. AI filter bypassed (currently all active setups skip Claude)
9. Risk approves: there is capital, no drawdown, R:R is good
10. Limit order at 50% of OB, SL below OB, TP at 2:1 R:R

**In short:** institutions hunt stops → confirmed reversal → bot enters where institutions entered → risk controls the size.

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
│   ├── ml_features.py       # ML feature extraction (setup features + risk context)
│   ├── logger.py            # Loguru setup (stdout + daily rotated files)
│   ├── notifier.py          # Telegram push notifications (fire-and-forget)
│   └── alert_manager.py     # Priority-based Telegram alerts (trade lifecycle, errors, daily summary)
├── data_service/
│   ├── __init__.py
│   ├── service.py           # DataService facade (only public interface)
│   ├── websocket_feeds.py   # OKX WebSocket (candles on /business)
│   ├── exchange_client.py   # OKX REST via ccxt (backfill, funding, OI)
│   ├── cvd_calculator.py    # CVD from OKX trade stream (/public)
│   ├── oi_flush_detector.py  # OI-based flush event detection (OI drop >2%)
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
│   ├── setups.py            # Setup A/B/F/G assembly + confluence counting + OB scoring
│   └── quick_setups.py      # Setup C/D/E (data-driven, quick duration)
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
│   ├── test_execution.py
│   ├── test_quick_setups.py
│   └── test_ml_features.py
├── scripts/
│   ├── backtest.py          # Offline backtester (historical candle replay)
│   └── optimize.py          # Optuna parameter optimizer (automated tuning)
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
Market Data → [1. Data Service] → [2. Strategy Service] → [3. AI Service*] → [4. Risk Service] → [5. Execution Service] → Exchange
* AI currently bypassed for all active setups
```

### Inter-layer Communication

Direct Python calls. The pipeline is triggered on every confirmed candle:

```python
# main.py — on_candle_confirmed callback
candle = ...  # from OKX WebSocket (confirmed=True)
setup = strategy_service.evaluate(candle.pair, candle)
if setup:
    # Dedup check (all setup types)
    # AI: currently all active setups bypass Claude (synthetic AIDecision)
    if setup.setup_type in QUICK_SETUP_TYPES or setup.setup_type in AI_BYPASS_SETUP_TYPES:
        decision = AIDecision(confidence=1.0, approved=True, ...)
    else:
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
* Entry: configurable depth into OB body (`SETUP_A_ENTRY_PCT`, default 50%).
* SL: Below/above entire OB.
* OB selection: composite scoring via `_score_ob()` — volume (35%), freshness (30%), proximity (20%), body size (15%). `OB_MIN_BODY_PCT` (0.1%) filters micro-OBs.
* Crypto note: Only use fresh OBs (< 72 hours, `OB_MAX_AGE_HOURS`). Older OBs invalidate faster than in forex.

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
* Equilibrium (49-51%): Allowed by default (`ALLOW_EQUILIBRIUM_TRADES=True`).
* Crypto note: Recalculate every 4–6 hours using 4H swing high/low.

**6. Volume & Institutional Indicators**

* OB volume: >1.5x average = valid. Low volume = ignore.
* Sweep volume: >2x average + visible liquidations = strong confirmation.
* CVD divergence = reversal signal.
* Open Interest rising + price rising = strong trend.
* Funding extreme positive = caution shorts. Extreme negative = long opportunity.
* Liquidation cascade during sweep = strong confirmation.

---

### Setup A (Primary) — Liquidity Sweep + CHoCH + Order Block (ENABLED, AI bypassed)

1. Confirm HTF trend (4H or 1H)
2. Identify liquidity buildup (equal highs/lows)
3. Liquidity sweep occurs
4. CHoCH confirms direction change
5. Fresh OB forms (<72h)
6. OB in discount (long) or premium (short) — or 5+ confluences override PD
7. Retrace to OB — entry at `SETUP_A_ENTRY_PCT` (default 65% — Optuna optimized)
8. Volume spike >2x + liquidations visible
9. SL-too-close filter (`MIN_RISK_DISTANCE_PCT` 0.2%) in strategy layer
10. AI bypassed (synthetic approval) + Risk check passes

---

### Setup B (Secondary) — BOS + FVG + Order Block (DISABLED)

Disabled from ENABLED_SETUPS. 0-7.7% WR in backtests. Code remains for future re-enable.

---

### Setup D_choch (Quick) — LTF CHoCH Scalp (ENABLED)

1. CHoCH on 5m
2. Fresh OB near price
3. HTF bias + PD zone aligned
4. Entry at 50% of OB
5. AI bypassed (QUICK_SETUP_TYPES) + Risk check passes
6. 1h entry timeout, 4h max duration

Setup D_bos disabled (20-33% WR, net negative in all runs).

---

### Minimum Confluence Rule

* Minimum 2 confirmations per trade
* OB alone → DO NOT trade
* FVG alone → DO NOT trade
* Liquidity Sweep alone → DO NOT trade

---

### Exit Rules

* Stop Loss mandatory. Never move against trade.
* Default mode (`TRAILING_TP_ENABLED=false`):
  * Single TP at tp2 (2:1 R:R) — closes 100% of position
  * Breakeven: price crosses tp1 (1:1 R:R) → SL moves to entry
  * Trailing SL: price crosses midpoint of tp1 and tp2 (1.5:1 R:R) → SL moves to tp1
* Progressive trail mode (`TRAILING_TP_ENABLED=true`):
  * Ceiling TP at 5:1 R:R (crash protection)
  * SL trails in 0.5 R:R steps, always one step behind highest level reached
* Max duration: 12 hours
* Invalidation: Close through full OB/FVG → exit immediately

---

### Layer 3: AI Service (`ai_service/`)

Claude API (Sonnet) as filter. Does not originate trades. **Claude has NO internet access** — all data must be provided as structured context.

**Current status (2026-03-12):** AI filter is **bypassed for all active setups**. Setup A is in `AI_BYPASS_SETUP_TYPES` (89.6% approval rate = no filtering value). Setup D variants are in `QUICK_SETUP_TYPES` (skip AI by design). Setup B and F are disabled. Zero Claude API calls in the pipeline currently. All code and infrastructure remain for re-enable when recalibrated.

**Bypass mechanism:** `config/settings.py` defines `QUICK_SETUP_TYPES` (setup_c, setup_d, setup_d_bos, setup_d_choch, setup_e) and `AI_BYPASS_SETUP_TYPES` (setup_a, setup_b). Both generate synthetic `AIDecision(confidence=1.0, approved=True)` instead of calling Claude.

**Prompt approach (Scoring Rubric v2):**
No narrative doctrine. Claude scores 4 dimensions (0-5): setup_quality, market_support, contradiction, data_sufficiency. Decision rules are mechanical: approve if setup_quality >= 3 AND contradiction <= 2 AND confidence >= threshold. "Insufficient edge" is a valid rejection — approval requires positive evidence, not just absence of contradiction.

**What Claude receives (built by `prompt_builder.py`):**
* The detected setup (type, pair, direction, entry, SL, TP levels, confluences with [SUPPORTING]/[CONTEXT] tags)
* HTF bias (labeled as "aligned" or "COUNTER-TREND")
* Current funding rate + neutral interpretation (directional crowding, not narrative)
* Open Interest (snapshot only — no trend inference)
* CVD snapshot (buy dominance %)
* Recent liquidation cascades (OI proxy — estimated USD from OI drops)
* Whale movements (net exchange flow, individual movements — no bullish/bearish labels)
* News sentiment (Fear & Greed + headlines)
* Recent price action (1H and 4H candle % change)

**Data availability rule:** Absent data = neutral. Neither penalize nor reward.

**What Claude does NOT have access to:**
* Twitter/X, Reddit, or any real-time social media data
* Macro economic data (Fed rates, CPI)

**Claude's output:** Structured JSON with:
* `confidence`: float 0.0–1.0 (minimum 0.50 to proceed)
* `approved`: bool (must be true AND confidence ≥ 0.50)
* `scores`: dict with setup_quality, market_support, contradiction, data_sufficiency (0-5 each)
* `supporting_factors`: list of concise factors supporting the trade
* `contradicting_factors`: list of concise factors against the trade
* `adjustments`: optional SL/TP modifications
* `warnings`: list of risk factors detected

Service constructs `reasoning` from factors ("Supporting: X; Y | Against: Z") and stores `scores` in `adjustments["scores"]`.

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
* Max daily DD: 5%
* Max weekly DD: 10%
* Max open positions: 5
* Minimum R/R: 1:1.2
* Max leverage: 7x
* Cooldown after loss: 15 min
* Max trades/day: 10

Position size formula:

```
# Fixed margin mode (default, FIXED_TRADE_MARGIN=20):
Margin = $20 (fixed)
Leverage = MAX_LEVERAGE (7x)
Notional = Margin × Leverage = $20 × 7x = $140
Position Size = Notional / Entry Price

# Percentage mode (fallback, FIXED_TRADE_MARGIN=0):
Notional = Capital × TRADE_CAPITAL_PCT
Leverage = MAX_LEVERAGE (7x)
Position Size = Notional / Entry Price
```

Note: `PositionSizer` (in `position_sizer.py`) computes dynamic leverage from risk%, capped at MAX_LEVERAGE. However, `RiskService.check()` uses fixed `MAX_LEVERAGE` directly for position sizing. The dynamic sizer is only used by the backtester.

---

### Layer 5: Execution Service (`execution_service/`)

Executes orders on OKX via ccxt (exchange id: `"okx"`).
Authentication: API key + secret + passphrase (`OKX_API_KEY`, `OKX_SECRET`, `OKX_PASSPHRASE` in `.env`).
Instrument format: `"BTC-USDT-SWAP"`, `"ETH-USDT-SWAP"` (OKX convention).

**Order flow:**
1. Receive approved trade from Risk Service (TradeSetup + RiskApproval)
2. Place limit entry order at calculated price (50% OB/FVG)
3. Attach SL as stop-market order (guaranteed fill on volatile moves)
4. Attach TP as limit order at tp2 (2:1 R:R, 100% close)
5. Monitor fill status + progressive SL management

**Order types:**
* Entry: Limit order. If not filled within 24 hours (swing) / 1 hour (quick) → cancel.
* Stop Loss: Stop-market (not stop-limit — stop-limits can skip during crashes)
* Take Profit: Single limit order at tp2 (100% of position)

**Partial fills & edge cases:**
* Entry partially fills → keep order open, SL/TP scale to filled amount
* Entry fails (insufficient margin, API error) → log ERROR, do NOT retry automatically
* SL/TP placement fails after entry fills → EMERGENCY close at market immediately
* Slippage tracking: log expected vs actual fill price on every order

**Periodic SL verification:**
* Every `SL_VERIFY_INTERVAL_SECONDS` (60s), `_verify_sl_exists()` calls `find_pending_algo_orders()` to confirm SL algo order still exists on exchange
* Catches silent SL drops that `fetch_order()` status checks miss (OKX may report `open` but order is gone)
* If SL not found + position still open → re-place SL immediately
* Complements the existing SL vanished fallback (12 consecutive fetch failures)

**PnL tracking:**
* All exits (TP, SL, emergency, timeout, excessive slippage) calculate PnL net of trading fees
* Trading fee: `TRADING_FEE_RATE=0.0005` (0.05% per side, OKX taker). Total fees = (entry_notional + exit_notional) × rate
* `actual_exit_price` tracked on every close and persisted to PostgreSQL `trades.actual_exit`

**Position management after entry (default, `TRAILING_TP_ENABLED=false`):**
* Breakeven: price crosses tp1 (1:1 R:R) → SL moves to entry price
* Trailing SL: price crosses midpoint of tp1 and tp2 (1.5:1 R:R) → SL moves to tp1
* TP: price reaches tp2 (2:1 R:R) → 100% close

**Position management (progressive trail, `TRAILING_TP_ENABLED=true`):**
* SL trails in `TRAIL_STEP_RR` (0.5) R:R steps, always one step behind
* Ceiling TP at `TRAIL_CEILING_RR` (5:1) as crash protection
* Backtester supports both modes via the same setting

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

### ML Instrumentation (`shared/ml_features.py`)

Collects structured training data for two future classical ML models:
1. **Fill probability model** — will a limit order get filled? (trains on: filled_*, unfilled_timeout, replaced)
2. **Trade quality model** — if filled, will the trade be profitable? (trains on: filled_* only)

**How it works:**
* Every detected setup gets a `setup_id` (auto-generated in TradeSetup, 16-char hex UUID)
* At detection time (BEFORE dedup/risk checks), `extract_setup_features()` captures ~40 features and writes to `ml_setups` table
* At every terminal point (dedup, risk rejection, fill timeout, SL, TP, trailing, replace), outcome is resolved via `update_ml_setup_outcome()`
* Features include: setup geometry (entry/SL/TP distances, R:R), decomposed confluences (has_sweep, has_choch, ob_volume_ratio, cvd_aligned, pd_zone, etc.), market state (funding, OI, CVD, buy dominance), missingness flags (has_funding, has_oi, has_cvd, has_news, has_whales), stale-entry features (setup_age_minutes, entry_distance_pct)
* Risk context (capital, open positions, drawdown) captured separately — potentially leaky for quality model

**Outcome types:** filled_tp, filled_sl, filled_trailing, filled_timeout, unfilled_timeout, risk_rejected, deduped, replaced

**Data leakage safety:** Features captured at strategy detection time, before any downstream decision. Risk context stored in separate columns, flagged for careful use.

**Health metrics:** `ml_setup_insert_ok/error`, `ml_outcome_update_ok/error` emitted to `bot_metrics` for Grafana monitoring.

**Config:** `ML_FEATURE_VERSION` (int, in settings.py) — increment when strategy params change to segment training data.

**Files:** `shared/ml_features.py` (feature extraction), `data_service/data_store.py` (ml_setups table + CRUD), `main.py` (pipeline instrumentation), `execution_service/monitor.py` (close outcome resolution)

---

## Backtesting & Optimization (`scripts/`)

### `scripts/backtest.py`
Offline backtester — replays historical candles through StrategyService + simulates fills.

```bash
python scripts/backtest.py --days 60                      # basic run
python scripts/backtest.py --days 60 --detail             # 1m resolution for SL/TP ordering
python scripts/backtest.py --days 60 --fill-prob 0.8      # 80% fill probability
python scripts/backtest.py --days 60 --fill-mode conservative --fill-buffer 0.001
```

**Timeframe-detail mode** (`--detail`): loads 1m candles from PostgreSQL. When a 5m/15m candle contains both SL and TP in its range, replays 1m sub-candles to determine which was hit first. Falls back to SL-first if no 1m data. Requires: `python scripts/fetch_history.py --timeframe 1m --days 90`.

**Fill probability** (`--fill-prob`): after price reaches entry level, applies a random probability (seeded with `--seed` for reproducibility) to simulate realistic limit order fill rates. Default 1.0 = always fill.

**Settings overrides**: `run_backtest(overrides={"PARAM": value})` for programmatic use (Optuna).

### `scripts/optimize.py`
Optuna parameter optimizer — automated strategy tuning via `run_backtest()`.

```bash
python scripts/optimize.py --days 60 --trials 100 --metric profit_factor
python scripts/optimize.py --days 60 --trials 50 --walk-forward --jobs 2
```

**10 tunable parameters**: SETUP_A_ENTRY_PCT, SETUP_A_MAX_SWEEP_CHOCH_GAP, OB_PROXIMITY_PCT, OB_MAX_DISTANCE_PCT, MIN_RISK_DISTANCE_PCT, OB_MIN_VOLUME_RATIO, OB_MAX_AGE_HOURS, OB_MIN_BODY_PCT, MIN_ATR_PCT, MIN_TARGET_SPACE_R.

**Metrics**: profit_factor (default), sharpe, pnl, win_rate, composite.

**Walk-forward validation** (`--walk-forward`): splits data 70% train / 30% test. Optimizes on train, validates on test vs baseline. Detects overfitting.

**Output**: JSON in `backtest_results/` with best params, top 5 trials, parameter importance.

**Last optimization (2026-03-15)**: 20 trials, 30d, PF 1.05→2.65. Walk-forward validated (test PF=3.07 vs baseline PF=0.88). Key changes: SETUP_A_ENTRY_PCT 0.50→0.65, OB_MAX_DISTANCE_PCT 0.08→0.04, MIN_ATR_PCT 0.0025→0.0045. Full param table in `backtest_results/TRACKER.md`.

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
* `OIFlushEvent` — OI flush event (pair, side, size_usd, source) detected via OI proxy
* `WhaleMovement` — ETH/BTC transfer (wallet, action, amount, exchange, significance, chain). Actions: `exchange_deposit`, `exchange_withdrawal`, `transfer_out`, `transfer_in`
* `SourceFreshness` — per-source freshness (name, priority, age_ms, is_stale)
* `SnapshotHealth` — aggregate snapshot health (completeness_pct, critical_sources_healthy, stale/missing)
* `MarketSnapshot` — aggregates all of the above for a single pair at a point in time (includes `health` field)

**Layer 2 (Strategy) outputs:**
* `TradeSetup` — detected setup with entry/SL/TP1/TP2, confluences list, htf_bias, ob_timeframe, setup_id (auto-generated 16-char hex for ML tracking)

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
    tp1_price FLOAT, tp2_price FLOAT, tp3_price FLOAT,  -- tp3 kept for historical data
    actual_entry FLOAT,       -- Real fill price (slippage tracking)
    actual_exit FLOAT,
    exit_reason VARCHAR(20),  -- "tp", "sl", "breakeven_sl", "trailing_sl", "timeout", "invalidation"
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
