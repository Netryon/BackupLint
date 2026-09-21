<p align="center">
  <img src="docs/images/logo-mark-blue.svg" width="72" height="72" alt="BackupLint logo">
</p>

<h1 align="center">BackupLint</h1>

<p align="center"><strong>Find Docker data your backups missed before you discover it during a restore.</strong></p>

<p align="center"><em>Don’t just assume your backups work. Prove they work.</em></p>

BackupLint is an independent **backup-assurance** layer for Docker Compose stacks. It compares Compose mounts with your backup configuration and reports **PASS**, **WARN**, **FAIL**, or operational **ERROR**.

It is **not** a backup engine. It does not replace Restic, Borg, Kopia, or your backup jobs. For v1, **Restic is the only supported engine** when engine-backed checks are enabled. It does not SSH into hosts, delete data, or restore over live application files.

```text
coverage ≠ integrity ≠ restore verification ≠ disaster recovery
presence (online / stale / offline) ≠ audit health
coverage FAIL ≠ repository-unavailable ERROR
```

## Choose your deployment

BackupLint is **one package**. Pick a role:

| Role | Use it when | What it does |
| --- | --- | --- |
| **Standalone** | One machine, local backup assurance only | Audits this machine. No fleet controller/dashboard and does not receive other agents. |
| **Agent** | This machine reports to an existing BackupLint controller | Audits locally and reports over mTLS. No controller/dashboard on this machine. |
| **Controller** | Dedicated central management node | Receives agent reports, hosts the optional dashboard, central policy, fleet state, and optional SIEM export. |
| **All-in-one** | The main server should audit itself **and** manage other agents | Runs local checks **and** the fleet controller/dashboard on the same machine. |

```text
One server only → Standalone
Dedicated management server → Controller
Additional monitored servers → Agent
Main server + fleet management on one machine → All-in-one
```

**Standalone vs all-in-one:** standalone never runs a controller. All-in-one does: it is a controller that also audits the host it runs on. Use standalone unless you need a fleet.

Details: [docs/installation.md](docs/installation.md), [docs/installation-profiles.md](docs/installation-profiles.md).

## 60-second local check (standalone)

1. Install the release wheel (see [Native install](#native--bare-metal-install)).
2. Create `backuplint.yml` next to your Compose file:

```yaml
backup_paths:
  - /srv/docker
  - /srv/app-config
```

3. Scan:

```bash
backuplint scan compose.yml
backuplint scan compose.yml --config backuplint.yml --json
```

```text
BackupLint Audit

✓ sonarr       /srv/docker/sonarr    protected
✓ radarr       /srv/docker/radarr    protected
✗ vaultwarden  /srv/vaultwarden      not protected

Result: FAIL
```

### Want fleet management?

Use **controller** on a management node (or **all-in-one** on the main server), then **agent** on every other host. See [Choose your deployment](#choose-your-deployment), [docs/fleet.md](docs/fleet.md), and [docs/dashboard.md](docs/dashboard.md).

## What BackupLint needs from you

BackupLint does **not** require one broad privileged “admin account”. You supply only what each role needs.

### Local audit (standalone, agent, or all-in-one)

- Compose file path
- BackupLint config (`backuplint.yml`)
- Expected backup paths
- Docker Engine + Compose plugin access when Compose discovery is used
- Restic repository location for Restic-backed checks
- Restic password via `password_file`, `RESTIC_PASSWORD`, or `RESTIC_PASSWORD_FILE` (`password_file` always wins). Passwords are never printed.

### Controller

- Hostname/DNS name agents will use (certificate SAN)
- Listener address/port (typically `8443`)
- Controller state directory
- Dashboard operator password if the dashboard is enabled
- Optional SIEM endpoint/config

### Agent

- Controller URL
- Unique agent ID from `enroll-token`
- One-time enrollment token
- Controller CA certificate
- Local Compose/config/Restic access required for the checks that agent will run

Enrollment:

- the **agent generates its private key locally**
- the controller signs a **CSR**
- the **agent private key is not copied to the controller**
- the enrollment token is **one-use**
- after enrollment, traffic is **mTLS**

Revoke with `backuplint controller revoke <agent_id>`.

## Native / bare-metal install

Python **3.11+** (`venv`). Rocky/Alma 9: use `python3.11`. Docker Engine + Compose plugin for `scan`. Restic on `PATH` only if you enable engine-backed checks.

Download `backuplint-1.0.0-py3-none-any.whl` from the [v1.0.0 GitHub Release](https://github.com/Netryon/BackupLint/releases/tag/v1.0.0), then:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install backuplint-1.0.0-py3-none-any.whl
backuplint --version
```

Interactive native installer (directories, optional packages, systemd):

```bash
sudo backuplint install --interactive --yes
```

Unattended profile:

```bash
sudo backuplint install --profile profile.yaml --non-interactive --yes
```

Roles, layout, CSR enrollment, upgrades, uninstall: [docs/installation.md](docs/installation.md), [docs/native-installer.md](docs/native-installer.md).

## Docker install

Official GHCR images are published for **linux/amd64**. Raspberry Pi 4 remains a native-install current-candidate platform.

```bash
docker pull ghcr.io/netryon/backuplint-controller:1.0.0
docker pull ghcr.io/netryon/backuplint-agent:1.0.0
```

The same images are also tagged `v1.0.0` and `latest` (stable 1.0.0 convenience tag). Prefer digest pinning in production; immutable digests are listed on the [v1.0.0 GitHub Release](https://github.com/Netryon/BackupLint/releases/tag/v1.0.0).

Controller and agent images run as **uid 10001**. Privileged mode is not required. A Docker socket is **not** required for heartbeat, enrollment, or policy. Compose discovery **inside** an agent container needs intentional host/Docker access (not the default).

Local source builds remain available as an advanced option:

```bash
docker build -f Dockerfile.controller --build-arg BACKUPLINT_VERSION=1.0.0 -t backuplint-controller:1.0.0 .
docker build -f Dockerfile.agent --build-arg BACKUPLINT_VERSION=1.0.0 -t backuplint-agent:1.0.0 .
```

[docs/docker.md](docs/docker.md)

## Dashboard

Optional **read-only** UI on the controller (`https://…:8443/dashboard/`). Presence and audit health stay separate. The dashboard does not write policy.

v1 authentication: a **shared operator password**, stored as a **scrypt** hash. There is **no multi-user RBAC** and **no SSO** in v1.

```bash
backuplint controller dashboard-password --data-dir ./bl-controller
backuplint controller run --data-dir ./bl-controller --listen 127.0.0.1:8443 --dashboard
```

[docs/dashboard.md](docs/dashboard.md)

## Screenshots

Public-safe demo fleet (synthetic labels).

![Fleet health at a glance — presence and audit state remain separate.](docs/images/01-dashboard-fleet-overview.png)

![A healthy agent stays online with a current PASS audit.](docs/images/02-dashboard-healthy-agent.png)

![Coverage failures and operational repository errors are reported distinctly.](docs/images/03-dashboard-fail-error-mixed.png)

![Historical assurance trends from real stored audit events.](docs/images/04-dashboard-history-trends.png)

![Read-only central policy visibility with drift and rollout state.](docs/images/05-dashboard-policy.png)

![CLI coverage plus Restic integrity and isolated restore verification.](docs/images/07-cli-coverage-integrity.png)

![BackupLint failing when Vaultwarden data is missing from backups](docs/demo/backup-audit-fail.gif)

## Supported platforms (v1.0.0)

| Platform | Arch | Current-candidate |
| --- | --- | --- |
| Ubuntu | x86_64 | tested |
| Debian | x86_64 | tested |
| Rocky Linux 9 | x86_64 | tested |
| Fedora 43 | x86_64 | tested |
| Raspberry Pi 4 / Raspberry Pi OS | ARM64 | tested |
| Native controller + agent | — | tested |
| Container controller + agent, including mixed topologies | — | tested |

Raspberry Pi **3** is not a current-candidate proof. WSL2 is **unsupported**.

Controller sizing is evidence-backed **through 500 agents** (about **6–8 MB/agent/day** management-plane traffic at the tested 30s cadence). That **excludes Restic backup payload**. **Do not treat >500 agents as certified capacity.**

[docs/support-matrix.md](docs/support-matrix.md) · [docs/validation.md](docs/validation.md) · [docs/architecture.md](docs/architecture.md)

## Security

Read-only diagnostics. No deletion of user data, no modification of backups, no secret printing from Compose, `.env`, or backup tools.

Container images: digest-pinned `python:3.12-slim-bookworm`, Trivy-reviewed, residual CVEs remain. **Not a “zero CVE” claim.** Agent: non-root uid 10001, unprivileged, no baked secrets.

[docs/security.md](docs/security.md) · [SECURITY.md](SECURITY.md)

## Known limitations

- Named volume coverage depends on local `docker volume inspect`; missing volumes are **WARN**, not FAIL
- Cache/temporary mounts are skipped by path heuristics
- Restic coverage uses snapshot path roots, not file-level contents
- Database image warnings are advisory; filesystem coverage is not a consistent DB backup
- Isolated restore verification is not a disaster-recovery drill
- Unit tests alone are not production readiness

## Documentation

| Topic | Doc |
| --- | --- |
| Native install | [docs/installation.md](docs/installation.md) |
| Native installer / systemd | [docs/native-installer.md](docs/native-installer.md) |
| Docker | [docs/docker.md](docs/docker.md) |
| Fleet / enrollment | [docs/fleet.md](docs/fleet.md) |
| Dashboard | [docs/dashboard.md](docs/dashboard.md) |
| Troubleshooting | [docs/troubleshooting.md](docs/troubleshooting.md) |
| Validation evidence | [docs/validation.md](docs/validation.md) |
| Release notes | [docs/release-notes-v1.0.0.md](docs/release-notes-v1.0.0.md) |

## Feedback, bugs, and questions

Feedback is welcome. Please use the channel that matches what you need:

- **Bug report:** [open a bug report](https://github.com/Netryon/BackupLint/issues/new?template=bug_report.yml)
- **Feature request / improvement idea:** [request a feature](https://github.com/Netryon/BackupLint/issues/new?template=feature_request.yml)
- **Setup / usage question:** [ask a question](https://github.com/Netryon/BackupLint/issues/new?template=question.yml)
- **Security vulnerability:** use a [private GitHub Security Advisory](https://github.com/Netryon/BackupLint/security/advisories/new) — do **not** post secrets or vulnerability details in a public issue.

Before posting logs or configuration, remove passwords, tokens, private keys, internal hostnames/IPs, repository credentials, and personal data.

## Development install

For contributors only (editable tree + test extras):

```bash
git clone https://github.com/Netryon/BackupLint.git
cd BackupLint
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
ruff check src tests
pytest -q tests/unit
```

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE) © 2026 BackupLint contributors.

[CONTRIBUTING.md](CONTRIBUTING.md) · [SECURITY.md](SECURITY.md)
