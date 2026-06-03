# Plan: VPS migration (Nitro → Hetzner EU)

**Slug:** vps-migration-2026-06-03
**Source grill:** inline (this conversation, 2026-06-03) — full grill (infra, real data-loss risk)
**Created:** 2026-06-03
**Status:** APPROVED, pending implementation. Not started.

## Decision summary (grilled 2026-06-03)
- **Why:** continuity of real-time-only data (OI, funding, CVD, whale, shadow
  outcomes — no history API, lost on downtime) + decouple from the Nitro 5 during
  an upcoming move + definitive base to scale to $1k+. Candles themselves backfill
  from OKX REST so they are NOT the reason.
- **Region:** **Europe — Hetzner** (CX22, ~€4/mo). **NOT US** — OKX exited the US
  market and geo-fences US IPs harder than CA; a US IP risks blocking the API.
  Account is Mexican-registered; EU is OKX-clean and cheap. (Correction of an
  earlier mistaken "US VPS" suggestion.)
- **Overlap:** net available at new place day 1 → **zero-gap cutover** (VPS up +
  migrated + verified BEFORE Nitro bot stops).
- **Permanence:** **definitive** — harden + backups, this is the $1k-scale VPS.
- **Stakes:** bot is **shadow-only** (`OKX_SANDBOX=false` but `ENABLED_SETUPS=[]`)
  → NO live OKX orders during migration. Lowers risk: a misconfig can't lose money,
  only data.

## Stack to move (docker-compose.yml — 8 services)
postgres(16-alpine), redis(7-alpine, AOF), bot, api(:8000), web(:3000),
bybit-watcher, explain-bot, grafana(:3001). Volumes: `pgdata` (241MB), `redisdata`
(shadow state — AOF), `grafana_data` (re-provisionable from `./monitoring`).
Footprint ~860MB RAM → 4GB VPS comfortable.

## Phase 0 — Pre-flight inventory (on Nitro, no changes)
**Goal:** baseline + secret list before touching anything.
- Enumerate `.env` keys needed: OKX (key/secret/passphrase, SANDBOX), Bybit,
  ETHERSCAN, ANTHROPIC, TELEGRAM (token/chat), POSTGRES_*, REDIS_*,
  DASHBOARD_API_KEY, GRAFANA_ADMIN_PASSWORD, EXPERIMENT_ID, ENABLED_SETUPS.
- Record baseline counts for post-migration verify: `candles`, `ml_setups`,
  `trades`, `bybit_executions`, `bybit_closed_pnl`, latest shadow `setup_id`.
- Note current EXPERIMENT_ID runtime value (memory: verify, don't assume).
**Gate:** secret list + baseline snapshot captured.

## Phase 1 — Provision + harden VPS (definitive)
**Goal:** secure host, parity with Nitro's posture.
- Hetzner CX22 (2 vCPU / 4GB / 40GB SSD), Ubuntu 24.04, EU region.
- SSH keys only; disable root login + password auth; fail2ban.
- `ufw`: default deny incoming; allow SSH (rate-limited); allow `tailscale0`.
  Lock 3000/8000/3001 to `tailscale0` only (memory: firewall_port_hygiene).
- Docker engine + compose plugin.
- Tailscale: install, `tailscale up`, join tailnet, record VPS tailscale IP.
**Gate:** SSH-key-only login works; ufw active; tailscale IP reachable; Docker runs hello-world.

## Phase 2 — Stand up stack (data services only, no cutover)
**Goal:** infra running on VPS, ready to receive data; app still off.
- `git clone` repo, checkout `main`.
- Transfer `.env` securely over tailscale (scp), NOT git. Verify every key from
  Phase 0 present. Double-check `OKX_SANDBOX` + `ENABLED_SETUPS` match Nitro.
- `docker compose up -d postgres redis` only (bot/api/web/grafana stay down).
- **Sanity:** test OKX API auth from the VPS (one signed REST call) — confirms EU
  IP isn't blocked BEFORE committing to cutover.
**Gate:** postgres+redis healthy; OKX API call from VPS returns 200.

## Phase 3 — Data migration
**Goal:** move the irreplaceable state.
- **Postgres:** `pg_dump` on Nitro → restore into VPS postgres (over tailscale or
  scp dump file). Same image version → clean. Verify row counts vs Phase 0 baseline.
- **Redis:** `BGSAVE` on Nitro → copy `dump.rdb`/AOF into `redisdata` before redis
  starts, OR `redis-cli --rdb`. Verify `qf:bot:*` keys + shadow snapshot present.
  (Shadow restore is deferred-until-connected — PR #66 — so it reloads on first
  candle tick; the snapshot must exist in Redis for that to work.)
- grafana_data: skip (dashboards re-provision from `./monitoring/dashboards`).
**Gate:** VPS DB row counts == Nitro; shadow snapshot key present in VPS Redis.

## Phase 4 — Zero-gap cutover (overlap)
**Goal:** switch with no data gap and no double-bot conflict.
- **Critical constraint:** only ONE bot may hold the Telegram token (getUpdates
  conflict) and own shadow state at a time. So order matters:
  1. VPS stack fully built EXCEPT `bot` (`up -d` api/web/grafana + postgres/redis).
  2. **Stop Nitro `bot`** (`docker compose stop bot`).
  3. Final delta sync: re-dump candles + real-time tables changed since Phase 3
     dump → load into VPS (small window; candles also self-backfill on VPS bot
     start, well within the 500-candle cap for a minutes-long window).
  4. **Start VPS `bot`** (`docker compose up -d --build bot`).
- Net at new place day 1 means this can run with both machines online → the only
  "gap" is the seconds between Nitro-stop and VPS-start, covered by backfill.
**Gate:** VPS bot RUNNING, Nitro bot STOPPED, single Telegram owner.

## Phase 5 — Verify (9-step checklist)
Run memory: `reference_deploy_verification` (post `docker compose up -d --build bot`):
candle freshness <5min, all expected setups loaded, shadow restored from Redis,
Telegram alive, no warmup hang, dashboard+grafana reachable via tailscale.
Plus: `ml_setups` count and latest shadow `setup_id` advancing on VPS.
**Gate:** all 9 steps pass; new shadow rows appearing.

## Phase 6 — Decommission + backups (definitive)
**Goal:** retire Nitro safely, make VPS durable.
- Keep Nitro stack STOPPED (not deleted) as cold rollback for ≥5 days.
- Automated backup on VPS: cron `pg_dump` (daily) + redis BGSAVE copy, retained
  locally and/or offsite. (New script — none exists today; only `sync_bybit.py`.)
- Docs (`/doc-update`): OPERATIONS.md (new host/tailscale IP, threat model),
  SYSTEM_BASELINE (server change + changelog), memory regulatory note
  (mark "migrate at $1k+" as DONE / superseded by this).
**Gate:** one successful automated backup cycle; docs updated.

## Risks
- **Double-bot Telegram conflict** → enforced stop-before-start (Phase 4).
- **Redis shadow state lost** → verify snapshot key post-restore; deferred restore
  (PR #66) reloads on first tick.
- **OKX EU-IP block** → de-risked by Phase 2 sanity call before cutover.
- **.env misconfig** (sandbox/real mix) → shadow-only means no money at risk;
  still verify SANDBOX + ENABLED_SETUPS explicitly.
- **CPU:** CX22 = 2 shared vCPU vs Nitro 4 dedicated. Bot runtime fine. Run
  `scripts/optimize.py` (Optuna) and heavy backtests on LOCAL/Nitro, not VPS.
- **Disk:** 40GB ample (pgdata 241MB, slow growth); monitor.

## Rollback
Nitro stack intact (stopped) for ≥5 days. To roll back: stop VPS bot (avoid
Telegram conflict) → restart Nitro bot. DB on Nitro is the pre-cutover snapshot;
re-sync delta if needed.
