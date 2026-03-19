ML training data health check. Tracks feature collection progress toward model training.
Based on AFML (López de Prado) pipeline: feature importance → meta-labeling → bet sizing.

## Steps

Run ALL in parallel:

1. **Outcome distribution by feature version:**
```sql
psql -h localhost -U jer -d quant_fund -t -c "
SELECT feature_version, outcome_type, count(*)
FROM ml_setups
WHERE outcome_type IS NOT NULL
GROUP BY feature_version, outcome_type
ORDER BY feature_version DESC, count(*) DESC"
```

2. **Training-ready data (v4+ fills):**
```sql
psql -h localhost -U jer -d quant_fund -t -c "
SELECT outcome_type, count(*) AS n,
  round(avg(pnl_pct)::numeric, 4) AS avg_pnl,
  round(avg(EXTRACT(EPOCH FROM (resolved_at - created_at))/3600)::numeric, 1) AS avg_hours
FROM ml_setups
WHERE feature_version >= 4
  AND outcome_type IN ('filled_tp','filled_sl','filled_trailing','filled_timeout','filled_guardian')
GROUP BY outcome_type"
```

3. **Feature completeness (v4+, including v6 features):**
```sql
psql -h localhost -U jer -d quant_fund -t -c "
SELECT
  count(*) AS total,
  count(*) FILTER (WHERE has_funding) AS has_funding,
  count(*) FILTER (WHERE has_oi) AS has_oi,
  count(*) FILTER (WHERE has_cvd) AS has_cvd,
  count(*) FILTER (WHERE buy_dominance IS NOT NULL) AS has_buy_dom,
  count(*) FILTER (WHERE has_news) AS has_news,
  count(*) FILTER (WHERE has_whales) AS has_whales,
  count(*) FILTER (WHERE daily_vol IS NOT NULL) AS has_daily_vol,
  count(*) FILTER (WHERE atr_pct IS NOT NULL) AS has_atr,
  count(*) FILTER (WHERE oi_delta_pct IS NOT NULL AND oi_delta_pct != 0) AS has_oi_delta
FROM ml_setups WHERE feature_version >= 4"
```

4. **Daily collection rate (14 days) + v6 tracking:**
```sql
psql -h localhost -U jer -d quant_fund -t -c "
SELECT created_at::date AS day,
  count(*) AS total,
  count(*) FILTER (WHERE feature_version >= 6) AS v6,
  count(*) FILTER (WHERE outcome_type LIKE 'filled_%') AS fills
FROM ml_setups
WHERE created_at > NOW() - INTERVAL '14 days'
GROUP BY day ORDER BY day"
```

5. **Rejection funnel — WHY aren't setups becoming trades?**
```sql
psql -h localhost -U jer -d quant_fund -t -c "
SELECT outcome_type, count(*) AS n,
  round(100.0 * count(*) / sum(count(*)) OVER(), 1) AS pct
FROM ml_setups WHERE feature_version >= 4
GROUP BY outcome_type ORDER BY n DESC"
```

6. **Sample concurrency for uniqueness estimation (AFML Ch.4):**
```sql
psql -h localhost -U jer -d quant_fund -t -c "
SELECT count(*) AS total_fills,
  count(*) FILTER (WHERE trade_duration_ms > 0) AS has_duration,
  round(avg(trade_duration_ms/3600000.0)::numeric, 1) AS avg_hours,
  round(max(trade_duration_ms/3600000.0)::numeric, 1) AS max_hours
FROM ml_setups
WHERE outcome_type LIKE 'filled_%' AND feature_version >= 4"
```

Do NOT read source files.

## Analysis Framework (AFML Pipeline)

**Phase 1: Feature Importance (Current Goal)**
- Need: 50+ labeled fill outcomes (tp + sl + trailing + guardian + timeout)
- Method: Purged k-fold CV with MDI/MDA/SFI (AFML Ch.8)
- Script: `python scripts/feature_importance.py --label barrier`
- Goal: Identify which features predict trade quality
- Status: Check if fill_outcomes >= 50

**Phase 2: Meta-Labeling (Next)**
- Need: 200+ labeled outcomes + Phase 1 results
- Method: Train binary classifier on primary model's predictions (AFML Ch.3.6)
- Goal: "Should we take this trade?" Replaces bypassed AI filter.

**Phase 3: Bet Sizing (Future)**
- Need: Calibrated meta-labeling model
- Method: `getSignal()` from AFML Ch.10 — probability → position size via `2*Phi(z)-1`
- Goal: Size proportional to confidence, not fixed $20

**Data quality checks:**
- Feature completeness < 80% on critical feature = pipeline bug
- `daily_vol` NULL on v6+ rows = `_get_daily_vol` not receiving candles — check recent_candles passed
- Zero v6 rows = bot not restarted with new code
- `buy_dominance` NULL when `has_cvd=true` = CVD calculation bug
- Ratio (deduped + rejected) > 70% = too many blocked setups — check rejection funnel
- Zero TP outcomes = strategy edge problem, not data

**Non-stationary features (AFML Ch.5) — DO NOT use for training:**
- Absolute prices → use `risk_distance_pct`, `entry_distance_pct`, `rr_ratio`
- `oi_usd` → use `oi_delta_pct`
- `cvd_5m/15m/1h` → use `buy_dominance`

**Triple-barrier labels (AFML Ch.3) — use with `--label barrier`:**
- `filled_tp/trailing` → +1 (upper barrier)
- `filled_sl` → -1 (lower barrier)
- `filled_timeout/guardian` → sign(pnl_pct) (vertical barrier)

## Output Format

```
## ML Data — [date]

### Collection Status
v6 rows: N | v4+ total: N
Fill outcomes (v4+): N / 50 needed for Phase 1
  TP: X | SL: X | Trail: X | Timeout: X | Guardian: X
Progress: [====>     ] XX%

### Rejection Funnel (v4+)
| Stage | N | % | Issue? |
[table — flag if rejections > 70%]

### Feature Completeness (v4+)
| Feature | Coverage | Status |
[table — flag any < 80%, especially daily_vol on v6+]

### Daily Rate (14d)
| Day | Total | v6 | Fills |
[table]

ETA Phase 1: ~N days at current fill rate
[or "BLOCKED — zero v6 data, restart bot"]

### Data Quality Flags
[any issues found]
```

Keep under 40 lines.
