Post-mortem analysis of recent closed trades. Diagnose WHY trades won or lost.

## Steps

1. Run this SQL query to get the last N trades (default 20, or use $ARGUMENTS as count):

```sql
psql -h localhost -U jer -d quant_fund -t -A -F'|' -c "
SELECT id, pair, direction, setup_type, entry_price, sl_price, tp1_price, tp2_price,
  actual_entry, actual_exit, exit_reason, pnl_usd, pnl_pct,
  opened_at, closed_at,
  EXTRACT(EPOCH FROM (closed_at - opened_at))/60 AS duration_min
FROM trades WHERE status='closed'
ORDER BY closed_at DESC LIMIT ${1:-20}
"
```

2. Run aggregate diagnostics in parallel:

```sql
-- Exit reason distribution
psql -h localhost -U jer -d quant_fund -t -c "
SELECT exit_reason, count(*), round(sum(pnl_usd)::numeric, 2) as total_pnl,
  round(avg(pnl_usd)::numeric, 4) as avg_pnl
FROM trades WHERE status='closed' GROUP BY exit_reason ORDER BY count(*) DESC"

-- Per setup type performance
psql -h localhost -U jer -d quant_fund -t -c "
SELECT setup_type, count(*) as n,
  count(*) FILTER (WHERE pnl_usd > 0) as wins,
  round(100.0 * count(*) FILTER (WHERE pnl_usd > 0) / NULLIF(count(*),0), 1) as wr_pct,
  round(sum(pnl_usd)::numeric, 2) as total_pnl,
  round(avg(CASE WHEN pnl_usd > 0 THEN pnl_usd END)::numeric, 4) as avg_win,
  round(avg(CASE WHEN pnl_usd <= 0 THEN pnl_usd END)::numeric, 4) as avg_loss
FROM trades WHERE status='closed' GROUP BY setup_type ORDER BY n DESC"

-- Avg hold duration by exit reason
psql -h localhost -U jer -d quant_fund -t -c "
SELECT exit_reason,
  round(avg(EXTRACT(EPOCH FROM (closed_at - opened_at))/60)::numeric, 1) as avg_min,
  round(min(EXTRACT(EPOCH FROM (closed_at - opened_at))/60)::numeric, 1) as min_min,
  round(max(EXTRACT(EPOCH FROM (closed_at - opened_at))/60)::numeric, 1) as max_min
FROM trades WHERE status='closed' AND closed_at IS NOT NULL GROUP BY exit_reason"
```

3. Check logs for guardian activity on the most recent trades:

```bash
grep -E "guardian_|Guardian" logs/execution_service_$(date +%Y-%m-%d).log 2>/dev/null | tail -20
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
- If sample < 30 trades per setup: flag insufficient sample. Use binomial CI for WR.

**B. Exit Classification**
- `sl`: Was SL distance realistic vs ATR? (SL < 1×ATR₁₅ₘ = noise stop)
- `timeout`: Was TP reachable? (TP > 2×ATR₁₅ₘ in max_duration = unrealistic target)
- `guardian_*`: Was guardian tightening or killing? Check if price later reached TP.
- `orphaned_restart`: Lost tracking = infrastructure bug, not strategy.

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
## Trade Review — [date range]

### Summary
Total: N trades | Wins: W (WR%) | PnL: $X.XX
Profit Factor: X.XX | Avg Win: $X.XX | Avg Loss: $X.XX
Expectancy: $X.XX per trade

### By Setup Type
| Setup | N | WR% | PnL | PF | Avg Win | Avg Loss | Verdict |
[table]

### Exit Analysis
| Exit Reason | N | Avg Duration | Total PnL | Pattern |
[table]

### Critical Findings
1. [CRITICAL/WARNING] Description — evidence — recommendation
2. ...

### Actionable Next Steps
- [specific parameter changes or disables with rationale]
```

Keep output under 60 lines. No fluff. Data-driven verdicts only.
