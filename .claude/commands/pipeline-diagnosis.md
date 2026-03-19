Pipeline diagnosis — trace WHY the bot isn't executing more trades. Follows the setup through every gate.

This is the most important diagnostic when the bot is running but trade frequency is low.

## Steps

Run ALL in parallel:

1. **Setup detection rate (last 7 days):**
```sql
psql -h localhost -U jer -d quant_fund -t -c "
SELECT created_at::date AS day, setup_type, count(*)
FROM ml_setups
WHERE created_at > NOW() - INTERVAL '7 days'
GROUP BY day, setup_type ORDER BY day, count(*) DESC"
```

2. **Full pipeline funnel — where do setups die?**
```sql
psql -h localhost -U jer -d quant_fund -t -c "
SELECT outcome_type, count(*) AS n,
  round(100.0 * count(*) / sum(count(*)) OVER(), 1) AS pct
FROM ml_setups
WHERE created_at > NOW() - INTERVAL '7 days'
GROUP BY outcome_type ORDER BY n DESC"
```

3. **Risk rejection reasons (if risk_rejected is high):**
```bash
grep -E "Risk rejected|risk.*rejected|guardrail|RiskApproval.*approved=False" logs/main_$(date +%Y-%m-%d).log 2>/dev/null | tail -20
grep -E "Risk rejected|risk.*rejected|guardrail|RiskApproval.*approved=False" logs/main_$(date -d 'yesterday' +%Y-%m-%d).log 2>/dev/null | tail -20
```

4. **Dedup cache hits (if deduped is high):**
```bash
grep -i "dedup\|already_evaluated\|cache hit\|skipping.*recent" logs/main_$(date +%Y-%m-%d).log 2>/dev/null | tail -10
```

5. **Unfilled orders (limit orders that expired):**
```sql
psql -h localhost -U jer -d quant_fund -t -c "
SELECT outcome_type, setup_type, count(*),
  round(avg(entry_distance_pct)::numeric * 100, 3) AS avg_entry_dist_pct
FROM ml_setups
WHERE outcome_type = 'unfilled_timeout'
  AND created_at > NOW() - INTERVAL '7 days'
GROUP BY outcome_type, setup_type"
```

6. **Last 24h bot activity — is it even detecting?**
```bash
grep -c "Setup detected\|setup_type\|TradeSetup" logs/main_$(date +%Y-%m-%d).log 2>/dev/null || echo "0"
grep -c "Executing\|execute_trade\|Placing" logs/main_$(date +%Y-%m-%d).log 2>/dev/null || echo "0"
grep -c "WebSocket.*confirmed\|candle.*confirmed" logs/main_$(date +%Y-%m-%d).log 2>/dev/null || echo "0"
```

7. **Current enabled setups + key thresholds from config:**
```bash
grep -E "ENABLED_SETUPS|MIN_ATR_PCT|MIN_TARGET_SPACE_R|OB_PROXIMITY_PCT|OB_MAX_DISTANCE_PCT|MAX_OPEN_POSITIONS|COOLDOWN_AFTER_LOSS" logs/main_$(date +%Y-%m-%d).log 2>/dev/null | head -10
```

Do NOT read source files.

## Analysis Framework — Pipeline Gates

The bot has 5 sequential gates. A setup must pass ALL of them:

```
Candle → [1. Strategy Detection] → [2. Dedup Cache] → [3. AI/Bypass] → [4. Risk Check] → [5. Execution] → Fill
```

**Gate 1: Strategy Detection**
- Is the bot receiving candles? (check WebSocket confirmed count)
- Are any setups being detected? (check setup detection count)
- If zero: market is in compression (ATR too low), or HTF bias is undefined (lateral market)
- Common bottleneck: `htf_bias == "undefined"` when market is lateral (~60% of time)

**Gate 2: Dedup Cache**
- Same setup re-evaluated within 1h TTL = deduped
- High dedup count = bot correctly avoiding re-evaluation (not a problem)
- Very high dedup (>50% of detections) = setups are persistent but not filling

**Gate 3: AI / Bypass**
- Currently ALL active setups bypass AI (synthetic approval)
- Should show 0 `ai_rejected` on recent data. If not: config bug.

**Gate 4: Risk Check**
- Most common rejection gate. Check logs for specific reason:
  - `max_positions`: Already 5+ open (but we see 0 open now — not this)
  - `daily_dd_limit`: Drawdown exceeded 5% (check recent losses)
  - `cooldown`: Loss within last 15 min
  - `max_trades_today`: Hit 10/day limit
  - `rr_too_low`: R:R below 1.2 (setup geometry problem)
  - `insufficient_capital`: Not enough margin

**Gate 5: Execution**
- Limit order placed but not filled within timeout (1h quick, 24h swing)
- High unfilled rate + high avg_entry_distance = entry levels too far from market
- This is the `unfilled_timeout` outcome

## Output Format

```
## Pipeline Diagnosis — [date]

### Activity (24h)
Candles received: ~N | Setups detected: N | Orders placed: N | Fills: N

### 7-Day Funnel
| Gate | Outcome | N | % | Bottleneck? |
|------|---------|---|---|-------------|
| Detection | total setups | N | 100% | |
| Dedup | deduped | N | X% | [yes/no] |
| AI | ai_rejected | N | X% | [should be 0] |
| Risk | risk_rejected | N | X% | [MAIN if >30%] |
| Execution | unfilled_timeout | N | X% | |
| Fill | filled_* | N | X% | [TARGET] |

### Bottleneck Analysis
[Identify the #1 gate killing trade frequency]
[Specific reason from logs if risk_rejected]
[Entry distance if unfilled_timeout]

### Recommendation
[1-3 specific actions to increase fill rate]
[e.g., "Relax MIN_ATR_PCT from 0.35% to 0.25% — current ATR is 0.20%"]
[e.g., "Reduce COOLDOWN_AFTER_LOSS from 15m to 5m — blocking back-to-back setups"]
```

Keep under 30 lines. This is a DIAGNOSTIC, not a report.
