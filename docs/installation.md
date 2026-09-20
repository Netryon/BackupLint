# Native installation

BackupLint is one Python package. Choose a **role** (standalone, agent, controller, or all-in-one) rather than installing multiple products.

Python **3.11+** with `venv` is required. Rocky/Alma 9 should use `python3.11`. [Restic](https://restic.net/) is required on `PATH` only when you enable engine-backed coverage, integrity, or restore verification. Docker Engine plus the Compose plugin (`docker compose`) is required for `backuplint scan`.

## Install from a release wheel (recommended)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install dist/backuplint-1.0.0-py3-none-any.whl
backuplint --version   # BackupLint 1.0.0
```

From a clone of this repository:

```bash
git clone https://github.com/Netryon/BackupLint.git
cd BackupLint
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Confirm:

```bash
backuplint --help
backuplint --version
```

## Roles

| Role | Typical use |
| --- | --- |
| Standalone | `backuplint scan` / `daemon` on the Compose host |
| Controller | HTTPS fleet API, optional dashboard, optional SIEM export |
| Agent | Enroll, heartbeat, submit local audits |
| All-in-one | Controller and agent on the same host |

Profiles and unattended native provisioning: [installation-profiles.md](installation-profiles.md) and [native-installer.md](native-installer.md).

## Default filesystem layout (system install)

When using `backuplint install` without `--prefix`:

```text
/etc/backuplint/                 config
/var/lib/backuplint/             state root
/var/lib/backuplint/controller/  controller CA, TLS, SQLite
/var/lib/backuplint/agent/       agent identity and durable queue
/var/log/backuplint/             logs
/etc/systemd/system/backuplint-*.service
```

Lab installs can use `--prefix /path` so nothing is written to system directories.

Permissions are restrictive (`0700`/`0750` directories, `0640` config). The process should not run as a shared unprivileged user that other software can read private keys from.

## Standalone scan

```yaml
# backuplint.yml next to compose.yml
backup_paths:
  - /srv/docker
```

```bash
backuplint scan compose.yml
backuplint scan compose.yml --config backuplint.yml --json
```

Local scheduling: [scheduling.md](scheduling.md).

## Controller (native)

```bash
backuplint controller init --data-dir ./bl-controller --hostname controller.example
backuplint controller run --data-dir ./bl-controller --listen 127.0.0.1:8443
```

- Listen port **8443/tcp** (or the address you pass to `--listen`). Open it only to agents that should enroll/heartbeat.
- State directory holds the fleet CA, server TLS key, SQLite, and dashboard password hash. Back it up; see [controller-recovery.md](controller-recovery.md).
- Optional dashboard: enable dashboard flags/env as documented in [dashboard.md](dashboard.md).

Mint a one-time enrollment token:

```bash
backuplint controller enroll-token --data-dir ./bl-controller --label web1
```

## Agent (native) — CSR enrollment

The agent generates its own private key and submits a CSR. The controller signs the CSR and never handles the agent private key.

```bash
backuplint agent enroll \
  --controller https://controller.example:8443 \
  --token "$ENROLL_TOKEN" \
  --agent-id "$AGENT_ID" \
  --ca-cert ./bl-controller/ca/ca.crt \
  --identity-dir ./bl-agent
```

Identity files (`client.key`, `client.crt`, `ca.crt`) live under `--identity-dir`. After enrollment, heartbeats and submits use mTLS.

```bash
backuplint agent run --controller https://controller.example:8443 --identity-dir ./bl-agent
backuplint agent submit compose.yml -c backuplint.yml \
  --controller https://controller.example:8443 --identity-dir ./bl-agent
```

Revoke an agent on the controller with `backuplint controller revoke <agent_id>`.

## systemd

Unit templates live under `docs/systemd/`. The native installer can install units for scheduler, controller, and agent. Typical operations:

```bash
sudo systemctl enable --now backuplint-controller.service
sudo systemctl restart backuplint-controller.service
sudo systemctl stop backuplint-controller.service
```

Prefix/lab installs skip host `systemctl` and write units under the prefix.

## Upgrades

Install the new wheel or git tree into the same venv (or let the native installer replace the package). Keep `--data-dir` / `/var/lib/backuplint` unchanged. Agents and controllers negotiate a small protocol window; see [fleet.md](fleet.md).

Do not copy a fleet CA between unrelated controllers.

## Uninstall / cleanup

```bash
backuplint install uninstall --dry-run
sudo backuplint install uninstall --execute --yes
```

Uninstall removes installer-managed units and manifest paths. It is designed to **keep** secrets and state unless you delete those directories yourself.

Remove a venv install with `deactivate` and deleting the venv. Delete `--data-dir` only when you intend to destroy fleet identity.

## Troubleshooting

See [troubleshooting.md](troubleshooting.md).
