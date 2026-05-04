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
| **Grafana** (:3001) | Tailscale mesh | Admin login (password in .env) | GRAFANA_ADMIN_PASSWORD required |

### Key Rotation Procedure

1. **OKX API keys**: Generate new keys in OKX console → update `config/.env` → `docker compose up -d --build bot`
2. **DASHBOARD_API_KEY**: Generate with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` → update `config/.env` + `dashboard/web/.env.local` → restart bot + rebuild frontend
3. **TELEGRAM_BOT_TOKEN**: Revoke via @BotFather → update `.env` → restart
4. **ANTHROPIC_API_KEY**: Rotate in Anthropic console → update `.env` → restart
5. **POSTGRES/REDIS passwords**: Update in `.env` + respective Docker configs → restart all services
6. **GRAFANA_ADMIN_PASSWORD**: Update in `config/.env` → `docker compose up -d grafana`

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
docker compose up -d --build web
```

### Rules

- **NEVER** use `sudo`, `nohup`, or `kill` to manage services — always Docker Compose
- **NEVER** commit directly to `main` — merge from `develop` via PR
- Verify health after deploy: `curl http://localhost:8000/api/health`
- Check Grafana for anomalies after deploy: http://100.120.181.11:3001

### Parallel Branch Work — git worktrees

Each repo can only have ONE branch checked out per working directory. Multiple Claude/terminal sessions opened on the same path share that branch. To work on two branches in parallel without `git checkout` thrashing, use git worktrees: each worktree is a sibling directory with its own checked-out branch, sharing the same `.git` history.

| Worktree | Branch | Use |
|---|---|---|
| `/home/jer/quant-fund` | active feature (e.g. `feat/scalp-shadow-signals`) | primary editor / bot deploy source |
| `/home/jer/quant-fund-engine1` | `feat/engine1-v1b-eth-short` | parallel feature in flight |
| `/home/jer/quant-fund/.claude/worktrees/agent-*` | scratch | spawned by Claude agents (auto-pruned when no changes) |

```bash
# Create
git worktree add /home/jer/quant-fund-NAME feat/branch-name

# List
git worktree list

# Remove (only if branch is merged or you're sure)
git worktree remove /home/jer/quant-fund-NAME
```

**Rules:**
- Bot deploy reads from `/home/jer/quant-fund` only — `docker compose up -d --build bot` builds whatever is checked out there. Worktrees do NOT auto-deploy.
- One worktree per branch (git refuses duplicate checkouts).
- PRs are remote-only — worktrees do not affect PR state on GitHub.
- Each Claude session's statusline shows the branch of ITS worktree, so sessions don't override each other.

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
3. Restore `config/.env` from secure backup (NOT in git) — includes GRAFANA_ADMIN_PASSWORD
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

### Current Schema Version: 21

| Version | Description | Date |
|---------|-------------|------|
| 1-5 | Base tables (candles, trades, ai_decisions, risk_events, bot_metrics, funding/OI/CVD history, campaigns, ml_setups) | Initial |
| 6 | ml_setups: daily_vol column | 2026-03 |
| 7 | ml_setups: shadow mode + risk check columns | 2026-03 |
| 8 | trades: setup_id + sizing columns, trade_rejections table | 2026-03 |
| 10 | ml_setups: volume profile features (POC/HVN/LVN) | 2026-04 |
| 13 | ml_setups: RSI + microstructure features | 2026-04 |
| 14 | ml_setups: orderbook, BTC correlation, volatility regime, session | 2026-04 |
| 15 | ml_setups: experiment_id column for freeze protocol | 2026-04 |
| 16 | ml_setups: WaveTrend + ADX/DI + Bollinger + Stochastic RSI | 2026-04 |
| 17 | ml_setups: shadow resolution candle trace | 2026-04 |
| 18 | trades: capital_at_trade snapshot | 2026-04 |
| 19 | ml_setups: regime_label categorical | 2026-04 |
| 20 | ml_setups: widen outcome_type to VARCHAR(50) (idempotent — prod already widened ad-hoc) | 2026-04 |
| 21 | ml_setups: Engine 1 lossless metric columns (engine1_impulse_atr_multiple, engine1_pullback_depth_pct, engine1_pullback_candle_count, engine1_entry_atr_distance) | 2026-04 |

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

## 5. Documentation Truth Checks

Operational docs are guarded by `scripts/check_docs_truth.py`. It verifies high-impact facts that must match code: setup status, ML feature version, selected risk/strategy constants, and schema migration version.

Run manually after config/schema/pipeline changes:
```bash
python3 scripts/check_docs_truth.py
```

Use `/doc-audit` when the checker fails or when docs feel stale. It fixes only proven drift, then reruns the checker.

The Claude pre-commit hook (`scripts/check-critical-commit.sh`) runs this check automatically when staged changes touch `config/settings.py`, `data_service/data_store.py`, `main.py`, `strategy_service/`, `risk_service/`, or `execution_service/`.

---

## 6. Monitoring Checklist

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

- [ ] `curl http://localhost:8000/api/health` returns `{"status": "ok", "postgres": true, "redis": true, "sandbox": false}`
- [ ] `docker compose logs --tail=20 bot` — no errors
- [ ] Grafana System Health dashboard — no gaps in metrics
- [ ] Telegram test alert received

### Emergency Trading Halt

Use when: flash crash, suspected key compromise, runaway trades, or any situation needing immediate position closure.

**Step 1 — Freeze execution (stop new trades, keep monitoring):**
```bash
# Telegram: send /emergency to the bot (2-step confirm)
# OR set env var and restart:
echo "TRADING_HALTED=true" >> config/.env
docker compose up -d --build bot
```

**Step 2 — Cancel open orders + close all positions:**
```bash
# Via Telegram /emergency command (preferred — handles both)
# OR manually via OKX:
#   1. Go to OKX Futures → open positions → "Close All"
#   2. Go to Open Orders → "Cancel All"
```

**Step 3 — Reconcile:**
```bash
# Check what the bot thinks vs what OKX has:
docker compose exec bot python3 -c "
from execution_service.service import ExecutionService
# Compare DB open trades vs exchange positions
"
# Or check DB directly:
docker compose exec postgres psql -U jer quant_fund -c \
  "SELECT pair, direction, status, entry_price, opened_at FROM trades WHERE status='open'"
```

**Step 4 — Snapshot for post-mortem:**
```bash
docker compose logs --since=1h bot > /tmp/emergency_$(date +%Y%m%d_%H%M).log
docker compose exec postgres pg_dump -U jer quant_fund > /tmp/emergency_backup_$(date +%Y%m%d_%H%M).sql
```

**Step 5 — Resume (when safe):**
```bash
# Remove the halt flag:
sed -i '/TRADING_HALTED/d' config/.env
docker compose up -d --build bot
docker compose logs -f bot  # verify normal operation
```

### Incident Response

1. **Bot crash loop**: Check logs, identify error, fix, redeploy
2. **Exchange API error**: Check OKX status page, verify API keys, check rate limits
3. **Unexpected trade**: Check `trades` table, `ai_decisions`, `risk_events` for that trade_id
4. **High drawdown alert**: Trigger emergency halt (above). Review positions before resuming

---

## 7. API Authentication

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
