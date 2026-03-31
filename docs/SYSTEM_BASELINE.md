# SYSTEM BASELINE — Quant Fund Trading Bot

> Source of truth for system state. Updated on every material change.
> Reflects code reality — if code and doc disagree, fix the doc.

**Last updated:** 2026-03-31
**ML Feature Version:** 8
**Bot status:** LIVE (OKX_SANDBOX=false, ~$86 capital)

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
| H (Momentum/Impulse) | **DISABLED** (live+shadow) | — | 11% WR live, 0/12 aligned shadow. Chases impulse tips without OB retest. Needs pullback redesign. |

### Risk Guardrails
| Parameter | Value | Notes |
|-----------|-------|-------|
| RISK_PER_TRADE | 1% | Dynamic sizing via PositionSizer (was: flat $20 margin) |
| MAX_LEVERAGE | 7x | Cap on PositionSizer output |
| MAX_OPEN_POSITIONS | 8 | |
| MAX_TRADES_PER_DAY | 20 | |
| MAX_DAILY_DRAWDOWN | 10% | was 5%, raised for $20/$108 capital ratio |
| MAX_WEEKLY_DRAWDOWN | 10% | |
| COOLDOWN_MINUTES | 5 | after loss |
| MIN_RISK_REWARD | 2.0 | swing setups (was 1.2) |
| MIN_RISK_REWARD_QUICK | 1.5 | quick setups (was 1.0) |
| MIN_RISK_DISTANCE_PCT | 0.5% | SL-too-close filter |
| ATR_SL_FLOOR_MULTIPLIER | 3.0 | SL widened to 3× ATR(14) if structural SL is tighter |
| MAX_SL_PCT | 4% | SL-too-far cap — rejects setups with OB SL > 4% |
| REGIME_EXTREME_FEAR_GATE | 10 | F&G < 10 → reject ALL setups (systemic crisis only) |
| MAX_PORTFOLIO_HEAT_PCT | 6% | Sum of (size × SL_distance) across all positions |
| MAX_SLIPPAGE_PCT | 0.3% | emergency close if exceeded |
| FIXED_TRADE_MARGIN | $20 | Fallback only (if PositionSizer fails) |

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
| SETUP_TP2_RR | A=2.5, B/F/G/H=2.0, C/E=2.0, D=1.5 |
| TRAILING_TP_ENABLED | false |
| MAX_TRADE_DURATION | 12h swing / 4h quick |
| ENTRY_TIMEOUT | 24h swing / 1h quick |

---

## 2. Gating Logic (Pipeline Order)

```
Candle confirmed → StrategyService.evaluate()
  ├── HTF bias undefined? → BLOCK (all setups)
  ├── LTF direction != HTF bias? → BLOCK (REQUIRE_HTF_LTF_ALIGNMENT=True)
  ├── Swing setups (15m only): A → B → F → G
  │     Each: detect pattern → PD check → OB selection → volume confirmation
  │     → structural confluence ≥ 2 (metrics don't count)
  │     Post-detection: ATR SL floor (widen to 3× ATR if tight) → ATR filter → target space filter
  ├── Quick setups (5m): C → D → E (with per-type cooldown)
  └── TradeSetup produced
        ├── ENABLED_SETUPS / SHADOW_MODE_SETUPS check
        ├── Data integrity gate (DEGRADED blocks all; RECOVERING allows candle-only setups)
        ├── Regime gate (F&G < 20 → BLOCK all)
        ├── Dedup cache (1h TTL) + shadow dedup (active position check)
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
| Fear & Greed | **Hard gate** + pre-filter | F&G < 10 → reject ALL (systemic crisis). Also: < 5 reject longs, > 85 reject shorts |

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
| 03-30 | Pre-diagnostic (30d) | 104 | 36.5% | -$717 | 0.87 | -1.38 | Setup H = 74 trades, -$1,144 |
| 03-30 | Post-diagnostic (30d) | 18 | 61.1% | +$885 | 2.63 | 9.30 | H disabled, regime gate, ATR SL floor, confluence fix |

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
| H6 | Meta-labeling model (AFML Ch.3) > LLM filter | AI v1 destroyed B, AI v2 89.6% approval = no value | Train classifier on ml_setups v4+ data, replace Claude |
| H7 | Half-Kelly bet sizing improves risk-adjusted returns | AFML Ch.10: size proportional to calibrated P(profit) | Wire BET_SIZING after calibrated model exists |

---

## 6. Open Problems

| # | Problem | Severity | Notes |
|---|---------|----------|-------|
| P1 | SWING_LOOKBACK=5 never tested at other values | Medium | Different values produce completely different market structure |
| P2 | OB scoring weights (impulse 25/vol 20/fresh 20/prox 15/retest 10/size 10) | Medium | Impulse + retest added 03-25. Weights still need empirical validation via backtest/Optuna. |
| P3 | 40 ML features collected, 0 models trained | High | System is collecting but not learning |
| P4 | SETUP_H_MIN_DIRECTIONAL_PCT=0.60 close to random | Low | 3/5 same-color = ~50% by random walk |
| P5 | Same % thresholds applied across all pairs | Medium | BTC at $84K vs DOGE at $0.15 |
| P6 | Whale flows collected but never used | Low | Data exists, no strategy integration |
| P7 | Cross-pair correlation not used | Low | BTC/ETH correlation breaks predict regime shifts |
| P8 | AI service is LLM filter, not trained model | High | AFML: meta-labeling + bet sizing requires calibrated classifier, not Claude. See `docs/audits/ai-service-audit-2026-03-18.md` |

---

## 7. ML Feature Versioning

**Current version:** 8 (set in `config/settings.py:ML_FEATURE_VERSION`)
**Storage:** `ml_setups.feature_version` column in PostgreSQL
**Query training data:** `SELECT * FROM ml_setups WHERE feature_version >= 4 AND outcome_type IS NOT NULL AND outcome_type NOT IN ('shadow_dedup', 'data_blocked', 'shadow_risk_rejected', 'risk_rejected', 'regime_extreme_fear')`

| Version | Date | Changes | Training Status |
|---------|------|---------|-----------------|
| v1 | pre 03-17 | Fixed TP (2:1), legacy trailing, MIN_RISK 0.2%, HTF campaigns OFF | **DO NOT USE** — CVD in contracts, OI existence-only, asymmetric funding |
| v2 | 03-17 | Progressive trailing ON, HTF campaigns ON, TP2 3:1→2:1, MIN_RISK 0.5% | **DO NOT USE** — CVD still wrong units |
| v3 | 03-17 to 03-18 | Setup H momentum, guardian close tracking, CVD units fixed | **DO NOT USE** — OB vol=1.0, ATR=0.20%, funding asymmetric |
| v4 | 03-18 | OB vol 1.3, ATR 0.35%, target space 1.4, CVD divergence, OI delta, symmetric funding | **TRAINING READY** |
| v5 | 03-19 | Graduated signal weighting (sweep/CVD/OI/funding by strength, not binary), tier features | **TRAINING READY** |
| v6 | 03-19+ | daily_vol (AFML Ch.3 getDailyVol), EWMA volatility for barrier normalization | **TRAINING READY** |
| v7 | 03-25+ | Shadow mode risk_approved/risk_reject_reason columns, OB impulse/retest scoring | **TRAINING READY** |
| v8 | 03-30+ | confluence_count = structural only (BOS/CHoCH/FVG/OB/sweep/breaker), regime gate, ATR SL floor | **TRAINING READY** |

**When to bump:** Increment `ML_FEATURE_VERSION` whenever strategy params change in ways that alter feature semantics (OB scoring weights, PD rules, confluence logic, threshold changes).

**Minimum for Phase 1 (feature importance):** 50+ labeled outcomes with `feature_version >= 4` (filled_tp + filled_sl + filled_trailing).

---

## 8. Changelog

### 2026-03-31 — Manual Trading Module
**What changed:**
- New manual trading module at `dashboard/api/manual/` — calculator, trade CRUD, partial closes, analytics
- Supports linear (USDT-margined) and inverse (coin-margined) position sizing
- 50/50 TP plan with auto-suggest, balance tracking, analytics (win rate, R multiples, TP hit rates)
- Standalone HTML page at `/manual`, API endpoints at `/api/manual/*`
- New PostgreSQL tables: `manual_trades`, `manual_partial_closes`, `manual_balances`

**Why:** Track and analyze discretionary trades alongside the bot, with proper position sizing math and journal-style review.

**Expected impact:** Zero impact on bot pipeline — completely isolated module. Dashboard API now also accepts PATCH method (CORS updated).

### 2026-03-30 — HTF Alignment Enforced + RECOVERING Gate Fix
**What changed:**
- **`REQUIRE_HTF_LTF_ALIGNMENT=True`**: Setups A/B/F now require LTF structure direction (CHoCH/BOS) to match HTF bias. Counter-trend trades blocked.
- **Data gate: RECOVERING allows candle-only setups**: Previously, `service=RECOVERING` blocked ALL setups. Now candle-only setups (A/B/D/F/H) bypass the global RECOVERING gate since WebSocket still delivers candles. Setups needing non-candle deps (C=funding+CVD, E=OI) remain blocked. Removed duplicate Gate 1 in `main.py`; all filtering goes through `can_trade_setup()`.

**Why:** Shadow diagnostic showed 17/17 SL on counter-trend setup_a (all long against bearish HTF). Also, 62 setup_a detections on 03-25 lost to `data_blocked` during a 3h RECOVERING window — setup_a only needs candles, which were flowing fine via WebSocket.

**Expected impact:** Cleaner ML shadow data (no more counter-trend noise). Fewer data_blocked losses during recovery episodes.

### 2026-03-30 — Shadow Diagnostic: Setup H Disabled, Regime Gate, Confluence Fix, ATR SL Floor
**What changed:**
- **Setup H disabled from shadow mode**: 12/14 aligned-HTF shadow losses were setup_h. Chases impulse tips at current price instead of waiting for OB retest. Only 1 structural confluence (BOS) inflated to 6-9 by impulse metrics. Kept in codebase for redesign with pullback requirement.
- **Regime gate (F&G < 20)**: Hard gate rejects ALL setups (any direction) when Fear & Greed < 20. Diagnostic: 14/14 trades lost at F&G=8. Market structure is unreliable in extreme fear. `REGIME_EXTREME_FEAR_GATE=20`, env-var overridable.
- **Structural-only confluence counting**: `confluence_count` and `_check_confluence_minimum()` now only count structural items (BOS, CHoCH, FVG, OB, sweep, breaker, pd_zone). Metrics (CVD, OI, funding, volume ratios, impulse stats) are captured as separate ML features but don't inflate the ≥2 gate. With corrected counting, all 14 shadow losses had only 0-1 structural confluences.
- **ATR SL floor**: SL widened to `max(structural_SL, 3× ATR(14))`. If OB-based SL is tighter than 3× ATR, the SL moves out and TPs recalculate to preserve R:R. Diagnostic: avg SL was 2.97× ATR → all 14 got stopped by noise. 7/14 would have hit TP2 with wider SL.
- **Shadow dedup fix**: `ShadowMonitor.add_shadow()` now rejects if an active shadow already exists for the same (pair, direction, setup_type). Previously, the 1h pipeline dedup TTL expired before shadow resolution, creating duplicate tracking + duplicate Telegram notifications. 552 orphan `ml_setups` rows cleaned up.
- **ML_FEATURE_VERSION bumped to 8** for confluence_count semantic change.

**Why:** Shadow data diagnostic (63 resolved setups, 18 unique filled trades) showed 0/14 aligned-HTF WR. Root causes: setup_h adverse selection, no regime filter, inflated confluence counts, SL within noise range. Backtest confirmed: old code -$717 (36.5% WR, PF 0.87), new code +$885 (61.1% WR, PF 2.63).

**Expected impact:** Fewer but higher-quality shadow trades. Zero trades during extreme fear (F&G < 20). Wider SLs with proportionally wider TPs. Shadow data collection continues for setups A/B/C/D/E/G.

### 2026-03-26 — Risk Management Overhaul + Trade Journal
**What changed:**
- **Dynamic position sizing**: PositionSizer now wired into live pipeline. `size = (capital × 1%) / SL_distance`. Replaces flat $20 margin. Queries OKX balance before each trade, falls back to tracked capital.
- **R:R minimums raised**: swing 1.2→2.0, quick 1.0→1.5. TP2 R:R raised: A=2.5, B/F/G/H=2.0, D=1.5.
- **MAX_SL_PCT=4%**: Rejects setups where SL > 4% from entry. `_check_sl_distance()` consolidates min+max SL checks across all 7 setup types.
- **Portfolio heat**: `MAX_PORTFOLIO_HEAT_PCT=6%`. Sum of (size × SL_distance) across all open positions. Checked after position sizing, before approval.
- **OB impulse score + retest count**: OB scoring weights: impulse 25%, volume 20%, freshness 20%, proximity 15%, retest 10%, size 10%. `impulse_score` measures post-OB displacement. `retest_count` penalizes multi-touch OBs.
- **Trade journal in PostgreSQL**: `trades` table gains `margin_used`, `risk_usd`, `r_multiple`, `rejection_reason`, `notes`. New `trade_rejections` table. `get_journal_summary()` query helper.
- **Startup pair diagnostic**: Logs per-pair capital adequacy (1% risk vs min order size) at startup.
- **Backtest**: MAX_SL_PCT + portfolio heat checks added to TradeSimulator.

**Why:** Audit identified 5 gaps: no correlation awareness, no aggregate notional cap, no portfolio heat, flat sizing ignoring volatility, no SL upper bound. Backtest comparison (30d): same PnL ($7.54 vs $7.22), max DD halved (3.5% vs 7.3%), avg R-multiple doubled (0.29 vs 0.14), PF improved (1.51 vs 1.23).

**Expected impact:** Smaller but better-calibrated positions. Lower drawdown. Higher-quality trade selection via R:R filter (30 low-R:R setups rejected in backtest). Portfolio heat prevents correlated blowup.

### 2026-03-26 — Shadow Telegram Alerts + HTF Leverage Fix
**What changed:**
- Shadow monitor now sends Telegram notifications for 3 lifecycle events: detection (SHADOW TRACKING), theoretical fill (SHADOW FILL), and outcome resolution (SHADOW TP/SL/TIMEOUT/NO_FILL). Shows pair, direction, setup type, prices, R:R, margin, and PnL.
- `ShadowMonitor` accepts optional `notifier` param, wired in `main.py`.
- **Bug fix**: `executor.configure_pair()` passed `lever` as int to OKX `set_margin_mode()`. OKX API requires string. All HTF campaign orders were failing silently since campaigns were enabled. Fixed: `{"lever": str(leverage)}`.
- Shadow mode now sizes against `SHADOW_CAPITAL` ($500 virtual) via `capital_override` param in `risk_service.check()`, instead of using live capital.
- Startup pair diagnostic now shows both live and shadow viability per pair.

**Why:** Shadow trades were invisible without checking logs. Telegram alerts enable remote monitoring from phone. The leverage bug blocked all HTF campaign execution since the feature was enabled.

**Expected impact:** Shadow alerts on Telegram (~5-15/day depending on market). HTF campaigns will now execute when AI+Risk approve.

### 2026-03-26 — Shadow Mode Risk Integration (ML_FEATURE_VERSION 7)
**What changed:**
- Shadow mode now runs `risk_service.check(dry_run=True)` — same guardrails, same sizing as live pipeline. No state mutation, no API calls, no `risk_events` persistence.
- Shadow position sizing uses `RiskApproval.position_size` (dynamic % of `SHADOW_CAPITAL` $500 virtual), via `capital_override` param in `risk_service.check()`.
- New `ml_setups` columns: `risk_approved` (bool), `risk_reject_reason` (text). Risk filtering is now a separate dimension from market outcome — both available for ML.
- Shadow risk rejections logged as `outcome_type = shadow_risk_rejected`.
- `SHADOW_CAPITAL` fallback in `shadow_monitor.py` removed — if risk service unavailable, shadow tracking is skipped (bad data worse than no data).
- `ML_FEATURE_VERSION` bumped to 7 (sizing model changed, new columns).

**Why:** Shadow mode needs to mirror the live pipeline exactly for forward-test validity. Standalone sizing produced different position sizes than live, making shadow PnL incomparable.

### 2026-03-25 — Execution Bug Fixes (SL labeling, orphaned PnL, DB cleanup)
**What changed:**
- **SL exit reason labeling** (`monitor.py`): 3 code paths hardcoded `"sl"` for all stop-loss exits, ignoring `breakeven_hit` and `trailing_sl_moved` flags. Now correctly labels `breakeven_sl` and `trailing_sl`. Affects ML outcome mapping (`filled_sl` vs `filled_trailing`).
- **Orphaned restart PnL** (`service.py`): reconciliation hardcoded `pnl_usd=0.0` without querying exchange. Now estimates PnL from SL price (worst case) and sets `actual_exit`. `fetch_open_trades()` query expanded to include `sl_price`, `actual_entry`, `position_size`.
- **DB cleanup**: 13 trades with `closed_at < opened_at` (backfill corruption from 03-20) corrected. 7 orphaned trades backfilled with SL-estimated PnL. 3 exit reasons relabeled (#18, #19 → `breakeven_sl`, #23 → `trailing_sl`).

**Corrected performance (40 trades):** Total PnL = -$16.32. Setup H: 28 trades, -$15.96 (disabled 03-19). Setup F: +$0.35 (only profitable setup).

### 2026-03-25 — Shadow Mode (Paper Trading for Data Collection)
**What changed:**
- New `SHADOW_MODE_SETUPS` config: setups in this list are detected and ML-logged but NOT executed. A `ShadowMonitor` tracks theoretical outcomes (TP/SL/timeout) from price action.
- Default: only `setup_f` executes live. Shadow mode setups: `setup_a`, `setup_b`, `setup_c`, `setup_d_choch`, `setup_d_bos`, `setup_e`, `setup_g`. Setup H removed from shadow (03-30) pending pullback redesign.
- `SHADOW_CAPITAL = $500` — fictional capital for realistic position sizing in shadow trades.
- Shadow outcomes feed `ml_setups` with the same 40+ features. Additional columns: `shadow_mode`, `shadow_position_size`, `shadow_leverage`, `shadow_margin`, `shadow_spread_at_detection`, `shadow_depth_at_entry`, `shadow_fill_time_ms`, `shadow_fill_candle_volume_ratio`, `shadow_slippage_estimate_pct`.
- Orderbook snapshot (spread + depth ±0.1%) captured at detection via `fetch_orderbook_snapshot()`.
- New files: `execution_service/shadow_monitor.py`, public `DataService.get_orderbook_snapshot()`.

**Why:** López de Prado forward-testing approach — collect labeled outcomes for all setup types without risking capital on unvalidated signals. When 200-300 detections per setup type accumulate (est. 2-4 weeks), run feature importance analysis (random forest) to identify which features predict outcome and which are noise.

**Caveats:** Shadow mode assumes perfect fill at theoretical entry (no slippage, no partial fills). Tracked mitigation: orderbook depth, fill candle volume ratio, and slippage estimate are recorded to discount unrealistic fills. Position sizing uses %-risk model (not FIXED_TRADE_MARGIN) — shadow PnL will differ from live PnL at the same capital.

### 2026-03-25 — OB Impulse Score + Retest Counter
**What changed:**
- `OrderBlock` enriched with `impulse_score` (0-1) and `retest_count` (int)
- Impulse score: measures post-OB displacement strength (price move + volume intensity of impulse candles). Computed at detection time.
- Retest count: tracks how many candles wick into OB zone without mitigating. Updated on every `update()` call. First-touch OBs are stronger.
- OB scoring weights redistributed: impulse 25%, volume 20%, freshness 20%, proximity 15%, retest penalty 10%, size 10% (was: vol 35%, fresh 30%, prox 20%, size 15%)
- `_check_volume_confirmation()` adds `ob_impulse_strong` (≥0.6) or `ob_impulse_moderate` (≥0.35) as confluence
- New settings: `OB_SCORE_IMPULSE_W`, `OB_SCORE_RETEST_W`, `OB_MAX_RETESTS`
- Applies to ALL setups that use OBs: A, B, D, F, G (via `_score_ob` and `_check_volume_confirmation`)

**Why:** OB scoring had no measure of institutional conviction (strong displacement vs weak drift) or zone absorption (retested OBs weaker). Review identified these as highest-impact improvements for trade quality.

**Expected impact:** Better OB selection — favors OBs with strong institutional displacement, penalizes multi-touch zones. May slightly change which OB is "best" in scenarios with multiple candidates.

### 2026-03-20 — Fix PnL $0 bug + ml_setups INSERT fix
**What changed:**
- `_persist_trade_close` used `pos.pnl_usd` directly instead of re-deriving from `pnl_pct` (formula broke after 3be110a changed denominator from entry_notional to capital)
- `ManagedPosition.pnl_usd` field added, set by `_calculate_pnl`
- `ml_setups` INSERT had 65 columns but 63 `%s` placeholders — silent failure on every insert. Fixed.
- Backfilled PnL for 21 historical trades. Total realized: -$10.31 across 39 trades.
- Telegram close notifications now show USD PnL.

**Why:** All trades since 3be110a showed `pnl_usd=0` in DB. Drawdown tracker worked (reads from capital %) but trade history was wrong. ml_setups inserts were silently failing.

**Expected impact:** Correct PnL in trades table and Telegram notifications. ml_setups inserts will succeed.

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
