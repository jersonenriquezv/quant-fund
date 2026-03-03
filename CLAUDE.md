# ONE-MAN QUANT FUND — Personal Crypto Trading Bot

## What this project is

A personal automated trading system that uses Smart Money Concepts (SMC) to detect setups in crypto and execute trades 24/7. It is a micro-scale version of how institutional hedge funds like Citadel or Two Sigma operate.

**Core principle:** Deterministic bot detects → Claude filters → Risk approves → Execution executes.
If any layer says NO, the trade does NOT execute.

---
## Language Rule
ALL code, comments, variable names, docstrings, commit messages, and log messages MUST be in English. No exceptions. The docs/context/ files can be in Spanish (they are for the user). Agent instructions are in Spanish but all output code is in English.

## Technical Stack

* **Language:** Python (entire system)
* **Exchange:** OKX via ccxt (REST) + native OKX WebSocket
* **Pairs:** BTC/USDT and ETH/USDT (linear perpetuals). Instrument IDs: BTC-USDT-SWAP, ETH-USDT-SWAP.
* **Analysis:** pandas + numpy
* **AI Filter:** Claude API (Sonnet)
* **Database:** PostgreSQL (historical) + Redis (real-time cache)
* **On-chain:** Etherscan API for top ETH wallet movements
* **Dashboard:** FastAPI + Next.js
* **Containers:** Docker + Docker Compose
* **Server:** Acer Nitro 5 (i5-9300H, 16GB RAM, GTX 1050) running Ubuntu Server 24.04

---
## Infrastructure
- **Server:** Acer Nitro 5 (i5-9300H, 4 cores/8 threads, 16GB RAM, SSD 240GB) running Ubuntu Server 24.04 — dedicated 24/7 server, no GUI
- **IP:** 192.168.1.238 (static, local network)
- **Internet:** Direct connection, no proxy. Required for OKX WebSocket, Binance WebSocket, Etherscan API, Claude API.
- **Docker + Docker Compose:** Installed and ready
- **Python:** 3.12 with venv at ~/quant-fund/venv
- **Node.js:** 20.x (for Claude Code only)
- **Development:** VS Code Remote SSH from main PC (ASUS). Claude Code runs directly on the server.
- **The bot runs ON this server 24/7.** All code, Docker containers, databases, and WebSocket connections live here. It's not a dev machine — it's the production server.

## ARCHITECTURE — 5 Layers

The system has 5 independent services communicating sequentially:

```
Market Data → [1. Data Service] → [2. Strategy Service] → [3. AI Service] → [4. Risk Service] → [5. Execution Service] → Exchange
```

---

### Layer 1: Data Service (`data_service/`)

Receives real-time data from multiple sources:

* **OKX WebSocket**: Candles on `/business` (`wss://ws.okx.com:8443/ws/v5/business`), trades on `/public` (`wss://ws.okx.com:8443/ws/v5/public`)
* **OKX REST** (via ccxt): Candle backfill, funding rate (every 8 hours), open interest
* **Binance Futures WebSocket** (`wss://fstream.binance.com/ws/!forceOrder@arr`): Liquidation data (public, no API key)
* **Etherscan REST**: Whale ETH wallet movements
* Funding rates: OKX charges every 8 hours (standard CEX schedule)
* Liquidations: Binance forceOrder (primary) + OKX OI drop >2% in 5min (proxy)
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

Claude API as filter. Does not originate trades.

Evaluates macro context, sentiment, news, HTF confluence, liquidity, OI/funding trends, and on-chain ETH activity.

Minimum confidence: ≥ 0.60.

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
Order types: limit, market, stop-loss (trigger orders), take-profit — all supported.
Places SL/TP automatically. Monitors fills and slippage.

**Trading Mode:**
* Demo first (4-week minimum): set `OKX_SANDBOX=true`, adds `x-simulated-trading: 1` header
* Live: set `OKX_SANDBOX=false`

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
