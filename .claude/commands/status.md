Quick status check. Run these commands in parallel and report a brief summary:

1. `docker compose ps --format "table {{.Name}}\t{{.Status}}"` — container health
2. `pgrep -af "python main.py"` — bot process alive?
3. `tail -5 logs/main_$(date +%Y-%m-%d).log 2>/dev/null` — last 5 log lines (errors? trades?)
4. `psql -h localhost -U jer -d quant_fund -t -c "SELECT COUNT(*), status FROM trades GROUP BY status"` — trade counts by status
5. `redis-cli PING` — Redis alive?

## Output format

```
Bot: running/stopped
Containers: N/M up (list any down)
Redis: ok/down
Recent trades: X open, Y closed
Last log: [last meaningful line]
Errors: [any ERROR/CRITICAL from last 20 log lines, or "none"]
```

Do NOT read source files. Do NOT run pytest. Keep it under 10 lines.
