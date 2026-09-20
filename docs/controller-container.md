# Controller container (v0.5)

Run the BackupLint **fleet controller** in a container without Docker socket,
Restic, Compose inspection, or host root mounts.

Native `backuplint controller` behavior is unchanged. Container support is additive.

## What is included

- HTTPS controller (`backuplint controller run`)
- OpenSSL (CA / server / client cert issuance)
- Persistent state directory for CA, TLS keys, SQLite, and enrolled agent cert copies

## What is not included

- Docker CLI / Docker socket
- Restic
- Compose file inspection
- BackupLint agent process
- Baked-in CA or private keys

---

## Persistent state layout (authoritative)

Mount a single writable volume at `/state` (default `BACKUPLINT_CONTROLLER_DATA_DIR`):

```text
/state/
  ca/
    ca.key          # PRIVATE — mode 0600 — never embed in image
    ca.crt          # public trust anchor (distribute to agents)
    ca.cnf          # openssl helper config (recreatable)
    ca.srl          # serial file — persist with CA
  server/
    server.key      # PRIVATE — mode 0600 — never embed in image
    server.crt      # server TLS cert (SAN includes BACKUPLINT_CONTROLLER_HOSTNAME)
  agents/
    <agent_id>/
      client.crt    # signed agent certificate copy (private key stays on the agent)
  controller.sqlite3
  controller.sqlite3-wal   # while running — same volume as DB
  controller.sqlite3-shm
  siem/
    siem_export.sqlite3    # optional SIEM durable queue (v0.7)
    telemetry.jsonl        # SIEM export health snapshots
```

### What must be backed up

| Path | Why |
|------|-----|
| Entire `/state` tree | CA, TLS keys, agent registry, submissions/events |
| Especially `ca/`, `server/`, `agents/`, `controller.sqlite3` (+ WAL/SHM or after clean stop) | Loss = new fleet identity / lost history |

Prefer stopping the controller (SIGTERM) before filesystem snapshots so WAL is checkpointed. If hot-copying, include `-wal` and `-shm` siblings on the **same** volume.

### What may be recreated

| Item | Notes |
|------|-------|
| Container filesystem / image layers | Ephemeral; rebuild anytime |
| `/tmp` tmpfs | Ephemeral scratch for OpenSSL/Python |
| `ca.cnf` | Regenerated helpers if missing alongside keys |

### What contains private key material

- `ca/ca.key`
- `server/server.key`
- `agents/*/client.crt` (signed agent certificate copy)

**Enrollment is CSR-based:** the agent generates and holds `client.key`. The controller signs the CSR and does not receive the agent private key. Controller volume theft still exposes the fleet CA and historical results; treat `/state` as high-impact.

### What must never be embedded in the image

- Any `*.key`, PEMs with private material, SQLite DBs, enrollment tokens, or local evidence paths

### Required permissions / ownership

| Item | Expectation |
|------|-------------|
| Process user | `backuplint` UID/GID **10001** (override via build-args) |
| `/state` volume | Writable by that UID (named volumes usually inherit image `/state` ownership) |
| Private keys | `0600` when BackupLint writes them |
| Privileged mode | **Not required and must not be used** |

Incorrectly owned bind mounts fail at startup with a clear preflight error (non-zero exit). The entrypoint does **not** relax key modes automatically.

### If state is lost

- Fleet CA trust is gone; agents must re-enroll against a new CA
- Historical submissions/events are gone
- Treat as disaster recovery: restore from backup, or accept a new fleet

Keep the SQLite database and its WAL/SHM siblings on the **same** volume.

Optional `/config` is not required today: controller runtime is configured via
environment variables / CLI flags rather than a YAML config file.

---

## Writable locations under read-only rootfs

| Path | Type | Purpose |
|------|------|---------|
| `/state` | persistent volume | CA, TLS, SQLite, agent cert copies |
| `/tmp` | tmpfs | OpenSSL/Python ephemeral files (`TMPDIR=/tmp`) |

No other writable paths are required. `HOME=/state` so libraries that write under `$HOME` do not need the image root.

---

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `BACKUPLINT_CONTROLLER_DATA_DIR` | `/state` | Durable data directory |
| `BACKUPLINT_CONTROLLER_LISTEN` | `0.0.0.0:8443` | Bind address inside the container |
| `BACKUPLINT_CONTROLLER_HOSTNAME` | `localhost` | Value embedded in the server certificate SAN |
| `BACKUPLINT_CONTROLLER_DASHBOARD` | unset/`0` | Set `1`/`true` to enable optional dashboard routes |
| `BACKUPLINT_DASHBOARD_PASSWORD_FILE` | `/state/dashboard/password.scrypt` | Optional override path for dashboard password hash |
| `BACKUPLINT_SIEM_CONFIG_FILE` | unset | Path to SIEM YAML mounted into the container |
| `BACKUPLINT_CONTROLLER_SIEM` | unset/`0` | Set `1`/`true` with `BACKUPLINT_SIEM_CONFIG_FILE` to enable export |
| `HOME` | `/state` | Avoid writes outside the volume |
| `TMPDIR` | `/tmp` | Ephemeral scratch |

Set `BACKUPLINT_CONTROLLER_HOSTNAME` to the DNS name or IP that agents will use
in `--controller https://…`. The issued server certificate **always** includes
`DNS:localhost` and `IP:127.0.0.1` plus the configured hostname (lab-friendly).
Agents connecting by a hostname/IP **not** in that SAN set will fail TLS verification.
Changing hostname after first start does **not** automatically reissue `server.crt`
(existing files are kept); delete `server/` certs deliberately if SAN must change.

---

## UID / GID

The image runs as non-root user **`backuplint` (UID/GID 10001)** by default.

```bash
# Host bind-mount bootstrap (does not weaken key modes):
sudo mkdir -p /var/lib/backuplint-controller
sudo chown 10001:10001 /var/lib/backuplint-controller
```

Custom image UIDs:

```bash
docker build -f Dockerfile.controller \
  --build-arg BACKUPLINT_UID=10001 \
  --build-arg BACKUPLINT_GID=10001 \
  -t backuplint-controller:local .
```

---

## Linux capabilities

The hardened Compose example uses `cap_drop: [ALL]`. Binding port **8443** does not
require `NET_BIND_SERVICE`. Do not set `privileged: true`.

---

## Build and run

```bash
docker build -f Dockerfile.controller -t backuplint-controller:local .
docker run --rm --name backuplint-controller \
  -p 8443:8443 \
  -e BACKUPLINT_CONTROLLER_HOSTNAME=localhost \
  -v backuplint-controller-state:/state \
  backuplint-controller:local
```

Compose:

```bash
# Simple bring-up
docker compose -f deploy/controller/compose.yml up --build

# Hardened (read-only rootfs, cap_drop ALL, no-new-privileges, tmpfs, limits)
docker compose -f deploy/controller/compose.hardened.yml up --build
```

Hardened run equivalent:

```bash
docker run --rm --read-only --cap-drop ALL --security-opt no-new-privileges \
  --tmpfs /tmp:size=32m,mode=1777 \
  -p 8443:8443 \
  -e BACKUPLINT_CONTROLLER_HOSTNAME=localhost \
  -v backuplint-controller-state:/state \
  backuplint-controller:local
```

---

## Healthcheck

The image `HEALTHCHECK` probes `GET https://127.0.0.1:<port>/v1/health` over
loopback with TLS 1.2+, without agent credentials. It parses JSON and requires
`{"ok": true, ...}`. It does **not** mutate state and does **not** accept plaintext HTTP.

This proves **listener readiness**, not full fleet correctness (enrollment, DB
integrity, or agent paths are out of scope for the probe).

---

## Graceful shutdown / signals

The entrypoint uses `exec` so SIGTERM/SIGINT reach `backuplint controller run`.
The process stops the HTTPS server, closes SQLite, and exits. Prefer
`docker stop -t 10` (or longer) over immediate kill when possible.

After SIGKILL, restarting against the same `/state` volume is expected to recover
via SQLite WAL as long as the volume was not corrupted.

---

## Upgrade / recreate sequence

1. **Backup** `/state` (after SIGTERM stop when possible).
2. **Stop** old container (`docker stop -t 10`).
3. **Build/pull** the new controller image. Do not push to a registry unless the owner has authorized publication.
4. **Start** new container with the **same** `/state` volume and hostname env.
5. **Health** — wait until HEALTHCHECK / `/v1/health` succeeds.
6. **Validate** — `controller agents` still lists expected agents; revoked stay revoked.
7. **Rollback** — if health fails or agents cannot enroll/submit, stop new container and start previous image tag against the same (or restored) `/state`.

Do not change store migration internals here; forward migrations run on open as implemented by the shared controller store.

---

## Native agent → containerized controller

```bash
backuplint agent enroll \
  --controller https://127.0.0.1:8443 \
  --token "$TOKEN" \
  --ca-cert /path/to/ca.crt \
  --identity-dir ./bl-agent
```

Copy `ca.crt` from `/state/ca/ca.crt` on the persistent volume.

Admin one-shot:

```bash
docker run --rm \
  -v backuplint-controller-state:/state \
  --entrypoint backuplint \
  backuplint-controller:local \
  controller enroll-token --data-dir /state --label web1
```

---

## Automated hardening harness

```bash
./deploy/controller/run-hardening-harness.sh
```

Uses unique volumes/names, free ports, cleans up on success, preserves logs under
a temp directory on failure, and never touches operator databases outside those unique volumes.

---

## Image hygiene / SBOM / vulnerability scan (release procedure)

Build contains only runtime package sources (see `.dockerignore`: no tests,
docs, evidence, keys, or local DBs). OCI labels include title, source,
license, and version.

Repeatable local release checks (tools optional — do not fail CI if missing):

```bash
# Inventory packages inside the image
docker run --rm --entrypoint pip backuplint-controller:local freeze

# SBOM (if syft is installed)
syft backuplint-controller:local -o spdx-json > controller.sbom.spdx.json

# Vulnerability scan (if trivy or grype is installed)
trivy image backuplint-controller:local
# or: grype backuplint-controller:local
```

This is an operator workflow, not an independent security review.

---

## Docker vs Podman

- **Docker Engine** is the primary validated runtime for this image and Compose examples.
- **Podman / rootless Podman** should work for basic `podman build` / `podman run` with
  the same volume and `cap_drop` flags, but is **not validated** in environments where
  Podman is not installed. No Docker-only socket is required by the controller image.
- Rootless volume ownership still must match UID 10001 inside the container.

---

## Packaging note

The controller image installs the BackupLint Python package plus OpenSSL only.
A pip-extra / install-profile split for controller-only installs is owned by the
install-profile workstream (Agent 2) and is not required for this container path.

## Optional SIEM export (v0.7)

See [siem.md](siem.md) for `BACKUPLINT_SIEM_CONFIG_FILE`, queue persistence under
`/state/siem/`, and mTLS `/v1/siem/status`.
