# Backtest Results Tracker

## Key Runs

| Date | File | Profile | AI | Setups | Trades | WR | PnL | PF | Sharpe | DD | Notes |
|------|------|---------|-----|--------|--------|-----|------|-----|--------|-----|-------|
| 2026-03-10 | `175231_aggressive_60d` | aggressive | no | A+B+D+F | 97 | 51.5% | +$7,558 | 1.81 | 4.90 | 15.2% | **Baseline combinado** — B dominant (+$3,647), D strong (+$2,553) |
| 2026-03-10 | `203103_aggressive_60d_ai` | aggressive | v1 | A+B+D+F | 54 | 44.4% | +$2,104 | 1.45 | 3.44 | 15.2% | **AI v1** — Claude too restrictive on B (17.9% approval). Missing CVD treated as negative evidence. -$5,454 vs baseline |
| 2026-03-10 | `162420_aggressive_60d` | aggressive | no | D only | 56 | 42.9% | +$3,596 | 2.26 | 8.51 | 4.8% | D solo — best Sharpe, lowest DD |
| 2026-03-10 | `152909_aggressive_60d` | aggressive | no | B+F | 86 | 53.5% | +$6,324 | 1.86 | 4.19 | 12.7% | B+F only (before A/D enabled) |
| 2026-03-10 | `154307_aggressive_60d` | aggressive | no | A+B+F | 95 | 49.5% | +$4,538 | 1.53 | 3.14 | 16.9% | A+B+F (before D enabled) |
| 2026-03-15 | `baseline_pre_optuna_60d` | default | no | A+D_choch | 26 | 42.3% | +$123 | 1.05 | 0.63 | 6.2% | **Pre-Optuna baseline** (60d) |
| 2026-03-15 | `detail_1m_pre_optuna_60d` | default | no | A+D_choch | 26 | 46.2% | +$445 | 1.18 | 2.28 | 6.2% | **Timeframe-detail** (1m resolution) — 1 trade flipped SL→TP |
| 2026-03-15 | `optuna_best_trial8_30d` | **optuna** | no | A+D_choch | 17 | 58.8% | +$1,683 | **2.65** | 3.92 | 8.2% | **Optuna best (30d train)**. Walk-forward: test PF=3.07, baseline PF=0.88 |
| 2026-03-30 | `20260330_203441_30d` | default | no | all (pre-diag) | 104 | 36.5% | -$717 | 0.87 | -1.38 | 24.5% | **Pre-diagnostic baseline** — setup_h = 74 trades, -$1,144 |
| 2026-03-30 | `20260330_204311_30d` | default | no | A+B+D+F+G (post-diag) | 18 | 61.1% | +$885 | **2.63** | 9.30 | 2.1% | **Post-diagnostic** — H disabled, regime gate F&G<20, ATR SL floor 3×, structural confluence only |

## Optuna Optimization (2026-03-15)

20 trials, 30-day period, metric=profit_factor, walk-forward validated.

**Best params applied to production (Trial 8, PF=2.65):**
| Param | Before | After | Why |
|-------|--------|-------|-----|
| SETUP_A_ENTRY_PCT | 0.50 | 0.65 | Shallower entry → higher fill rate |
| SETUP_A_MAX_SWEEP_CHOCH_GAP | 40 | 45 | More temporal tolerance |
| OB_PROXIMITY_PCT | 0.008 | 0.007 | Tighter proximity |
| OB_MAX_DISTANCE_PCT | 0.08 | 0.04 | Reject distant OBs (biggest impact) |
| OB_MIN_VOLUME_RATIO | 1.2 | 1.3 | More selective |
| OB_MAX_AGE_HOURS | 72 | 84 | Longer OB lifespan |
| OB_MIN_BODY_PCT | 0.001 | 0.0015 | Filter micro-OBs |
| MIN_ATR_PCT | 0.0025 | 0.0045 | Skip dead markets |
| MIN_TARGET_SPACE_R | 1.2 | 1.4 | Require more room to target |

Walk-forward: test_optimized PF=3.07 vs test_baseline PF=0.88 → **NOT overfitting**.

## AI Calibration History

| Version | Approval Rate | Avg Conf | B Approval | PnL vs Baseline | Key Issue |
|---------|--------------|----------|------------|-----------------|-----------|
| v1 | 35.0% | 0.72 | 17.9% (131/731) | -$5,454 | Missing CVD = negative evidence. "When in doubt reject" too aggressive |
| v2 | pending | - | - | - | Prompt fix: unavailable data = neutral, stronger counter-trend support |

## Per-Setup Comparison (Baseline vs AI v1)

| Setup | Base Trades | Base WR | Base PnL | AI Trades | AI WR | AI PnL | AI Impact |
|-------|------------|---------|----------|-----------|-------|--------|-----------|
| A | 20 | 45.0% | -$395 | 20 | 50.0% | +$122 | +$517 (improved) |
| B | 51 | 49.0% | +$3,647 | 14 | 21.4% | -$1,028 | -$4,675 (destroyed) |
| D | 9 | 66.7% | +$2,553 | 12 | 58.3% | +$2,473 | -$80 (similar, bypasses AI) |
| F | 17 | 58.8% | +$1,753 | 8 | 50.0% | +$537 | -$1,216 (too filtered) |

## ML Feature Version Log

| Version | Date | Changes | Rationale |
|---------|------|---------|-----------|
| v1 | 2026-03-15 | Initial: fixed TP (2:1), legacy trailing (BE+tp1), MIN_RISK 0.2%, HTF campaigns OFF | Baseline data collection |
| v2 | 2026-03-17 | Progressive trailing ON, HTF campaigns ON, TP2 3:1, MIN_RISK 0.5% | v1 trades had micro SLs (0.2-0.4%) swept in 3-5 min, TP never hit (0 TPs out of ~10 filled trades). Switched to impulse-riding: wider SL filters noise, progressive trail lets winners run, HTF campaigns for multi-hour/day holds |

**v1→v2 data boundary:** All `ml_setups` rows with `feature_version=1` are v1 regime (tight SL/TP, legacy trailing). Rows with `feature_version=2` are v2 regime (wide SL, progressive trail, HTF). Do NOT mix for training — strategy behavior is fundamentally different.

## Prompt Changes Log

### v1 (original)
- "CVD is the strongest real-time signal — weigh it heavily"
- "When in doubt, reject. Capital preservation > opportunity capture."
- Counter-trend: "CAN be valid... Approve with moderate confidence if data supports it"

### v2 (2026-03-10)
- Added "CRITICAL — DATA AVAILABILITY" section: unavailable = neutral, not negative
- Setup quality is now PRIMARY factor (4+ confluences + high OB volume = approve)
- Counter-trend: "these are VALID setups... Approve if LTF structure is clear"
- Removed blanket "when in doubt reject" — replaced with "reject only when present data creates clear case AGAINST"
- Added Setup F to setup_names map
