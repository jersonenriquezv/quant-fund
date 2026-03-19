Live position monitor + recent trade activity. Token-optimized snapshot.

## Steps

Run ALL of these in parallel:

1. **Open positions** (from DB + exchange):
```sql
psql -h localhost -U jer -d quant_fund -t -A -F'|' -c "
SELECT id, pair, direction, setup_type, entry_price, sl_price, tp2_price, actual_entry,
  opened_at, EXTRACT(EPOCH FROM (NOW() - opened_at))/60 AS age_min
FROM trades WHERE status='open' ORDER BY opened_at"
```

2. **Last 5 closed trades** (quick pulse):
```sql
psql -h localhost -U jer -d quant_fund -t -A -F'|' -c "
SELECT pair, direction, setup_type, exit_reason, round(pnl_usd::numeric, 3),
  round(EXTRACT(EPOCH FROM (closed_at - opened_at))/60::numeric, 1) AS dur_min,
  to_char(closed_at, 'HH24:MI') AS closed
FROM trades WHERE status='closed' ORDER BY closed_at DESC LIMIT 5"
```

3. **Today's P&L**:
```sql
psql -h localhost -U jer -d quant_fund -t -c "
SELECT count(*) as trades,
  count(*) FILTER (WHERE pnl_usd > 0) as wins,
  round(sum(pnl_usd)::numeric, 3) as pnl,
  round(sum(CASE WHEN pnl_usd < 0 THEN pnl_usd ELSE 0 END)::numeric, 3) as drawdown
FROM trades WHERE status='closed' AND closed_at::date = CURRENT_DATE"
```

4. **Guardian activity today**:
```bash
grep -c "guardian_" logs/execution_service_$(date +%Y-%m-%d).log 2>/dev/null || echo "0"
grep -c "guardian_" logs/main_$(date +%Y-%m-%d).log 2>/dev/null || echo "0"
```

5. **Last log line** (bot alive?):
```bash
tail -3 logs/main_$(date +%Y-%m-%d).log 2>/dev/null
```

Do NOT read source files. Do NOT run pytest.

## Output Format

```
## Monitor — [HH:MM]

### Open Positions
| # | Pair | Dir | Setup | Entry | SL | TP | Age |
[or "No open positions"]

### Last 5 Trades
| Pair | Dir | Setup | Exit | PnL | Dur | Time |
[table]

### Today
Trades: N | Wins: W | PnL: $X.XX | DD: $X.XX
Guardian closes: N

### Bot
[alive/dead + last meaningful log line]
```

Keep output under 30 lines.
