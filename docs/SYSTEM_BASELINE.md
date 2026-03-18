# SYSTEM BASELINE — Quant Fund Trading Bot

> Source of truth for system state. Updated on every material change.
> Reflects code reality — if code and doc disagree, fix the doc.

**Last updated:** 2026-03-18
**ML Feature Version:** 4
**Bot status:** LIVE (OKX_SANDBOX=false, $108 capital)

---

## 1. Active Configuration

### Pairs & Timeframes
| Setting | Value |
|---------|-------|
| TRADING_PAIRS | ETH, BTC, SOL, DOGE, XRP, LINK, AVAX (/USDT) |
| HTF_TIMEFRAMES | 4h, 1h |
| LTF_TIMEFRAMES | 15m, 5m |
| SWING_SETUP_TIMEFRAMES | 15m |

### Enabled Setups
| Setup | Status | Type | Historical WR |
|-------|--------|------|---------------|
| A (Sweep+CHoCH+OB) | **ENABLED** | swing, AI bypass | 45-50% |
| B (BOS+FVG+OB) | **DISABLED** | — | 0-7.7% |
| C (Funding Squeeze) | **ENABLED** | quick | live, collecting data |
| D_choch (LTF CHoCH) | **ENABLED** | quick | 75% backtest |
| D_bos (LTF BOS) | **DISABLED** | — | 20-33% |
| E (Cascade Reversal) | **ENABLED** | quick | live, collecting data |
| F (Pure OB Retest) | **ENABLED** | swing, AI bypass | 34-59% |
| G (Breaker Block) | **DISABLED** | — | unvalidated |
| H (Momentum/Impulse) | **ENABLED** | quick, AI bypass | live, collecting data |

### Risk Guardrails
| Parameter | Value | Notes |
|-----------|-------|-------|
| FIXED_TRADE_MARGIN | $20 | × 7x leverage = $140 notional |
| MAX_LEVERAGE | 7x | |
| MAX_OPEN_POSITIONS | 8 | |
| MAX_TRADES_PER_DAY | 20 | |
| MAX_DAILY_DRAWDOWN | 5% | |
| MAX_WEEKLY_DRAWDOWN | 10% | |
| COOLDOWN_MINUTES | 5 | after loss |
| MIN_RISK_REWARD | 1.2 | swing setups |
| MIN_RISK_REWARD_QUICK | 1.0 | quick setups |
| MIN_RISK_DISTANCE_PCT | 0.5% | SL-too-close filter |
| MAX_SLIPPAGE_PCT | 0.3% | emergency close if exceeded |

### Strategy Thresholds (Optuna-validated + audit-restored)
| Parameter | Value | Source |
|-----------|-------|--------|
| OB_MIN_VOLUME_RATIO | 1.3 | Optuna 03-15, restored audit 03-18 |
| OB_MAX_AGE_HOURS | 84 | Optuna 03-15 |
| OB_MIN_BODY_PCT | 0.15% | Optuna 03-15 |
| OB_PROXIMITY_PCT | 1.0% | aggressive mode |
| OB_MAX_DISTANCE_PCT | 3% | tightened from 8% |
| MIN_ATR_PCT | 0.35% | Optuna 03-15, restored audit 03-18 |
| MIN_TARGET_SPACE_R | 1.4 | Optuna 03-15, restored audit 03-18 |
| SWING_LOOKBACK | 5 | default, never Optuna-tested |
| BOS_CONFIRMATION_PCT | 0.1% | default |
| SWEEP_MIN_VOLUME_RATIO | 1.5x | default |
| SETUP_A_ENTRY_PCT | 65% | Optuna 03-15 |
| SETUP_A_MAX_SWEEP_CHOCH_GAP | 60 | aggressive mode (Optuna: 45) |
| FUNDING_EXTREME_THRESHOLD | 0.0003 | symmetric for both long/short |
| PD_AS_CONFLUENCE | true | aggressive mode |
| PD_OVERRIDE_MIN_CONFLUENCES | 5 | |

### Setup-Specific Parameters
| Parameter | Value | Setup |
|-----------|-------|-------|
| SETUP_F_MAX_BOS_AGE_CANDLES | 40 | F |
| SETUP_F_MIN_BOS_DISPLACEMENT_PCT | 0.1% | F |
| SETUP_F_MAX_OB_BOS_GAP_CANDLES | 20 | F |
| SETUP_F_MIN_OB_SCORE | 0.35 | F |
| SETUP_F_MAX_ENTRY_DISTANCE_PCT | 5% | F |
| SETUP_F_MIN_CONFLUENCES | 2 | F |
| SETUP_H_MIN_DIRECTIONAL_PCT | 60% | H |
| SETUP_H_MIN_IMPULSE_PCT | 0.3% | H |
| SETUP_H_VOLUME_SPIKE_RATIO | 1.5x | H |
| SETUP_H_MAX_SL_PCT | 3% | H |
| SETUP_H_DECEL_RATIO | 0.4 | H |
| SETUP_H_MAX_EXTENDED_PCT | 1.5% | H |
| SETUP_D_ENTRY_PCT | 85% | D |
| QUICK_OB_MAX_DISTANCE_PCT | 1.5% | quick |
| QUICK_SETUP_COOLDOWN | 1h | quick |

### TP/SL Configuration
| Parameter | Value |
|-----------|-------|
| TP1_RR_RATIO | 1.0 (breakeven trigger) |
| TP2_RR_RATIO | 2.0 (single TP, 100% close) |
| TRAILING_TP_ENABLED | false |
| MAX_TRADE_DURATION | 12h swing / 4h quick |
| ENTRY_TIMEOUT | 24h swing / 1h quick |

---

## 2. Gating Logic (Pipeline Order)

```
Candle confirmed → StrategyService.evaluate()
  ├── HTF bias undefined? → BLOCK (all setups)
  ├── Swing setups (15m only): A → B → F → G
  │     Each: detect pattern → PD check → OB selection → volume confirmation → confluence ≥ 2
  │     Post-detection: ATR filter → target space filter
  ├── Quick setups (5m): C → D → E → H (with per-type cooldown)
  └── TradeSetup produced
        ├── ENABLED_SETUPS check → discard if not in list
        ├── Dedup cache (1h TTL)
        ├── AI filter → BYPASSED for all active setups (synthetic approval)
        ├── Risk Service → guardrails, position sizing
        └── Execution Service → limit order + SL + TP
```

### Key Signal Hierarchy (audit 03-18)
| Signal | Role | Assessment |
|--------|------|------------|
| HTF bias (4H/1H) | **Hard gate** | Blocks all if undefined (~60% of time in range) |
| Sweep (Setup A only) | **Core trigger** | Strongest microstructure signal |
| CHoCH / BOS | **Core trigger** | Required for all setups |
| Order Block | **Core trigger** | Required for 5/6 enabled setups |
| CVD (divergence + MTF) | Confluence | Upgraded: price vs CVD direction, 3-TF agreement |
| OI delta | Confluence | Upgraded: tracks delta between evaluations |
| Funding rate | Confluence | Fixed: symmetric threshold (0.0003) |
| PD zone | Confluence | Demoted from hard gate (PD_AS_CONFLUENCE=true) |
| OB volume | Confluence | Restored: 1.3x minimum (was 1.0 = disabled) |
| Whale flows | Logging only | Collected, never used in decisions |
| Fear & Greed | Pre-filter | F&G < 5 or > 85 (almost never triggers) |

---

## 3. Infrastructure
| Component | Status | Details |
|-----------|--------|---------|
| Server | Acer Nitro 5 (Ubuntu 24.04) | 24/7, static IP 192.168.1.236 |
| Exchange | OKX live | API from Canada, account Mexico |
| Database | PostgreSQL + Redis | local |
| Dashboard | FastAPI :8000 + Next.js :3000 | Tailscale accessible |
| Grafana | :3001 | 3 dashboards |
| AI filter | Claude Sonnet | BYPASSED for all active setups |
| CVD warmup | Progressive | 5m→VALID in 5min, 15m in 15min, 1h in 60min |

### Resource Profile (compute audit 03-18)
Reference for VPS sizing when migrating from Nitro 5.

| Resource | Current Usage | Capacity | Headroom |
|----------|--------------|----------|----------|
| **RAM** | ~85 MB (5 MB buffers + Python) | 16 GB | 188× |
| **CPU** | Pipeline <300ms, mostly idle | 4 cores i5-9300H | 50×+ |
| **OKX API (market)** | 1.4 req/min | 600 req/min | 430× |
| **OKX API (trading)** | 104 req/min (8 positions) | 1,800 req/min | 17× |
| **Disk (PostgreSQL)** | 0.2 MB/day | 190 GB SSD | ~2600 years |
| **Disk (logs)** | ≤10.5 GB (30d retention) | 190 GB SSD | 18× |
| **WebSocket** | 2 connections, <<1 Mbps | broadband | n/a |
| **Asyncio tasks** | ~15 concurrent | event loop idle | n/a |

**Scaling limits (same architecture, no code changes):**

| Pairs | RAM | OKX API | DB Growth | Verdict |
|-------|-----|---------|-----------|---------|
| 7 (current) | 85 MB | <2% | 0.2 MB/day | OK |
| 14 (2×) | ~100 MB | <2% | 0.4 MB/day | OK |
| 35 (5×) | ~120 MB | <3% | 1 MB/day | OK |
| 70 (10×) | ~160 MB | <5% | 2 MB/day | OK |
| 200 | ~400 MB | **17%** | 6 MB/day | **API rate limit watch** |

**Minimum VPS spec for current 7 pairs:** 1 vCPU, 1 GB RAM, 20 GB SSD ($5/mo DigitalOcean/Hetzner).
**Recommended VPS for 20+ pairs:** 2 vCPU, 2 GB RAM, 40 GB SSD ($12/mo).

---

## 4. Backtest Results (Historical)

### Baseline Runs
| Date | Config | Trades | WR | PnL | PF | Sharpe | Notes |
|------|--------|--------|-----|------|-----|--------|-------|
| 03-10 | Aggressive, no AI | 97 | 51.5% | +$7,558 | 1.81 | 4.90 | All setups, 60d |
| 03-10 | AI v1 | 54 | 44.4% | +$2,104 | 1.45 | 3.44 | AI destroyed B |
| 03-15 | Pre-Optuna | 26 | 42.3% | +$123 | 1.05 | — | |
| 03-15 | Optuna best (30d) | 17 | 58.8% | +$1,683 | 2.65 | — | Walk-forward: PF 3.07 |

### Per-Setup Performance (60d baseline, no AI)
| Setup | Trades | WR | PnL | Status |
|-------|--------|-----|------|--------|
| A | 20 | 45.0% | -$395 | ENABLED |
| B | 51 | 49.0% | +$3,647 | DISABLED (audit: F better) |
| D | 9 | 66.7% | +$2,553 | D_choch ENABLED |
| F | 17 | 58.8% | +$1,753 | ENABLED |

### AI Impact (v1)
| Setup | No-AI WR | AI WR | Delta | Verdict |
|-------|----------|-------|-------|---------|
| A | 45.0% | 50.0% | +5% | Marginal |
| B | 49.0% | 21.4% | **-28%** | Destroyed |
| D | 66.7% | 58.3% | -8% | Slight negative |
| F | 58.8% | 50.0% | -9% | Negative |

---

## 5. Hypotheses (Active)

| # | Hypothesis | Evidence | Action |
|---|-----------|----------|--------|
| H1 | CVD divergence > simple boolean for entry quality | Audit: cvd_15m>0 discards magnitude/direction info | Monitor CVD divergence vs aligned vs MTF confluences in live trades |
| H2 | OI delta adds predictive value | Audit: was existence-check only | Track oi_rising/oi_dropping confluence presence vs trade outcome |
| H3 | Setup F ≥ Setup B (F = B minus FVG gate) | Backtest: F 58.8% WR vs B 49% when both structural | B disabled, F enabled — compare live |
| H4 | Restored thresholds (ATR 0.35%, OB vol 1.3) reduce false positives | Audit: relaxed values let noise through | Compare setup frequency and WR vs aggressive period |
| H5 | HTF undefined blocks too many setups in range markets | ~60% of time no 4H/1H trend defined | If no trades after 1-2 weeks, try HTF_BIAS_REQUIRE_4H=false |

---

## 6. Open Problems

| # | Problem | Severity | Notes |
|---|---------|----------|-------|
| P1 | SWING_LOOKBACK=5 never tested at other values | Medium | Different values produce completely different market structure |
| P2 | OB scoring weights arbitrary (35/30/20/15) | Medium | Zero empirical basis |
| P3 | 40 ML features collected, 0 models trained | High | System is collecting but not learning |
| P4 | SETUP_H_MIN_DIRECTIONAL_PCT=0.60 close to random | Low | 3/5 same-color = ~50% by random walk |
| P5 | Same % thresholds applied across all pairs | Medium | BTC at $84K vs DOGE at $0.15 |
| P6 | Whale flows collected but never used | Low | Data exists, no strategy integration |
| P7 | Cross-pair correlation not used | Low | BTC/ETH correlation breaks predict regime shifts |

---

## 7. Changelog

### 2026-03-18 — Compute Audit + Health Observability
**What changed:**
- Asyncio task count emitted as `asyncio_tasks` metric every 30s
- Warning logged if task count > 25 (expected ~15, leak detection)
- Task count added to `health()` dict for dashboard visibility
- Resource profile and VPS sizing reference added to SYSTEM_BASELINE

**Why:** Compute audit found system is sustainable (85 MB / <2% API / 15 tasks) but lacked observability for task leaks.

**Expected impact:** No behavior change. Early warning if asyncio tasks leak.

### 2026-03-18 — Strategy Audit Implementation
**What changed:**
- OB_MIN_VOLUME_RATIO: 1.0 → 1.3 (restored Optuna value)
- MIN_ATR_PCT: 0.20% → 0.35% (restored Optuna value)
- MIN_TARGET_SPACE_R: 1.0 → 1.4 (restored Optuna value)
- Setup B: ENABLED → DISABLED (F is strictly better)
- Funding rate: asymmetric (-0.0001/+0.0003) → symmetric (±0.0003)
- CVD: simple boolean (cvd_15m > 0) → divergence + MTF agreement
- OI: existence check → delta tracking between evaluations
- Setups B/F/G: inline confluence logic → shared `_check_volume_confirmation()`
- ML_FEATURE_VERSION: 3 → 4

**Why:** Strategy audit identified overweighted weak signals (OBs without volume), underweighted strong signals (CVD, OI, funding), and thresholds relaxed beyond Optuna-validated values.

**Expected impact:** Fewer but higher-quality setups. Setup frequency will drop (tighter filters), WR should increase.

### 2026-03-17 — Position Guardian + Setup H Exhaustion
- Position Guardian service added (counter-candle, momentum decay, stall detection, CVD adverse)
- Setup H exhaustion filters (deceleration, extended move, volume decay)
- ML_FEATURE_VERSION: 2 → 3

### 2026-03-16 — Data Integrity Layer
- State machine (RECOVERING → RUNNING → DEGRADED)
- CVD fix (contract size normalization)
- Execution gating (require RUNNING state)
- Reconnect recovery (gap backfill)

### 2026-03-15 — Optuna Optimization + Aggressive Mode
- 20-trial optimization: PF 1.05 → 2.65 (walk-forward validated)
- Aggressive validation mode: thresholds relaxed for data collection
- All setups enabled, AI bypassed for all
- PD_AS_CONFLUENCE=true

### 2026-03-13 — Setup F/B Hardening
- BOS age, displacement, OB-BOS gap, OB score filters
- Setup B direction bug fix

### 2026-03-11 — HTF Campaign Trading
- 4H position trading with pyramid adds
- Daily bias instead of 4H/1H
- HTF_CAMPAIGN_ENABLED (default false, now true)

### 2026-03-10 — AI v1 Backtest Results
- AI destroyed Setup B (49% → 21.4% WR)
- Root cause: missing CVD treated as negative evidence
- Decision: bypass AI for all setups until recalibrated

### 2026-03-06 — Bot Goes Live
- OKX_SANDBOX=false, FIXED_TRADE_MARGIN=$20
- Capital: ~$108 on OKX live
