# SYSTEM BASELINE — Quant Fund Trading Bot

> Source of truth for system state. Updated on every material change.
> Reflects code reality — if code and doc disagree, fix the doc.
> Documentation rule: this file is the operational source of truth. `README.md` is a portfolio overview; `docs/context/*` explains concepts and history and may intentionally lag unless this baseline links to it.

**Last updated:** 2026-05-06
**ML Feature Version:** 18
**Bot status:** SHADOW-ONLY (OKX_SANDBOX=false, ENABLED_SETUPS=[], ~$86 capital untouched)
**Active experiment:** `engine1_eth_short_v1b_2026_05_04` (settings.py default since commit `7ccf2bc` 2026-05-04). Engine 1 v1b isolates ETH short only after v1 diagnostics: BTC and ETH long negative, ETH short only positive slice; benchmarks mirror ETH scope.

> **Data tag reality (2026-05-06):** All 1510 engine1 ml_setups rows in DB are tagged `redesign_pre_2026_04_27` (legacy env override active during the v1 collection window). Zero rows under `engine1_eth_short_v1b_2026_05_04` because engine1 stopped emitting on 2026-05-05 14:10 UTC when HTF flipped to long (engine1 is short-only via `SHADOW_DIRECTION_FILTER`). `scripts/report_engine1_shadow.py` reads the new ID and therefore reports 0 — querying under the legacy ID is the only way to see existing data until HTF flips back.
**Monitoring:** Grafana dashboard `shadow-health` + systemd user timer `shadow-health-alert.timer` (hourly)

---

## 1. Active Configuration

### Pairs & Timeframes
| Setting | Value |
|---------|-------|
| TRADING_PAIRS | ETH, BTC, SOL, DOGE, XRP, LINK, AVAX (/USDT) |
| HTF_TIMEFRAMES | 4h, 1h |
| LTF_TIMEFRAMES | 15m, 5m |
| SWING_SETUP_TIMEFRAMES | 15m |

### Setup Status
| Setup | Status | Type | Historical WR |
|-------|--------|------|---------------|
| A (Sweep+CHoCH+OB) | **SHADOW (short only)** | swing, long disabled (5% WR 1/20) | short 33%, long 5% |
| B (BOS+FVG+OB) | **SHADOW** | swing, max entry dist 2% (was 3%) | 0-7.7% |
| C (Funding Squeeze) | **DISABLED** | signal folded into confluence | 0 resolved |
| D_choch (LTF CHoCH) | **SHADOW (BTC+ETH only)** | quick, quarantined 2026-04-27 (redesign §3.4) | 75% backtest |
| D_bos (LTF BOS) | **SHADOW (BTC+ETH only)** | quick, quarantined 2026-04-27 (redesign §3.5) | 50% (2/4 shadow) |
| E (Cascade Reversal) | **DISABLED** | signal folded into confluence | 0W/1L |
| F (Pure OB Retest) | **SHADOW** | swing, was live until 04-15 | 50% (1TP/1SL live) |
| G (Breaker Block) | **DISABLED** | 0/4 WR. Removed 04-16. | 0% |
| H (Momentum/Impulse) | **DISABLED** | — | 10.7% WR (28 trades). Removed 04-13. |
| Engine 1 (Trend-Pullback / Impulse Retest) | **SHADOW (ETH short only) — frozen since 2026-05-05 14:10** | v1b isolated 2026-05-04; pre-emission scope filter prevents out-of-scope benchmark orphans; benchmarks mirror ETH scope. HTF on ETH flipped long → no qualifying short impulses; engine still scans (logs show `impulse dir short != HTF long` rejections) | v1: ETH short only positive slice; BTC + ETH long quarantined |

### Risk Guardrails
| Parameter | Value | Notes |
|-----------|-------|-------|
| RISK_PER_TRADE | 1% | Dynamic sizing via PositionSizer (was: flat $20 margin) |
| MAX_LEVERAGE | 10x | Cap on PositionSizer output (raised 2026-04-29 — see changelog) |
| MAX_OPEN_POSITIONS | 8 | |
| MAX_TRADES_PER_DAY | 20 | |
| MAX_DAILY_DRAWDOWN | 10% | was 5%, raised for $20/$108 capital ratio |
| MAX_WEEKLY_DRAWDOWN | 10% | |
| COOLDOWN_MINUTES | 5 | after loss |
| MIN_RISK_REWARD | 2.0 | swing setups (was 1.2) |
| MIN_RISK_REWARD_QUICK | 1.5 | quick setups (was 1.0) |
| MIN_RISK_DISTANCE_PCT | 0.5% | SL-too-close filter |
| ATR_SL_FLOOR_MULTIPLIER | 4.5 | SL widened to 4.5× ATR(14) if structural SL is tighter |
| MAX_SL_PCT | 4% | SL-too-far cap — rejects setups with OB SL > 4% |
| REGIME_EXTREME_FEAR_GATE | 10 | F&G < 10 → reject ALL live setups (systemic crisis only) |
| ~~SHADOW_FEAR_LONG_GATE~~ | removed | F&G kept as ML feature, not used as gate |
| ~~SHADOW_MIN_HOUR_UTC~~ | removed | Hour captured as ML feature (created_at), not used as gate |
| SHADOW_DEDUP_TTL | 5 min | Pipeline dedup for shadow (live remains 1h) |
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
| SETUP_A_ENTRY_PCT | 50% | deepened from 65% (04-02): shadow 9% WR, SL within noise |
| SETUP_A_MODE | continuation | changed from "both" (04-02): 17/17 SL on counter-trend |
| SETUP_A_MAX_SWEEP_CHOCH_GAP | 45 | Optuna-validated; synced from 60 on 2026-04-27 (doc-truth pre-work, redesign §3.1) |
| SETUP_A_MAX_ENTRY_DISTANCE_PCT | 5% | added 04-15: consistency with B/F |
| FUNDING_MILD_THRESHOLD | 0.0001 | 0.01% — mild directional crowding |
| FUNDING_MODERATE_THRESHOLD | 0.0003 | 0.03% — was EXTREME, now moderate |
| FUNDING_EXTREME_THRESHOLD | 0.0006 | 0.06% — extreme crowding, high reversal risk |
| PD_AS_CONFLUENCE | true | aggressive mode |
| PD_OVERRIDE_MIN_CONFLUENCES | 5 | |

### Setup-Specific Parameters
| Parameter | Value | Setup |
|-----------|-------|-------|
| SETUP_F_MAX_BOS_AGE_CANDLES | 60 | F |
| SETUP_F_MIN_BOS_DISPLACEMENT_PCT | 0.1% | F |
| SETUP_F_MAX_OB_BOS_GAP_CANDLES | 20 | F |
| SETUP_F_MIN_OB_SCORE | 0.35 | F |
| SETUP_F_MAX_ENTRY_DISTANCE_PCT | 2.5% | F |
| SETUP_F_MIN_CONFLUENCES | 2 | F |
| SETUP_B_MAX_BOS_AGE_CANDLES | 12 | B |
| SETUP_B_MAX_ENTRY_DISTANCE_PCT | 3% | B |
| ~~SETUP_H_*~~ | removed | H tombstoned 04-13 (0/13 WR). Values in code comments only |
| SETUP_D_ENTRY_PCT | 85% | D |
| QUICK_OB_MAX_DISTANCE_PCT | 1.5% | quick |
| QUICK_SETUP_COOLDOWN | 1h | quick |

### TP/SL Configuration
| Parameter | Value |
|-----------|-------|
| TP1_RR_RATIO | 1.0 (breakeven trigger) |
| SETUP_TP2_RR | A/B/F/G=2.0, D=1.5 (C/E/H removed) |
| TRAILING_TP_ENABLED | false |
| MAX_TRADE_DURATION | 12h swing / 4h quick |
| ENTRY_TIMEOUT | 24h swing / 1h quick |

---

## 2. Gating Logic (Pipeline Order)

```
Candle confirmed → StrategyService.evaluate()
  ├── HTF bias undefined? → BLOCK (all setups)
  ├── LTF direction != HTF bias? → ALLOWED (REQUIRE_HTF_LTF_ALIGNMENT=False since 04-13)
  ├── Swing setups (15m only): A → B → F → G
  │     Each: detect pattern → PD check → OB selection → volume confirmation
  │     → structural confluence ≥ 2 (metrics don't count)
  │     Post-detection: ATR SL floor (widen to 4.5× ATR if tight) → ATR filter → target space filter
  ├── Quick setup candidates (5m): D only (C/E removed 04-13)
  └── TradeSetup produced
        ├── ENABLED_SETUPS / SHADOW_MODE_SETUPS check
        ├── Data integrity gate (DEGRADED blocks all; RECOVERING allows candle-only setups)
        ├── **Shadow path** (setup in SHADOW_MODE_SETUPS):
        │     ├── Dedup cache (5min TTL — short, for data collection)
        │     ├── Risk check → logged as ML feature, NOT a gate (tracks anyway)
        │     ├── Shadow monitor dedup (only blocks unfilled + same entry ±1%)
        │     └── Fallback sizing if risk rejects (5% of SHADOW_CAPITAL)
        ├── **Live path** (setup_f):
        │     ├── Regime gate (F&G < 10 → BLOCK)
        │     ├── Dedup cache (1h TTL)
        │     ├── AI filter → BYPASSED (synthetic approval)
        │     ├── Risk Service → guardrails, position sizing
        │     └── Execution Service → limit order + SL + TP
```

### Key Signal Hierarchy (audit 03-18)
| Signal | Role | Assessment |
|--------|------|------------|
| HTF bias (4H/1H) | **Hard gate** | Blocks all if undefined (~60% of time in range) |
| Sweep (Setup A only) | **Core trigger** | Strongest microstructure signal |
| CHoCH / BOS | **Core trigger** | Required for all setups |
| Order Block | **Core trigger** | Required for live swing setup and most shadow-tracked setups |
| CVD (divergence + MTF) | Confluence | Upgraded: price vs CVD direction, 3-TF agreement |
| OI delta | Confluence | Upgraded: tracks delta between evaluations |
| Funding rate | Confluence | 3-tier graduated: mild 0.01% / moderate 0.03% / extreme 0.06% |
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
| ~~H5~~ | ~~HTF undefined blocks too many setups in range markets~~ | Already implemented: `HTF_BIAS_REQUIRE_4H=False` (settings.py:402). Hypothesis closed 2026-04-27. | — |
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

**Current version:** 18 (set in `config/settings.py:ML_FEATURE_VERSION`)
**Storage:** `ml_setups.feature_version` column in PostgreSQL
**Query training data:** `SELECT * FROM ml_setups WHERE feature_version >= 4 AND outcome_type IS NOT NULL AND outcome_type NOT IN ('ai_rejected','data_blocked','filled_orphaned','replaced','risk_rejected','shadow_dedup','shadow_direction_filtered','shadow_pair_filtered','shadow_orphaned','trading_halted','unfilled_timeout')`

Whitelist autoritativa de `outcome_type` en `data_service.data_store.VALID_OUTCOMES`. Labels fuera del set generan WARNING. El filtro non-market se centraliza en `NON_MARKET_OUTCOMES` + helper `ml_market_outcome_filter_sql()` (mismo módulo) — usarlo en scripts/queries nuevas para evitar drift.
**Experiment tracking:** `experiment_id` column (migration 15). settings.py default: `engine1_eth_short_v1b_2026_05_04` (active for emissions since commit `7ccf2bc` 2026-05-04). Legacy data still under `redesign_pre_2026_04_27` (env override that was active during v1 collection window — all 1510 engine1 rows + 109 scalp rows). When querying engine1 historically, filter on the legacy ID; when querying scalp, see Side experiment §9 — note `SCALP_EXPERIMENT_ID` is currently a reporting-only field (does not tag inserts).

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
| v9 | 04-02+ | geometry cascade (dynamic entry/SL from OB wick + ATR floor candidates), ATR SL absorbed into cascade | **TRAINING READY** |
| v10 | 04-09+ | volume profile (POC/VAH/VAL/HVN), structural TPs, 1H/4H OBs for swing setups, VP OB quality | **TRAINING READY** |
| v12 | 04-13+ | C/E/H removed, OI cascade confluence booster, sweep touch_count, CHoCH displacement filter | **TRAINING READY** |
| v13 | 04-14+ | RSI(14) + RSI zone + RSI divergence, avg_body_ratio (candle decisiveness) | **TRAINING READY** |
| v14 | 04-14+ | Orderbook spread/imbalance, BTC correlation (return + vol ratio), volatility regime, trading session | **TRAINING READY** |
| v15 | 04-16+ | WaveTrend (Cipher B core): wt_wt1/wt_wt2 oscillator, wt_cross (bull/bear), wt_zone (oversold/overbought/neutral), wt_aligned (cross matches setup direction in extreme zone) | **TRAINING READY** |
| v16 | 04-16+ | ADX(14) + DI+/DI- (trend strength + direction), Bollinger(20,2) width/%B/squeeze percentile, Stochastic RSI(14,14,3,3) %K/%D/zone/cross | **TRAINING READY** |
| v17 | 04-23+ | `pd_aligned` strict (equilibrium no longer counts as aligned for either side); VALID_OUTCOMES whitelist + `filled_slippage` outcome; setup_d normalization | **TRAINING READY** |
| v18 | 04-27+ | `regime_label` categorical (trend_strong/weak/range/compression/breakout/hostile) from ADX+BBW+ATR+spread+btc-return+F&G; `funding_tier`/`oi_rising_tier` derived from raw signal magnitude (decoupled from direction-gated confluence strings — fixes W17 100% null) | **TRAINING READY** |

**When to bump:** Increment `ML_FEATURE_VERSION` whenever strategy params change in ways that alter feature semantics (OB scoring weights, PD rules, confluence logic, threshold changes).

**Minimum for Phase 1 (feature importance):** 50+ labeled outcomes with `feature_version >= 4` (filled_tp + filled_sl + filled_trailing).

### 7.0 Dataset Ground Truth (where to train from)

Three storages hold trade-like rows. Only ONE is authoritative for ML training / edge analysis.

| Table | Origin | Role | Use for ML? |
|-------|--------|------|-------------|
| `ml_setups` | Bot detector (strategy_service) | **Authoritative** — features at detection + triple-barrier outcome. One row per setup, shadow or live. | **YES** — always filter by `feature_version >= 4` + `NOT IN NON_MARKET_OUTCOMES`. |
| `trades` | Bot executor (execution_service) | Operational — real live fills, capital_at_trade, exit_reason. | NO for ML training. YES for P&L / dashboard. Filter `orphaned_restart` for DD / stats. |
| `bybit_executions` + `bybit_closed_pnl` + `bybit_trade_annotations` | Bybit manual trades (sync + watcher) | Journal of manual decisions. Separate venue, separate capital, different execution characteristics. | **NO** — never cross with `ml_setups`. The annotation thesis is user-written prose, not ML-graded. |

**Rules:**
- `ml_setups` is the *only* ground truth for training queries, feature-importance runs, meta-label experiments, edge-audits.
- `trades` is appropriate for realized P&L, DD reconcile, dashboard recent-trades — but NOT for feature → outcome modeling (lacks features).
- Manual Bybit trades live in their own schema and must not leak into bot-edge analysis. Cross-venue comparison is fine for journaling, never for training.
- If an analysis script needs both features and realized cash, join `ml_setups` → `trades` on `setup_id`, but still filter training labels from `ml_setups.outcome_type`.

### 7.1 ML Activation Gate

**Current state (2026-04-23):** Pipeline is rule-based SMC + ML **logger** (not ML-driven). AI filter is bypassed for all active setups (`AI_BYPASS_SETUP_TYPES`). `BET_SIZING_ENABLED` effectively inert because synthetic `AIDecision(confidence=1.0)` never triggers it. Do not market this as an AFML/ML system in its current form — it is a feature collector.

**Reactivation is gated on ALL of:**

| # | Threshold | Verification |
|---|-----------|--------------|
| G1 | ≥ 500 resolved labeled outcomes | `SELECT COUNT(*) FROM ml_setups WHERE feature_version >= 4 AND outcome_type IN ('filled_tp','filled_sl','filled_trailing','filled_timeout','filled_guardian') AND experiment_id = <current>` |
| G2 | Class balance within 60/40 | WR between 40–60% on the slice above. Extreme skew → meta-label target is degenerate. |
| G3 | Meta-label classifier trained with **purged k-fold CV** (AFML Ch.7) | Purge window ≥ max holding period; embargo ≥ 1× bar length. No leakage of overlapping labels. |
| G4 | Out-of-sample **ROC AUC ≥ 0.60** and **Brier ≤ 0.22** | Calibrated with Platt/Isotonic. Uncalibrated probabilities cannot drive bet sizing. |
| G5 | Kelly-safe: expected `f*` from model probabilities > 0 on validation set | If mean Kelly < 0 on held-out fold, model has no edge — do not enable sizing. |
| G6 | Shadow comparison: model-gated setups beat rule-only baseline on ≥ 200 paper trades after calibration | `strategy_service` + shadow monitor can replay resolved setups through the classifier without touching live path. |

**Order of re-wiring after gate passes:**
1. Remove setup types from `AI_BYPASS_SETUP_TYPES`.
2. Route through `ai_service.evaluate()` using the calibrated classifier (not Claude).
3. Enable `BET_SIZING_ENABLED=true` **only** after G4 + G5 pass.
4. Keep Claude as an audit-only path in parallel (log both decisions, act on the classifier).

**Anti-patterns to avoid:**
- Training on pre-v4 data (corrupted semantics — CVD units, OI existence-only, asymmetric funding).
- Mixing `experiment_id` regimes without regime-aware CV folds.
- Using non-purged CV with overlapping triple-barrier labels (leakage inflates AUC by ≥ 0.10).
- Enabling bet sizing without calibration — Kelly on miscalibrated probabilities is strictly worse than flat size.

---

## 9. Active Roadmap (2026-04-20)

**Context:** 7d shadow audit — 79% breakeven rate, `setup_d_*` R:R hardcoded 1.5, 43 orphans/7d.

| Batch | Goal | Exit bar | Status |
|-------|------|----------|--------|
| 0 — Infra trust | Extract `shared/pnl_engine.py`, real-data tests (replay DB + sandbox OKX), shadow redis persistence, risk_capital consistency, resolve-candle trace (migration 17) | Outcomes match DB exact, 0 mocks in Tier 2/3 | **done 2026-04-20** (exact-replay test skipped until traced rows accumulate) |
| 1 — BE fix | Raise TP1_RR or require 2-candle confirm before SL→BE | Shadow BE rate <40% | **code shipped 2026-04-20** (TP1_RR=1.3). Awaiting 7d live shadow validation. |
| 2 — Backtest reinforce | Bootstrap CI + chronological stability split + regime split (DB-backed). Walk-forward optimization deferred (same-class failures caught by stability). Orderbook slippage + maker/taker fees deferred. | Backtest vs shadow WR within 5% — validate when Batch 1 data accumulates | **partial done 2026-04-20** (analytics + tests shipped; simulator refactor deferred) |
| 3 — Setup isolation | `SHADOW_MODE_SETUPS=["setup_f"]` only, 2w | WR≥45%, PF≥1.3, N≥50 | blocked by 1,2 |
| 4 — Quick setup TP | Port structural TP to `quick_setups.py` | setup_d avg R:R >1.5, PF >1.2 | **code shipped 2026-04-21**. Deploys with setup_d re-enablement (Batch 3 follow-up). |
| 5 — Add setup_b | Enable alongside setup_f, 2w | Same as Batch 3 | blocked by 4 |
| 6 — Test brutality | Rewrite 10 weakest tests, hypothesis property tests | Mock count <400 (from 781) | **done 2026-04-21** — 20 new property + real-data tests, mock count 401 (target essentially met). test_execution.py deferred (big scope). |
| 7 — Monitoring | Grafana BE rate + orphan + dedup panels, alerts | Rolling 7d BE alert functional | **done 2026-04-20** (dashboard + cron-ready alert script shipped) |
| 8 — Setup_a or remaining | Only if 3+5 healthy | Same bar | blocked by 4,6 |

**Principle:** each batch ships + passes bar before next starts. No parallel strategy work during infra phase.

### Side experiment — Scalp Shadow v1 (2026-05-04)

Independent shadow-only experiment for microstructural scalping signals, separate from the SMC roadmap above. Plan: `docs/plans/scalp_shadow_v1.md`.

- **experiment_id:** `scalp_v2_filtered_2026_05_05` (env-overridable via `SCALP_EXPERIMENT_ID`). Bumped from `scalp_v1_2026_05` after v1 review of `scalp_sweep_choch_v1` (76 outcomes, 5:1 SL:TP) wired the v2 fade-pattern filters described below. Old v1 rows stay queryable under the previous experiment_id.
- **Master switch:** `SCALP_SHADOW_ENABLED` (default `false`)
- **Timeframe:** `SCALP_TIMEFRAME` (default `5m`; bumps to `1m` once a fetcher commit lands)
- **Setup types:** `scalp_liq_reclaim_v1`, `scalp_sweep_choch_v1`, `scalp_vol_cvd_div_v1`, `scalp_funding_extreme_v1`, `scalp_random_baseline_v1` — all routed through `SHADOW_MODE_SETUPS`, zero live execution.
- **`scalp_sweep_choch_v1` v2 filters (added 2026-05-05):**
  - **ADX(14) gate:** ADX on `SCALP_TIMEFRAME` must be `>= SCALP_SWEEP_CHOCH_MIN_ADX` (default `18.0`). When the candle window is too short for ADX warmup the detector also blocks rather than emit blind. Sub-trend regimes dominated v1 SLs.
  - **Book imbalance gate (fade pattern):** when an orderbook snapshot is available, `book_imbalance_ratio = depth_bid_usd / depth_ask_usd`:
    - `long`  requires imbalance `< SCALP_SWEEP_CHOCH_BOOK_IMB_LONG_MAX` (default `3.0`).
    - `short` requires imbalance `> SCALP_SWEEP_CHOCH_BOOK_IMB_SHORT_MIN` (default `3.0`).
    - Missing or zero-depth orderbook → gate skipped (do not block on stale data).
  - Caller wiring: `StrategyService.evaluate_scalp` now fetches the cached orderbook before `evaluate_sweep_choch` (was: only before `evaluate_vol_cvd_divergence`) and bumps the candle pull from 30 to 50 to cover ADX warmup.
- **Per-signal params:** `settings.SCALP_SIGNAL_PARAMS` (TP%, SL%, time_stop_seconds). `ShadowMonitor` reads `time_stop_seconds` at `add_shadow` and resolves as `shadow_time_stop`.
- **Cross-signal dedup:** 30s window per pair (`SCALP_DEDUP_WINDOW_SECONDS`) inside `StrategyService.evaluate_scalp`.
- **Pipeline wiring:** `main.py` calls `evaluate_scalp` only when the SMC cascade returned `None`, gated by the master switch.
- **Validation rules (must all hold to graduate to live):** N >= 100, WR_post_fees > 50%, PF_post_fees > 1.5, beats `scalp_random_baseline_v1` WR by >= 15pp, freq >= 5/day. Fees adjustment uses `SCALP_ROUND_TRIP_FEE_PCT` (default 0.11%).
- **Report:** `python scripts/report_scalp_shadow.py [--since YYYY-MM-DD] [--pair BTC/USDT]`. Markdown table per signal with raw + post-fees metrics, baseline delta, decision rule output.
- **Exit:** >= 100 outcomes per signal OR 4 weeks elapsed. Final summary lands in `docs/audits/`.

---

## 8. Changelog

### 2026-05-06 — Shadow capital basis: real-capital backup for shadow sizing
**Files:** `config/settings.py`, `execution_service/shadow_monitor.py`, `main.py`, `tests/test_shadow_monitor_sizing.py`

**What changed:**
- New setting `SHADOW_CAPITAL_BASIS` env (`"fictional"` default | `"real"`). When `"real"`, shadow sizing uses `SHADOW_REAL_CAPITAL_USD` (default $108, mirrors current OKX balance) instead of the historical `SHADOW_CAPITAL` ($500 fiction).
- New helper `settings.effective_shadow_capital` returns the active basis value. All shadow callsites (`shadow_monitor.add_shadow` fallback, `ShadowPosition.target_risk_usd`, `_ml_log_setup` capital_override, `_process_pipeline_setup` risk dry-run, pair-diagnostic boot log, shadow-monitor init log) now read through this helper.
- `__post_init__` validates basis ∈ {fictional, real} and `SHADOW_REAL_CAPITAL_USD > 0`. Boot log emits both `SHADOW_CAPITAL_BASIS` and the resolved value.
- 4 new tests in `TestShadowCapitalBasis` lock the toggle behavior.

**Why:** Previous shadow PnL was projected against $500 fictional capital while the live OKX account has ~$108. Position notionals are 4.6× larger in shadow than they would be in reality, which makes shadow PnL non-comparable with live execution. With the new toggle, an operator can flip basis to `"real"` to project what each signal would have earned/lost given the actual capital constraint — materially changing the kill/keep call for tight-SL signals (scalp 0.15% SL: real notional $1,080 → fee-adjusted R:R degrades from clean 2:1 to ~0.8:1 net).

**How to use:**
- Default behavior unchanged — opt-in only.
- Enable: `SHADOW_CAPITAL_BASIS=real SHADOW_REAL_CAPITAL_USD=108` in `.env`.
- **Always bump experiment_id when flipping** — sizing change alters PnL distributions. Examples: `SCALP_EXPERIMENT_ID=scalp_v3_real_2026_05_06`, `EXPERIMENT_ID=engine1_real_capital_2026_05_06`. Otherwise post-flip rows mix with pre-flip rows under the same tag, contaminating analysis.
- `risk_capital` column on `ml_setups` already snapshots the effective capital at insert time, so historical queries can group/filter by it.

**Operator decision deferred:** flipping basis changes the meaning of every shadow PnL going forward. Current dataset is already noisy from prior contamination (sizing-fix mid-experiment, experiment_id misrouting). Recommend running both basis modes in parallel for at least one signal cycle before fully cutting over — flip first on a fresh `EXPERIMENT_ID` and let it accumulate N≥30 before comparing real vs fictional.

### 2026-05-06 — Fix `SCALP_EXPERIMENT_ID` wiring + bump to `scalp_v3_clean_2026_05_06`
**Files:** `main.py`, `config/settings.py`

**What changed:**
- `_ml_log_setup` now branches on `setup.setup_type in SCALP_SETUP_TYPES` and tags scalp inserts with `SCALP_EXPERIMENT_ID` (default `scalp_v3_clean_2026_05_06`). Non-scalp inserts continue using the global `EXPERIMENT_ID`.
- Boot log now prints both IDs + sources (env override vs settings default).

**Why:** `SCALP_EXPERIMENT_ID` was defined in `config/settings.py` and read by `scripts/report_scalp_shadow.py`, but `_ml_log_setup` always wrote `settings.EXPERIMENT_ID`. Result: every scalp v1 + v2 row in `ml_setups` is tagged with whichever global experiment was active at insertion time (`redesign_pre_2026_04_27` for early v1 data, `engine1_eth_short_v1b_2026_05_04` after the 2026-05-04 flip). Reports under the new ID returned zero. v2 fade-pattern filter changes (PR #14) silently mixed with engine1 ID instead of isolating under their own.

**Impact:**
- All future scalp inserts go under `scalp_v3_clean_2026_05_06`. Old v1/v2 data **not migrated** — stays under the engine1/legacy IDs and is queryable via explicit `experiment_id` predicate. Migration would require parsing setup_type and rewriting rows; not worth it for shadow-only data.
- Fresh dataset starts at zero. Need ~2-4 weeks for `scalp_sweep_choch_v1` to accumulate N≥30 under v3 before any kill/keep decision. `liq_reclaim` and `funding_extreme` (calibrated 2026-05-05) still on slow timeline.

**Tests:** existing scalp tests (`tests/test_scalp_setups.py`, `tests/test_report_scalp_shadow.py`) still pass — 80 cases. No new test for the wiring branch because it's a one-line conditional in `_ml_log_setup`; hitting it requires DB integration test infrastructure that doesn't exist for that callsite yet.

**Operator note:** Old data still queryable. Examples:
- v1 sweep_choch under legacy: `experiment_id='redesign_pre_2026_04_27' AND setup_type='scalp_sweep_choch_v1'` (63 rows)
- v2 sweep_choch (filters added but tagged with engine1 ID): `experiment_id='engine1_eth_short_v1b_2026_05_04' AND setup_type='scalp_sweep_choch_v1'` (19 rows)
- v3 clean (fresh): `experiment_id='scalp_v3_clean_2026_05_06'`

### 2026-05-06 — Engine 1 status snapshot (docs-only sync, no code change)
**Files:** `docs/SYSTEM_BASELINE.md`, memory `project_engine1_shadow.md`

**Reality check** at end of v1b experiment window:
- Total engine1 resolved outcomes in DB: **115** (60 short + 55 long), all under legacy `experiment_id=redesign_pre_2026_04_27`. Memory snapshot from 2026-04-30 said 51 — outdated.
- Era split (era boundary = sizing fix on 2026-05-05):
  - **pre-2026-05-05** (sizing $250 fixed + cluster bias pre-dedup): 98 resolved. Long 38 → 9 TP / 4 SL / 25 BE, net **−$14.00**. Short 60 → 16 TP / 14 SL / 30 BE, net **−$3.53**.
  - **2026-05-05 (sizing-fix day)**: 17 long resolved → 5 TP / 2 SL / 10 BE, net **+$1.33**. Only era with risk-based sizing AND cluster dedup partially in effect, N too small.
  - **post-2026-05-06 (dedup-fix merged)**: **0 resolved**. Engine1 has not emitted since 2026-05-05 14:10 — direction filter `["short"]` × HTF flipped long.
- Useful WR (TP/(TP+SL)) by direction across all eras: short 53.3% (16/30), long 70% (14/20). Long high WR but losing money — TPs ($3 avg) smaller than SLs ($10 avg). Geometry asymmetry, not direction edge.

**Why this matters:**
- Memory said checkpoint was 75 outcomes; we're at 115 but the data is **dirty** (sizing changed mid-experiment, cluster bias inflates pre-dedup data). Effective clean N ≈ 17.
- The "v1b isolation" decision (commit `7ccf2bc`) tagged settings default but didn't bump explicit env, so data continued under legacy ID. Reports under new ID return zero.
- Re-run `scripts/engine1_fillrate_study.py` is BLOCKED until either (a) HTF flips back to short and N≥30 fresh outcomes accumulate under `engine1_eth_short_v1b_2026_05_04`, or (b) we relax the direction filter for ETH long during this regime.

**No config change in this entry.** Decision deferred — see `Open Problems` and memory `project_engine1_shadow.md`.

### 2026-05-06 — Engine 1 cluster dedup: suppress repeated emissions on same impulse
**Files:** `strategy_service/service.py`, `strategy_service/engines/trend_pullback.py`, `tests/test_engine_trend_pullback.py`, `tests/test_strategy_integration.py`

**What changed:**
- `TrendPullbackEngine.evaluate` now writes `engine1_impulse_origin_ts` (timestamp of the impulse's first candle) into `TradeSetup.extra_features`.
- `StrategyService._engine1_is_cluster_duplicate(setup)` is the new dedup helper. It tracks `(pair, direction) -> last impulse_origin_ts` per service instance and returns True when a fresh setup repeats the cached impulse. Hit at the engine1 callsite in `_evaluate_for_state` so duplicate emissions never reach `on_match`.

**Why:** Maker fill-rate audit (`docs/audits/engine1-maker-fillrate-2026-05-05.md` §3.4) showed a single 2026-04-29 ETH impulse produced 5 detections in 50 minutes, all resolving as identical-priced `shadow_tp`. Inflated headline N (37 raw → effective ~10–15 events) and concentrated edge in single market events. The previous shadow_monitor dedup released once a position filled, allowing the engine to re-emit on every confirmed 5m bar over the same impulse-pullback cycle.

**Expected impact:** engine1 emission volume drops on impulse re-detections (no more 5 fires/50min on one impulse). Per-impulse-cycle effective sample N should now match raw N. Re-running `scripts/engine1_fillrate_study.py` post-merge will give a cleaner edge measurement; the 2 winners that flipped sign in the 3bps margin scenario should disappear or stay as a single contribution rather than 5.

**Tests:** 6 new `TestEngine1ClusterDedup` cases (first-emit caches, repeat suppresses, new impulse re-arms, per-direction and per-pair scoping, missing-field fallback) + 1 engine-level test verifying `engine1_impulse_origin_ts` matches the impulse start candle. 288 strategy/engine/scalp tests pass.

**Operator note:** No experiment_id bump — this is a behavior fix, not a parameter regime change. Old engine1 ml_setups rows under `redesign_pre_2026_04_27` retain their cluster-duplicated outcomes; new rows are clean from this commit forward.

### 2026-05-05 — Scalp silent detectors: calibrate `liq_reclaim` and `funding_extreme` thresholds
**Files:** `strategy_service/scalp_setups.py`, `tests/test_scalp_setups.py`, `scripts/scalp_silent_detector_audit.py`, `docs/audits/scalp-silent-detectors-2026-05-05.md`

**What changed:**
- `_LIQ_RECLAIM_WICK_THRESHOLD`: `0.005` → `0.003` (0.5% → 0.3%).
- `_LIQ_RECLAIM_FLUSH_MAX_AGE_MS`: `5 * 60 * 1000` → `10 * 60 * 1000` (5min → 10min).
- `_FUNDING_RATE_THRESHOLD`: `0.0005` → `0.0002` (0.05% → 0.02%).

**Why:** Phase 1A (PR #15) reported both signals at zero outcomes ever. Audit script (`scripts/scalp_silent_detector_audit.py`) confirmed:
- `funding_extreme` threshold of 0.05% was 5× higher than the 30-day max funding rate observed across all 7 pairs (max abs |rate| = 0.0427% on AVAX). Mathematically impossible to fire.
- `liq_reclaim` gates aligned only 2/72 (2.8%) historical OI flushes. Root cause: 0.5% wick threshold is large for 5m candles even after 2% OI flushes; OI poll cadence (5min) sometimes misaligns with the 5m candle close that completes the wick-reclaim pattern. Relaxed to 0.3% wick + 10min window → 11/72 (15.3%) historical alignment.

**Expected impact:** post-calibration projected fires per 30 days — `liq_reclaim` ~5–10, `funding_extreme` ~1–3. Both still slow; will need 1–6 months to accumulate N≥30. If a signal still produces zero after 30 days, deeper redesign required (different thesis, different threshold, or kill).

**Tests:** 52 scalp tests pass. Two threshold assertions updated; one no-lookahead test had its appended-candle wicks shrunk to stay sub-threshold under the new value.

**Operator note:** `SCALP_EXPERIMENT_ID` not bumped here — PR #14 (`feat/scalp-v2-fade-pattern-filters`) already bumps it to `scalp_v2_filtered_2026_05_05`. Old data was empty for these signals so no contamination risk from sharing the v2 id once both PRs land.

### 2026-05-05 — `scalp_sweep_choch_v1` v2 fade-pattern filters
**Files:** `strategy_service/scalp_setups.py`, `strategy_service/service.py`, `config/settings.py`, `tests/test_scalp_setups.py`, `docs/context/02-strategy.md`

**What changed:**
- `evaluate_sweep_choch` now applies two filters before emitting:
  1. **ADX(14) trend gate** — rejects setups when `ADX < SCALP_SWEEP_CHOCH_MIN_ADX` (default `18.0`) on the scalp timeframe, and when ADX cannot be computed at all.
  2. **Orderbook imbalance fade gate** — when an orderbook snapshot is available, longs require `book_imbalance < SCALP_SWEEP_CHOCH_BOOK_IMB_LONG_MAX` and shorts require `book_imbalance > SCALP_SWEEP_CHOCH_BOOK_IMB_SHORT_MIN` (both default `3.0`). Missing orderbook → gate skipped.
- `StrategyService.evaluate_scalp` fetches the cached orderbook before `evaluate_sweep_choch` (was: only before `evaluate_vol_cvd_divergence`) and pulls 50 scalp candles instead of 30 to cover ADX warmup.
- `SCALP_EXPERIMENT_ID` bumped from `scalp_v1_2026_05` to `scalp_v2_filtered_2026_05_05` so v1 vs v2 outcomes stay separable in `ml_setups`. The scalp report script reads the live setting and now filters automatically.
- Confluence list adds `adx_14=<value>` for traceability.

**Why:** v1 of `scalp_sweep_choch_v1` produced 76 resolved outcomes with `30 SL / 6 TP / 8 BE / 23 timeout` (5:1 SL:TP, worse than the random baseline). Feature analysis showed:
- LONG SL avg `book_imbalance` 16.0 vs LONG TP avg 1.2 → stacked bids correlated with losses (institutional absorption / spoofing pattern, not real support).
- SHORT TP avg `book_imbalance` 11.6 vs SHORT SL avg 4.5 → high bid stack + sweep = shorts that worked. Inverts the naive book-imbalance read.
- Regime breakdown: range/compression/hostile dominated SL outcomes; only `trend_strong` had BE > SL.
- Median time-to-SL `<1.1 min` for longs, `<0.6 min` for shorts → 0.15% SL stopped on noise inside sub-trend regimes.

**Expected impact:** lower emission rate (sub-trend candles + balanced books no longer fire). Hypothesis: WR moves from ~16% (TP / TP+SL+BE+timeout) toward parity with or above random baseline; minimum bar before any decision is `N≥30` resolved outcomes under the new experiment_id.

**Tests:** `tests/test_scalp_setups.py::TestSweepChochV2Filters` — 9 cases covering ADX unavailable, ADX below min, default-warmup pass-through, long blocked by stacked bids, long passes balanced book, short blocked by balanced book, short passes stacked bids, zero-depth orderbook fall-through, ADX value emitted in confluences. Existing 12 sweep_choch tests updated for new helper signature.

**Operator note:** `python scripts/report_scalp_shadow.py` will report empty for the new experiment_id until shadow outcomes accumulate. Old v1 data stays queryable by overriding `SCALP_EXPERIMENT_ID=scalp_v1_2026_05`.

### 2026-05-05 — Shadow fallback sizing: replace fixed-notional hack with risk-based formula
**Files:** `execution_service/shadow_monitor.py`, `tests/test_shadow_monitor_sizing.py`

**What changed:**
- **Shadow fallback sizing** (the path taken when `risk_service.check()` rejects, typically by `MIN_RISK_DISTANCE_PCT` for tight-SL setups like scalp) now uses the same formula as `risk_service.PositionSizer.calculate`:
  - `distance = abs(entry - sl)`
  - `risk_amount = SHADOW_CAPITAL × RISK_PER_TRADE` (= $5 with defaults)
  - `position_size = risk_amount / distance`
  - `notional = size × entry`; `leverage = notional / SHADOW_CAPITAL`, capped at `MAX_LEVERAGE`
- The old hack (commit `7bd8827`) used a fixed `$25 margin × MAX_LEVERAGE` (= `$250` notional) regardless of SL distance, producing SL/TP losses in cents that did not reflect any real risk per trade.
- **Live execution unaffected.** When `risk_service` rejects in live, it returns `position_size=0`, which `main._process_pipeline_setup` filters before `execute()` runs. The fallback path is shadow-only.
- New `RiskApproval` dataclass (frozen) replaces the previous `_FallbackApproval` ad-hoc class for type safety.

**Why:** under the old hack, a `0.40%` SL on a `$2,374` ETH setup produced `~$5` gross gain on TP and `~$1.50` gross loss on SL — neither reflected the intended `$5` risk-per-trade target. With this fix, gross SL = `$5` whenever the leverage cap does not bind. When the cap binds (very tight SLs, < `RISK_PER_TRADE / MAX_LEVERAGE` ≈ `0.10%`), the cap shrinks the position so realized loss is **below** target risk (safer direction, same as live `PositionSizer`).

**Operator note (post-deploy):** Shadow PnL collected before this date used the fixed-`$250` fallback. Reports comparing pre/post must filter by `created_at >= 2026-05-05` or by `experiment_id` switch. `shadow_position_size` and `shadow_margin` distributions in `ml_setups` will scale `5–10×` larger on tight-SL setups.

**Tests:** `tests/test_shadow_monitor_sizing.py` — 14 cases covering risk-amount contract, leverage cap, edge cases (`distance=0`, `entry<=0`), parametrized parity with `PositionSizer.calculate`.

### 2026-05-04 — Engine 1 v1b ETH-Short Isolation
**What changed:**
- `EXPERIMENT_ID` changed to `engine1_eth_short_v1b_2026_05_04`.
- `engine1_trend_pullback` shadow scope narrowed to `ETH/USDT` shorts only.
- Engine 1 benchmarks narrowed to `ETH/USDT` and are only co-emitted after the primary Engine 1 setup passes the same research scope.

**Why:** Engine 1 v1 reached enough outcomes to make a coarse decision before 100: BTC long/short and ETH long were negative, while ETH short was the only positive slice. The old sample also had repeated geometries and benchmark orphan/drift artifacts. v1b asks one clean question: does ETH-short Engine 1 survive with cleaner scope and 10x runtime sizing?

**Expected impact:** Lower Telegram volume, fewer repeated/out-of-scope benchmark rows, and a cleaner ETH-short sample. No TP, BE, trailing, entry geometry, SL geometry, or detector thresholds changed.

### 2026-05-04 — Shadow Sizing Clarity + Runtime Leverage Sync + Benchmark Telegram Silencing
**What changed:**
- Runtime `config/.env` `MAX_LEVERAGE` synced from 5x to the documented 10x policy.
- Shadow Telegram/log messages now distinguish risk target/effective risk, margin, notional, and leverage.
- `bench_engine1_*` setups now silence TRACKING + FILL Telegram alerts; only RESOLVE ships (`execution_service/shadow_monitor.py:_notify_detection`, `_notify_fill`).

**Why:** Shadow mode should size from `SHADOW_CAPITAL=$500` at `RISK_PER_TRADE=1%` (target risk `$5`) when leverage allows it. The previous Telegram wording showed only margin (for example `$25`), which made capped BTC shadows look like fixed-margin trades even when risk-based sizing was active. Each Engine 1 detection co-emits two benchmark setups, which previously triggered 9 alerts per detection (3 lifecycle × 3 setups) — too noisy without adding signal.

**Expected impact:** Tight-SL shadow setups can reach the intended `$5` risk more often at 10x. Telegram alert volume drops ~44% (9 → 5 per detection) without losing edge-comparison data — benchmark RESOLVE outcomes still ship and DB rows are unaffected. No entry, SL, TP, BE, trailing, detector, or live-promotion logic changed.

### 2026-04-29 — MAX_LEVERAGE 7x → 10x (policy change)
**What changed:**
- `MAX_LEVERAGE` raised from 7 to 10 in `config/settings.py`
- SYSTEM_BASELINE §1 risk guardrails table updated

**Why:** Position sizing was capital-bound on small SL distances at 7x — kept rejecting valid setups with risk_pct < 1%. Raising to 10x lets the PositionSizer hit the intended risk per trade for tight SL geometries.

**Expected impact:** Slightly larger positions on tight-SL setups. RISK_PER_TRADE (1%) and MAX_PORTFOLIO_HEAT_PCT (6%) remain unchanged — total risk envelope is preserved.

### 2026-04-24 — Pre-trade Bybit checklist (`/check` Telegram)
**Files:** `scripts/pretrade_check.py` (new), `scripts/explain_bot.py`, `bybit_pretrade_checks` table (new)

**What changed:**
- **New Telegram command `/check SYMBOL side entry SL TP [lev=N] [thesis…]`** — manual Bybit trade sanity check before entry.
- Gathers: live Bybit ticker (price, funding, OI, 24h range), account balance, last 15 bybit_trade_annotations same symbol+side, aggregated ml_setups stats for same pair+direction (last 90d).
- Feeds structured payload to Claude Opus 4.7 (`CLAUDE_MODEL_AUDIT`). Returns strict JSON: score 0-10, verdict (strong/ok/weak/skip), size suggestion, max safe leverage, green/red flags, missing confluences, coaching notes.
- Formats verdict with emoji to Telegram; logs full payload + report to `bybit_pretrade_checks` table for regression (compare pre-trade check vs actual outcome).
- CLI standalone: `python scripts/pretrade_check.py "/check BTC long 77500 76800 79000"`.

**Why:** manual Bybit trading is the biggest dollar lever ($4.6k vs $86 bot). Pre-entry second-opinion forces thesis articulation + surfaces historical self-leaks ("your last 5 BTC longs are 0/5 — this setup has the same structure"). Low-risk, high-value application of Opus 4.7.

**Safety:** read-only (no order placement). Direction sanity enforced in parser (long requires SL<entry<TP; short the inverse). Symbol whitelist = bot's 7 pairs. Fails soft if Bybit API down.

### 2026-04-24 — Weekly edge audit (Claude Opus 4.7)
**Files:** `scripts/weekly_edge_audit.py` (new), `config/settings.py`, `systemd/quant-edge-audit.{service,timer}` (new), `ml_edge_audits` table (new)

**What changed:**
- **New offline audit pipeline.** Pulls resolved `ml_setups` + closed `trades` + shadow outcomes for last N days, aggregates by setup/pair/hour/htf_bias/feature tier, feeds to Claude Opus 4.7, emits narrative markdown audit.
- **Model:** `CLAUDE_MODEL_AUDIT=claude-opus-4-7` (env-overridable). Separate from live AI filter `CLAUDE_MODEL`. Prompt caching enabled via `cache_control` on system prompt (not yet crossing 1024-token threshold — activates as prompt grows).
- **Storage:** `ml_edge_audits` table (period_start/end, experiment_id, payload JSONB, report_md, tokens, cache metrics). Markdown written to `docs/audits/edge-audit-YYYY-WW.md`.
- **Schedule:** `quant-edge-audit.timer` — Sunday 10:00 UTC weekly. Install: `sudo cp systemd/quant-edge-audit.{service,timer} /etc/systemd/system/ && sudo systemctl enable --now quant-edge-audit.timer`.
- **Safety:** `--min-setups 10` default bails if sample too small; `--dry-run` skips API call. Offline-only — never touches live pipeline.

**Why:** meta-labeling roadmap (AFML) needs narrative edge analysis, not just numbers. Shadow mode data accumulating; weekly audit surfaces leaks (feature tier instrumentation gaps, session-time effects, long/short asymmetry) before live re-enable.

**How to use:**
- On-demand: `venv/bin/python scripts/weekly_edge_audit.py --days 7`
- Force with low sample: `--min-setups 0`
- Query history: `SELECT period_start, n_setups, win_rate_pct, tokens_in FROM ml_edge_audits ORDER BY id DESC`

### 2026-04-24 — Fix: `_evaluate_quick_setups` NameError (state_4h/state_1h/volume_profile)
**Files:** `strategy_service/service.py`

**What changed:**
- Added `state_4h`, `state_1h`, `volume_profile` as explicit parameters to `_evaluate_quick_setups`. Previously referenced as free variables (introduced by commit `28f66841` Batch 4 on 2026-04-21), causing `NameError` on every LTF candle across all pairs.
- `evaluate()` now passes these down to the quick-setup path.

**Why:** Production bug. Bot logged `Pipeline callback error: ... name 'state_4h' is not defined` on every 5m+15m candle for ~3 days. Setup D (quick) was completely dead — every evaluation raised before any setup logic ran. Swing setups (A/B/F) survived only because `return setup` on match exited `evaluate()` before reaching the broken quick-setup call.

**Impact:** Setup D reactivated. Zero pipeline callback errors post-fix (verified in logs). Tests: 915 pass.

### 2026-04-23 — Audit fase 4.3: §BAJA hardening
**Files:** `main.py`, `risk_service/state_tracker.py`

**What changed:**
- **`_trades_today` placeholders** usan sentinel `{"_placeholder": True}` en vez de `{"pair": "reconciled/restored", "pnl_pct": 0, "timestamp": 0}`. Cualquier iteración futura sobre fields falsos explota visible.
- **`_log_trade_rejection` divisors** blindados. `entry`, `risk` validados antes de dividir; previene ZeroDivisionError si setup llega con precios malformados.
- **`reconcile_drawdown_from_db` asimetría** documentada explícitamente + log INFO cuando Redis es peor que DB (antes silencioso). Comportamiento idéntico — min(Redis,DB) by design — pero la decisión queda visible al operador.
- **`TRADING_SESSIONS` clarity comment** explicando por qué coexiste con `trading_session` feature de ml_features (overlapping Telegram alerts vs non-overlapping ML categorical).

**Why:** audit §BAJA — hardening no-comportamental, cierra remaining observations. No hay cambios de lógica de guardrails ni de labels.

### 2026-04-23 — Audit fase 4.2: Bybit link robustness + dataset ground truth §7.0
**Files:** `data_service/bybit_watcher.py`, `docs/SYSTEM_BASELINE.md`

**What changed:**
- **Bybit pending → annotation link** ahora matchea por qty + entry_price (score combinado rel-diff, ventana 10 min) en lugar de solo tiempo. Guard: rechaza si `qty_rel>20%` o `price_rel>2%`. Fallback legacy (5 min, sin qty/price) si la PositionState no trae tamaño. Evita cross-link con 2 pendings similares.
- **§7.0 Dataset Ground Truth** formaliza que `ml_setups` es la **única** fuente autoritativa para training / edge / meta-label. `trades` es operacional (PnL realizado + dashboard). Bybit tables son journal de decisiones manuales — **nunca** cruzar con ml_setups para entrenamiento.

**Why:** audit §MEDIA. El link 5-min heurístico podía cross-linkear tesis en bursts; y la mezcla manual/bot/bybit sin contrato explícito arriesgaba contaminar training queries.

### 2026-04-23 — Audit fase 4.1: cleanup §MEDIA (observabilidad + shadow batching)
**Files:** `config/settings.py`, `main.py`, `execution_service/monitor.py`, `execution_service/shadow_monitor.py`

**What changed:**
- **`_emit_metric` no es silent anymore** — contador in-memory de fallos + WARNING cada 5 min (max). Main.py y monitor.py. Antes `except Exception: pass` ocultaba Postgres degradado.
- **Silent catch → `logger.debug`** en orderbook snapshot, BTC candle fetch, timeout spread probe, funding cost estimate. Errores siguen fire-and-forget pero trazables en debug.
- **Shadow `_save_to_redis` batched** — un save por tick en `check_candle` (en lugar de 1 por fill + 1 por TP1 touch + 1 por batch resolve). Dirty-flag pattern con `_dirty_from_inner_checks` para TP1 transitions dentro de `_check_tp_sl`.
- **Comment drift fix** en `HTF_MIN_RISK_DISTANCE_PCT` (settings.py:833) — decía "vs 0.2% intraday", actual intraday es 0.5%.

**Why:** audit §MEDIA. Observabilidad opaca hacía que Postgres degradado fuera invisible; redis hot-path Hypotéticamente hasta 20 writes/tick con 7 pares × varios shadows activos.

### 2026-04-23 — Audit fase 4: estructural (ML gate + cache + contrato manual)
**Files:** `main.py`, `config/settings.py`, `risk_service/service.py`, `execution_service/service.py`, `docs/SYSTEM_BASELINE.md`, tests

**What changed:**
- **EXPERIMENT_ID boot log** — `main.py` loggea feature_version + experiment_id + source ('env override' vs 'settings default') al arrancar. Antes el tag ML de la sesión solo aparecía implícito en writes.
- **ML Activation Gate** formalizado en §7.1: 6 gates duros (G1–G6) antes de re-habilitar AI filter o bet sizing. Incluye ROC AUC ≥ 0.60, Brier ≤ 0.22, purged k-fold CV, shadow comparison ≥ 200 paper trades. Anti-patterns documentados.
- **Balance cache TTL 5 min** en `RiskService._query_account_balance`. Bursts de señales ya no martillean `fetch_usdt_balance`. `refresh_capital_from_exchange` bypassea cache (close → always fresh).
- **Contrato bot+manual explícito** — nuevo `settings.ALLOW_BOT_WITH_MANUAL` (default **false**). Con manual abierto en un pair, bot signal es rechazada (portfolio heat no puede ver manual SL → stacking dejaba exposición real invisible). Legacy coexistence opt-in via env var. Emite metric `bot_signal_blocked_by_manual`.

**Tests:** `test_balance_cache_reuses_within_ttl`, `test_balance_cache_bypassed_on_force`, `test_manual_blocks_bot_by_default`. Test existente `test_adopted_cancelled_before_new_bot_entry` actualizado para flag opt-in.

**Out of scope (aún pendientes):** Bybit pending→annotation link por tiempo, `_log_trade_rejection` division guard, `TRADING_SESSIONS` dedup, `reconcile_drawdown_from_db` asimétrico. Bajo impacto — diferir hasta que justifiquen prioridad.

### 2026-04-23 — Audit fase 3: observabilidad + contratos ML
**Files:** `data_service/data_store.py`, `execution_service/service.py`, `execution_service/monitor.py`, `execution_service/shadow_monitor.py`, `.claude/commands/pipeline-diagnosis.md`, tests

**What changed:**
- **#14 Tests restart safety** — `tests/test_data_store_filters.py`. Regresión SQL: `fetch_closed_trades_pnl`, `fetch_recent_closed_trades`, `get_journal_summary` obligados a contener `orphaned_restart` en el filtro. Si un refactor futuro lo pierde, DD reconcile y dashboard saltan al instante.
- **#15 Metric counters** en sitios silenciosos: `orphan_reconcile_error/count`, `shadow_outcome_resolved_ok/error` (con label `outcome`), `shadow_redis_save_error`, `shadow_redis_load_error`, `on_sl_hit_callback_error` (con label `source` = excessive_slippage | sl_too_close | sl_verify | sl_vanished | sl_status_closed). `ShadowMonitor` gana método `_emit_metric` propio.
- **#16 `update_ml_setup_outcome` detecta orphan-row**: `cur.rowcount == 0` → WARNING `ML outcome orphan` + `return False`. Antes actualizaba "silenciosamente" cero filas cuando el insert_ml_setup había fallado o el shadow no se había registrado jamás.
- **#17 Filtro non-market unificado**: constante `NON_MARKET_OUTCOMES` + helper `ml_market_outcome_filter_sql(column)` en `data_store.py`. Training query en SYSTEM_BASELINE §7 y `.claude/commands/pipeline-diagnosis.md` alineadas con la constante (removidos labels obsoletos `shadow_hour_filtered`, `shadow_fear_long_filtered`, `shadow_risk_rejected`).

**Tests:** `test_data_store_filters.py` — 11 tests, incluye subset-check `NON_MARKET_OUTCOMES ⊆ VALID_OUTCOMES`, SQL determinismo, contrato de labels emitidos (live / shadow / pre-exec).

### 2026-04-23 — Audit fase 2: ML label cleanup (v17)
**Files:** `data_service/data_store.py`, `execution_service/monitor.py`, `config/settings.py`, `shared/ml_features.py`, `docs/context/00-architecture.md`, tests

**What changed:**
- **#10 Whitelist `VALID_OUTCOMES`** en `data_service.data_store`. `update_ml_setup_outcome` loggea WARNING si outcome_type no matchea. Previene drift silencioso entre docs y runtime.
- **#10 Docs outcome_type synced** (`docs/context/00-architecture.md:315` + SYSTEM_BASELINE §7). Removidos labels que nunca se emitían (`deduped`, `regime_extreme_fear`, `shadow_risk_rejected`). Añadidos los emitidos reales (`trading_halted`, `filled_slippage`, `ai_rejected`). Referencia a la constante VALID_OUTCOMES.
- **#11 `filled_slippage` dedicated outcome** para `excessive_slippage` + `sl_too_close`. Antes se mapeaban a `filled_timeout` — perdía señal "OB no aguantó la entrada". También llama `on_sl_hit` si pnl_pct<0 → marca OB como failed para no re-trigger.
- **#12 `setup_d` removido** de `QUICK_SETUP_TYPES`. Strategy emite `setup_d_bos`/`setup_d_choch`; `setup_d` a pelo nunca matcheaba.
- **#13 `_is_pd_aligned` strict**. Equilibrium ya no cuenta como aligned para ningún lado. Antes `pd_aligned=True` en la zona más ambigua diluía predictive power. `pd_zone` categorical sigue capturando equilibrium.
- **ML_FEATURE_VERSION 16 → 17** por cambio de semántica en `pd_aligned`.

**Training query actualizado** para excluir labels non-market (`trading_halted`, `ai_rejected`, `shadow_direction_filtered`, `unfilled_timeout`, `replaced`, además de los previos).

**Tests:** `test_pd_equilibrium_not_aligned` (regression).

### 2026-04-23 — Audit fix #9: capital_at_trade snapshot (migration 18)
**Files:** `data_service/data_store.py`, `execution_service/models.py`, `execution_service/service.py`, `execution_service/monitor.py`, `tests/test_execution.py`

**What changed:**
- **Migration 18**: `trades.capital_at_trade DOUBLE PRECISION` (nullable).
- `ManagedPosition.capital_at_trade: float = 0.0`.
- `execute()` en ExecutionService hace snapshot de `risk._state.get_capital()` al crear la ManagedPosition.
- `_calculate_pnl` denomina `pnl_pct` por `pos.capital_at_trade` cuando > 0 (fallback: live capital → entry_notional).
- `insert_trade` persiste `capital_at_trade` en la fila.

**Why:** antes el denominador de `pnl_pct` era el capital tracked al momento del close, que ya había driftado por PnL de trades intermedios. Un trade que ganó $5 con capital abierto $100 y cerrado $120 reportaba 4.17% en vez de 5.00%. Ahora cada fila es self-consistent.

**Interaction con fix #8:** `refresh_capital_from_exchange` mueve tracked capital tras cada close; el snapshot por trade protege la historia de ese movimiento.

**Tests:** `TestCapitalAtTrade` (2): snapshot usado, fallback a live cuando snapshot=0 (adopted).

### 2026-04-23 — Audit fix #8: capital refresh tras realized close
**Files:** `risk_service/service.py`, `execution_service/monitor.py`, `execution_service/campaign_monitor.py`, `tests/test_risk_service.py`

**What changed:**
- Nuevo método `RiskService.refresh_capital_from_exchange()`: refetch vía `_query_account_balance`, actualiza tracked capital, setea `_balance_ever_fetched`.
- `PositionMonitor._close_position` y `CampaignMonitor._close_campaign` lo llaman tras `on_trade_closed` (nunca en `cancelled`). Fire-and-forget con warning en fallo.

**Why:** capital tracked se seteaba solo al arranque. Cada close realizado movía el balance en OKX pero risk tracker seguía usando el snapshot viejo → `pnl_pct` denominado por capital estático → DD drift al compoundear. En cuentas chicas ($100) 3 losses seguidas daban -7.8% real vs -7.5% sumado (no negligible al 5% DD cap).

**Tests:** `TestCapitalRefresh` (2): actualiza al éxito, no-op al fallo.

### 2026-04-23 — Audit fix #7: risk tracker row match por opened_timestamp
**Files:** `risk_service/state_tracker.py`, `risk_service/service.py`, `execution_service/monitor.py`, `execution_service/service.py`, `execution_service/campaign_monitor.py`

**What changed:**
- `record_trade_filled/closed/cancelled` aceptan `opened_timestamp: int | None`. Cuando se provee, matchean la fila exacta (pair, direction, timestamp). Sin él, fallback first-match (backward compat).
- Todos los callers en monitor/service/campaign_monitor ahora pasan `opened_timestamp=pos.created_at` (o `c.created_at` para campaigns).
- `_matches` helper estático centraliza la lógica de match.

**Why:** ante dos posiciones concurrentes con el mismo `(pair, direction)` — ej. bot + adopted manual pre-fix #6, o HTF campaign corriendo junto a intraday — first-match popeaba la fila equivocada, dejando ghosts permanentes. Agotaba `MAX_OPEN_POSITIONS` silenciosamente.

**Tests:** `TestConcurrentSamePairDirection` (3): close/cancel por timestamp, backward compat sin timestamp.

### 2026-04-23 — Audit fix #6: bot+manual coexistence phantom fix
**Files:** `execution_service/service.py`, `tests/test_execution.py`

**What changed:**
- Al abrir trade del bot sobre un pair con posición adoptada (manual), `execute()` ahora llama `risk.on_trade_cancelled(pair, existing.direction)` ANTES del `monitor.positions.pop()`. Inmediatamente después `on_trade_opened` del nuevo trade añade la entrada real.

**Why:** sin el cancel previo, el risk tracker mantenía la entrada adopted + añadía la nueva = 2 entradas. `record_trade_closed` después popeaba la primera match (el phantom), dejando la del bot viva permanentemente. Cada ciclo bot-sobre-manual agotaba silenciosamente `MAX_OPEN_POSITIONS`.

**Tests:** `TestBotAlongsideManual::test_adopted_cancelled_before_new_bot_entry` — verifica orden cancel→open.

### 2026-04-23 — Audit fix #5: adopted position SL recovery
**Files:** `execution_service/service.py`, `tests/test_execution.py`, `docs/context/05-execution.md`

**What changed:**
- **`sync_exchange_positions` ya no hardcodea `sl_price=0.0`** para adopted positions. Nuevo helper `_extract_adopted_sl`: (1) busca SL real en algo orders de OKX (`slTriggerPx` / `triggerPx` en lado correcto), (2) fallback a `entry ± entry × MAX_SL_PCT` (4%) si no hay SL en exchange.
- Adopted positions ahora se registran con `sl_price` no-cero en monitor + risk tracker.

**Why:** `get_portfolio_heat_usd` salta entradas con `sl<=0` → una posición manual abierta en OKX era invisible al heat guardrail. `MAX_PORTFOLIO_HEAT_PCT` (6%) podía exceder silenciosamente cuando bot + manual coexistían. Fallback MAX_SL_PCT es conservador: no es el SL real, solo fuerza contabilización.

**Tests:** `TestSyncExchangeAdoptedSL` (4 tests): attached SL, standalone trigger, fallback, ignora triggers en lado equivocado.

### 2026-04-23 — Audit fixes fase 0 (risk + restart safety)
**Files:** `risk_service/service.py`, `execution_service/service.py`, `data_service/data_store.py`, `dashboard/api/queries.py`, tests

**What changed:**
- **RiskApproval kwargs fix** — `risk_service/service.py` pasaba `margin=`/`risk_amount=` al construir `RiskApproval`, campos inexistentes → TypeError latente en el camino "refuse when `INITIAL_CAPITAL` + balance never fetched". Ahora usa los 5 kwargs reales. Test regresión añadido.
- **`_query_account_balance` atributo fix** — usaba `getattr(data_service, 'exchange', None)` siempre None → `_balance_ever_fetched` nunca pasaba a True → capital tracked congelado post-arranque. Ahora llama `self._data_service.fetch_usdt_balance()` directo. Tests `TestBalanceQueryWiring` (happy + fallback).
- **Orphan reconcile no inventa PnL** — `_reconcile_orphaned_trades` estimaba `pnl_usd = (sl-entry)*size` como "worst case", contaminando `trades` table con losses sintéticos que nunca ocurrieron. Ahora deja PnL/actual_exit NULL, solo marca `exit_reason='orphaned_restart'` + `outcome_type='filled_orphaned'` en ml_setups.
- **Filtro `orphaned_restart` en readers** — `data_store.fetch_closed_trades_pnl` (DD reconcile daily+weekly), `fetch_recent_closed_trades`, stats agregadas (total/by pair/by setup), `dashboard/api/queries.get_trade_stats`. Todos añaden `exit_reason IS DISTINCT FROM 'orphaned_restart'`. Previene DD inflado + dashboard sesgado por orphans.

**Why:** audit 2026-04-23 detectó agujeros en restart safety. PnL sintético entraba en DD reconcile → guardrails bloqueaban trades reales falsamente. TypeError latente podía crashear pipeline en arranque con balance fetch fallido.

**ML impact:** ninguno. `ml_setups` ya se resolvía con `outcome_type='filled_orphaned'` sin PnL; training query ya los excluye. Solo limpia `trades` table.

### 2026-04-21 — Batch 6: Test brutality pass
**Files:** `tests/test_market_structure_invariants.py` (new), `tests/test_order_block_invariants.py` (new), `tests/test_real_candle_integration.py` (new), `tests/test_quick_setups.py`

**New property-based tests** (hypothesis lib, 460-500 random inputs each):
- **Market structure invariants (9 tests):** deterministic output, swing highs are local maxima within SWING_LOOKBACK, swing lows are local minima, chronological ordering, bullish breaks must be above broken level, break types in {bos, choch}, empty/single/flat candle edge cases.
- **Order block invariants (6 tests):** body within wick bounds, entry_price is exact body midpoint, direction matches associated structure break, volume_ratio non-negative, active list excludes mitigated OBs, detector determinism.

**New real-data integration tests** (5 tests, @pytest.mark.db):
- Detection produces swings+breaks on real 500-candle windows across BTC/ETH/SOL
- OB detection on real BTC candles (bounds 0 ≤ active OBs ≤ 50, all unmitigated)
- Per-pair state isolation (BTC detector doesn't pollute ETH state)

**Weak-assert fixes:** 2 bare `assert result is not None` in test_quick_setups.py displacement tests replaced with exact-value follow-ups (setup_type, direction, entry-in-OB-body, SL matches OB low).

**Metrics:**
- Tests: 884 pass (+20), 1 skipped, 1 xfailed
- Mock count: 401 (from 781 pre-Batch 6, target was <400 — essentially met)
- Mock/assert ratio: ~45% (from ~90% pre-Batch 6)

**Why these tests matter:** property tests catch bugs that hand-picked cases miss — a swing detector that worked on the fixture but failed on randomness would now break CI. Real-candle integration tests catch detection regressions on actual market data (what the bot sees live), not synthetic fixtures.

**Not done:** test_execution.py (206 mocks) refactor — deferred, would require OKX sandbox fixtures. test_main_pipeline.py (16 mocks) also left — genuine integration harness is bigger scope than one batch.

### 2026-04-21 — Batch 4: Quick setup structural TP port
**Files:** `strategy_service/quick_setups.py`, `strategy_service/service.py`, `tests/test_quick_setups.py`

**Change:** `evaluate_setup_d` now delegates TP calculation to `SetupEvaluator._calculate_tp_levels`, the same function used by swing setups A/B/F/G. When structural levels (HTF swing highs/lows, Volume Profile POC/VAH/VAL/HVNs) beat the fixed R:R minimums, tp2 snaps to those structural targets. Fixed R:R fallback preserved when no structural data.

**Why:** Pre-Batch 4 `quick_setups.py:153-159` hardcoded `tp2 = entry + risk × SETUP_TP2_RR[variant]` with no access to structural context. This capped setup_d at R:R 1.5 regardless of market geometry. Batch 0 audit showed setup_d_bos avg 1.50 / setup_d_choch avg 1.50 with zero variation. With the port, setup_d can reach R:R 3+ when swings support it — matching setup_f's observed avg 2.13 / max 3.09.

**Integration:** `strategy_service/service.py:evaluate_setup_d` caller now passes `swing_highs_htf`, `swing_lows_htf`, `volume_profile` (already gathered for swing setups). No new data collection needed.

**Deploy:** code ready, no config change. Will affect new setup_d shadows once bot redeployed. Deferred until Batch 1 validation completes (7d), then deploy combined with setup_d re-enablement plan.

**Deferred:** GEOMETRY_CASCADE for setup_d (multi-entry/SL candidate search). Setup D uses `SETUP_D_ENTRY_PCT` (single depth into OB body); cascade adds alternatives. Low priority — structural TP delivers the bulk of the R:R improvement.

**Test count:** 864 pass (+5 new), 1 skipped, 1 xfailed. New tests: fallback fixed R:R, structural TP snap on long, short uses swing lows, VP POC as candidate, minimum-RR gate prevents regression.

### 2026-04-20 — Batch 2: Backtest analytics reinforcement
**Files:** `scripts/backtest_bootstrap.py` (new), `scripts/backtest_stability.py` (new), `scripts/backtest_regime_split.py` (new), `tests/test_backtest_analytics.py` (new)

**Added:**
- **Bootstrap CI** — resample trades 2000× (configurable), report P5/P25/P50/P75/P95 for PF, WR, PnL, max DD. Per-setup breakdown. Kills point-estimate overconfidence.
- **Chronological stability split** — splits trades into N windows (default quartiles), reports per-window metrics + coefficient of variation. Exposes the "golden period + collapse" overfit failure. CV guide: PF <0.3 stable, >0.7 unstable.
- **Regime split** — queries `ml_setups` directly and slices outcomes by volatility regime, trading session, BTC 20-bar return, direction, ADX trend strength. Uses existing v14+ feature columns (no new instrumentation needed).
- **29 new tests** — hand-computed point metrics, percentile ordering, bootstrap determinism + invariants, REAL CSV assertions (trade count, PF, WR match TRACKER.md), stability detects known golden-period edge concentration, hypothesis property tests (total_pnl == sum, WR ∈ [0,1], DD ≥ 0), CSV malformed-row handling.

**Usage:**
```
python scripts/backtest_bootstrap.py backtest_results/trades.csv
python scripts/backtest_stability.py backtest_results/trades.csv --windows 4
python scripts/backtest_regime_split.py --days 60 --experiment batch1_tp1_rr_1_3_2026_04_20
```

**Deferred:** proper walk-forward optimization (requires simulator refactor for train/test split injection) — not blocking. Stability split catches the same class of failures with far less scope.

**Test count:** 859 pass, 1 skipped, 1 xfailed. No regressions.

### 2026-04-20 — Batch 7: Shadow health monitoring
**Files:** `monitoring/dashboards/shadow-health.json` (new), `scripts/shadow_health_alert.py` (new)

**Dashboard "Shadow Health — Batch 1 BE Fix"** (Grafana uid `shadow-health`):
- BE rate on current experiment (threshold 40%/50%)
- Resolved N + WR on current experiment
- Orphan count 24h (threshold 5/10)
- Outcome breakdown per setup_type
- Experiment comparison (prior vs current)
- Daily outcome distribution (14d bar chart)
- Dedup rate per setup 24h
- Avg time-to-resolution by outcome

**Alert script** runs via cron (hourly). Checks: BE rate > 50% (N≥10), orphans > 5/day, no resolutions > 48h. State file `/tmp/shadow_health_alert_state.json` dedupes repeated alerts. Telegram delivery via existing notifier.

**Cron setup (user action):**
```
0 * * * * cd /home/jer/quant-fund && ./venv/bin/python scripts/shadow_health_alert.py >> /var/log/shadow_alerts.log 2>&1
```

**Access:** Grafana at `http://localhost:3001` or Tailscale `http://100.120.181.11:3001`. Dashboard URL path `/d/shadow-health`.

### 2026-04-20 — Batch 1: TP1_RR_RATIO 1.0 → 1.3 (BE fix)
**EXPERIMENT_ID:** `shadow_tuning_v16_2026_04_18_be_fix` → `batch1_tp1_rr_1_3_2026_04_20`
**Files:** `config/settings.py`, `scripts/be_knob_comparison.py` (new), `tests/test_setups.py`, `tests/test_volume_profile.py`

**Change:** `TP1_RR_RATIO` raised from 1.0 to 1.3. TP1 now sits further from entry, so normal candle wicks stop triggering the SL→breakeven move. TP1 is still used for partial-exit logic when live trading returns.

**Evidence (30d shadow replay via `scripts/be_knob_comparison.py`):**

| Variant | WR | BE% | PF | PnL |
|---|---|---|---|---|
| baseline (TP1=1.0) | 48.5% | 28% | 1.30 | $32.54 |
| BE_CONFIRM=1 only | 45.0% | 8% | 1.21 | $26.82 |
| **TP1×1.3 (chosen)** | **53.7%** | **8%** | **1.65** | **$74.28** |
| TP1×1.5 | 52.4% | 2% | 1.64 | $74.31 |
| TP1×2.0 | 46.5% | 0% | 1.35 | $46.15 |

1.3 beats 1.5 on WR and 1.5 beats 1.3 on BE elimination — near tie. Chose 1.3 to keep partial-exit logic useful when live trading resumes (half-position locked in at 1.3 R:R is still a reasonable risk-off point).

`BE_CONFIRM_CLOSES` knob added in Batch 0 but kept at 0 — the comparison showed it alone made things worse (PF 1.21 vs 1.30 baseline). TP1 distance fix is sufficient.

**Why bump experiment_id:** pre-change outcomes are noise (79%-BE scratches). Filter training by `experiment_id = 'batch1_tp1_rr_1_3_2026_04_20'` going forward.

**Next:** deploy via `docker compose up -d --build bot`. Collect 7d. Bar = BE rate <40% (from 79% / 28%-replay baseline). If met → Batch 2.

### 2026-04-20 — Batch 0 infra trust (migration 17)
**Files:** `shared/pnl_engine.py` (new), `execution_service/shadow_monitor.py`, `scripts/backtest.py`, `execution_service/monitor.py`, `shared/ml_features.py`, `config/settings.py`, `data_service/data_store.py`, `tests/test_pnl_engine.py` (new), `tests/test_shadow_infra.py` (new), `pytest.ini` (new)

**Changes:**
- Extracted unified `shared/pnl_engine.py` — TP/SL/BE resolution + `compute_pnl` with per-side fees. Shadow monitor, backtest (trades + campaigns), and execution monitor all delegate here. Single source of truth.
- Added `BE_CONFIRM_CLOSES` setting (default 0 = legacy any-touch arms BE; knob for Batch 1 — setting to 1 will require candle CLOSE through TP1 before SL→BE).
- Migration 17: `shadow_resolve_candle_{ts,tf,high,low,close}` + `shadow_fill_candle_{ts,tf}` on ml_setups. Captures the exact candle shadow_monitor saw at resolution for deterministic replay.
- `extract_risk_context(..., capital_override=...)` — shadow setups now write `risk_capital=SHADOW_CAPITAL` instead of live OKX balance. Fixes the $86 vs $500 mismatch in ml_setups rows.
- 43 new tests (32 pnl_engine + 11 shadow_infra): Tier 1 exact math, Tier 2 DB replay (`@pytest.mark.db`), Tier 3 hypothesis property (1000+ cases). 1 known-drift test marked xfail documenting the ~30% engine/DB outcome disagreement on pre-migration data.

**Test count:** 830 pass, 1 xfailed, 1 skipped. No regressions.

**Why:** Audit revealed duplicated fee math across 4 call sites + 79% breakeven scratch rate. Unification prerequisite for Batch 1 BE fix (single place to flip the knob) and Batch 2 backtest reinforce (backtest and shadow must agree on outcomes to compare).

### 2026-04-18 — Shadow breakeven same-candle bug fix
**EXPERIMENT_ID:** `shadow_tuning_v16_2026_04_16` → `shadow_tuning_v16_2026_04_18_be_fix`
**ML_FEATURE_VERSION:** 16 (unchanged)

**Bug:** `ShadowMonitor._check_tp_sl` moved SL to entry on TP1 touch, then checked `hit_sl` against the new SL in the SAME candle. The fill candle by definition touches entry, so `hit_sl` returned True trivially, resolving the shadow as `shadow_breakeven` in ~100 ms. Dozens of outcomes in the experiment `shadow_tuning_v16_2026_04_16` dataset have `actual_entry == actual_exit == entry_price` and `trade_duration_ms < 1s` because of this.

**Fix (`execution_service/shadow_monitor.py`):** when TP1 is newly touched in a candle, the breakeven SL activates only on SUBSEQUENT candles. Same-candle `hit_sl` against the moved-to-entry SL is skipped. Same-candle TP2 still resolves legitimately as `shadow_tp`.

**Why bump experiment_id:** pre-fix shadow_breakeven outcomes are contaminated (not representative of live breakeven behavior). Filter training dataset by `experiment_id = 'shadow_tuning_v16_2026_04_18_be_fix'` to exclude.

**Expected impact:** fewer `shadow_breakeven` outcomes, more `shadow_tp` and `shadow_sl` — closer to the live SL-to-entry semantics (breakeven requires price to RETURN to entry after going to TP1, not touch entry at fill time).

### 2026-04-16 — ML Feature Expansion: WT + ADX + BB + StochRSI (v15 → v16)
**ML_FEATURE_VERSION:** 14 → 15 → 16
**EXPERIMENT_ID:** unchanged (`shadow_tuning_v16_2026_04_16`)

**What changed:**
- **v15: WaveTrend (Cipher B core)** added to `shared/ml_features.py`. Helper `_compute_wavetrend()` with LazyBear Pine formula (n1=10, n2=21). Features: `wt_wt1`, `wt_wt2`, `wt_cross` (bullish/bearish), `wt_zone` (oversold/overbought/neutral), `wt_aligned` (cross matches setup direction in opposite extreme zone).
- **v16: ADX + Bollinger + Stochastic RSI** added. Helpers `_compute_adx()` (Wilder 14), `_compute_bollinger()` (20,2), `_compute_stoch_rsi()` (14,14,3,3). 14 new features: `adx_14`, `plus_di_14`, `minus_di_14`, `adx_trend_strength`, `adx_direction`, `bb_width_pct`, `bb_percent_b`, `bb_squeeze_percentile`, `bb_squeeze`, `stoch_rsi_k`, `stoch_rsi_d`, `stoch_rsi_zone`, `stoch_rsi_cross`.

**Why:**
- Gap analysis vs existing features: WT covers momentum-exhaustion timing (RSI too slow for reversal detection). ADX covers trend strength (missing — had `volatility_regime_ratio` but no directional strength). BBW covers squeeze/expansion (missing — had `atr_pct` for absolute vol only). StochRSI covers fast momentum reversal (complements RSI with leading signal).
- MACD/Ichimoku/Supertrend/Parabolic SAR rejected as redundant or poorly suited to crypto volatility.

**Expected impact:**
- Richer feature space for meta-labeling model (AFML roadmap Phase 2). Discretionary gate still fully structural — these are ML-only inputs.
- No strategy behavior change (pure observability). Shadow data under `experiment_id=shadow_tuning_v16_2026_04_16` will mix v14/v15/v16 rows; filter by `feature_version >= 16` for cleanest training set once enough outcomes resolved.

**Schema migration 16:** added 18 ml_setups columns (wt_*, adx_*, plus_di_14, minus_di_14, bb_*, stoch_rsi_*). INSERT statement in `data_store.py:insert_ml_setup()` updated accordingly. Without this, features were computed in `ml_features.py` but silently discarded at DB insert.

**Operational:** `main.py` `recent_candles` count raised 50 → 100 to guarantee enough history for ADX (42 bars min) and BB (40) even during backfill phase. Features gracefully return None if history insufficient — no failure path.

**Tests:** 785/785 full suite pass. Fixed stale `test_structural_tp_with_volume_profile` assertion (`tp2 >= 52500` → `>= 52000`) after SETUP_TP2_RR["setup_a"] was lowered from 2.5 to 2.0 in earlier April shadow tuning.

### 2026-04-16 — Shadow Tuning v16: Data Quality Over Freeze Purity
**EXPERIMENT_ID:** `shadow_tuning_v16_2026_04_16`
**ML_FEATURE_VERSION:** 14 (unchanged)
**Mode:** shadow only (5 setups: A-short, B, D_bos, D_choch, F)

**Why freeze was amended (same day):**
Pipeline diagnosis showed freeze was collecting garbage: setup_a long 5% WR (1/20), setup_b entries 2-3% from market (never fill), setup_g 0/4 WR. Collecting more data on broken setups = more garbage. Amended to focus on viable setups.

**Changes from freeze_v15:**
- **setup_a long DISABLED** from shadow: 5% WR (1/20) — proven systematically broken. Short-only (33% WR). New `SHADOW_DIRECTION_FILTER` setting.
- **setup_g REMOVED** from shadow: 0/4 WR, breaker blocks too weak.
- **SETUP_B_MAX_ENTRY_DISTANCE_PCT**: 3% → 2%. Entries >2% never fill — kill at detection.
- **SHADOW_ENTRY_TIMEOUT_HOURS**: 24 → 12. Stale OBs meaningless after 12h. Faster slot rotation.
- **Shadow dedup staleness**: Unfilled shadows >4h old no longer block new shadows. Prevents 1 stale shadow from locking out all detections for 12-24h.

**STILL FROZEN:**
- Detection logic, feature extraction, R:R, confluence thresholds
- Only data collection plumbing + proven-broken setup filtering changed

**EXIT CRITERIA:** 100+ resolved shadow outcomes OR 30 days. Daily `/pipeline-diagnosis`.

### 2026-04-16 — FREEZE PROTOCOL v15 (superseded by v16 same day)
**EXPERIMENT_ID:** `freeze_v15_2026_04_16`
**Superseded:** Amended to shadow_tuning_v16 after pipeline diagnosis showed garbage data collection.

**Pre-freeze changes (still apply):**
- **4 execution bugs fixed**: `filled_qty` crash, `cancel_order` args swapped, PnL zero-check (breakeven=None), orphan ML mislabel (`filled_timeout`→`filled_orphaned`)
- **Shadow TP1 tracking**: `_check_tp_sl()` now simulates breakeven SL move when TP1 touched. New `shadow_breakeven` outcome. Fixes artificially low shadow WR — trades that would be breakeven in live were counted as SL losses.
- **F&G regime gate REMOVED**: Retail signal was blocking institutional SMC setups during fear. `fear_greed_score` kept as ML feature.
- **`experiment_id` column added** (migration 15): Tracks which parameter regime generated each sample. `feature_version` = what columns mean, `experiment_id` = what rules generated sample.

### 2026-04-15 — Shadow Data Quality: Orphan Fix + Parameter Tuning
**What changed:**
- **Orphaned shadow cleanup**: New `resolve_orphaned_shadow_setups()` in PostgresStore. Runs on ShadowMonitor startup + every 6h. Marks NULL-outcome rows older than entry+trade timeout as `shadow_orphaned`. Fixes 53 stuck rows from April.
- **Setup B BOS age tightened**: `SETUP_B_MAX_BOS_AGE_CANDLES` 30→12 (~1h on 5m). Was causing 48% dedup rate — same BOS re-detected for 2.5h.
- **Setup B entry distance tightened**: `SETUP_B_MAX_ENTRY_DISTANCE_PCT` 4%→3%. Distant entries contribute to unfilled timeouts.
- **Setup F entry distance tightened**: `SETUP_F_MAX_ENTRY_DISTANCE_PCT` 5%→2.5%. 3/5 resolved as unfilled_timeout — OBs too far from price.
- **Setup A TP2 R:R lowered**: `SETUP_TP2_RR["setup_a"]` 2.5→2.0. 4/4 SL in April shadow at 2.5 RR. Now matches B/F/G at 2.0.

**Why:** April shadow review: 319 detections, only 18 resolved (6%). 53 orphaned (DB rows with no Redis tracking after restart). setup_b 48% dedup = too noisy. setup_f 60% unfilled = entries too far. setup_a 0% WR at 2.5 RR = TP too ambitious. setup_d_bos was only winner (67% WR, 2 TP / 3 resolved).

### 2026-04-15 — Strategy Audit: Entry Distance + Dead Setup Cleanup
**What changed:**
- **Setup A entry distance filter ADDED**: `SETUP_A_MAX_ENTRY_DISTANCE_PCT = 5%`. Consistency with Setup B (4%) and F (5%). Prevents zombie entries at distant OBs.
- **Setup G evaluation SHORT-CIRCUITED**: Was running full evaluate_setup_g() then discarding because G not in ENABLED/SHADOW lists. Now skips evaluation entirely when disabled.
- **SETUP_F_MIN_CONFLUENCES comment FIXED**: Comment said "3" but value was 2. Updated comment to match reality.

**Why:** Strategy audit found 3 code/doc mismatches. Setup A was the only setup without an entry distance guard — could produce entries at edge of OB_MAX_DISTANCE_PCT (3%) without explicit limit. Setup G was burning CPU on every 15m candle for 7 pairs to produce setups that were always discarded.

### 2026-04-15 — Shadow-Only Mode: Disable Live Trading
**What changed:**
- **ENABLED_SETUPS emptied**: No live trades. All setups run shadow-only for ML data collection.
- **Setup F moved to SHADOW**: Was last live setup. 6 trades, 33% WR, -$0.60. Not enough data to justify live risk.
- **Setup G re-added to SHADOW**: Collecting data again (was removed 04-02 at 0/4 WR, now G evaluation short-circuits when not in shadow list).

**Why:** 43 total trades, -$17.27. 28 were setup_h (10.7% WR, -$15.96) — already killed. Remaining setups have too few trades for statistical confidence. Shadow mode collects ML training data without risking capital. DD limit fix (5%→10%) resolved Apr 9 blockage but no reason to keep live while sample size is tiny.

### 2026-04-14 — Shadow Pipeline Ungate: Maximize ML Data Collection
**What changed:**
- **Hour filter REMOVED** from shadow: `SHADOW_MIN_HOUR_UTC` no longer gates shadow setups. Hour is already captured as an ML feature via `created_at`. Was killing 21% of all shadow detections.
- **Fear-long filter REMOVED** from shadow: Already removed in code but old deploy was still producing `shadow_fear_long_filtered` outcomes. Confirmed current deploy has no fear gate. Was killing 33% of all shadow detections.
- **Risk rejection NO LONGER gates shadow**: Risk check still runs and result is stored as ML feature (`risk_approved`, `risk_reject_reason`), but rejected setups proceed to tracking with fallback sizing (5% of SHADOW_CAPITAL × MAX_LEVERAGE).
- **Shadow monitor dedup RELAXED**: Previously blocked any new shadow if same (pair, direction, setup_type) was already tracking — up to 36h block. Now only blocks if an unfilled shadow exists with entry price within 1%. Filled shadows don't block new ones.
- **Pipeline dedup TTL reduced for shadow**: 5 min (was 1h). Shadow is data collection — only dedup same-candle repeats.
- **Updated `/trade-review` and `/pipeline-diagnosis` skills**: Now filter disabled setups from queries, include "Known Context" section to avoid repeating known issues, and focus on what changed since last run.
- **Updated `/status` skill**: Fixed psql/redis commands to go through Docker instead of host.

**Why:** Shadow pipeline was collecting almost no resolved data — 93% of detections were filtered before reaching tracking. In 14 days: 190 detections → only 13 resolved (2 TP, 9 SL, 2 no_fill). User confirmed manual setups exist that bot was filtering. Shadow mode exists to collect ML training data; aggressive filtering defeats its purpose. Risk check result and hour are stored as features — ML can learn to use them without hard-gating.

**Starting point (14-day shadow baseline before this change):**
| Metric | Value |
|--------|-------|
| Total detections | 190 |
| fear_long_filtered | 62 (33%) |
| hour_filtered | 39 (21%) |
| shadow_dedup | 38 (20%) |
| Resolved (TP/SL/no_fill) | 13 (7%) |
| Shadow WR (resolved) | 15% (2 TP / 13 resolved) |
| Pending | 38 (20%) |

**Expected impact:** 3-5× more shadow setups reaching tracking and resolving. More ML training data, faster path to model training. Trade-off: noisier data, but ML is designed to handle that.

### 2026-04-13 — Institutional Strategy Overhaul: Remove Retail Setups, Harden Setup A
**What changed:**
- **Setup H REMOVED**: 0/13 WR, 27 trades at 11% WR, PF 0.10. Entry at market during impulse = adverse selection (AFML Ch.5). Tombstoned in code — `evaluate_setup_h()` returns None.
- **Setup C REMOVED**: 0 resolved trades. No OB anchor (market order + fixed 0.5% SL). Funding extreme signal already flows as confluence via `_check_volume_confirmation()`.
- **Setup E REMOVED**: 0W/1L. No OB anchor when no OB found. OI cascade signal migrated to `_check_volume_confirmation()` as `oi_cascade_long_liq_support` / `oi_cascade_short_liq_support` confluence booster.
- **Setup A HARDENED**: (1) Sweep significance filter: `SETUP_A_MIN_SWEEP_TOUCH_COUNT=3` — sweeps of 2-touch levels (noise) rejected. (2) CHoCH displacement filter: `SETUP_A_MIN_CHOCH_DISPLACEMENT_PCT=0.002` (0.2%) — micro-CHoCH on 15m rejected.
- **ML_FEATURE_VERSION → 12**: New confluence strings (`oi_cascade_*_liq_support`, `sweep_touch_count_N`), `LiquiditySweep.swept_level_touch_count` field.

**Why:** Audit showed only Setup F has institutional backing + positive WR. Setups C/E/H entered at market price with no OB anchor — retail behavior. Setup A had correct concept (sweep+CHoCH+OB) but 8.7% WR due to sweeps of insignificant levels and micro-CHoCH noise. Golden rule enforced: **no Order Block = no trade**.

**Expected impact:** Fewer but higher-quality setups. Setup A will produce fewer but more meaningful detections (significant sweeps + real CHoCH). OI cascade and funding signals now boost confidence of OB-anchored setups instead of firing standalone trades.

### 2026-04-13 — Disable HTF-LTF Alignment Requirement
**What changed:**
- **`REQUIRE_HTF_LTF_ALIGNMENT=False`**: Setups A/B/F no longer require LTF structure direction (CHoCH/BOS) to match HTF bias. Counter-trend trades allowed.

**Why:** 4 days with zero setup_f detections (Apr 10-13). All 7 pairs showed "BOS bearish != HTF bullish" — the alignment gate blocked 455 evaluations/day (26% of all setup_f evals). Requiring full alignment means the bot never catches trend reversals or bottoms. ML will learn which counter-trend setups work; for now this gate was the #1 configurable blocker.

**Expected impact:** More setup_f detections during trend transitions. Counter-trend setups will fire — expect lower WR initially but more data for ML to filter later.

### 2026-04-13 — Relax BOS Max Age (40 → 60 candles)
**What changed:**
- **`SETUP_F_MAX_BOS_AGE_CANDLES=60`** (was 40). BOS up to 15h old (on 15m TF) now qualifies for setup_f.

**Why:** "BOS too old" rejected ~300 evals/day (17% of setup_f). Most rejections clustered at 41-64 candles — just past the old limit. 10h was conservative for 24/7 crypto. 60 candles (15h) captures same-session BOS without accepting day-old stale structure.

**Expected impact:** More setup_f candidates from BOS that formed earlier in the session. ML tracks `candles_since_bos` as a feature — will learn optimal freshness threshold over time.

### 2026-04-09 — Volume Profile, Structural TPs, 1H/4H OBs for Swing Setups
**What changed:**
- **Volume Profile module** (`strategy_service/volume_profile.py`): Approximates VP from 4H candles by distributing volume uniformly across each candle's [low, high] range. Computes POC (Point of Control), VAH/VAL (Value Area), HVNs (High Volume Nodes), and LVNs (Low Volume Nodes). Recalculates only on new 4H candles. 200-bin resolution, 500 candles (~83 days lookback).
- **Structural TPs**: TPs now target structural levels (swing highs/lows from 4H/1H, VP POC/VAH/VAL/HVNs, liquidity levels) instead of fixed R:R multiples. Falls back to fixed R:R when no structural level found or when structural level gives worse R:R than fixed. `STRUCTURAL_TP_ENABLED=true` (env-var overridable).
- **1H/4H OBs for swing setups**: Swing setups (A/B/F/G) now use 1H Order Blocks (primary) or 4H OBs (fallback) instead of 15m OBs. 15m OB bodies (~0.15%) produce SLs within noise range; 1H/4H OBs have structural significance. Quick setups (C/D/E) still use 5m/15m OBs. `SWING_OB_TIMEFRAMES=["1h", "4h"]`.
- **VP as OB quality filter**: OBs near a High Volume Node or POC get `vp_hvn_confluence` / `vp_poc_confluence` added. OBs in Low Volume Nodes get `vp_lvn_warning`. Informational only (not a gate).
- **ML features**: `has_vp_poc`, `has_vp_hvn`, `has_vp_lvn`, `vp_poc_distance_pct` added. `ML_FEATURE_VERSION` bumped to 10.
- **New settings**: `VP_ENABLED`, `VP_BIN_COUNT`, `VP_VALUE_AREA_PCT`, `VP_HVN_THRESHOLD`, `VP_LVN_THRESHOLD`, `STRUCTURAL_TP_ENABLED`, `STRUCTURAL_TP_MIN_SEPARATION_PCT`, `SWING_OB_TIMEFRAMES`. All env-var overridable.
- **17 new tests** for VP computation, caching, helpers, and structural TP logic.

**Why:** Shadow data showed 8.7% WR — root causes: (1) 15m OBs produce SLs within noise (avg 0.7-1%, 15m ATR ~0.3%), (2) fixed R:R TPs have no structural basis (price doesn't care about 2:1 ratios), (3) no volume profile analysis to validate OB zones. User's manual approach (VP POC as entry magnet, structural TPs, multi-TF volume analysis) outperforms fixed R:R mechanically.

**Expected impact:** Wider SLs from 1H/4H OBs = fewer noise stop-outs. Structural TPs = targets that have a reason to hold (volume clusters, swing levels). VP confluence = higher-quality OB selection. May reduce setup frequency (fewer 1H OBs than 15m), but quality should improve significantly. All changes have instant rollback via env vars.

### 2026-04-09 — Skip Broken Swing Levels in Target Space Check
**What changed:**
- Target space filter (`_check_target_space`) now ignores swing highs/lows that price already broke through. Previously, a swing high below current price (already invalidated) could falsely block a long setup.
- `ml_setups.outcome_type` column widened from VARCHAR(20) to VARCHAR(50) to fix DB write errors on longer outcome strings.

**Why:** Bot was rejecting valid setups because stale swing levels (already broken by price) were treated as resistance/support barriers. The target space check should only consider levels that are still ahead of price.

**Expected impact:** More setups pass the target space gate. No change to setups that already had clear space.

### 2026-04-02 — Shadow Performance Audit: SL Tightness Fix
**What changed:**
- `ATR_SL_FLOOR_MULTIPLIER`: 3.0 → 4.5. Shadow data: 42 SL vs 4 TP (8.7% WR). 15m ATR ~0.3%, so 3× = 0.9% — within normal wick noise. 4.5× = ~1.35% breathing room.
- `SETUP_A_ENTRY_PCT`: 0.65 → 0.50. Shallow entry kept SL within noise range. Midpoint entry adds distance from SL at cost of lower fill rate.
- `SETUP_A_MODE`: "both" → "continuation". Counter-trend trades were 17/17 SL. Only trade with HTF bias now.
- `setup_g` removed from `SHADOW_MODE_SETUPS`. 0/4 WR — breaker blocks (failed OBs) are structurally weak levels. Not worth tracking.

**Why:** Shadow audit revealed 91% SL rate across all shadow setups. Root causes: SL distances (avg 0.76%) within 15m candle noise, counter-trend setup_a trades, and structurally flawed setup_g/h signals.

**Expected impact:** Fewer but higher-quality shadow detections. Setup A should see wider SLs (~1.35% floor) and only continuation trades. Monitor for 1-2 weeks to validate improvement.

### 2026-04-13 — Remove Fear-Long Gate from Shadow
**What changed:**
- Removed `SHADOW_FEAR_LONG_GATE` (was 25). F&G score remains as ML feature (`fear_greed_score` in ml_setups), no longer used as a gate.
- `SHADOW_MIN_HOUR_UTC=11` kept (0% WR data still valid).

**Why:** F&G < 25 was blocking 100% of shadow longs during sustained fear (Apr 9-13, F&G 14-16), producing zero shadow data for 4 days. More fundamentally: SMC follows institutional order flow — institutions accumulate during retail fear. Filtering longs in fear contradicts the system's thesis. The ML model will learn when fear matters with more nuance than a binary gate.

**Expected impact:** Shadow pipeline resumes collecting data in fear conditions. ML training data grows faster and includes fear-regime examples for the model to learn from.

### 2026-04-06 — Shadow Quality Filters (Feature Importance Analysis)
**What changed:**
- New `SHADOW_FEAR_LONG_GATE=25`: rejects long setups in shadow when F&G < 25. Data: longs 5.9% WR (2/34) vs shorts 20% WR (3/15) in extreme fear.
- New `SHADOW_MIN_HOUR_UTC=11`: skips shadow setups before 11 UTC. Data: 0% WR (0/23) pre-11 UTC vs 19% WR post-11 UTC.
- Both env-var overridable. Applied inside shadow path only (live path unaffected).

**Why:** Feature importance analysis (Cohen's d) on 49 resolved shadow trades identified direction and hour_of_day as the two strongest predictors of outcome. Filtering these reduces noise in ML training data while keeping informative short setups.

**Expected impact:** Fewer but higher-quality shadow trades. Estimated ~37% WR on remaining setups (vs 8.5% unfiltered). Cleaner ML dataset for future model training.

### 2026-03-31 — Shadow Position Redis Persistence
**What changed:**
- `ShadowMonitor` now persists active positions to Redis (`qf:bot:shadow_positions`, 48h TTL)
- Positions restored on startup; expired positions (>36h) are pruned during load
- Save points: after add, after fill, after resolve. Fire-and-forget (Redis failure never blocks pipeline)

**Why:** Shadow positions were in-memory only. On bot restart, `_positions` was wiped and the in-memory dedup allowed re-tracking of identical setups. This caused 17 duplicate trades for the same OB on 2026-03-30 (7× XRP, 6× SOL, 4× LINK — same entry/SL, all filled+stopped in the same candle).

**Expected impact:** No more duplicate shadow tracking after restarts. Cleaner ML data.

### 2026-03-31 — Orderbook Depth Confirmation + Regime Gate Fix
**What changed:**
- New `fetch_orderbook_depth()` in exchange_client.py — fetches 20-level L2 orderbook with raw (price, size_usd) levels
- New `_enrich_with_ob_depth()` in strategy service — analyzes orderbook liquidity around OB zone for all swing setups (A/B/F/G)
- Dynamic search zone: `max(OB body size, ATR) × 1.5` — scales per pair and volatility
- Measures depth ratio (supporting/opposing) and concentration (largest level / total)
- Confluence `ob_depth_confirmed` added when ratio ≥ 1.0 AND concentration ≥ 0.2
- ML features: `ob_depth_ratio`, `ob_depth_concentration`, `ob_depth_confirmed`, `geometry_adjusted`, `geometry_cascade_rank`
- **R:R floating point fix**: `guardrails.py` now uses `rr < min_rr - 1e-9` to prevent rejecting R:R 2.00 as "below 2.0"
- **Regime gate moved after shadow path**: Shadow setups now collect ML data during extreme fear (F&G < 10). Live setups still blocked. Previously shadow was also blocked, losing data in the most interesting market conditions.

**Why:** OB detection uses historical candles but never validated against real-time liquidity. Now the bot checks if institutional orders actually exist at the detected zone. Not a hard gate — just bonus confirmation for ML to evaluate over time.

### 2026-03-31 — Geometry Cascade (Dynamic Entry/SL Selection)
**What changed:**
- New `_cascade_geometry()` in `strategy_service/setups.py` — tries 3 entry depths × 2 SL candidates (OB wick + ATR floor) per setup before killing for bad R:R
- Integrated into swing setups A, B, F, G. Quick setups unchanged.
- ATR SL floor now evaluated as cascade candidate (was post-processing in service.py). Removed 4 `_apply_atr_sl_floor()` calls.
- Early exit at R:R ≥ 3.0 (no need to check remaining combos)
- Cascade metadata in confluences: `geometry_adjusted_N` for ML tracking
- `GEOMETRY_CASCADE_ENABLED=true` (env var override), `GEOMETRY_CASCADE_EARLY_EXIT_RR=3.0`
- ML_FEATURE_VERSION bumped to 9

**Why:** Bot was rejecting valid setups because fixed entry depth + OB wick SL produced R:R below minimum. Position sizer guarantees fixed dollar risk regardless of geometry, so exploring alternative structural levels costs nothing. 9 new tests added.

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
- Default: only `setup_f` executes live. Shadow mode setups: `setup_a`, `setup_b`, `setup_c`, `setup_d_choch`, `setup_d_bos`, `setup_e`. Setup G removed from shadow (04-02): 0/4 WR, breaker blocks are structurally weak. Setup H removed from shadow (03-30) pending pullback redesign.
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
