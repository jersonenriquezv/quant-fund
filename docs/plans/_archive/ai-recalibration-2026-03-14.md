# AI Filter Recalibration Plan

## What
Re-enable Claude AI filter only when ML data proves it can identify losing trades before entry. Three phases: offline analysis → shadow mode → selective re-enable.

## Why
AI v1 destroyed $5,454 of value (97→54 trades, 51.5%→44.4% WR). AI v2 prompt was fixed but never validated. Setup A had 89.6% approval rate (rubber stamp). All setups now bypassed. Re-enabling without data = repeating v1 mistake.

## Current State (2026-03-14)
- **AI fully bypassed** since ~March 10. Zero Claude API calls.
- **ML data collection live** via `ml_setups` table (~40 features per setup, outcomes tracked).
- **Bot live** since March 6 with ~$108 capital. Likely <20 trades so far.
- **Active setups:** Setup A (AI_BYPASS_SETUP_TYPES) + Setup D_choch (QUICK_SETUP_TYPES).
- **Prompt v2** exists in `ai_service/prompt_builder.py` (scoring rubric, unavailable data = neutral).

## Prerequisites

**Minimum 100 resolved trades in `ml_setups`** with `outcome_type` in ('filled_tp', 'filled_sl', 'filled_trailing', 'filled_timeout').

Monitoring query:
```sql
SELECT outcome_type, COUNT(*), AVG(pnl_pct), SUM(pnl_usd)
FROM ml_setups WHERE outcome_type LIKE 'filled_%'
GROUP BY outcome_type;
```

At ~8 trades/week, expect to reach 100 around **May-June 2026**.

## Steps

### Phase 1: Offline Analysis (0 risk, $0)
> When: 100 trades reached

1. **Export ml_setups** to DataFrame → `scripts/analyze_ml_features.py` (NEW)
   - Label: `is_winner = outcome_type in ('filled_tp', 'filled_trailing')`
   - Compare feature distributions: winners vs losers
   - Key features: `confluence_count`, `ob_volume_ratio`, `entry_distance_pct`, `pd_aligned`, `has_liquidity_sweep`, `cvd_aligned`, `funding_rate`, `buy_dominance`, `htf_bias`
   - Decision tree (scikit-learn, max_depth=3) for top splits
   - Done when: report shows "X% of losers share [pattern], filtering improves PnL by $Y"

2. **Decision gate:** If no identifiable loser pattern found → AI stays bypassed permanently. No amount of prompt engineering helps if losers look identical to winners in feature space.

### Phase 2: Shadow Mode (~$0.50/month Claude API)
> When: Phase 1 finds identifiable patterns

3. **Add shadow evaluation** → `main.py` + `config/settings.py`
   - New setting: `AI_SHADOW_MODE: bool = False`
   - After AI bypass block, fire-and-forget Claude evaluation
   - Log to `ai_decisions` table with `shadow=True` flag (or new `ai_shadow_decisions` table)
   - Never blocks or modifies trade pipeline
   - Done when: shadow decisions logged alongside live trades

4. **Run shadow for 50+ trades** (4-8 weeks)

5. **Analyze shadow results:**
   ```sql
   SELECT s.outcome_type, COUNT(*), AVG(s.pnl_pct), SUM(s.pnl_usd)
   FROM ml_setups s
   JOIN ai_decisions d ON d.shadow = TRUE
       AND ABS(EXTRACT(EPOCH FROM d.created_at) - s.timestamp/1000) < 60
   WHERE d.approved = FALSE AND s.outcome_type LIKE 'filled_%'
   GROUP BY s.outcome_type;
   ```
   - If rejected trades had net negative PnL → proceed to Phase 3
   - If Claude rubber-stamps (>85%) or rejects winners → revise prompt, repeat shadow

### Phase 3: Selective Re-enable (Setup A only)
> When: Shadow validates Claude adds value

6. **Remove `setup_a` from `AI_BYPASS_SETUP_TYPES`** → `config/settings.py`
   - Keep `setup_b` bypassed (disabled anyway)
   - Keep `QUICK_SETUP_TYPES` unchanged (D variants never go through AI)
   - Done when: Setup A trades pass through Claude live

7. **Kill switch:** If WR drops >5pp below bypass-period WR within 20 trades → revert to bypass

### Prompt v3 (informed by Phase 1 data)

8. **Encode specific losing patterns** found in data into prompt
   - Example: "ob_volume_ratio < 1.3 AND confluence_count <= 2 = low quality"
   - Remove scoring dimensions with zero predictive value
   - Tighten/loosen decision boundary based on actual outcome data
   - Done when: prompt reflects data-driven criteria, not generic SMC doctrine

## Success Metrics

| Metric | AI Adds Value If... | AI Destroys Value If... |
|--------|-------------------|----------------------|
| Win Rate | WR(AI) >= WR(bypass) - 3pp | WR(AI) < WR(bypass) - 5pp |
| Profit Factor | PF(AI) > PF(bypass) | PF(AI) < 1.0 |
| Approval Rate | 50-80% (meaningful filter) | >85% (rubber stamp) or <30% (too restrictive) |
| Net PnL | PnL(AI) > PnL(bypass) × 0.9 | PnL(AI) < PnL(bypass) × 0.7 |
| Filtered trades | Net negative PnL (mostly losers) | Net positive PnL (filtering winners) |

## Timeline

| When | What | Risk | Cost |
|------|------|------|------|
| Now → May 2026 | Keep bypass, collect ML data | Zero | $0 |
| ~100 trades | Phase 1: Offline analysis | Zero | $0 |
| If patterns found | Phase 2: Shadow mode 4-8 weeks | Zero (fire-and-forget) | ~$0.50/month |
| If shadow validates | Phase 3: Re-enable Setup A | Low (kill switch) | ~$0.50/month |
| If no patterns | Stay bypassed permanently | Zero | $0 |

## Risks

| Risk | Mitigation |
|------|-----------|
| Insufficient trades by May | Lower threshold to 75 if distribution is clear enough |
| Shadow mode adds latency | Fire-and-forget async — does not block pipeline |
| Prompt v3 still rubber-stamps | If >85% approval after data-driven prompt, AI is fundamentally unsuited for this filter role |
| Overfitting to small sample | Use walk-forward: train on first 70 trades, validate on last 30 |

## Out of Scope
- AI for Setup D (quick setups — latency matters more than filtering)
- AI for position sizing or SL/TP adjustments (no backtested track record)
- Training a custom ML model to replace Claude (premature — need 500+ trades minimum)
- Real-time feature engineering changes (ML_FEATURE_VERSION handles segmentation)
