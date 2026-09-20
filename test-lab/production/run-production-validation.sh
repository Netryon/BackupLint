#!/usr/bin/env bash
# Production validation harness for BackupLint.
# Creates disposable stacks, runs good/broken backup audits, records matrix results.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
LAB_ROOT="$ROOT/test-lab/production"
WORKDIR="${BACKUPLINT_PROD_WORKDIR:-/tmp/backuplint-prod-$$}"
MATRIX_OUT="$LAB_ROOT/results.tsv"
REPORT_MD="$ROOT/docs/production-test-matrix-results.md"

export PATH="$ROOT/.venv/bin:${PATH:-/usr/bin}"
mkdir -p "$WORKDIR" "$LAB_ROOT"
: >"$MATRIX_OUT"

log() { printf '%s\n' "$*" >&2; }

record() {
  # id|workload|storage|backup|expected|actual|exit|edge|gate|bug
  printf '%s\n' "$*" >>"$MATRIX_OUT"
}

run_scan() {
  local compose="$1" config="$2"
  set +e
  OUT="$(backuplint scan "$compose" --config "$config" 2>&1)"
  EC=$?
  set -e
  ACTUAL="ERROR"
  if echo "$OUT" | grep -q 'Result: PASS'; then ACTUAL=PASS
  elif echo "$OUT" | grep -q 'Result: WARN'; then ACTUAL=WARN
  elif echo "$OUT" | grep -q 'Result: FAIL'; then ACTUAL=FAIL
  fi
  # Exit 2 always ERROR regardless of body
  if [[ "$EC" -eq 2 ]]; then ACTUAL=ERROR; fi
  printf '%s\n' "$OUT"
  return "$EC"
}

expect_result() {
  local id="$1" workload="$2" storage="$3" method="$4" expected="$5" edge="$6"
  local compose="$7" config="$8"
  local out ec actual gate bug=""
  set +e
  out="$(backuplint scan "$compose" --config "$config" 2>&1)"
  ec=$?
  set -e
  actual=ERROR
  if echo "$out" | grep -q 'Result: PASS'; then actual=PASS
  elif echo "$out" | grep -q 'Result: WARN'; then actual=WARN
  elif echo "$out" | grep -q 'Result: FAIL'; then actual=FAIL
  fi
  if [[ "$ec" -eq 2 ]]; then actual=ERROR; fi
  gate=pass
  if [[ "$actual" != "$expected" ]]; then
    gate=fail
    bug="expected $expected got $actual (exit $ec)"
    log "MISMATCH $id: $bug"
    log "$out"
  else
    log "OK $id expected=$expected exit=$ec"
  fi
  record "$id|$workload|$storage|$method|$expected|$actual|$ec|$edge|$gate|$bug"
}

write_config() {
  local path="$1"
  shift
  {
    echo "backup_paths:"
    for p in "$@"; do
      echo "  - $p"
    done
  } >"$path"
}

cleanup_compose() {
  local dir="$1"
  (cd "$dir" && docker compose down -v --remove-orphans >/dev/null 2>&1 || true)
}

############################################
# Storage pattern labs (lightweight alpine)
############################################
storage_patterns() {
  local base="$WORKDIR/storage"
  mkdir -p "$base"

  # S01 single bind
  local d="$base/s01"
  mkdir -p "$d/data"
  echo x >"$d/data/file"
  cat >"$d/compose.yml" <<EOF
services:
  app:
    image: alpine:3.20
    command: ["sleep", "120"]
    volumes:
      - ./data:/data
EOF
  write_config "$d/good.yml" "$d/data"
  write_config "$d/bad.yml" "$d/other"
  (cd "$d" && docker compose up -d --quiet-pull >/dev/null)
  expect_result S01 alpine "single bind" backup_paths PASS "single bind" "$d/compose.yml" "$d/good.yml"
  expect_result S01b alpine "single bind" backup_paths FAIL "missing bind" "$d/compose.yml" "$d/bad.yml"
  cleanup_compose "$d"

  # S02 multiple binds
  d="$base/s02"
  mkdir -p "$d/a" "$d/b"
  cat >"$d/compose.yml" <<EOF
services:
  a:
    image: alpine:3.20
    command: ["sleep", "120"]
    volumes: ["./a:/a"]
  b:
    image: alpine:3.20
    command: ["sleep", "120"]
    volumes: ["./b:/b"]
EOF
  write_config "$d/good.yml" "$base/s02"
  write_config "$d/partial.yml" "$d/a"
  (cd "$d" && docker compose up -d --quiet-pull >/dev/null)
  expect_result S02 alpine "multi bind" backup_paths PASS "multi bind" "$d/compose.yml" "$d/good.yml"
  expect_result S02b alpine "multi bind" backup_paths FAIL "omit one bind" "$d/compose.yml" "$d/partial.yml"
  cleanup_compose "$d"

  # S07 read-only bind
  d="$base/s07"
  mkdir -p "$d/ro"
  cat >"$d/compose.yml" <<EOF
services:
  app:
    image: alpine:3.20
    command: ["sleep", "120"]
    volumes:
      - type: bind
        source: ./ro
        target: /config
        read_only: true
EOF
  write_config "$d/good.yml" "$d/ro"
  (cd "$d" && docker compose up -d --quiet-pull >/dev/null)
  expect_result S07 alpine "ro bind" backup_paths PASS "read-only mount" "$d/compose.yml" "$d/good.yml"
  cleanup_compose "$d"

  # S11 tmpfs only
  d="$base/s11"
  mkdir -p "$d"
  cat >"$d/compose.yml" <<EOF
services:
  app:
    image: alpine:3.20
    command: ["sleep", "120"]
    tmpfs: ["/tmp"]
EOF
  write_config "$d/good.yml"
  # empty backup_paths with only tmpfs => PASS (nothing critical)
  echo "backup_paths: []" >"$d/good.yml"
  (cd "$d" && docker compose up -d --quiet-pull >/dev/null)
  expect_result S11 alpine tmpfs backup_paths PASS "tmpfs skip" "$d/compose.yml" "$d/good.yml"
  cleanup_compose "$d"

  # S12 persistent + cache
  d="$base/s12"
  mkdir -p "$d/data" "$d/cache"
  cat >"$d/compose.yml" <<EOF
services:
  app:
    image: alpine:3.20
    command: ["sleep", "120"]
    volumes:
      - ./data:/data
      - ./cache:/cache
EOF
  write_config "$d/good.yml" "$d/data"
  (cd "$d" && docker compose up -d --quiet-pull >/dev/null)
  expect_result S12 alpine "persistent+cache" backup_paths PASS "cache skipped" "$d/compose.yml" "$d/good.yml"
  cleanup_compose "$d"

  # S14 no mounts
  d="$base/s14"
  mkdir -p "$d"
  cat >"$d/compose.yml" <<EOF
services:
  app:
    image: alpine:3.20
    command: ["sleep", "120"]
EOF
  echo "backup_paths: []" >"$d/good.yml"
  (cd "$d" && docker compose up -d --quiet-pull >/dev/null)
  expect_result S14 alpine "no mounts" backup_paths PASS "no storage" "$d/compose.yml" "$d/good.yml"
  cleanup_compose "$d"

  # S17/S18 short vs long syntax
  d="$base/s17"
  mkdir -p "$d/data"
  cat >"$d/compose.yml" <<EOF
services:
  short:
    image: alpine:3.20
    command: ["sleep", "120"]
    volumes: ["./data:/data"]
  long:
    image: alpine:3.20
    command: ["sleep", "120"]
    volumes:
      - type: bind
        source: ./data
        target: /data2
EOF
  write_config "$d/good.yml" "$d/data"
  (cd "$d" && docker compose up -d --quiet-pull >/dev/null)
  expect_result S17 alpine "short+long" backup_paths PASS "compose syntax" "$d/compose.yml" "$d/good.yml"
  cleanup_compose "$d"

  # similar names
  d="$base/similar"
  mkdir -p "$d/app" "$d/app2"
  cat >"$d/compose.yml" <<EOF
services:
  app2:
    image: alpine:3.20
    command: ["sleep", "120"]
    volumes: ["./app2:/data"]
EOF
  write_config "$d/bad.yml" "$d/app"
  (cd "$d" && docker compose up -d --quiet-pull >/dev/null)
  expect_result S_sim alpine "similar name" backup_paths FAIL "app vs app2" "$d/compose.yml" "$d/bad.yml"
  cleanup_compose "$d"
}

############################################
# Named volumes
############################################
named_volumes() {
  local base="$WORKDIR/volumes"
  mkdir -p "$base/v01"
  local d="$base/v01"
  local vol="blprod_v01_$$"
  docker volume create "$vol" >/dev/null
  cat >"$d/compose.yml" <<EOF
services:
  app:
    image: alpine:3.20
    command: ["sleep", "120"]
    volumes:
      - data:/data
volumes:
  data:
    external: true
    name: $vol
EOF
  local mp
  mp="$(docker volume inspect "$vol" --format '{{.Mountpoint}}')"
  write_config "$d/good.yml" "$mp"
  write_config "$d/bad.yml" "/srv/not-this"
  (cd "$d" && docker compose up -d --quiet-pull >/dev/null)
  expect_result V01 alpine "named volume" backup_paths PASS "one volume" "$d/compose.yml" "$d/good.yml"
  expect_result V01b alpine "named volume" backup_paths FAIL "volume omitted" "$d/compose.yml" "$d/bad.yml"
  cleanup_compose "$d"
  docker volume rm "$vol" >/dev/null 2>&1 || true

  # shared volume
  d="$base/shared"
  mkdir -p "$d"
  vol="blprod_shared_$$"
  docker volume create "$vol" >/dev/null
  cat >"$d/compose.yml" <<EOF
services:
  a:
    image: alpine:3.20
    command: ["sleep", "120"]
    volumes: ["data:/data"]
  b:
    image: alpine:3.20
    command: ["sleep", "120"]
    volumes: ["data:/data"]
volumes:
  data:
    external: true
    name: $vol
EOF
  mp="$(docker volume inspect "$vol" --format '{{.Mountpoint}}')"
  write_config "$d/good.yml" "$mp"
  (cd "$d" && docker compose up -d --quiet-pull >/dev/null)
  expect_result V06 alpine "shared volume" backup_paths PASS "shared" "$d/compose.yml" "$d/good.yml"
  cleanup_compose "$d"
  docker volume rm "$vol" >/dev/null 2>&1 || true

  # external (same pattern, tracked separately)
  d="$base/ext"
  mkdir -p "$d"
  vol="blprod_ext_$$"
  docker volume create "$vol" >/dev/null
  cat >"$d/compose.yml" <<EOF
services:
  app:
    image: alpine:3.20
    command: ["sleep", "120"]
    volumes:
      - data:/data
volumes:
  data:
    external: true
    name: $vol
EOF
  mp="$(docker volume inspect "$vol" --format '{{.Mountpoint}}')"
  write_config "$d/good.yml" "$mp"
  (cd "$d" && docker compose up -d --quiet-pull >/dev/null)
  expect_result V05 alpine "external volume" backup_paths PASS "external" "$d/compose.yml" "$d/good.yml"
  cleanup_compose "$d"
  docker volume rm "$vol" >/dev/null 2>&1 || true
}

############################################
# Real applications
############################################
app_nginx() {
  local d="$WORKDIR/apps/nginx"
  mkdir -p "$d/html"
  echo "ok" >"$d/html/index.html"
  cat >"$d/compose.yml" <<EOF
services:
  web:
    image: nginx:1.27-alpine
    volumes:
      - ./html:/usr/share/nginx/html:ro
EOF
  write_config "$d/good.yml" "$d/html"
  write_config "$d/missing.yml" "$d/missing"
  write_config "$d/parent.yml" "$d"
  mkdir -p "$d/html-old"
  write_config "$d/similar.yml" "$d/html-old"
  (cd "$d" && docker compose up -d --quiet-pull >/dev/null)
  expect_result A01a nginx "ro bind html" backup_paths PASS "complete" "$d/compose.yml" "$d/good.yml"
  expect_result A01b nginx "ro bind html" backup_paths FAIL "missing" "$d/compose.yml" "$d/missing.yml"
  expect_result A01c nginx "ro bind html" backup_paths PASS "parent" "$d/compose.yml" "$d/parent.yml"
  expect_result A01d nginx "ro bind html" backup_paths FAIL "similar name" "$d/compose.yml" "$d/similar.yml"
  cleanup_compose "$d"
}

app_redis() {
  local d="$WORKDIR/apps/redis"
  mkdir -p "$d"
  cat >"$d/compose.yml" <<EOF
services:
  redis:
    image: redis:7-alpine
    volumes:
      - redisdata:/data
volumes:
  redisdata:
EOF
  (cd "$d" && docker compose up -d --quiet-pull >/dev/null)
  # project volume name is typically <dir>_redisdata
  local vol
  vol="$(docker compose -f "$d/compose.yml" -p "$(basename "$d")" config --volumes)"
  # inspect via docker volume ls
  local name mp
  name="$(docker volume ls -q --filter "name=redisdata" | head -1)"
  mp="$(docker volume inspect "$name" --format '{{.Mountpoint}}')"
  write_config "$d/good.yml" "$mp"
  write_config "$d/bad.yml" "/tmp/not-redis"
  expect_result A02a redis "named volume" backup_paths PASS "complete" "$d/compose.yml" "$d/good.yml"
  expect_result A02b redis "named volume" backup_paths FAIL "missing" "$d/compose.yml" "$d/bad.yml"
  cleanup_compose "$d"
}

app_postgres() {
  local d="$WORKDIR/apps/postgres"
  mkdir -p "$d"
  cat >"$d/compose.yml" <<EOF
services:
  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_PASSWORD: backuplint-test
    volumes:
      - pgdata:/var/lib/postgresql/data
volumes:
  pgdata:
EOF
  (cd "$d" && docker compose up -d --quiet-pull >/dev/null)
  sleep 2
  local name mp
  name="$(docker volume ls -q --filter "name=pgdata" | grep postgres | head -1)"
  [[ -n "$name" ]] || name="$(docker volume ls -q --filter "name=pgdata" | head -1)"
  mp="$(docker volume inspect "$name" --format '{{.Mountpoint}}')"
  write_config "$d/good.yml" "$mp"
  write_config "$d/bad.yml" "/tmp/not-pg"
  expect_result A03a postgres "named volume" backup_paths WARN "complete+db warn" "$d/compose.yml" "$d/good.yml"
  expect_result A03b postgres "named volume" backup_paths FAIL "missing+db warn" "$d/compose.yml" "$d/bad.yml"
  cleanup_compose "$d"
}

app_mariadb() {
  local d="$WORKDIR/apps/mariadb"
  mkdir -p "$d"
  cat >"$d/compose.yml" <<EOF
services:
  db:
    image: mariadb:11
    environment:
      MARIADB_ROOT_PASSWORD: backuplint-test
    volumes:
      - mariadata:/var/lib/mysql
volumes:
  mariadata:
EOF
  (cd "$d" && docker compose up -d --quiet-pull >/dev/null)
  sleep 2
  local name mp
  name="$(docker volume ls -q --filter "name=mariadata" | head -1)"
  mp="$(docker volume inspect "$name" --format '{{.Mountpoint}}')"
  write_config "$d/good.yml" "$mp"
  write_config "$d/bad.yml" "/tmp/not-maria"
  expect_result A04a mariadb "named volume" backup_paths WARN "complete+db" "$d/compose.yml" "$d/good.yml"
  expect_result A04b mariadb "named volume" backup_paths FAIL "missing" "$d/compose.yml" "$d/bad.yml"
  cleanup_compose "$d"
}

app_mongo() {
  local d="$WORKDIR/apps/mongo"
  mkdir -p "$d"
  cat >"$d/compose.yml" <<EOF
services:
  db:
    image: mongo:7
    volumes:
      - mongodata:/data/db
volumes:
  mongodata:
EOF
  (cd "$d" && docker compose up -d --quiet-pull >/dev/null)
  sleep 2
  local name mp
  name="$(docker volume ls -q --filter "name=mongodata" | head -1)"
  mp="$(docker volume inspect "$name" --format '{{.Mountpoint}}')"
  write_config "$d/good.yml" "$mp"
  write_config "$d/bad.yml" "/tmp/not-mongo"
  expect_result A05a mongo "named volume" backup_paths WARN "complete+db" "$d/compose.yml" "$d/good.yml"
  expect_result A05b mongo "named volume" backup_paths FAIL "missing" "$d/compose.yml" "$d/bad.yml"
  cleanup_compose "$d"
}

app_vaultwarden() {
  local d="$WORKDIR/apps/vaultwarden"
  mkdir -p "$d/vw-data" "$d/vw-data-old"
  cat >"$d/compose.yml" <<EOF
services:
  vaultwarden:
    image: vaultwarden/server:1.32.5-alpine
    volumes:
      - ./vw-data:/data
EOF
  write_config "$d/good.yml" "$d/vw-data"
  write_config "$d/bad.yml" "$d/missing"
  write_config "$d/similar.yml" "$d/vw-data-old"
  (cd "$d" && docker compose up -d --quiet-pull >/dev/null)
  expect_result A06a vaultwarden "bind data" backup_paths PASS "complete" "$d/compose.yml" "$d/good.yml"
  expect_result A06b vaultwarden "bind data" backup_paths FAIL "missing" "$d/compose.yml" "$d/bad.yml"
  expect_result A06c vaultwarden "bind data" backup_paths FAIL "similar" "$d/compose.yml" "$d/similar.yml"
  cleanup_compose "$d"
}

app_jellyfin() {
  local d="$WORKDIR/apps/jellyfin"
  mkdir -p "$d/config" "$d/cache" "$d/media"
  echo movie >"$d/media/demo.txt"
  cat >"$d/compose.yml" <<EOF
services:
  jellyfin:
    image: jellyfin/jellyfin:10.10.3
    volumes:
      - ./config:/config
      - ./cache:/cache
      - ./media:/media
EOF
  write_config "$d/good.yml" "$d/config" "$d/media"
  write_config "$d/omit_media.yml" "$d/config"
  write_config "$d/cache_only.yml" "$d/cache"
  (cd "$d" && docker compose up -d --quiet-pull >/dev/null)
  expect_result A07a jellyfin "config+media+cache" backup_paths PASS "complete (cache skip)" "$d/compose.yml" "$d/good.yml"
  expect_result A07b jellyfin "config+media+cache" backup_paths FAIL "omit media" "$d/compose.yml" "$d/omit_media.yml"
  expect_result A07c jellyfin "config+media+cache" backup_paths FAIL "cache-only config" "$d/compose.yml" "$d/cache_only.yml"
  cleanup_compose "$d"
}

app_sonarr() {
  local d="$WORKDIR/apps/sonarr"
  mkdir -p "$d/config"
  cat >"$d/compose.yml" <<EOF
services:
  sonarr:
    image: lscr.io/linuxserver/sonarr:4.0.14
    environment:
      PUID: "1000"
      PGID: "1000"
    volumes:
      - ./config:/config
EOF
  write_config "$d/good.yml" "$d/config"
  write_config "$d/bad.yml" "$d/nope"
  (cd "$d" && docker compose up -d --quiet-pull >/dev/null)
  expect_result A08a sonarr "config bind" backup_paths PASS "complete" "$d/compose.yml" "$d/good.yml"
  expect_result A08b sonarr "config bind" backup_paths FAIL "missing" "$d/compose.yml" "$d/bad.yml"
  cleanup_compose "$d"
}

app_nextcloud() {
  local d="$WORKDIR/apps/nextcloud"
  mkdir -p "$d"
  cat >"$d/compose.yml" <<EOF
services:
  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_PASSWORD: backuplint-test
      POSTGRES_DB: nextcloud
    volumes:
      - nc_db:/var/lib/postgresql/data
  app:
    image: nextcloud:30-apache
    volumes:
      - nc_data:/var/www/html
    depends_on: [db]
volumes:
  nc_db:
  nc_data:
EOF
  (cd "$d" && docker compose up -d --quiet-pull >/dev/null)
  sleep 3
  local dbvol datavol dbmp datamp
  dbvol="$(docker volume ls -q --filter "name=nc_db" | head -1)"
  datavol="$(docker volume ls -q --filter "name=nc_data" | head -1)"
  dbmp="$(docker volume inspect "$dbvol" --format '{{.Mountpoint}}')"
  datamp="$(docker volume inspect "$datavol" --format '{{.Mountpoint}}')"
  write_config "$d/good.yml" "$dbmp" "$datamp"
  write_config "$d/omit_db.yml" "$datamp"
  write_config "$d/omit_nc.yml" "$dbmp"
  expect_result A09a nextcloud "db+app volumes" backup_paths WARN "complete+db warn" "$d/compose.yml" "$d/good.yml"
  expect_result A09b nextcloud "db+app volumes" backup_paths FAIL "omit db" "$d/compose.yml" "$d/omit_db.yml"
  expect_result A09c nextcloud "db+app volumes" backup_paths FAIL "omit nextcloud" "$d/compose.yml" "$d/omit_nc.yml"
  cleanup_compose "$d"
}

############################################
# Restic disposable repo scenarios
############################################
restic_labs() {
  local d="$WORKDIR/restic"
  mkdir -p "$d/data/a" "$d/data/b" "$d/unrelated"
  echo 1 >"$d/data/a/f"
  echo 2 >"$d/data/b/f"
  echo 3 >"$d/unrelated/f"
  local repo="$d/repo"
  local pass="$d/pass"
  echo "backuplint-prod-pass" >"$pass"
  export RESTIC_PASSWORD_FILE="$pass"
  restic init --repo "$repo" >/dev/null

  # compose pointing at data/a and data/b
  cat >"$d/compose.yml" <<EOF
services:
  a:
    image: alpine:3.20
    command: ["sleep", "30"]
    volumes: ["./data/a:/data"]
  b:
    image: alpine:3.20
    command: ["sleep", "30"]
    volumes: ["./data/b:/data"]
EOF

  cat >"$d/bl.yml" <<EOF
backup_paths: []
restic:
  repository: $repo
  password_file: $pass
EOF

  # R01 complete
  restic -r "$repo" backup "$d/data/a" "$d/data/b" >/dev/null
  (cd "$d" && docker compose up -d --quiet-pull >/dev/null)
  expect_result R01 restic "bind×2" restic PASS "complete snapshot" "$d/compose.yml" "$d/bl.yml"

  # R02 omit b
  rm -rf "$repo"
  restic init --repo "$repo" >/dev/null
  restic -r "$repo" backup "$d/data/a" >/dev/null
  expect_result R02 restic "bind×2" restic FAIL "path omitted" "$d/compose.yml" "$d/bl.yml"

  # R03 no snapshots
  rm -rf "$repo"
  restic init --repo "$repo" >/dev/null
  expect_result R03 restic "bind×2" restic FAIL "no snapshots" "$d/compose.yml" "$d/bl.yml"

  # R07 newest unrelated + older relevant
  rm -rf "$repo"
  restic init --repo "$repo" >/dev/null
  restic -r "$repo" backup "$d/data/a" "$d/data/b" >/dev/null
  sleep 1
  restic -r "$repo" backup "$d/unrelated" >/dev/null
  expect_result R07 restic "multi-root" restic PASS "newest unrelated" "$d/compose.yml" "$d/bl.yml"

  # R08 stale
  cat >"$d/bl-stale.yml" <<EOF
backup_paths: []
max_backup_age: 1s
restic:
  repository: $repo
  password_file: $pass
EOF
  sleep 2
  expect_result R08 restic "stale" restic WARN "stale relevant" "$d/compose.yml" "$d/bl-stale.yml"

  # R16 similar path
  mkdir -p "$d/data/a2"
  echo x >"$d/data/a2/f"
  cat >"$d/compose-sim.yml" <<EOF
services:
  a2:
    image: alpine:3.20
    command: ["sleep", "30"]
    volumes: ["./data/a2:/data"]
EOF
  rm -rf "$repo"
  restic init --repo "$repo" >/dev/null
  restic -r "$repo" backup "$d/data/a" >/dev/null
  expect_result R16 restic "similar" restic FAIL "a vs a2" "$d/compose-sim.yml" "$d/bl.yml"

  # R19 wrong password — ensure ambient env cannot rescue a bad config file
  unset RESTIC_PASSWORD RESTIC_PASSWORD_FILE || true
  echo "wrong-password-value" >"$d/wrong.pass"
  cat >"$d/bl-wrong.yml" <<EOF
backup_paths: []
restic:
  repository: $repo
  password_file: $d/wrong.pass
EOF
  set +e
  out="$(env -u RESTIC_PASSWORD -u RESTIC_PASSWORD_FILE backuplint scan "$d/compose.yml" --config "$d/bl-wrong.yml" 2>&1)"
  ec=$?
  set -e
  actual=ERROR
  gate=pass
  bug=""
  if [[ "$ec" -ne 2 ]]; then gate=fail; bug="wrong password exit=$ec"; fi
  if echo "$out" | grep -q "wrong-password-value"; then gate=fail; bug="password leaked"; fi
  record "R19|restic|auth|restic|ERROR|$actual|$ec|wrong password|$gate|$bug"
  log "OK R19 secret scrub exit=$ec gate=$gate"

  # R22 broken symlink password
  ln -s "$d/missing-pass" "$d/broken.pass"
  cat >"$d/bl-broken.yml" <<EOF
backup_paths: []
restic:
  repository: $repo
  password_file: $d/broken.pass
EOF
  set +e
  out="$(backuplint scan "$d/compose.yml" --config "$d/bl-broken.yml" 2>&1)"
  ec=$?
  set -e
  gate=pass; bug=""
  if [[ "$ec" -ne 2 ]]; then gate=fail; bug="broken symlink exit=$ec"; fi
  record "R22|restic|auth|restic|ERROR|ERROR|$ec|broken pass symlink|$gate|$bug"

  cleanup_compose "$d"
  unset RESTIC_PASSWORD_FILE
}

############################################
# Compose / CLI failure scenarios
############################################
compose_cli_failures() {
  local d="$WORKDIR/failures"
  mkdir -p "$d/dir" "$d"
  echo "backup_paths: []" >"$d/cfg.yml"

  set +e
  out="$(backuplint scan "$d/missing.yml" --config "$d/cfg.yml" 2>&1)"
  ec=$?
  set -e
  gate=pass; bug=""
  [[ "$ec" -eq 2 ]] || { gate=fail; bug="missing compose exit $ec"; }
  echo "$out" | grep -qi Traceback && { gate=fail; bug="traceback on missing compose"; }
  record "C01|cli|missing compose|n/a|ERROR|ERROR|$ec|missing file|$gate|$bug"

  set +e
  out="$(backuplint scan "$d/dir" --config "$d/cfg.yml" 2>&1)"
  ec=$?
  set -e
  gate=pass; bug=""
  [[ "$ec" -eq 2 ]] || { gate=fail; bug="dir compose exit $ec"; }
  record "C02|cli|dir as compose|n/a|ERROR|ERROR|$ec|directory|$gate|$bug"

  : >"$d/empty.yml"
  set +e
  out="$(backuplint scan "$d/empty.yml" --config "$d/cfg.yml" 2>&1)"
  ec=$?
  set -e
  gate=pass; bug=""
  [[ "$ec" -eq 2 ]] || { gate=fail; bug="empty compose exit $ec"; }
  record "C03|cli|empty compose|n/a|ERROR|ERROR|$ec|empty|$gate|$bug"

  echo "not: valid: compose: [" >"$d/bad.yml"
  set +e
  out="$(backuplint scan "$d/bad.yml" --config "$d/cfg.yml" 2>&1)"
  ec=$?
  set -e
  gate=pass; bug=""
  [[ "$ec" -eq 2 ]] || { gate=fail; bug="invalid yaml exit $ec"; }
  record "C04|cli|invalid yaml|n/a|ERROR|ERROR|$ec|invalid|$gate|$bug"
}

############################################
# Scale fixtures (compose config only)
############################################
scale_tests() {
  local d="$WORKDIR/scale"
  mkdir -p "$d/data"
  echo x >"$d/data/f"
  for n in 1 10 50 100; do
    local c="$d/compose-$n.yml"
    {
      echo "services:"
      for i in $(seq 1 "$n"); do
        echo "  s$i:"
        echo "    image: alpine:3.20"
        echo "    command: [\"true\"]"
        echo "    volumes: [\"./data:/data\"]"
      done
    } >"$c"
    write_config "$d/cfg.yml" "$d/data"
    local start end elapsed
    start=$(date +%s%N)
    set +e
    out="$(backuplint scan "$c" --config "$d/cfg.yml" 2>&1)"
    ec=$?
    set -e
    end=$(date +%s%N)
    elapsed=$(( (end - start) / 1000000 ))
    actual=ERROR
    echo "$out" | grep -q 'Result: PASS' && actual=PASS
    gate=pass; bug=""
    [[ "$actual" == PASS && "$ec" -eq 0 ]] || { gate=fail; bug="scale $n actual=$actual exit=$ec"; }
    record "SCALE$n|alpine×$n|many binds|backup_paths|PASS|$actual|$ec|${elapsed}ms|$gate|$bug"
    log "SCALE $n services ${elapsed}ms gate=$gate"
  done
}

############################################
# Main
############################################
main() {
  log "WORKDIR=$WORKDIR"
  storage_patterns
  named_volumes
  app_nginx
  app_redis
  app_postgres
  app_mariadb
  app_mongo
  app_vaultwarden
  app_jellyfin
  app_sonarr
  app_nextcloud
  restic_labs
  compose_cli_failures
  scale_tests

  {
    echo "# Production validation results"
    echo
    echo "Generated: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "Workdir: \`$WORKDIR\`"
    echo
    echo "| ID | Workload | Storage | Method | Expected | Actual | Exit | Edge | Gate | Bug |"
    echo "|----|----------|---------|--------|----------|--------|------|------|------|-----|"
    while IFS='|' read -r id wl st me ex ac ec edge gate bug; do
      echo "| $id | $wl | $st | $me | $ex | $ac | $ec | $edge | $gate | $bug |"
    done <"$MATRIX_OUT"
    echo
    echo "## Totals"
    total=$(wc -l <"$MATRIX_OUT")
    passed=$(grep -c '|pass|' "$MATRIX_OUT" || true)
    failed=$(grep -c '|fail|' "$MATRIX_OUT" || true)
    echo "- rows: $total"
    echo "- pass: $passed"
    echo "- fail: $failed"
  } >"$REPORT_MD"

  log "Wrote $REPORT_MD"
  if grep -q '|fail|' "$MATRIX_OUT"; then
    log "SOME SCENARIOS FAILED"
    grep '|fail|' "$MATRIX_OUT" >&2 || true
    return 1
  fi
  log "ALL RECORDED SCENARIOS PASSED"
}

main "$@"
