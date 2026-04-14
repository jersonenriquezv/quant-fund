Quick status check. Run these commands in parallel and report a brief summary:

1. `docker compose ps --format "table {{.Name}}\t{{.Status}}"` — container health
2. `tail -5 logs/main_$(date +%Y-%m-%d).log 2>/dev/null` — last 5 log lines (errors? trades?)
3. `docker compose exec -T postgres psql -U jer -d quant_fund -t -c "SELECT COUNT(*), status FROM trades GROUP BY status"` — trade counts by status
4. `docker compose exec -T redis redis-cli PING` — Redis alive?
5. `grep -cE "ERROR|CRITICAL" logs/main_$(date +%Y-%m-%d).log 2>/dev/null || echo "0"` — error count today

## Output format

```
Bot: running/stopped (uptime from docker ps)
Containers: N/M up (list any down)
Redis: ok/down
Trades: X open, Y closed
Last log: [last meaningful line]
Errors: N today [show last error if any, or "none"]
```

Do NOT read source files. Do NOT run pytest. Keep it under 10 lines.
