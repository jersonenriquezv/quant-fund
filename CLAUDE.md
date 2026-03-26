# ONE-MAN QUANT FUND — Personal Crypto Trading Bot

A personal automated trading system using Smart Money Concepts (SMC) to detect setups in crypto and execute trades 24/7.

**Core principle:** Deterministic bot detects → Risk approves → Execution executes. If any layer says NO, the trade does NOT execute.

**Current state:** See `docs/SYSTEM_BASELINE.md` for active config, thresholds, setup status, and changelog.
**Service details:** See `docs/context/` for per-service documentation (Spanish).

---

## Rules

### Language
ALL code, comments, variable names, docstrings, commit messages, and log messages MUST be in English. No exceptions. The docs/context/ files can be in Spanish. Agent instructions are in Spanish but all output code is in English.

### Mobile Responsiveness
ALL dashboard UI changes MUST work on mobile (375px+). The dashboard uses 2 CSS breakpoints in `globals.css`: tablet (<=1023px, 2-column grid) and mobile (<=639px, single column). When modifying `dashboard/web/`:
* Test at 375px width (iPhone SE) — nothing should overflow or be unusable
* Use CSS classes instead of inline styles for responsive layout properties
* Hide low-priority table columns on mobile via `display: none` classes
* Tables must scroll horizontally on narrow screens (`.scroll-y` already handles this)

---

## Technical Stack

* **Language:** Python 3.12 (entire system, venv at ~/quant-fund/venv)
* **Exchange:** OKX via ccxt (REST) + native OKX WebSocket
* **Pairs:** 7 linear perpetuals — BTC, ETH, SOL, DOGE, XRP, LINK, AVAX (/USDT). OKX instrument IDs: `BTC-USDT-SWAP` (hyphens, not slashes)
* **Analysis:** pandas + numpy
* **AI Filter:** Claude API (Sonnet) — currently bypassed for all active setups
* **Database:** PostgreSQL (historical) + Redis (real-time cache)
* **On-chain:** Etherscan API (ETH wallets) + mempool.space API (BTC wallets)
* **Dashboard:** FastAPI (port 8000) + Next.js (port 3000)
* **Monitoring:** Grafana (port 3001)
* **Containers:** Docker + Docker Compose
* **Server:** Acer Nitro 5 (i5-9300H, 16GB RAM) running Ubuntu Server 24.04 — dedicated 24/7 production server
* **Access:** VS Code Remote SSH from main PC. Dashboard + Grafana via Tailscale (100.120.181.11)

---

## Project Structure

```
quant-fund/
├── main.py                  # Entry point — starts all services, runs pipeline
├── config/
│   ├── settings.py          # All configuration (mode, API keys, pairs, thresholds)
│   └── .env                 # Secrets — NOT in git
├── shared/
│   ├── models.py            # Dataclasses shared between all services
│   ├── ml_features.py       # ML feature extraction (setup features + risk context)
│   ├── logger.py            # Loguru setup (stdout + daily rotated files)
│   ├── notifier.py          # Telegram push notifications
│   └── alert_manager.py     # Priority-based Telegram alerts
├── data_service/            # Layer 1: OKX WebSocket, REST, whale tracking, Redis/PostgreSQL
├── strategy_service/        # Layer 2: SMC pattern detection (BOS/CHoCH, OB, FVG, sweeps)
│   ├── setups.py            # Swing setups A/B/F/G + confluence + OB scoring
│   └── quick_setups.py      # Quick setups C/D/E/H
├── ai_service/              # Layer 3: Claude filter (currently bypassed)
├── risk_service/            # Layer 4: Guardrails, position sizing
├── execution_service/       # Layer 5: OKX order placement, SL/TP lifecycle, PnL
├── tests/                   # pytest suite
├── scripts/
│   ├── backtest.py          # Offline backtester (historical candle replay)
│   └── optimize.py          # Optuna parameter optimizer
├── dashboard/
│   ├── api/                 # FastAPI backend (read-only, port 8000)
│   └── web/                 # Next.js frontend (port 3000)
├── docs/
│   ├── SYSTEM_BASELINE.md   # Source of truth: config, thresholds, setup status, changelog
│   ├── context/             # Per-service docs (Spanish)
│   └── audits/              # Scientific audits
└── backtest_results/
    └── TRACKER.md           # Historical optimization runs
```

---

## Architecture — 5 Layers

All layers run in the **same Python process**. Direct function calls, no message queues.

```
Market Data → [1. Data] → [2. Strategy] → [3. AI*] → [4. Risk] → [5. Execution] → OKX
* AI currently bypassed for all active setups (synthetic AIDecision)
** Shadow mode: setups in SHADOW_MODE_SETUPS skip AI/Risk/Execution, tracked by ShadowMonitor
```

### Pipeline Flow (main.py)

```python
candle = ...  # from OKX WebSocket (confirmed=True)
setup = strategy_service.evaluate(candle.pair, candle)
if setup:
    # Dedup check (1h TTL)
    # ML feature logging (all setups)
    # Shadow mode: track theoretical outcome, don't execute
    if setup.setup_type in SHADOW_MODE_SETUPS:
        shadow_monitor.add_shadow(setup)  # tracks TP/SL/timeout from price
        return
    # Live path continues for non-shadow setups
    if setup.setup_type in QUICK_SETUP_TYPES or setup.setup_type in AI_BYPASS_SETUP_TYPES:
        decision = AIDecision(confidence=1.0, approved=True, ...)
    else:
        decision = await ai_service.evaluate(setup, snapshot)
    if decision.approved:
        approval = risk_service.check(setup)
        if approval.approved:
            execution_service.execute(setup, approval)
```

### Redis Role
* State caching (candles, OI, funding, orderbook depth)
* Persistence between restarts (portfolio state, daily PnL)
* NOT for inter-module messaging

### OKX API
* **Rate limits:** 20 req/2s market data, 60 req/2s trading
* **Auth:** API key + secret + passphrase in `.env`
* **Demo mode:** `OKX_SANDBOX=true` → `x-simulated-trading: 1` header
* OKX website geo-blocked in Canada, API works fine

---

## Database Schema (PostgreSQL)

```sql
candles (pair, timeframe, timestamp, open, high, low, close, volume, volume_quote)
  -- UNIQUE(pair, timeframe, timestamp), timestamp = Unix ms

trades (pair, direction, setup_type, entry_price, sl_price, tp1_price, tp2_price,
        actual_entry, actual_exit, exit_reason, position_size, pnl_usd, pnl_pct,
        ai_confidence, opened_at, closed_at, status)
  -- exit_reason: "tp", "sl", "breakeven_sl", "trailing_sl", "timeout", "invalidation"
  -- status: "open", "closed", "cancelled"

ai_decisions (trade_id, confidence, reasoning, adjustments JSONB, warnings JSONB)
risk_events (event_type, details JSONB)
ml_setups (setup_id, feature_version, ~40 features, outcome_type, ...)
```

---

## Environment Variables (`.env`)

```
OKX_API_KEY=  OKX_SECRET=  OKX_PASSPHRASE=  OKX_SANDBOX=true
ETHERSCAN_API_KEY=  ANTHROPIC_API_KEY=
TELEGRAM_BOT_TOKEN=  TELEGRAM_CHAT_ID=
POSTGRES_HOST=localhost  POSTGRES_PORT=5432  POSTGRES_DB=quant_fund
POSTGRES_USER=jer  POSTGRES_PASSWORD=
REDIS_HOST=localhost  REDIS_PORT=6379
```

---

## Key Conventions

* **All inter-service data** uses typed frozen dataclasses from `shared/models.py`. No raw dicts between layers.
* **Minimum 2 confluences** per trade (OB/FVG/sweep alone = no trade)
* **SL is mandatory**, never moves against trade
* **ML instrumentation:** Every setup gets a `setup_id`, features captured at detection, outcome resolved at terminal point. See SYSTEM_BASELINE §7 for version history.
* **Backtesting:** `scripts/backtest.py` replays candles. `scripts/optimize.py` uses Optuna. See `backtest_results/TRACKER.md`.
