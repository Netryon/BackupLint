#!/usr/bin/env bash
# Build disposable demo stacks with short /srv paths for clean README media.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DEMO="${BACKUPLINT_DEMO_DIR:-/srv/backuplint-demo}"

sudo rm -rf "$DEMO" /srv/sonarr /srv/radarr /srv/vaultwarden /srv/postgres /srv/jellyfin /srv/app-data
sudo mkdir -p \
  "$DEMO/pass" "$DEMO/fail" "$DEMO/restic" \
  /srv/sonarr /srv/radarr /srv/vaultwarden /srv/postgres /srv/jellyfin /srv/app-data
sudo chown -R "$(id -u):$(id -g)" /srv/sonarr /srv/radarr /srv/vaultwarden \
  /srv/postgres /srv/jellyfin /srv/app-data "$DEMO"

echo demo >/srv/app-data/file.txt

# PASS
cat >"$DEMO/pass/compose.yml" <<'EOF'
services:
  sonarr:
    image: alpine:3.20
    command: ["sleep", "3600"]
    volumes:
      - /srv/sonarr:/config
  radarr:
    image: alpine:3.20
    command: ["sleep", "3600"]
    volumes:
      - /srv/radarr:/config
  vaultwarden:
    image: alpine:3.20
    command: ["sleep", "3600"]
    volumes:
      - /srv/vaultwarden:/data
EOF
cat >"$DEMO/pass/backuplint.yml" <<'EOF'
backup_paths:
  - /srv
EOF

# FAIL — vaultwarden omitted
cat >"$DEMO/fail/compose.yml" <<'EOF'
services:
  postgres:
    image: alpine:3.20
    command: ["sleep", "3600"]
    volumes:
      - /srv/postgres:/var/lib/postgresql/data
  jellyfin:
    image: alpine:3.20
    command: ["sleep", "3600"]
    volumes:
      - /srv/jellyfin:/config
  vaultwarden:
    image: alpine:3.20
    command: ["sleep", "3600"]
    volumes:
      - /srv/vaultwarden:/data
EOF
cat >"$DEMO/fail/backuplint.yml" <<'EOF'
backup_paths:
  - /srv/postgres
  - /srv/jellyfin
EOF

# Restic
echo "demo-pass" >"$DEMO/restic/pass"
rm -rf "$DEMO/restic/repo"
RESTIC_PASSWORD_FILE="$DEMO/restic/pass" restic init --repo "$DEMO/restic/repo" >/dev/null
RESTIC_PASSWORD_FILE="$DEMO/restic/pass" restic -r "$DEMO/restic/repo" backup /srv/app-data >/dev/null
cat >"$DEMO/restic/compose.yml" <<'EOF'
services:
  app:
    image: alpine:3.20
    command: ["sleep", "3600"]
    volumes:
      - /srv/app-data:/data
EOF
cat >"$DEMO/restic/backuplint.yml" <<EOF
backup_paths: []
max_backup_age: 24h
restic:
  repository: $DEMO/restic/repo
  password_file: $DEMO/restic/pass
EOF

echo "Demo fixtures ready (paths under /srv/...)"
