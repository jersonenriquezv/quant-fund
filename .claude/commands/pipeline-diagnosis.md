Pipeline diagnosis — trace WHY the bot isn't executing more trades. Follows the setup through every gate.

This is the most important diagnostic when the bot is running but trade frequency is low.

## Context: Two Pipelines

The bot runs TWO parallel pipelines. Understand this before diagnosing:

1. **Live pipeline** (setup_f only): Detection → Dedup → AI bypass → Risk → Execution → Fill
2. **Shadow pipeline** (setup_a, b, c, d_choch, d_bos, e): Detection → Shadow dedup → Shadow filters → Theoretical tracking (no real orders)

Shadow outcomes (shadow_sl, shadow_dedup, shadow_hour_filtered, shadow_fear_long_filtered, shadow_no_fill) are EXPECTED — they are paper trading for ML data collection, NOT pipeline failures.

Disabled setups (setup_b live, setup_d_bos live, setup_g everywhere, setup_h everywhere) produce ZERO output. If logs show "detected but disabled", that's correct — do NOT recommend enabling them without data.

## Steps

Run ALL in parallel. Use `docker compose exec postgres psql -U jer -d quant_fund -t -c` for SQL (psql not on host).

1. **Live pipeline funnel (setup_f only, last 7 days):**
```sql
docker compose exec postgres psql -U jer -d quant_fund -t -c "
SELECT outcome_type, count(*) AS n,
  round(100.0 * count(*) / sum(count(*)) OVER(), 1) AS pct
FROM ml_setups
WHERE created_at > NOW() - INTERVAL '7 days'
  AND setup_type = 'setup_f'
GROUP BY outcome_type ORDER BY n DESC"
```

2. **Shadow pipeline summary (all shadow setups, last 7 days):**
```sql
docker compose exec postgres psql -U jer -d quant_fund -t -c "
SELECT setup_type, outcome_type, count(*) AS n
FROM ml_setups
WHERE created_at > NOW() - INTERVAL '7 days'
  AND setup_type != 'setup_f'
GROUP BY setup_type, outcome_type ORDER BY setup_type, n DESC"
```

3. **Shadow win rate (resolved only):**
```sql
docker compose exec postgres psql -U jer -d quant_fund -t -c "
SELECT setup_type,
  count(*) FILTER (WHERE outcome_type LIKE 'shadow_tp%') AS tp,
  count(*) FILTER (WHERE outcome_type = 'shadow_sl') AS sl,
  count(*) FILTER (WHERE outcome_type = 'shadow_no_fill') AS no_fill,
  count(*) FILTER (WHERE outcome_type IS NULL) AS pending,
  round(100.0 * count(*) FILTER (WHERE outcome_type LIKE 'shadow_tp%') /
    NULLIF(count(*) FILTER (WHERE outcome_type IN ('shadow_sl','shadow_tp1','shadow_tp2','shadow_trailing')), 0), 1) AS wr_pct
FROM ml_setups
WHERE created_at > NOW() - INTERVAL '14 days'
  AND setup_type != 'setup_f'
  AND outcome_type NOT IN ('shadow_dedup', 'shadow_hour_filtered', 'shadow_fear_long_filtered', 'shadow_risk_rejected', 'data_blocked')
GROUP BY setup_type ORDER BY setup_type"
```

4. **Detection rate by day (last 7 days):**
```sql
docker compose exec postgres psql -U jer -d quant_fund -t -c "
SELECT created_at::date AS day, setup_type, count(*)
FROM ml_setups
WHERE created_at > NOW() - INTERVAL '7 days'
GROUP BY day, setup_type ORDER BY day, count(*) DESC"
```

5. **Risk rejection reasons (live pipeline only):**
```bash
grep -E "Risk rejected|risk.*rejected|guardrail|RiskApproval.*approved=False" logs/main_$(date +%Y-%m-%d).log 2>/dev/null | tail -20
grep -E "Risk rejected|risk.*rejected|guardrail|RiskApproval.*approved=False" logs/main_$(date -d 'yesterday' +%Y-%m-%d).log 2>/dev/null | tail -20
```

6. **Unfilled live orders:**
```sql
docker compose exec postgres psql -U jer -d quant_fund -t -c "
SELECT outcome_type, setup_type, count(*),
  round(avg(entry_distance_pct)::numeric * 100, 3) AS avg_entry_dist_pct
FROM ml_setups
WHERE outcome_type = 'unfilled_timeout'
  AND created_at > NOW() - INTERVAL '7 days'
GROUP BY outcome_type, setup_type"
```

7. **Last 24h bot activity:**
```bash
echo "Setups detected:"; grep -c "Setup detected\|setup_type\|TradeSetup" logs/main_$(date +%Y-%m-%d).log 2>/dev/null || echo "0"
echo "Executions:"; grep -c "Executing\|execute_trade\|Placing" logs/main_$(date +%Y-%m-%d).log 2>/dev/null || echo "0"
echo "Candles confirmed:"; grep -c "WebSocket.*confirmed\|candle.*confirmed" logs/main_$(date +%Y-%m-%d).log 2>/dev/null || echo "0"
echo "Disabled detected:"; grep -c "detected but disabled" logs/main_$(date +%Y-%m-%d).log 2>/dev/null || echo "0"
```

8. **Dedup cache hits:**
```bash
grep -i "dedup\|already_evaluated\|cache hit\|skipping.*recent" logs/main_$(date +%Y-%m-%d).log 2>/dev/null | tail -10
```

Do NOT read source files.

## Analysis Framework

### Live Pipeline (setup_f)
Only setup_f goes through the full live pipeline. Diagnose it separately:
- How many setup_f detected this week?
- How many passed risk? How many filled? How many hit TP vs SL?
- If 0 detected: market conditions don't favor OB retests (low volatility, no BOS)
- If detected but unfilled: entry distance too far (check avg_entry_dist_pct)
- If filled but all SL: strategy geometry issue (SL too tight, targets wrong)

### Shadow Pipeline (data collection)
Shadow setups are NOT a problem to fix — they're collecting ML training data. Evaluate shadow HEALTH:
- Are shadow setups being detected? (if 0: detection logic issue)
- Shadow dedup rate: <40% is healthy, >60% means setups are stale/persistent
- Shadow quality filters (hour, fear): are they working? (should filter ~20-30%)
- Shadow WR: track trend over time. Current baseline from 04-02 audit was ~8.7%
- Pending (NULL outcome): still being tracked, will resolve

### Disabled Setups
- setup_g: removed from shadow 04-02 (0/4 WR, breaker blocks structurally weak)
- setup_h: removed from shadow 03-30 (11% WR live, 0/12 shadow, chases impulse tips)
- setup_b (live): disabled 03-18 (setup_f is strictly better)
- setup_d_bos (live): disabled (20-33% WR)
- Do NOT recommend re-enabling these without new evidence

## Output Format

```
## Pipeline Diagnosis — [date]

### Live Pipeline (setup_f)
Detected: N (7d) | Risk passed: N | Filled: N | TP: N | SL: N | Unfilled: N
Bottleneck: [none / low detection / unfilled / risk rejected / all SL]
[If bottleneck exists: specific cause + recommendation]

### Shadow Pipeline (ML data collection)
Total: N (7d) | Resolved: N | Pending: N | Dedup: N | Filtered: N
Shadow WR: X% (N TP / M filled) — [improving / stable / declining vs baseline 8.7%]
[Flag if shadow detection dropped to 0 or dedup >60%]

### Bot Health (24h)
Candles: ~N | Detections: N | Disabled-but-firing: N
[Flag if candles=0 (WebSocket down) or detections=0 for >24h]

### Assessment
[1-3 sentences: Is the bot healthy? Is setup_f getting opportunities? Is shadow data flowing?]
[Only recommend config changes if backed by data from this diagnosis]
```

Keep under 25 lines. Separate live vs shadow clearly. Do NOT treat shadow outcomes as problems.
