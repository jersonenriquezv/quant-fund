Market regime detection. Determines if current conditions favor the bot's strategies.

$ARGUMENTS: optional pair (default: BTC/USDT)

## Steps

Run ALL in parallel:

1. **Price action regime** (last 24h from DB):
```sql
psql -h localhost -U jer -d quant_fund -t -c "
WITH recent AS (
  SELECT close, high, low, volume, timestamp
  FROM candles
  WHERE pair='${1:-BTC/USDT}' AND timeframe='15m'
    AND timestamp > EXTRACT(EPOCH FROM NOW() - INTERVAL '24 hours') * 1000
  ORDER BY timestamp
)
SELECT
  count(*) AS candles,
  round(((max(close) - min(close)) / min(close) * 100)::numeric, 2) AS range_pct,
  round(((last(close, timestamp) - first(close, timestamp)) / first(close, timestamp) * 100)::numeric, 2) AS net_move_pct,
  round(avg((high-low)/low * 100)::numeric, 3) AS avg_candle_range_pct,
  round(stddev((high-low)/low * 100)::numeric, 3) AS volatility_std
FROM recent"
```

2. **ATR proxy** (avg 15m candle range):
```sql
psql -h localhost -U jer -d quant_fund -t -c "
SELECT
  round(avg((high-low)/low * 100)::numeric, 3) AS atr_15m_pct,
  round(avg((high-low)/low * 100) FILTER (WHERE timestamp > EXTRACT(EPOCH FROM NOW() - INTERVAL '4 hours') * 1000)::numeric, 3) AS atr_4h_pct
FROM candles
WHERE pair='${1:-BTC/USDT}' AND timeframe='15m'
  AND timestamp > EXTRACT(EPOCH FROM NOW() - INTERVAL '24 hours') * 1000"
```

3. **Funding + OI snapshot** (from Redis):
```bash
redis-cli GET "funding:${1:-BTC/USDT}" 2>/dev/null | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print(f'Funding: {d.get(\"rate\",\"?\")}')" 2>/dev/null || echo "Funding: unavailable"
redis-cli GET "oi:${1:-BTC/USDT}" 2>/dev/null | head -1 || echo "OI: unavailable"
```

4. **Recent trade performance in this regime**:
```sql
psql -h localhost -U jer -d quant_fund -t -c "
SELECT count(*), round(sum(pnl_usd)::numeric, 2),
  round(100.0 * count(*) FILTER (WHERE pnl_usd > 0) / NULLIF(count(*),0), 1) AS wr
FROM trades WHERE status='closed' AND closed_at > NOW() - INTERVAL '24 hours'"
```

Do NOT read source files.

## Regime Classification (Marcos López de Prado, AFML Ch. 2)

**Trending**: net_move > 1.5% AND net_move/range > 0.5 (directional efficiency > 50%)
- Bot edge: HIGH for Setup A (sweep → reversal → OB), moderate for H (momentum continuation)

**Mean-reverting**: range > 2% AND net_move/range < 0.3 (chop, wicks dominate)
- Bot edge: LOW. Most setups need structure breaks which get faded in MR regimes.
- SL gets hit by wicks. Impulse fades before TP.

**Low-vol compression**: avg_candle_range < 0.15% AND range < 1%
- Bot edge: NONE. No setups trigger (ATR filter, impulse filter block correctly).
- Wait for breakout.

**High-vol expansion**: avg_candle_range > 0.5% OR volatility_std > 0.3
- Bot edge: MIXED. Good for Setup A (sweeps happen), bad for H (SL too tight for vol).
- Adjust: widen SL or skip H.

## Output Format

```
## Regime — [pair] [HH:MM]

| Metric | 24h | 4h |
|---|---|---|
| ATR (15m) | X.XX% | X.XX% |
| Range | X.XX% | — |
| Net Move | X.XX% | — |
| Efficiency | X.XX | — |

Regime: [TRENDING/MEAN-REVERTING/COMPRESSION/EXPANSION]
Bot edge: [HIGH/LOW/NONE/MIXED] — [reason]

Funding: X.XXXX% | OI: [snapshot]
24h trades: N | WR: X% | PnL: $X.XX

[1-line recommendation: trade/wait/adjust params]
```

Keep under 20 lines.
