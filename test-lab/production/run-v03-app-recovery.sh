#!/usr/bin/env bash
# Isolated application recovery drills for BackupLint v0.3.
# Uses logical dumps (pg_dump / mariadb-dump / mongodump) + static nginx content.
# Never claims that raw DB data files alone prove logical recovery.
# Disposable containers only — does not touch live user data.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate" 2>/dev/null || true

unset RESTIC_PASSWORD RESTIC_PASSWORD_FILE RESTIC_REPOSITORY || true

SUFFIX="$(openssl rand -hex 4 2>/dev/null || printf '%04x%04x' "$RANDOM" "$RANDOM")"
BASE="$(mktemp -d "/tmp/backuplint-v03-app-recovery-${SUFFIX}-XXXXXX")"
PASSFILE="$BASE/restic.pass"
REPO="$BASE/repo"

PASS=0
FAIL=0
ok() { echo "OK $*"; PASS=$((PASS + 1)); }
bad() { echo "FAIL $*"; FAIL=$((FAIL + 1)); }

CONTAINERS=()

cleanup() {
  set +e
  for c in "${CONTAINERS[@]:-}"; do
    docker rm -f "$c" >/dev/null 2>&1
  done
  while IFS= read -r -d '' yml; do
    docker compose -f "$yml" down --remove-orphans >/dev/null 2>&1
  done < <(find "$BASE" -name 'compose.yml' -print0 2>/dev/null)
  rm -rf "$BASE"
  find /tmp -maxdepth 1 -user "$(id -un)" -type d -name 'backuplint-restore-*' \
    -mmin -180 -exec rm -rf {} + 2>/dev/null
  set -e
}
trap cleanup EXIT

printf 'v03-app-recovery-pass\n' >"$PASSFILE"
chmod 600 "$PASSFILE"

wait_tcp() {
  local host="$1" port="$2" tries="${3:-90}"
  local i
  for ((i = 0; i < tries; i++)); do
    if python3 -c 'import socket,sys; s=socket.socket(); s.settimeout(0.5); s.connect((sys.argv[1], int(sys.argv[2]))); s.close()' \
      "$host" "$port" 2>/dev/null; then
      return 0
    fi
    sleep 0.3
  done
  return 1
}

free_port() {
  python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()'
}

init_repo() {
  export RESTIC_PASSWORD_FILE="$PASSFILE"
  restic init -r "$REPO" >/dev/null 2>&1 || true
  unset RESTIC_PASSWORD_FILE
}

backup_paths() {
  export RESTIC_PASSWORD_FILE="$PASSFILE"
  restic -r "$REPO" backup "$@" >/dev/null
  unset RESTIC_PASSWORD_FILE
}

write_app_stack() {
  local dir="$1"
  local data_dir="$2"
  mkdir -p "$dir" "$data_dir"
  cat >"$dir/compose.yml" <<EOF
services:
  app:
    image: alpine:3.20
    container_name: bl-app-rec-${SUFFIX}-$(basename "$dir")
    command: ["sleep", "3600"]
    volumes:
      - ${data_dir}:/data
EOF
}

write_cfg() {
  local cfg="$1"
  local data_note="$2"
  cat >"$cfg" <<EOF
# $data_note
backup_paths: []
restic:
  repository: ${REPO}
  password_file: ${PASSFILE}
  restore_verification:
    mode: selected
EOF
}

run_bl_restore() {
  local compose="$1"
  local cfg="$2"
  set +e
  OUT="$(timeout --signal=TERM --kill-after=10s 120 backuplint scan "$compose" --config "$cfg" 2>&1)"
  EC=$?
  set -e
}

mkdir -p "$REPO"
init_repo

# ---------------------------------------------------------------------------
# PostgreSQL logical dump recovery drill
# ---------------------------------------------------------------------------
echo "=== PostgreSQL logical dump drill ==="
PG_DIR="$BASE/postgres"
PG_PORT="$(free_port)"
PG_NAME="bl-app-pg-${SUFFIX}"
PG_DUMP_DIR="$PG_DIR/dumps"
mkdir -p "$PG_DUMP_DIR"

docker run -d --name "$PG_NAME" \
  -e POSTGRES_PASSWORD=lab-only-pg \
  -e POSTGRES_USER=lab \
  -e POSTGRES_DB=labdb \
  -p "127.0.0.1:${PG_PORT}:5432" \
  postgres:16-alpine >/dev/null
CONTAINERS+=("$PG_NAME")

if ! wait_tcp 127.0.0.1 "$PG_PORT" 100; then
  bad pg_listen
else
  ok pg_listen
fi

PG_READY=0
for _ in $(seq 1 60); do
  if docker exec -e PGPASSWORD=lab-only-pg "$PG_NAME" \
    pg_isready -U lab -d labdb >/dev/null 2>&1; then
    PG_READY=1
    break
  fi
  sleep 0.5
done
[[ "$PG_READY" -eq 1 ]] && ok pg_ready || bad pg_ready

docker exec -e PGPASSWORD=lab-only-pg "$PG_NAME" \
  psql -U lab -d labdb -c "CREATE TABLE items(id int primary key, name text); INSERT INTO items VALUES (1,'alpha'),(2,'beta');" >/dev/null

docker exec -e PGPASSWORD=lab-only-pg "$PG_NAME" \
  pg_dump -U lab -d labdb -Fc -f /tmp/labdb.dump
docker cp "$PG_NAME:/tmp/labdb.dump" "$PG_DUMP_DIR/labdb.dump"
docker exec -e PGPASSWORD=lab-only-pg "$PG_NAME" \
  pg_dump -U lab -d labdb --no-owner -f /tmp/labdb.sql
docker cp "$PG_NAME:/tmp/labdb.sql" "$PG_DUMP_DIR/labdb.sql"

printf 'LOGICAL_DUMP_ONLY\n' >"$PG_DUMP_DIR/README.recovery"
printf 'Raw postgres data directories were NOT used to claim logical recovery.\n' >>"$PG_DUMP_DIR/README.recovery"

backup_paths "$PG_DUMP_DIR"
write_app_stack "$PG_DIR/stack" "$PG_DUMP_DIR"
docker compose -f "$PG_DIR/stack/compose.yml" up -d >/dev/null
write_cfg "$PG_DIR/backuplint.yml" "PostgreSQL logical dump paths under bind mount"

run_bl_restore "$PG_DIR/stack/compose.yml" "$PG_DIR/backuplint.yml"
if [[ "$EC" -eq 0 ]] && printf '%s' "$OUT" | grep -qi 'selected-path restore verification passed'; then
  ok pg_backuplint_restore
else
  bad "pg_backuplint_restore_ec=${EC}"
fi

PG2_NAME="bl-app-pg-restore-${SUFFIX}"
PG2_PORT="$(free_port)"
docker run -d --name "$PG2_NAME" \
  -e POSTGRES_PASSWORD=lab-only-pg \
  -e POSTGRES_USER=lab \
  -e POSTGRES_DB=labdb \
  -p "127.0.0.1:${PG2_PORT}:5432" \
  postgres:16-alpine >/dev/null
CONTAINERS+=("$PG2_NAME")
wait_tcp 127.0.0.1 "$PG2_PORT" 100 || bad pg2_listen
for _ in $(seq 1 60); do
  docker exec -e PGPASSWORD=lab-only-pg "$PG2_NAME" \
    pg_isready -U lab -d labdb >/dev/null 2>&1 && break
  sleep 0.5
done
docker cp "$PG_DUMP_DIR/labdb.dump" "$PG2_NAME:/tmp/labdb.dump"
set +e
docker exec -e PGPASSWORD=lab-only-pg "$PG2_NAME" \
  pg_restore -U lab -d labdb --clean --if-exists /tmp/labdb.dump >/dev/null 2>&1
docker exec -e PGPASSWORD=lab-only-pg "$PG2_NAME" \
  pg_restore -U lab -d labdb /tmp/labdb.dump >/dev/null 2>&1
ROWS="$(docker exec -e PGPASSWORD=lab-only-pg "$PG2_NAME" \
  psql -U lab -d labdb -tAc "SELECT count(*) FROM items;" 2>/dev/null | tr -d '[:space:]')"
set -e
if [[ "$ROWS" == "2" ]]; then
  ok pg_logical_row_count
else
  docker cp "$PG_DUMP_DIR/labdb.sql" "$PG2_NAME:/tmp/labdb.sql"
  docker exec -e PGPASSWORD=lab-only-pg "$PG2_NAME" \
    psql -U lab -d labdb -f /tmp/labdb.sql >/dev/null
  ROWS="$(docker exec -e PGPASSWORD=lab-only-pg "$PG2_NAME" \
    psql -U lab -d labdb -tAc "SELECT count(*) FROM items;" | tr -d '[:space:]')"
  [[ "$ROWS" == "2" ]] && ok pg_logical_row_count_sql || bad "pg_logical_rows=${ROWS}"
fi
echo "NOTE: PostgreSQL drill used pg_dump/pg_restore (logical). Raw PGDATA was not treated as proof of recovery."

# ---------------------------------------------------------------------------
# MariaDB logical dump recovery drill
# ---------------------------------------------------------------------------
echo "=== MariaDB logical dump drill ==="
MDB_DIR="$BASE/mariadb"
MDB_PORT="$(free_port)"
MDB_NAME="bl-app-mdb-${SUFFIX}"
MDB_DUMP_DIR="$MDB_DIR/dumps"
mkdir -p "$MDB_DUMP_DIR"

docker run -d --name "$MDB_NAME" \
  -e MARIADB_ROOT_PASSWORD=lab-only-mdb \
  -e MARIADB_DATABASE=labdb \
  -e MARIADB_USER=lab \
  -e MARIADB_PASSWORD=lab-only-mdb \
  -p "127.0.0.1:${MDB_PORT}:3306" \
  mariadb:11 >/dev/null
CONTAINERS+=("$MDB_NAME")

for _ in $(seq 1 90); do
  if docker exec "$MDB_NAME" mariadb -uroot -plab-only-mdb -e "SELECT 1" >/dev/null 2>&1; then
    break
  fi
  sleep 0.5
done
ok mdb_ready

docker exec "$MDB_NAME" mariadb -uroot -plab-only-mdb -e \
  "USE labdb; CREATE TABLE items(id INT PRIMARY KEY, name VARCHAR(64)); INSERT INTO items VALUES (1,'alpha'),(2,'beta');" >/dev/null

docker exec "$MDB_NAME" mariadb-dump -uroot -plab-only-mdb labdb >"$MDB_DUMP_DIR/labdb.sql"
printf 'LOGICAL_DUMP_ONLY\n' >"$MDB_DUMP_DIR/README.recovery"

backup_paths "$MDB_DUMP_DIR"
write_app_stack "$MDB_DIR/stack" "$MDB_DUMP_DIR"
docker compose -f "$MDB_DIR/stack/compose.yml" up -d >/dev/null
write_cfg "$MDB_DIR/backuplint.yml" "MariaDB logical dump"

run_bl_restore "$MDB_DIR/stack/compose.yml" "$MDB_DIR/backuplint.yml"
if [[ "$EC" -eq 0 ]] && printf '%s' "$OUT" | grep -qi 'selected-path restore verification passed'; then
  ok mdb_backuplint_restore
else
  bad "mdb_backuplint_restore_ec=${EC}"
fi

MDB2_NAME="bl-app-mdb-restore-${SUFFIX}"
MDB2_PORT="$(free_port)"
docker run -d --name "$MDB2_NAME" \
  -e MARIADB_ROOT_PASSWORD=lab-only-mdb \
  -e MARIADB_DATABASE=labdb \
  -p "127.0.0.1:${MDB2_PORT}:3306" \
  mariadb:11 >/dev/null
CONTAINERS+=("$MDB2_NAME")
for _ in $(seq 1 90); do
  if docker exec "$MDB2_NAME" mariadb -uroot -plab-only-mdb -e "SELECT 1" >/dev/null 2>&1; then
    break
  fi
  sleep 0.5
done
docker cp "$MDB_DUMP_DIR/labdb.sql" "$MDB2_NAME:/tmp/labdb.sql"
docker exec "$MDB2_NAME" sh -c 'mariadb -uroot -plab-only-mdb labdb < /tmp/labdb.sql' >/dev/null 2>&1 || \
  docker exec -i "$MDB2_NAME" mariadb -uroot -plab-only-mdb labdb <"$MDB_DUMP_DIR/labdb.sql" >/dev/null
ROWS="$(docker exec "$MDB2_NAME" mariadb -uroot -plab-only-mdb -N -e "SELECT COUNT(*) FROM labdb.items;" | tr -d '[:space:]')"
[[ "$ROWS" == "2" ]] && ok mdb_logical_row_count || bad "mdb_logical_rows=${ROWS}"
echo "NOTE: MariaDB drill used mariadb-dump (logical). Raw datadir was not treated as proof of recovery."

# ---------------------------------------------------------------------------
# MongoDB mongodump drill
# ---------------------------------------------------------------------------
echo "=== MongoDB mongodump drill ==="
MONGO_DIR="$BASE/mongo"
MONGO_PORT="$(free_port)"
MONGO_NAME="bl-app-mongo-${SUFFIX}"
MONGO_DUMP_DIR="$MONGO_DIR/dumps"
mkdir -p "$MONGO_DUMP_DIR"

docker run -d --name "$MONGO_NAME" \
  -p "127.0.0.1:${MONGO_PORT}:27017" \
  mongo:7 >/dev/null
CONTAINERS+=("$MONGO_NAME")
wait_tcp 127.0.0.1 "$MONGO_PORT" 100 || bad mongo_listen
ok mongo_listen
for _ in $(seq 1 60); do
  docker exec "$MONGO_NAME" mongosh --quiet --eval 'db.runCommand({ ping: 1 })' >/dev/null 2>&1 && break
  sleep 0.5
done

docker exec "$MONGO_NAME" mongosh --quiet --eval \
  'db.getSiblingDB("labdb").items.insertMany([{_id:1,name:"alpha"},{_id:2,name:"beta"}])' >/dev/null

docker exec "$MONGO_NAME" mongodump --db=labdb --out=/tmp/mongodump >/dev/null
docker cp "$MONGO_NAME:/tmp/mongodump" "$MONGO_DUMP_DIR/mongodump"
printf 'LOGICAL_DUMP_ONLY (mongodump)\n' >"$MONGO_DUMP_DIR/README.recovery"

backup_paths "$MONGO_DUMP_DIR"
write_app_stack "$MONGO_DIR/stack" "$MONGO_DUMP_DIR"
docker compose -f "$MONGO_DIR/stack/compose.yml" up -d >/dev/null
write_cfg "$MONGO_DIR/backuplint.yml" "MongoDB mongodump"

run_bl_restore "$MONGO_DIR/stack/compose.yml" "$MONGO_DIR/backuplint.yml"
if [[ "$EC" -eq 0 ]] && printf '%s' "$OUT" | grep -qi 'selected-path restore verification passed'; then
  ok mongo_backuplint_restore
else
  bad "mongo_backuplint_restore_ec=${EC}"
fi

MONGO2_NAME="bl-app-mongo-restore-${SUFFIX}"
MONGO2_PORT="$(free_port)"
docker run -d --name "$MONGO2_NAME" \
  -p "127.0.0.1:${MONGO2_PORT}:27017" \
  mongo:7 >/dev/null
CONTAINERS+=("$MONGO2_NAME")
wait_tcp 127.0.0.1 "$MONGO2_PORT" 100 || bad mongo2_listen
for _ in $(seq 1 60); do
  docker exec "$MONGO2_NAME" mongosh --quiet --eval 'db.runCommand({ ping: 1 })' >/dev/null 2>&1 && break
  sleep 0.5
done
docker cp "$MONGO_DUMP_DIR/mongodump" "$MONGO2_NAME:/tmp/mongodump"
docker exec "$MONGO2_NAME" mongorestore --drop /tmp/mongodump >/dev/null
COUNT="$(docker exec "$MONGO2_NAME" mongosh --quiet --eval 'db.getSiblingDB("labdb").items.countDocuments()' | tr -d '[:space:]')"
[[ "$COUNT" == "2" ]] && ok mongo_logical_count || bad "mongo_logical_count=${COUNT}"
echo "NOTE: MongoDB drill used mongodump/mongorestore. Raw dbPath was not treated as proof of recovery."

# ---------------------------------------------------------------------------
# Static nginx content restore drill
# ---------------------------------------------------------------------------
echo "=== nginx static content drill ==="
NGX_DIR="$BASE/nginx"
NGX_WWW="$NGX_DIR/www"
NGX_PORT="$(free_port)"
NGX_NAME="bl-app-nginx-${SUFFIX}"
mkdir -p "$NGX_WWW"
cat >"$NGX_WWW/index.html" <<'EOF'
<!DOCTYPE html><html><head><title>BackupLint recovery lab</title></head>
<body><h1>backuplint-app-recovery</h1><p id="marker">static-ok</p></body></html>
EOF
echo 'asset' >"$NGX_WWW/asset.txt"

docker run -d --name "$NGX_NAME" \
  -p "127.0.0.1:${NGX_PORT}:80" \
  -v "$NGX_WWW:/usr/share/nginx/html:ro" \
  nginx:1.27-alpine >/dev/null
CONTAINERS+=("$NGX_NAME")
wait_tcp 127.0.0.1 "$NGX_PORT" 40 || bad nginx_listen
BODY="$(curl -fsS "http://127.0.0.1:${NGX_PORT}/" || true)"
printf '%s' "$BODY" | grep -q 'static-ok' && ok nginx_live_content || bad nginx_live_content

backup_paths "$NGX_WWW"
write_app_stack "$NGX_DIR/stack" "$NGX_WWW"
docker compose -f "$NGX_DIR/stack/compose.yml" up -d >/dev/null
write_cfg "$NGX_DIR/backuplint.yml" "nginx static www"

run_bl_restore "$NGX_DIR/stack/compose.yml" "$NGX_DIR/backuplint.yml"
if [[ "$EC" -eq 0 ]] && printf '%s' "$OUT" | grep -qi 'selected-path restore verification passed'; then
  ok nginx_backuplint_restore
else
  bad "nginx_backuplint_restore_ec=${EC}"
fi

NGX_RESTORE="$NGX_DIR/restored"
mkdir -p "$NGX_RESTORE"
export RESTIC_PASSWORD_FILE="$PASSFILE"
restic -r "$REPO" restore latest --target "$NGX_RESTORE" --include "$NGX_WWW" >/dev/null
unset RESTIC_PASSWORD_FILE
RESTORED_WWW="$(find "$NGX_RESTORE" -type f -name index.html | head -1 | xargs -r dirname)"
if [[ -z "$RESTORED_WWW" || ! -f "$RESTORED_WWW/index.html" ]]; then
  bad nginx_restic_materialize
else
  ok nginx_restic_materialize
  NGX2_NAME="bl-app-nginx-restore-${SUFFIX}"
  NGX2_PORT="$(free_port)"
  docker run -d --name "$NGX2_NAME" \
    -p "127.0.0.1:${NGX2_PORT}:80" \
    -v "$RESTORED_WWW:/usr/share/nginx/html:ro" \
    nginx:1.27-alpine >/dev/null
  CONTAINERS+=("$NGX2_NAME")
  wait_tcp 127.0.0.1 "$NGX2_PORT" 40 || bad nginx2_listen
  BODY2="$(curl -fsS "http://127.0.0.1:${NGX2_PORT}/" || true)"
  printf '%s' "$BODY2" | grep -q 'static-ok' && ok nginx_recovered_content || bad nginx_recovered_content
fi

if printf '%s' "$OUT" | grep -Fq 'v03-app-recovery-pass'; then
  bad password_leaked_in_output
else
  ok password_not_in_output
fi

echo
echo "=== summary PASS=$PASS FAIL=$FAIL ==="
echo "Caveats:"
echo "  - DB drills use logical dumps only; raw DB files do NOT prove logical recovery."
echo "  - BackupLint selected restore verifies dump/static files exist in the restic snapshot,"
echo "    separate from application-level dump restore into a fresh engine."
echo "  - All containers and temp dirs are disposable and removed on EXIT."
if [[ "$FAIL" -eq 0 ]]; then
  echo APP_RECOVERY_PASSED
  exit 0
fi
echo APP_RECOVERY_FAILED
exit 1
