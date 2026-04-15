Deploy the bot to production. Run these steps sequentially:

1. `source venv/bin/activate && python -m pytest tests/ -x -q 2>&1 | tail -5` — abort if any test fails
2. `docker compose up -d --build bot 2>&1` — rebuild and restart bot container
3. Wait 10 seconds, then `docker compose logs bot --tail=15 2>&1` — verify startup
4. `docker compose ps --format "table {{.Name}}\t{{.Status}}"` — confirm all containers healthy

## Output format

```
Tests: 747 passed (or ABORT: N failed — do not deploy)
Build: ok/failed
Bot: healthy/unhealthy
Containers: N/M up
Last log: [last meaningful line from bot]
```

Do NOT read source files. Do NOT modify code. If tests fail, stop and report which test failed.
