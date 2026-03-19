Statistical edge analysis per setup type. Uses AFML-grade quantitative methods. Run this weekly or after every 20+ trades.

$ARGUMENTS: optional — "all" for full history, or number of days (default: 7)

## Steps

1. Run the comprehensive edge query:

```sql
psql -h localhost -U jer -d quant_fund -t -c "
WITH trade_stats AS (
  SELECT setup_type, direction, exit_reason,
    pnl_usd, pnl_pct,
    EXTRACT(EPOCH FROM (closed_at - opened_at))/3600 AS hold_hours,
    abs(actual_entry - sl_price) / actual_entry AS sl_distance_pct,
    abs(tp2_price - actual_entry) / actual_entry AS tp_distance_pct,
    CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END AS is_win
  FROM trades
  WHERE status='closed'
    AND closed_at > NOW() - INTERVAL '${1:-7} days'
)
SELECT
  setup_type,
  count(*) AS n,
  round(100.0 * avg(is_win), 1) AS wr_pct,
  round(sum(pnl_usd)::numeric, 2) AS total_pnl,
  round(avg(CASE WHEN is_win=1 THEN pnl_usd END)::numeric, 4) AS avg_win,
  round(avg(CASE WHEN is_win=0 THEN pnl_usd END)::numeric, 4) AS avg_loss,
  CASE WHEN sum(CASE WHEN pnl_usd<0 THEN abs(pnl_usd) END) > 0
    THEN round((sum(CASE WHEN pnl_usd>0 THEN pnl_usd END) / sum(CASE WHEN pnl_usd<0 THEN abs(pnl_usd) END))::numeric, 2)
    ELSE NULL END AS profit_factor,
  round(avg(sl_distance_pct)::numeric * 100, 2) AS avg_sl_pct,
  round(avg(tp_distance_pct)::numeric * 100, 2) AS avg_tp_pct,
  round(avg(hold_hours)::numeric, 1) AS avg_hold_h
FROM trade_stats
GROUP BY setup_type ORDER BY n DESC
"
```

2. Exit reason breakdown per setup:
```sql
psql -h localhost -U jer -d quant_fund -t -c "
SELECT setup_type, exit_reason, count(*),
  round(sum(pnl_usd)::numeric, 2)
FROM trades WHERE status='closed'
  AND closed_at > NOW() - INTERVAL '${1:-7} days'
GROUP BY setup_type, exit_reason
ORDER BY setup_type, count(*) DESC"
```

3. Directional bias check:
```sql
psql -h localhost -U jer -d quant_fund -t -c "
SELECT direction, count(*),
  round(100.0 * count(*) FILTER (WHERE pnl_usd > 0) / NULLIF(count(*),0), 1) AS wr,
  round(sum(pnl_usd)::numeric, 2)
FROM trades WHERE status='closed'
  AND closed_at > NOW() - INTERVAL '${1:-7} days'
GROUP BY direction"
```

4. Hourly performance (regime detection):
```sql
psql -h localhost -U jer -d quant_fund -t -c "
SELECT EXTRACT(HOUR FROM opened_at) AS hour,
  count(*), round(sum(pnl_usd)::numeric, 2),
  round(100.0 * count(*) FILTER (WHERE pnl_usd > 0) / NULLIF(count(*),0), 1) AS wr
FROM trades WHERE status='closed'
  AND closed_at > NOW() - INTERVAL '${1:-7} days'
GROUP BY hour ORDER BY hour"
```

Do NOT read source files.

## Analysis Framework

Apply these quantitative tests to the data:

**1. Statistical Significance (Binomial Test)**
- For each setup, compute 95% confidence interval on WR:
  `CI = WR ± 1.96 × sqrt(WR × (1-WR) / N)`
- If CI lower bound < breakeven WR (= 1/(1+RR)), setup has NO proven edge.
- N < 30: insufficient sample. Flag, do not conclude.

**2. Profit Factor Interpretation (AFML Ch. 10)**
- PF > 1.5: Potential edge. Continue trading.
- PF 1.0–1.5: Marginal. May be noise. Need more data.
- PF < 1.0: Negative edge. Disable or redesign.
- PF < 0.5: Structurally broken. Disable immediately.

**3. Sharpe-like Metric**
```
Trade Sharpe = mean(pnl_per_trade) / std(pnl_per_trade) × sqrt(trades_per_week)
```
- < 0: Losing money on average.
- 0–1: Below institutional threshold.
- > 1: Decent. > 2: Strong.

**4. Adverse Selection Detection**
- All trades in one direction + market moved opposite = directional bias bug.
- Cluster of losses in short window = regime sensitivity.
- SL distance < 1% on crypto = noise stop (15m ATR on BTC ≈ 0.3–0.8%).

**5. Kelly Criterion (Position Sizing Diagnostic — AFML Ch. 10)**
```
f* = (WR × avg_win - (1-WR) × |avg_loss|) / avg_win
```
- f* < 0: Negative expectancy. Do not trade this setup.
- f* > 0: Optimal fraction to risk per trade.
- Current sizing: flat $20. When meta-labeling is ready, f* informs bet sizing via `getSignal()`.

**6. PSR — Probabilistic Sharpe Ratio (AFML Ch. 14)**
If N >= 20 trades for a setup:
```
SR = mean(pnl) / std(pnl) × sqrt(trades_per_week)
PSR = Phi((SR - 0) × sqrt(N-1) / sqrt(1 - skew×SR + (kurt-1)/4 × SR²))
```
- PSR < 0.95 = insufficient evidence the setup has edge.
- Report PSR when N >= 20. Below that, flag "INSUFFICIENT DATA — no statistical conclusion".
- MinTRL: How many trades needed? `1 + (1 - skew×SR + (kurt-1)/4×SR²) × (1.96/SR)²`

**7. Rejection funnel connection**
Also check: how many setups were DETECTED but never became trades?
```sql
psql -h localhost -U jer -d quant_fund -t -c "
SELECT setup_type, outcome_type, count(*)
FROM ml_setups
WHERE created_at > NOW() - INTERVAL '${1:-7} days'
GROUP BY setup_type, outcome_type ORDER BY setup_type, count(*) DESC"
```
A setup with 50 detections, 45 risk_rejected, 3 unfilled, 2 fills = 4% conversion rate. The edge analysis only sees the 2 fills — the 45 rejections are invisible but may contain signal.

## Output Format

```
## Edge Audit — Last N days

### Per Setup
| Setup | N | WR% [95% CI] | PF | Avg W/L | Sharpe | Kelly f* | Verdict |
[table]

### Exit Pathology
| Setup | SL% | TP% | Timeout% | Guardian% | Orphan% |

### Directional Bias
[long vs short WR + PnL]

### Time-of-Day
[any hour clusters with losses]

### Verdicts
- [DISABLE/KEEP/NEEDS DATA] Setup X — reason (evidence)

### Recommendations
[specific actionable changes]
```

Keep under 50 lines. Numbers, not narratives.
