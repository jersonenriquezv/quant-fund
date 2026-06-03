# Runbook: Nitro → Hetzner EU VPS migration

Operational, copy-paste companion to `docs/plans/vps-migration-2026-06-03.md`. Execute
top to bottom. Bot is **shadow-only** (`ENABLED_SETUPS=[]`) → no live orders at risk; a
misconfig can only cost data. **Region = EU, NOT US** (OKX geo-blocks US IPs).

Conventions: `NITRO$` = run on the current home server, `VPS$` = run on the new box.

---

## Phase 0 — Inventory ✅ (captured 2026-06-03)
- **Secrets — THREE env files to migrate** (all gitignored, copy verbatim):
  1. `./.env` (repo root, 6 keys) — **used by docker compose**: `GRAFANA_ADMIN_PASSWORD`,
     `POSTGRES_PASSWORD`, `REDIS_PASSWORD`, `BYBIT_API_KEY/SECRET`, `BYBIT_TESTNET`.
  2. `config/.env` (40 keys) — used by the Python app (OKX, Telegram, Anthropic, risk knobs…).
  3. `dashboard/web/.env.local` — `NEXT_PUBLIC_DASHBOARD_API_KEY` (frontend).
  - Confirm `OKX_SANDBOX=false` + `ENABLED_SETUPS=[]` carried over (shadow-only).
  - `EXPERIMENT_ID` lives as a settings.py default (not env) → carries over with the repo
    automatically; no action. (Same for `GRAFANA_ADMIN_PASSWORD` — it IS in `./.env`,
    earlier "missing" note was wrong, only `config/.env` had been checked.)
- **Baseline row counts to match post-migration:** candles 528,629 · ml_setups 10,355 ·
  trades 43 · bybit_executions 200 · bybit_closed_pnl 62 · bybit_trade_annotations 53 ·
  ai_decisions 534 · campaigns 5 · manual_trades 6. DB 241 MB.
- **Redis (106 keys):** `qf:bot:shadow_positions` MUST survive (shadow state restored on
  first tick via deferred-restore, PR #66) + `qf:bot:htf_bias` + `qf:bot:last_candle_ts:*`.
- **Pre-migration safety dump:** `NITRO$ bash scripts/backup.sh` → `backups/<stamp>/`.

---

## Phase 1 — Provision + harden VPS (definitive)
1. Hetzner Cloud → new server: **CX22** (2 vCPU / 4 GB / 40 GB), **EU** location
   (Nuremberg/Helsinki), Ubuntu 24.04, add your SSH key.
2. Harden:
   ```
   VPS$ sudo apt update && sudo apt -y upgrade
   VPS$ sudo apt -y install fail2ban ufw
   VPS$ sudo sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin no/;s/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
   VPS$ sudo systemctl restart ssh
   VPS$ sudo ufw default deny incoming && sudo ufw default allow outgoing
   VPS$ sudo ufw limit OpenSSH && sudo ufw enable
   ```
3. Docker + compose:
   ```
   VPS$ curl -fsSL https://get.docker.com | sudo sh
   VPS$ sudo usermod -aG docker $USER   # re-login after
   ```
4. Tailscale (keeps dashboard/grafana private — never expose 3000/8000/3001 publicly):
   ```
   VPS$ curl -fsSL https://tailscale.com/install.sh | sudo sh && sudo tailscale up
   VPS$ sudo ufw allow in on tailscale0    # dashboard/grafana reachable only over the tailnet
   ```
   Record the VPS tailscale IP.
**Gate:** key-only SSH, `ufw status` active, `docker run hello-world` ok, tailscale IP pings.

---

## Phase 2 — Stand up data services (no cutover yet)
```
VPS$ git clone https://github.com/jersonenriquezv/quant-fund.git && cd quant-fund
# Copy ALL THREE env files over tailscale (NOT git — they're gitignored):
NITRO$ scp .env <vps>:~/quant-fund/.env
NITRO$ scp config/.env <vps>:~/quant-fund/config/.env
NITRO$ scp dashboard/web/.env.local <vps>:~/quant-fund/dashboard/web/.env.local
VPS$ docker compose up -d postgres redis      # data services only; bot/api/web/grafana stay down
```
**OKX EU-IP sanity check — do this BEFORE committing to cutover.** One signed REST call
from the VPS; if it 401/403s on geo, stop and reconsider region:
```
VPS$ docker compose run --rm bot python -c "import ccxt,os; \
ex=ccxt.okx({'apiKey':os.environ['OKX_API_KEY'],'secret':os.environ['OKX_SECRET'],'password':os.environ['OKX_PASSPHRASE']}); \
print(ex.fetch_balance()['info']['code'])"   # expect '0'
```
**Gate:** postgres+redis healthy; OKX call returns code `0` (not a geo/auth block).

---

## Phase 3 — Data migration
```
# PostgreSQL — restore the Phase 0 safety dump (or take a fresh one) into the VPS:
NITRO$ scp backups/<stamp>/postgres.dump <vps>:~/quant-fund/restore.dump
VPS$ cat restore.dump | docker exec -i quant-fund-postgres-1 \
       pg_restore -U jer -d quant_fund --clean --if-exists --no-owner
# Redis — load the snapshot (container must be stopped to swap the RDB):
NITRO$ scp backups/<stamp>/redis.rdb <vps>:~/quant-fund/redis.rdb
VPS$ docker compose stop redis
VPS$ docker cp redis.rdb quant-fund-redis-1:/data/dump.rdb
VPS$ docker compose start redis
```
Verify counts match Phase 0 baseline:
```
VPS$ docker exec quant-fund-postgres-1 psql -U jer -d quant_fund -c \
  "select (select count(*) from candles) candles, (select count(*) from ml_setups) ml, (select count(*) from trades) trades;"
VPS$ docker exec quant-fund-redis-1 redis-cli -a "$REDIS_PASSWORD" EXISTS qf:bot:shadow_positions  # expect 1
```
**Gate:** VPS row counts == baseline; `qf:bot:shadow_positions` present.

---

## Phase 4 — Zero-gap cutover (overlap; net at new place day 1)
**Only ONE bot may hold the Telegram token + shadow state.** Order is strict:
```
VPS$ docker compose up -d --build api web grafana     # everything EXCEPT bot
NITRO$ docker compose stop bot                          # release Telegram + shadow ownership
# Final delta (seconds-small window; candles also self-backfill on VPS bot start):
NITRO$ bash scripts/backup.sh
NITRO$ scp backups/<newest>/postgres.dump <vps>:~/quant-fund/delta.dump
VPS$ cat delta.dump | docker exec -i quant-fund-postgres-1 pg_restore -U jer -d quant_fund --data-only --no-owner --disable-triggers 2>/dev/null || true
VPS$ docker compose up -d --build bot                   # VPS bot takes over
```
**Gate:** VPS bot RUNNING, Nitro bot STOPPED, single Telegram owner.

---

## Phase 5 — Verify (9-step deploy checklist)
Run the standard post-deploy checks (see memory `reference_deploy_verification`):
```
VPS$ curl -s localhost:8000/api/health | jq           # sandbox flag, PG+Redis ok
VPS$ docker compose logs --tail=80 bot | grep -iE "warmup|setup|shadow|error"
```
Confirm: candle freshness < 5 min, expected setups loaded, shadow restored from Redis,
Telegram alive (a startup ping), no warmup hang, dashboard+grafana reachable via tailscale,
new `ml_setups` rows appearing.
**Gate:** all checks pass; new shadow rows advancing.

---

## Phase 6 — Decommission + backups (definitive)
```
NITRO$ docker compose stop          # keep volumes as cold rollback ≥5 days; do NOT prune
VPS$ crontab -e
#   0 4 * * *  cd ~/quant-fund && bash scripts/backup.sh >> logs/backup.log 2>&1
```
Docs (`/doc-update`): update OPERATIONS.md (new host + tailscale IP + threat model),
SYSTEM_BASELINE (server change + changelog), and the memory regulatory note
(mark "migrate at $1k+" → DONE).
**Gate:** one successful cron backup; docs updated.

---

## Rollback
Nitro stack intact (stopped) ≥5 days. To revert: `VPS$ docker compose stop bot` (avoid
Telegram conflict) → `NITRO$ docker compose up -d bot`. Nitro DB is the pre-cutover state;
re-sync the delta if needed.
