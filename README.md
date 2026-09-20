# BackupLint

**Find Docker data your backups missed before you discover it during a restore.**

BackupLint is an **independent backup-assurance layer**. It is **not** a backup engine. It does not replace Restic, Borg, Kopia, or your backup jobs. For v1, **Restic is the only supported backup engine** when engine-backed checks are enabled. Borg and Kopia are not supported.

It is a read-only CLI (optional fleet controller/agent and dashboard) that compares Docker Compose mounts with your backup configuration and reports **PASS**, **WARN**, **FAIL**, or operational **ERROR**.

```bash
backuplint scan compose.yml
backuplint --version
```

![BackupLint failing when Vaultwarden data is missing from backups](docs/demo/backup-audit-fail.gif)

## What it does

BackupLint answers one question:

> Does my Docker Compose stack contain persistent data that my backup configuration is not protecting?

Those checks are separate:

```text
coverage ≠ integrity ≠ restore verification ≠ disaster recovery
presence (online / stale / offline) ≠ audit health
coverage FAIL ≠ repository-unavailable ERROR
```

- **Coverage** asks whether configured backup paths (or Restic snapshot roots) cover Compose mounts.
- **Restic standard integrity** (`restic check`) does **not** mean every stored byte was read.
- **`--read-data` / deep integrity** is still not a successful restore.
- **Filesystem restore verification** does not prove application or database consistency.
- A missing or unreachable repository is an operational **ERROR**, not a coverage **FAIL**.

## Why it exists

Self-hosted Docker stacks accumulate bind mounts and named volumes. Backup jobs miss paths after services are added or renamed. BackupLint catches those gaps before a restore is needed.

## Example

```text
BackupLint Audit

✓ sonarr       /srv/docker/sonarr    protected
✓ radarr       /srv/docker/radarr    protected
✗ vaultwarden  /srv/vaultwarden      not protected

Result: FAIL
```

Successful audits and Restic checks:

![Successful BackupLint audit](docs/demo/backup-audit-pass.gif)

![Restic snapshot coverage](docs/demo/restic.gif)

## Features

- Compose mount discovery (`docker compose config`)
- Persistent / cache / temporary classification
- Static `backup_paths` coverage checks
- Named Docker volume mountpoint checks
- Optional Restic snapshot coverage and `max_backup_age` freshness
- Optional Restic repository integrity checks (`standard` / opt-in `deep`)
- Optional restore verification (`selected` / `full`) into BackupLint-owned temp paths
- Local scheduled verification (`backuplint daemon` / `backuplint schedule`)
- Optional fleet controller/agent (HTTPS + mTLS); native or container
- Optional read-only dashboard (presence and audit health stay separate)
- Optional SIEM export (durable queue; outage isolation)
- Central policy / GitOps import-export-diff, rollout, drift (see [docs/central-policy.md](docs/central-policy.md))
- Database image warnings (Postgres, MySQL/MariaDB, MongoDB)
- Text and `--json` output with stable exit codes
- CI: ruff, Bandit, pip-audit, unit and integration tests

## Dashboard and CLI (v1)

These images are from a public-safe demo fleet (synthetic labels). Presence and audit health remain separate.

![Fleet health at a glance — presence and audit state remain separate.](docs/images/01-dashboard-fleet-overview.png)

![A healthy agent stays online with a current PASS audit.](docs/images/02-dashboard-healthy-agent.png)

![Coverage failures and operational repository errors are reported distinctly.](docs/images/03-dashboard-fail-error-mixed.png)

![Historical assurance trends from real stored audit events.](docs/images/04-dashboard-history-trends.png)

![Read-only central policy visibility with drift and rollout state.](docs/images/05-dashboard-policy.png)

![Real Restic-backed integrity and restore verification.](docs/images/07-cli-coverage-integrity.png)

## Platforms (v1.0.0 current-candidate evidence)

Documented current-candidate proofs for this release lineage:

| Platform | Arch | Current-candidate |
| --- | --- | --- |
| Ubuntu | x86_64 | tested |
| Debian | x86_64 | tested |
| Rocky Linux 9 | x86_64 | tested |
| Fedora 43 | x86_64 | tested |
| Raspberry Pi 4 Model B / Raspberry Pi OS | ARM64 | tested |
| Native controller + agent | — | tested |
| Container controller + agent, including mixed topologies | — | tested |

Raspberry Pi **3** is **not** a current-candidate proof for this release. WSL2 is **unsupported / not claimed**.

Details: [docs/support-matrix.md](docs/support-matrix.md), [docs/platform-compatibility.md](docs/platform-compatibility.md).

## Sizing (tested scope only)

Controller sizing and management-plane network estimates are evidence-backed **through 500 agents** on the tested 30s heartbeat/policy cadence (roughly **6–8 MB/agent/day on the wire**). That figure **excludes Restic backup payload traffic**, which does not traverse BackupLint. Compact synthetic report payloads were smaller than some production reports may be. Storage and network numbers are workload-dependent. **Do not treat >500 agents as a certified production capacity.**

## Container images (security wording)

Controller and agent images use a digest-pinned `python:3.12-slim-bookworm` base. They were scanned with Trivy. A base refresh removed an avoidable pcre2 High. Remaining Critical/High findings were reviewed as runtime-inapplicable, upstream no-fix/deferred, or bounded residual risk. **This is not a “zero CVE” claim.** No baked secrets were found. The agent runs non-root (uid 10001), unprivileged, with no Docker socket requirement.

## Installation

Prerequisites:

- Python 3.11+ with the standard `venv` module available
  - On Debian/Ubuntu this usually means installing `python3`, `python3-venv`, and `python3-pip`
  - On Rocky/Alma 9 install `python3.11` and `python3.11-pip` (the default `python3` is 3.9)
- Git
- Docker Engine and the Docker Compose plugin (`docker compose`) for `backuplint scan`
- Permission to talk to the Docker API (typically membership in the `docker` group)
- Optional: [Restic](https://restic.net/) on `PATH` when using the `restic:` configuration section

```bash
git clone git@github.com:Netryon/backuplint.git
# or: git clone https://github.com/Netryon/backuplint.git
cd backuplint
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Confirm the CLI:

```bash
backuplint --help
backuplint --version
```

## Quick start

Create `backuplint.yml` next to your Compose file (see `backuplint.yml.example`):

```yaml
backup_paths:
  - /srv/docker
  - /srv/app-config
```

```bash
backuplint scan path/to/compose.yml
backuplint scan path/to/compose.yml --config path/to/backuplint.yml
backuplint scan path/to/compose.yml --json
```

## Configuration

BackupLint reads a YAML file with:

```yaml
backup_paths:
  - /srv/docker
  - /srv/config
```

Default search order for `--config`:

1. `backuplint.yml` beside the Compose file
2. `./backuplint.yml` in the current working directory

Duplicate path entries are ignored. An empty `backup_paths` list is allowed and treats persistent bind mounts as unprotected.

## How detection works

See [docs/how-it-works.md](docs/how-it-works.md) for Compose discovery, classification, and path coverage rules.

## Docker Compose support

BackupLint runs `docker compose config --format json` against the Compose file you provide. That uses Docker's own resolution for relative paths and mount syntax instead of reimplementing Compose rules.

The tool is read-only: it does not modify Compose files or start/stop services during a normal scan (test labs may start disposable containers separately).

Requirements for `scan`:

- Docker Engine installed
- Docker Compose plugin available as `docker compose`
- Permission to talk to the Docker API (typically membership in the `docker` group)

## Restic support

Optional read-only inspection of a local or remote Restic repository. **Restic is the only v1 backup engine.**

```yaml
backup_paths: []
restic:
  repository: /var/backups/restic
  password_file: ./restic.pass
  integrity:
    mode: standard   # off | standard | deep
max_backup_age: 24h
```

When `restic` is configured, each mount is checked against the **newest snapshot that covers that path** (not merely the globally newest snapshot). Optional `max_backup_age` marks covered-but-stale backups as warnings.

Optional integrity verification runs `restic check` (`standard`) or `restic check --read-data` (`deep`, opt-in). CLI override: `--integrity off|standard|deep`. Optional restore verification: `restic.restore_verification.mode` / `--restore-verify off|selected|full`. Details: [docs/integrity-checks.md](docs/integrity-checks.md) and [docs/restore-verification.md](docs/restore-verification.md).

Password comes from `password_file`, `RESTIC_PASSWORD`, or `RESTIC_PASSWORD_FILE`. Configured `password_file` always overrides ambient environment credentials. Passwords are never printed.

BackupLint only runs `restic snapshots --json` and, when requested, `restic check` / `restic check --read-data`, plus isolated restore into owned temp paths when restore verification is enabled. It does not backup, prune, repair, or modify the repository, and it does not restore over live application data.

## Local scheduling

Run checks automatically on one host:

```bash
backuplint daemon compose.yml -c backuplint.yml
backuplint schedule status compose.yml -c backuplint.yml
```

See [docs/scheduling.md](docs/scheduling.md) and `docs/systemd/backuplint-scheduler.service`.

## Fleet reporting

Agents submit local scan results to a central HTTPS controller (mTLS after enrollment). Native and container deployments are supported; mixed topologies were tested.

```bash
backuplint controller init --data-dir ./bl-controller
backuplint agent enroll --controller https://controller:8443 --token-file ./enroll.token --ca-cert ca.crt
```

See [docs/fleet.md](docs/fleet.md), [docs/dashboard.md](docs/dashboard.md), [docs/agent-container.md](docs/agent-container.md), and [docs/controller-container.md](docs/controller-container.md).

## Installation profiles

BackupLint stays one package, but installs are role-aware (`standalone`, `agent`,
`controller`, `all_in_one`) with native/container deployment forms. Profiles are
YAML/JSON; planning never silently installs missing dependencies.

```bash
backuplint profile validate profile.yaml
backuplint profile plan profile.yaml
```

See [docs/installation-profiles.md](docs/installation-profiles.md). Native
installer/service provisioning: [docs/native-installer.md](docs/native-installer.md).

## Continuous integration

Pull requests and pushes to `main` run `.github/workflows/ci.yml`:

- Install the package with development extras
- `ruff check`
- `bandit` on `src/`
- `pip-audit` for known dependency vulnerabilities
- Unit tests, then integration tests (Docker and Restic available on the runner)

No repository secrets are written to workflow logs.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | `PASS` or `WARN` (no critical unprotected persistent mounts) |
| 1 | `FAIL` (unprotected persistent mounts and/or integrity failure) |
| 2 | Program, configuration, or runtime error |

## Limitations

- Named volume coverage depends on `docker volume inspect` mountpoints for volumes that exist locally
- Missing named volumes are **WARN** (review required), not FAIL — BackupLint cannot prove the host path
- Cache/temporary mounts are skipped by target-path heuristics (`/cache`, `/tmp`, …)
- Restic coverage uses snapshot path roots, not file-level contents inside snapshots
- Database detection is advisory; filesystem coverage does not prove a consistent DB backup
- A covering Restic snapshot without a usable timestamp is treated as protected unless `max_backup_age` is set
- Standard integrity checks repository metadata/structure; deep mode also reads data blobs — neither proves restore success
- Isolated restore verification is not a disaster-recovery drill and does not prove application consistency
- Read-only diagnostic design: BackupLint will not modify Compose files, volumes, or backup repositories
- Unit tests alone do not constitute production readiness

## Security model

BackupLint is intended as a read-only diagnostic utility. It must not delete data, modify backups, or expose secrets from Compose, `.env`, or backup tools.

See [docs/security.md](docs/security.md) and [SECURITY.md](SECURITY.md).

## Development

```bash
git clone git@github.com:Netryon/backuplint.git
cd backuplint
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

v1.0.0 release notes: [docs/release-notes-v1.0.0.md](docs/release-notes-v1.0.0.md).
Release engineering: [docs/release-engineering.md](docs/release-engineering.md).

## Testing

```bash
source .venv/bin/activate
pytest -q tests/unit
sg docker -c 'source .venv/bin/activate && pytest -q tests/integration'
ruff check src tests
bandit -r src
./test-lab/run-milestone6.sh
./test-lab/production/run-production-validation.sh
```

Demo GIF regeneration: [docs/demo/README.md](docs/demo/README.md).
