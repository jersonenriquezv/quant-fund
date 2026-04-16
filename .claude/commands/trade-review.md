Post-mortem analysis of recent closed trades. Diagnose WHY trades won or lost.

## Known Context (DO NOT repeat as findings)

These are already known and acted upon — do NOT flag them as discoveries:
- **setup_h**: Disabled since 03-30. 10.7% WR, impulse chaser. ALL setup_h trades are historical dead weight.
- **setup_g**: Disabled since 04-02. 0/4 WR, breaker blocks weak.
- **setup_b (live)**: Disabled since 03-18. setup_f is strictly better.
- **setup_d_bos (live)**: Disabled. 20-33% WR.
- **orphaned_restart**: Was an infrastructure bug from early days. Already fixed. Don't re-flag old occurrences.
- **setup_f sample size**: We know N is small. Only flag if N >= 30 and there's a real pattern.
- **setup_c, setup_e**: Removed from shadow 04-13 (no OB anchor, signal is now confluence booster).
- **Shadow filters removed 04-14**: hour, fear_long, risk rejection gate all removed from shadow. Shadow tracks everything now with 5min dedup.

**Rule: Only report NEW findings — things that changed since last review or patterns we haven't seen before.**

## Steps

1. Run this SQL query to get recent trades from **active setups only** (default last 20, or use $ARGUMENTS as count):

```sql
docker compose exec -T postgres psql -U jer -d quant_fund -t -A -F'|' -c "
SELECT id, pair, direction, setup_type, entry_price, sl_price, tp1_price, tp2_price,
  actual_entry, actual_exit, exit_reason, pnl_usd, pnl_pct,
  opened_at, closed_at,
  EXTRACT(EPOCH FROM (closed_at - opened_at))/60 AS duration_min
FROM trades WHERE status='closed'
  AND setup_type IN ('setup_f', 'setup_a', 'setup_b', 'setup_d_choch', 'setup_d_bos')
ORDER BY closed_at DESC LIMIT ${1:-20}
"
```

2. Run aggregate diagnostics in parallel — **active setups only, last 30 days**:

```sql
-- Exit reason distribution (active setups only)
docker compose exec -T postgres psql -U jer -d quant_fund -t -c "
SELECT exit_reason, count(*), round(sum(pnl_usd)::numeric, 2) as total_pnl,
  round(avg(pnl_usd)::numeric, 4) as avg_pnl
FROM trades WHERE status='closed'
  AND setup_type IN ('setup_f', 'setup_a', 'setup_b', 'setup_d_choch', 'setup_d_bos')
  AND closed_at > NOW() - INTERVAL '30 days'
GROUP BY exit_reason ORDER BY count(*) DESC"

-- Per setup type performance (active setups, last 30 days)
docker compose exec -T postgres psql -U jer -d quant_fund -t -c "
SELECT setup_type, count(*) as n,
  count(*) FILTER (WHERE pnl_usd > 0) as wins,
  round(100.0 * count(*) FILTER (WHERE pnl_usd > 0) / NULLIF(count(*),0), 1) as wr_pct,
  round(sum(pnl_usd)::numeric, 2) as total_pnl,
  round(avg(CASE WHEN pnl_usd > 0 THEN pnl_usd END)::numeric, 4) as avg_win,
  round(avg(CASE WHEN pnl_usd <= 0 THEN pnl_usd END)::numeric, 4) as avg_loss
FROM trades WHERE status='closed'
  AND setup_type IN ('setup_f', 'setup_a', 'setup_b', 'setup_d_choch', 'setup_d_bos')
  AND closed_at > NOW() - INTERVAL '30 days'
GROUP BY setup_type ORDER BY n DESC"

-- Avg hold duration by exit reason (active setups only)
docker compose exec -T postgres psql -U jer -d quant_fund -t -c "
SELECT exit_reason,
  round(avg(EXTRACT(EPOCH FROM (closed_at - opened_at))/60)::numeric, 1) as avg_min,
  round(min(EXTRACT(EPOCH FROM (closed_at - opened_at))/60)::numeric, 1) as min_min,
  round(max(EXTRACT(EPOCH FROM (closed_at - opened_at))/60)::numeric, 1) as max_min
FROM trades WHERE status='closed' AND closed_at IS NOT NULL
  AND setup_type IN ('setup_f', 'setup_a', 'setup_b', 'setup_d_choch', 'setup_d_bos')
  AND closed_at > NOW() - INTERVAL '30 days'
GROUP BY exit_reason"

-- Compare last 7d vs prior 7d (trend detection)
docker compose exec -T postgres psql -U jer -d quant_fund -t -c "
SELECT
  CASE WHEN closed_at > NOW() - INTERVAL '7 days' THEN 'last_7d' ELSE 'prior_7d' END AS period,
  count(*) as n,
  count(*) FILTER (WHERE pnl_usd > 0) as wins,
  round(sum(pnl_usd)::numeric, 2) as total_pnl
FROM trades WHERE status='closed'
  AND setup_type IN ('setup_f', 'setup_a', 'setup_b', 'setup_d_choch', 'setup_d_bos')
  AND closed_at > NOW() - INTERVAL '14 days'
GROUP BY period ORDER BY period"
```

3. Check logs for guardian activity on the most recent trades:

```bash
grep -E "guardian_|Guardian" logs/main_$(date +%Y-%m-%d).log 2>/dev/null | tail -20
```

4. Do NOT read source code. Analyze the DATA only.

## Analysis Framework (apply these, not opinion)

For each losing pattern, diagnose using these quantitative lenses:

**A. Expectancy Check (López de Prado, AFML Ch. 10)**
```
E[PnL] = (WR × avg_win) - ((1-WR) × |avg_loss|)
Profit Factor = gross_wins / |gross_losses|
```
- PF < 1.0 = negative edge. PF < 0.5 = structurally broken.
- If sample < 30 trades per setup: state "N=X, insufficient for conclusions" and move on. Do NOT over-analyze tiny samples.

**B. Exit Classification**
- `sl`: Was SL distance realistic vs ATR? (SL < 1×ATR₁₅ₘ = noise stop)
- `timeout`: Was TP reachable? (TP > 2×ATR₁₅ₘ in max_duration = unrealistic target)
- `guardian_*`: Was guardian tightening or killing? Check if price later reached TP.

**C. Adverse Selection (AFML Ch. 5)**
- All trades same direction in a down-trending market = no directional edge.
- Entry at impulse completion = adverse selection (buying from informed sellers).
- Cluster of losses in same hour = regime-dependent, not edge.

**D. SL Geometry**
- Compute SL distance as % of price for each trade.
- Compare to 15m ATR. If SL < ATR: you are stopped by noise, not invalidation.
- Optimal SL = at structural level (OB/FVG extreme) ≥ 1×ATR from entry.

## Output Format

```
## Trade Review — [date range] (active setups only)

### Summary
Total: N trades | Wins: W (WR%) | PnL: $X.XX
Profit Factor: X.XX | Avg Win: $X.XX | Avg Loss: $X.XX
Expectancy: $X.XX per trade
[If N < 30: "⚠ Small sample — trends noted but not actionable yet"]

### Trend (7d vs prior 7d)
[1 line: improving / stable / declining + numbers]

### By Setup Type
| Setup | N | WR% | PnL | Avg Win | Avg Loss |
[table — active setups only]

### Exit Analysis
| Exit Reason | N | Avg Duration | Total PnL | Pattern |
[table]

### New Findings (if any)
1. [CRITICAL/WARNING] Description — evidence — recommendation
[Only list things NOT in the "Known Context" section above]

### Actionable Next Steps
- [Only if backed by N >= 30 data, otherwise: "Continue collecting data. N=X, need 30+ for conclusions."]
```

Keep output under 40 lines. No fluff. Data-driven verdicts only. Do NOT repeat known issues.
