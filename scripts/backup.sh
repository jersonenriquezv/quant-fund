#!/bin/bash
# Backup PostgreSQL + Redis from the dockerized stack into timestamped files.
# - PG: pg_dump custom format (compressed, restorable with pg_restore) via the
#   postgres container (matches the server's PG major version exactly).
# - Redis: BGSAVE then copy the RDB out of the container.
# Secrets are read from config/.env and never printed.
#
# Usage:
#   bash scripts/backup.sh                 # write to ./backups/<UTC-stamp>/
#   BACKUP_DIR=/mnt/x bash scripts/backup.sh
#   RETAIN=14 bash scripts/backup.sh       # prune backup dirs older than the newest 14
#
# Cron (daily 04:00, Phase 6 of the VPS migration):
#   0 4 * * *  cd /home/jer/quant-fund && bash scripts/backup.sh >> logs/backup.log 2>&1
set -euo pipefail

cd "$(dirname "$0")/.."   # repo root
ENV_FILE="config/.env"
PG_CONTAINER="${PG_CONTAINER:-quant-fund-postgres-1}"
REDIS_CONTAINER="${REDIS_CONTAINER:-quant-fund-redis-1}"
RETAIN="${RETAIN:-14}"

[ -f "$ENV_FILE" ] || { echo "ERROR: $ENV_FILE not found"; exit 1; }

# Pull only the keys we need (no `source` — avoids executing arbitrary .env content).
getenv() { grep -oE "^$1=.*" "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"'; }
PG_DB="$(getenv POSTGRES_DB)";   PG_DB="${PG_DB:-quant_fund}"
PG_USER="$(getenv POSTGRES_USER)"; PG_USER="${PG_USER:-jer}"
PG_PASS="$(getenv POSTGRES_PASSWORD)"
REDIS_PASS="$(getenv REDIS_PASSWORD)"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="${BACKUP_DIR:-./backups}/$STAMP"
mkdir -p "$OUT"

echo "[backup $STAMP] PostgreSQL ($PG_DB) -> $OUT/postgres.dump"
docker exec -e PGPASSWORD="$PG_PASS" "$PG_CONTAINER" \
  pg_dump -U "$PG_USER" -d "$PG_DB" -Fc > "$OUT/postgres.dump"
PG_BYTES=$(wc -c < "$OUT/postgres.dump")
[ "$PG_BYTES" -gt 1000 ] || { echo "ERROR: pg dump suspiciously small ($PG_BYTES B)"; exit 1; }

echo "[backup $STAMP] Redis BGSAVE -> $OUT/redis.rdb"
docker exec "$REDIS_CONTAINER" redis-cli ${REDIS_PASS:+-a "$REDIS_PASS"} BGSAVE >/dev/null
# Wait for BGSAVE to finish (rdb_bgsave_in_progress:0).
for _ in $(seq 1 30); do
  inprog=$(docker exec "$REDIS_CONTAINER" redis-cli ${REDIS_PASS:+-a "$REDIS_PASS"} INFO persistence 2>/dev/null | grep -c "rdb_bgsave_in_progress:1" || true)
  [ "$inprog" = "0" ] && break
  sleep 1
done
docker cp "$REDIS_CONTAINER:/data/dump.rdb" "$OUT/redis.rdb"

echo "[backup $STAMP] done: $(du -sh "$OUT" | cut -f1)"

# Retention: keep the newest $RETAIN timestamped dirs, prune the rest.
BASE="${BACKUP_DIR:-./backups}"
mapfile -t old < <(ls -1dt "$BASE"/*/ 2>/dev/null | tail -n +"$((RETAIN + 1))")
for d in "${old[@]:-}"; do [ -n "$d" ] && { echo "[backup] prune $d"; rm -rf "$d"; }; done
