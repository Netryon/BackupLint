# Native installer and service provisioning

BackupLint remains **one product**. The native installer turns a validated
installation profile into local directories, optional package installs, and
systemd units.

This builds on [installation-profiles.md](installation-profiles.md).

> Interactive installer is implemented as a conservative CLI wizard.
> Unattended profile execution is the primary automation path.

## Commands

```bash
# Dry-run (no host mutation)
backuplint install --profile profile.yaml --non-interactive --dry-run

# Unattended mutation (root; explicit confirmation)
sudo backuplint install --profile profile.yaml --non-interactive --yes

# Interactive wizard (prompts; confirms before mutation)
sudo backuplint install --interactive --yes

# Lab/prefix install (writes under PREFIX; skips host systemctl)
backuplint install --profile profile.yaml --non-interactive --yes --prefix /tmp/bl-lab

# Feature add (installs support only; does not enable runtime)
sudo backuplint install feature restore_verification --yes

# Safe uninstall plan / execute (units + manifest only; keeps secrets/state)
backuplint install uninstall --dry-run
sudo backuplint install uninstall --execute --yes
```

Exit codes:

| Code | Meaning |
|------|---------|
| 0 | success |
| 2 | error |
| 3 | cancelled |
| 4 | mutation refused without `--yes` |

## Roles and features

Same role/feature model as the profile foundation. The installer only accepts
`deployment: native`.

## Package managers

| Family | Manager | Detection |
|--------|---------|-----------|
| Debian/Ubuntu | `apt-get` | `/etc/os-release` ID/ID_LIKE |
| Fedora/Rocky/RHEL | `dnf` | `/etc/os-release` ID/ID_LIKE |

- Argv lists only (no shell, no `curl | sh`)
- Explicit package maps (`docker.io`/`docker`, `restic`, `openssl`)
- Unsupported distros fail before mutation
- Root is required for real installs

## Filesystem layout

Default (system):

```text
/etc/backuplint/                 config
/var/lib/backuplint/             state root + install-manifest.json
/var/lib/backuplint/schedule/    scheduler state
/var/lib/backuplint/controller/  controller data
/var/lib/backuplint/agent/       agent identity/queue
/var/log/backuplint/             logs
/run/backuplint/                 runtime
/etc/systemd/system/backuplint-*.service
```

`--prefix PATH` mirrors this tree under `PATH/` for labs/tests.

Permissions are restrictive (`0700`/`0750` dirs, `0640` config/manifest).
Symlink targets for managed paths are refused.

Existing `backuplint.yml` is **not** overwritten.

Path migration from `$XDG_DATA_HOME/...` user layouts is **not** automated;
document and migrate manually if needed.

## systemd

| Unit | When |
|------|------|
| `backuplint-controller.service` | `fleet_controller` |
| `backuplint-scheduler.service` | `scheduler` |

All-in-one enables both (no duplicate controller/scheduler). There is no
long-running agent unit; agents use enroll/submit (often via scheduler fleet
submit). Prefixed lab installs write unit files but do not call host
`systemctl`.

## Installation manifest

`/var/lib/backuplint/install-manifest.json` (schema v1) records role, deployment,
installed features, managed paths/units, and installer version. No secrets.

`installed != enabled`: feature-add updates the manifest and dependencies but
does not silently turn on runtime checks in config.

## Failure / rollback

Preflight validates profile + distro. On failure after creating installer-owned
paths/files in the current transaction, those creates are rolled back when safe.
Pre-existing user config/state/CA/keys are never deleted by rollback.

## Uninstall / purge

Default uninstall removes installer-owned unit files and the manifest only.

Never auto-deletes:

- controller DB / CA keys
- agent identity
- scheduler history
- user config
- logs / backup repositories

A destructive purge is intentionally not the default action.

## Distro lab harness

See `test-lab/native-installer/run-prefix-lab.sh` for a disposable prefix-based
idempotency/feature-add/uninstall dry-run check (no root, no host systemd).

## Limitations

- No remote SSH / fleet-wide provisioning
- Container deployment is out of scope for this native installer
- Public packaging extras are not finalized (Python package must already exist)
- Real apt/dnf execution requires root and a supported distro
