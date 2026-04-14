# Operational Spec

Lightweight operational reference for the quant fund bot. Covers security, deploy, recovery, and monitoring.

---

## 1. Threat Model

### Attack Surface

| Layer | Exposure | Protection | Residual Risk |
|-------|----------|------------|---------------|
| **OKX API** | Internet (API key + secret + passphrase) | IP not geo-blocked (Mexican-registered account) | Key leak = full account access. Mitigate: OKX sub-account with trade-only permissions, no withdrawal |
| **Dashboard API** (:8000) | Tailscale mesh only (100.120.181.11) | API key auth on write endpoints (Bearer token) | Compromised Tailscale device = read access to trade data. Write ops require API key |
| **Dashboard UI** (:3000) | Tailscale mesh only | Same API key for write ops via frontend | Read-only without key |
| **Manual trading page** (/manual) | Tailscale mesh only | API key stored in localStorage (set via `?key=` URL param) | Same as dashboard |
| **PostgreSQL** (:5432) | localhost only (Docker network) | Password auth | Container escape = DB access |
| **Redis** (:6379) | localhost only (Docker network) | Password auth | Same |
| **SSH** | Local network (192.168.1.x) | Key-based auth | Physical network access |
| **Grafana** (:3001) | Tailscale mesh | Anonymous viewer (read-only) | No write risk |

### Key Rotation Procedure

1. **OKX API keys**: Generate new keys in OKX console → update `config/.env` → `docker compose up -d --build bot`
2. **DASHBOARD_API_KEY**: Generate with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` → update `config/.env` + `dashboard/web/.env.local` → restart bot + rebuild frontend
3. **TELEGRAM_BOT_TOKEN**: Revoke via @BotFather → update `.env` → restart
4. **ANTHROPIC_API_KEY**: Rotate in Anthropic console → update `.env` → restart
5. **POSTGRES/REDIS passwords**: Update in `.env` + respective Docker configs → restart all services

### What to Do If Keys Leak

1. **Immediately** rotate the leaked key (see above)
2. If OKX key leaked: check open positions, cancel unfamiliar orders, rotate to new sub-account
3. If DASHBOARD_API_KEY leaked: rotate key, check `manual_trades` table for unauthorized entries
4. Review `git log` for accidental commits containing secrets

---

## 2. Deploy Runbook

### Standard Deploy (code changes)

```bash
cd ~/quant-fund
git pull origin develop          # or merge feature branch
docker compose up -d --build bot # rebuilds + restarts bot container only
docker compose logs -f bot       # verify startup, watch for errors
```

### Full Stack Deploy (infra changes)

```bash
cd ~/quant-fund
docker compose down              # graceful stop all services
docker compose up -d --build     # rebuild everything
docker compose ps                # verify all services healthy
```

### Frontend-Only Deploy

```bash
cd ~/quant-fund/dashboard/web
npm run build
# If running via Docker:
docker compose up -d --build dashboard
```

### Rules

- **NEVER** use `sudo`, `nohup`, or `kill` to manage services — always Docker Compose
- **NEVER** commit directly to `main` — merge from `develop` via PR
- Verify health after deploy: `curl http://localhost:8000/api/health`
- Check Grafana for anomalies after deploy: http://100.120.181.11:3001

---

## 3. Recovery Procedures

### Bot Not Trading (Pipeline Stuck)

1. Check logs: `docker compose logs --tail=100 bot`
2. Check health: `curl http://localhost:8000/api/health`
3. Common causes:
   - WebSocket disconnected → bot auto-reconnects (circuit breaker)
   - Redis down → candle cache misses, no market snapshots
   - PostgreSQL down → ML logging fails but pipeline continues
   - All setups in shadow mode → no live execution (by design)
4. If stuck: `docker compose restart bot`

### Database Recovery

**PostgreSQL backup** (manual — run periodically):
```bash
docker compose exec postgres pg_dump -U jer quant_fund > backup_$(date +%Y%m%d).sql
```

**Restore from backup:**
```bash
docker compose exec -i postgres psql -U jer quant_fund < backup_YYYYMMDD.sql
```

**Schema version check:**
```sql
SELECT * FROM schema_version ORDER BY version;
```

### Redis Data Loss

Redis data is ephemeral cache — bot repopulates on next candle cycle. No recovery needed. Portfolio state (`qf:bot:*` keys) rebuilds from PostgreSQL on startup.

### Full Server Recovery (Acer Nitro 5 dies)

1. New server: install Ubuntu 24.04, Docker, Docker Compose
2. Clone repo: `git clone <repo> ~/quant-fund`
3. Restore `config/.env` from secure backup (NOT in git)
4. Restore `dashboard/web/.env.local`
5. Restore PostgreSQL backup
6. `docker compose up -d --build`
7. Verify: health endpoint, Grafana, Telegram alerts

---

## 4. Schema Management

### How It Works

- Schema is managed via `data_service/data_store.py` → `_create_tables()`
- On startup, bot creates all tables with `CREATE TABLE IF NOT EXISTS`
- Migrations use `ALTER TABLE ADD COLUMN IF NOT EXISTS` (idempotent)
- Version tracking via `schema_version` table — each migration records its version number

### Current Schema Version: 10

| Version | Description | Date |
|---------|-------------|------|
| 1-5 | Base tables (candles, trades, ai_decisions, risk_events, bot_metrics, funding/OI/CVD history, campaigns, ml_setups) | Initial |
| 6 | ml_setups: daily_vol column | 2026-03 |
| 7 | ml_setups: shadow mode + risk check columns | 2026-03 |
| 8 | trades: setup_id + sizing columns, trade_rejections table | 2026-03 |
| 10 | ml_setups: volume profile features (POC/HVN/LVN) | 2026-04 |

### Adding a New Migration

1. In `_create_tables()`, add a new version block:
   ```python
   if current_version < N:
       cur.execute("ALTER TABLE ... ADD COLUMN IF NOT EXISTS ...")
       self._apply_migration(cur, N, "description")
   ```
2. Increment `ML_FEATURE_VERSION` in `settings.py` if adding ML features
3. Update this table above
4. Deploy — migration runs automatically on startup

### Rollback

Manual only. Rollback DDL (DROP COLUMN) is destructive — always verify before executing:
```sql
-- Example: remove a column added by mistake
ALTER TABLE ml_setups DROP COLUMN IF EXISTS bad_column;
DELETE FROM schema_version WHERE version = N;
```

---

## 5. Monitoring Checklist

### Daily

- [ ] Check Grafana Trading Performance dashboard — any unexpected PnL?
- [ ] Verify bot is running: `docker compose ps`
- [ ] Check Telegram — alerts flowing?
- [ ] Review open positions (dashboard or `/trade-monitor`)

### Weekly

- [ ] Run `/edge-audit` — statistical edge per setup type
- [ ] Run `/trade-review` — post-mortem on closed trades
- [ ] Check shadow mode outcomes — any setup ready to graduate?
- [ ] Review `ml_setups` count: `SELECT feature_version, COUNT(*) FROM ml_setups GROUP BY 1`
- [ ] PostgreSQL backup: `pg_dump`

### On Deploy

- [ ] `curl http://localhost:8000/api/health` returns `{"status": "ok"}`
- [ ] `docker compose logs --tail=20 bot` — no errors
- [ ] Grafana System Health dashboard — no gaps in metrics
- [ ] Telegram test alert received

### Incident Response

1. **Bot crash loop**: Check logs, identify error, fix, redeploy
2. **Exchange API error**: Check OKX status page, verify API keys, check rate limits
3. **Unexpected trade**: Check `trades` table, `ai_decisions`, `risk_events` for that trade_id
4. **High drawdown alert**: Review positions, consider manual close via dashboard

---

## 6. API Authentication

### Dashboard API Key

Write endpoints (POST/PATCH/DELETE/PUT) require a Bearer token:

```
Authorization: Bearer <DASHBOARD_API_KEY>
```

**Protected endpoints:**
- `POST /api/manual/trades` — create trade
- `PATCH /api/manual/trades/{id}` — update trade
- `DELETE /api/manual/trades/{id}` — delete trade
- `POST /api/manual/trades/{id}/partial-close` — partial close
- `PUT /api/manual/balances/{pair}` — set balance
- `POST /api/trades/{pair}/cancel` — cancel position

**Read-only endpoints** (no auth required — behind Tailscale):
- All GET endpoints (market, candles, trades list, stats, whales, etc.)

### Frontend Setup

- **Next.js**: API key in `dashboard/web/.env.local` as `NEXT_PUBLIC_DASHBOARD_API_KEY`
- **Manual HTML page**: Set key via URL `http://host:8000/manual?key=<KEY>` (saved to localStorage)
