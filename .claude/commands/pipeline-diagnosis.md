Pipeline diagnosis — trace WHY the bot isn't executing more trades. Follows the setup through every gate.

This is the most important diagnostic when the bot is running but trade frequency is low.

## Known Context (DO NOT repeat as findings)

These are already known and acted upon — do NOT flag or recommend action on:
- **setup_h**: Removed from shadow 04-13. 0/13 WR. Impulse chaser. Dead.
- **setup_g**: Removed from shadow 04-02. 0/4 WR. Breaker blocks weak. Dead.
- **setup_c, setup_e**: Removed from shadow 04-13. No OB anchor — signal is now a confluence booster.
- **setup_b (live)**: Disabled since 03-18. setup_f is strictly better.
- **setup_d_bos (live)**: Disabled. 20-33% WR.
- **orphaned_restart**: Infrastructure bug from early days. Already fixed.
- **setup_f small sample**: We know. Only flag patterns if N >= 15.
- **"detected but disabled" log entries**: Expected behavior, not a bug.
- **Shadow filters removed (04-14)**: hour_filter, fear_long_filter, risk rejection gate ALL removed. Shadow now tracks everything. Only remaining filter is shadow_dedup (unfilled + similar entry ±1%). Pipeline dedup reduced to 5min for shadow.
- **Pre-04-14 shadow baseline**: 190 detections → 13 resolved in 14d (93% filtered). This is the OLD state — compare new data against this.

**Rule: Only surface NEW information — changes from the last diagnosis, emerging patterns, or things requiring action.**

## Context: Two Pipelines

1. **Live pipeline** (setup_f only): Detection → Dedup → AI bypass → Risk → Execution → Fill
2. **Shadow pipeline** (setup_a, setup_b, setup_d_choch, setup_d_bos): Detection → Shadow dedup → Shadow filters → Theoretical tracking

## Steps

Run ALL in parallel. Use `docker compose exec -T postgres psql -U jer -d quant_fund -t -c` for SQL.

1. **Live pipeline funnel (setup_f only, last 7 days):**
```sql
docker compose exec -T postgres psql -U jer -d quant_fund -t -c "
SELECT outcome_type, count(*) AS n,
  round(100.0 * count(*) / sum(count(*)) OVER(), 1) AS pct
FROM ml_setups
WHERE created_at > NOW() - INTERVAL '7 days'
  AND setup_type = 'setup_f'
GROUP BY outcome_type ORDER BY n DESC"
```

2. **Shadow pipeline summary (active shadow setups only, last 7 days):**
```sql
docker compose exec -T postgres psql -U jer -d quant_fund -t -c "
SELECT setup_type, outcome_type, count(*) AS n
FROM ml_setups
WHERE created_at > NOW() - INTERVAL '7 days'
  AND setup_type IN ('setup_a', 'setup_b', 'setup_d_choch', 'setup_d_bos')
GROUP BY setup_type, outcome_type ORDER BY setup_type, n DESC"
```

3. **Shadow win rate (resolved only, 14 days):**
```sql
docker compose exec -T postgres psql -U jer -d quant_fund -t -c "
SELECT setup_type,
  count(*) FILTER (WHERE outcome_type LIKE 'shadow_tp%' OR outcome_type = 'shadow_trailing') AS tp,
  count(*) FILTER (WHERE outcome_type = 'shadow_sl') AS sl,
  count(*) FILTER (WHERE outcome_type = 'shadow_no_fill') AS no_fill,
  count(*) FILTER (WHERE outcome_type IS NULL) AS pending,
  round(100.0 * count(*) FILTER (WHERE outcome_type LIKE 'shadow_tp%' OR outcome_type = 'shadow_trailing') /
    NULLIF(count(*) FILTER (WHERE outcome_type IN ('shadow_sl','shadow_tp1','shadow_tp2','shadow_trailing')), 0), 1) AS wr_pct
FROM ml_setups
WHERE created_at > NOW() - INTERVAL '14 days'
  AND setup_type IN ('setup_a', 'setup_b', 'setup_d_choch', 'setup_d_bos')
  AND outcome_type NOT IN ('ai_rejected', 'data_blocked', 'filled_orphaned', 'replaced', 'risk_rejected', 'shadow_dedup', 'shadow_direction_filtered', 'shadow_orphaned', 'trading_halted', 'unfilled_timeout')
GROUP BY setup_type ORDER BY setup_type"
```

4. **Detection rate by day (last 7 days, active setups only):**
```sql
docker compose exec -T postgres psql -U jer -d quant_fund -t -c "
SELECT created_at::date AS day, setup_type, count(*)
FROM ml_setups
WHERE created_at > NOW() - INTERVAL '7 days'
  AND setup_type IN ('setup_f', 'setup_a', 'setup_b', 'setup_d_choch', 'setup_d_bos')
GROUP BY day, setup_type ORDER BY day, count(*) DESC"
```

5. **Risk rejection reasons (live pipeline only, last 2 days):**
```bash
grep -E "Risk rejected|risk.*rejected|guardrail|RiskApproval.*approved=False" logs/main_$(date +%Y-%m-%d).log 2>/dev/null | tail -20
grep -E "Risk rejected|risk.*rejected|guardrail|RiskApproval.*approved=False" logs/main_$(date -d 'yesterday' +%Y-%m-%d).log 2>/dev/null | tail -20
```

6. **Unfilled live orders (7 days):**
```sql
docker compose exec -T postgres psql -U jer -d quant_fund -t -c "
SELECT setup_type, count(*),
  round(avg(entry_distance_pct)::numeric * 100, 3) AS avg_entry_dist_pct
FROM ml_setups
WHERE outcome_type = 'unfilled_timeout'
  AND created_at > NOW() - INTERVAL '7 days'
GROUP BY setup_type"
```

7. **Last 24h bot activity:**
```bash
echo "Candles confirmed:"; grep -c "WebSocket.*confirmed\|candle.*confirmed" logs/main_$(date +%Y-%m-%d).log 2>/dev/null || echo "0"
echo "Setup_f detected:"; grep -c "setup_f" logs/main_$(date +%Y-%m-%d).log 2>/dev/null || echo "0"
echo "Executions:"; grep -c "Executing\|execute_trade\|Placing" logs/main_$(date +%Y-%m-%d).log 2>/dev/null || echo "0"
```

8. **Dedup cache hits (last 10):**
```bash
grep -i "dedup\|already_evaluated\|cache hit\|skipping.*recent" logs/main_$(date +%Y-%m-%d).log 2>/dev/null | tail -10
```

Do NOT read source files.

## Analysis Framework

### Live Pipeline (setup_f)
- If 0 detected: market conditions don't favor OB retests (normal in low-vol / ranging)
- If detected but risk rejected: check specific rejection reasons
- If detected but unfilled: entry distance too far (check avg_entry_dist_pct)
- If filled but all SL: geometry issue

### Shadow Pipeline (data collection)
Evaluate HEALTH, not outcomes:
- Are setups being detected? (if 0 for >48h: possible detection logic issue)
- Shadow dedup rate: <40% healthy, >60% stale
- Shadow WR trend vs baseline 8.7% (from 04-02 audit)
- Only flag dramatic changes, not normal variation

## Output Format

```
## Pipeline Diagnosis — [date]

### Live Pipeline (setup_f)
Detected: N (7d) | Risk passed: N | Filled: N | TP: N | SL: N | Unfilled: N
Bottleneck: [none / low detection / unfilled / risk rejected / all SL]
[Only if NEW bottleneck or changed: specific cause + recommendation]
[If 0 detected and market is ranging: "No setup_f opportunities — normal for current conditions"]

### Shadow Pipeline Health
Total: N (7d) | Resolved: N | Pending: N
Shadow WR: X% — [vs 8.7% baseline: improving / stable / declining]
[Only flag if detection dropped to 0 or dedup >60% or WR changed >10pp from baseline]

### Bot Health (24h)
Candles: ~N | setup_f detections: N | Executions: N
[Only flag if candles=0 or something is broken]

### Assessment
[1-3 sentences focused on WHAT CHANGED or WHAT'S NEW. Not a repeat of known state.]
[If nothing changed: "Bot healthy, market quiet. No action needed."]
```

Keep under 20 lines. Do NOT repeat known issues. Do NOT recommend re-enabling disabled setups.
