# Security Notes

BackupLint is primarily a diagnostic/assurance tool. Coverage discovery, Restic
snapshot listing, and integrity checks do not modify live application data,
Compose files, Docker volumes, or backup repository contents.

An important opt-in exception is **restore verification**: BackupLint may run a
real `restic restore` into a safety-checked, BackupLint-owned temporary
directory, validate the restored content there, and then clean up that owned
directory. It must not restore over live audited application paths or mutate the
backup repository.

## Trust model

BackupLint inherits the privileges of the account that runs it:

- Access to the Docker API (typically via membership in the `docker` group) allows
  inspection of Compose configuration and volume mountpoints.
- Access to a Restic repository (local path or remote URL plus credentials) allows
  snapshot listing, optional integrity checks, and—only when explicitly
  requested—reading backup data through an isolated restore into local temporary
  storage.

Operators should treat Docker socket access and Restic credentials as sensitive.

## Command execution

BackupLint invokes external tools with argument lists (argv), not shell strings.

Allowed helpers:

```text
docker compose -f <compose-file> config --format json
docker volume inspect <name> --format {{.Mountpoint}}
restic snapshots --json
restic check
restic check --read-data
restic version
restic restore <snapshot> --target <owned-temp-dir> --include <path> [--verify]
```

BackupLint does **not** run Restic `backup`, `prune`, `forget`, `repair`,
`unlock`, or similar repository-mutating/destructive commands. `restic restore`
is permitted only for explicitly requested restore verification and only to an
owned, safety-checked temporary destination.

Application code does not use `shell=True`, `os.system`, `eval`, or `exec`.
Subprocess helpers resolve executables with `shutil.which` and apply timeouts
(Compose 60s, volume inspect 30s, Restic snapshots 120s, standard integrity 300s,
deep integrity 3600s, restore verification 1800s by default) so a hung helper
cannot block indefinitely.

## Input validation

- Compose paths must exist and be regular files before invocation.
- Configuration YAML is loaded with `yaml.safe_load` only.
- Unknown configuration keys are rejected.
- Integrity mode must be `off`, `standard`, or `deep`.
- Volume names used with `docker volume inspect` are constrained to a conservative
  allowlist pattern.

## Filesystem and path safety

- Path coverage uses pathlib semantics (not string-prefix matching).
- Relative `password_file` and integrity `state_file` paths resolve relative to the
  configuration file directory.
- Integrity state writes use a temporary file plus atomic replace and restrictive
  permissions (`0600` when the OS allows).
- Integrity state never stores passwords or password-file contents; repository
  identity is a hash after stripping remote userinfo.
- Restore verification creates a unique `0700` destination plus a sentinel and
  refuses unsafe system roots, home roots, the Restic repository (or unsafe
  overlap with it), and live audited bind/volume paths.
- Cleanup is limited to a destination that BackupLint created and whose sentinel
  still matches the current restore run.

## Secret handling

- Configured `restic.password` SecretRef takes precedence over legacy
  `restic.password_file`; configured credentials override ambient
  `RESTIC_PASSWORD` / `RESTIC_PASSWORD_FILE`.
- A failing configured credential source is not silently replaced by another
  source.
- Secrets are scrubbed from error messages before display.
- Lock-error host/user identity strings are redacted.
- Text and JSON reports must not include password values.

## Residual risks

- Docker API and Restic credential access remain powerful on the operator host.
- Standard integrity is not a full data-read proof; deep mode is slower and still
  does not prove restore success.
- Integrity checks take an exclusive Restic lock while running (Restic behavior).
- Restore verification writes restored data to local temporary storage for the
  duration of the check. Operators should treat that temporary restored content
  as sensitive and ensure adequate local disk space.

## What BackupLint does not guarantee

A PASS result means coverage and (when requested) repository integrity checks
succeeded at the level of the operations that actually ran. A restore-verification
PASS means the requested paths were restored into the isolated destination and
the expected restored content was present/readable. It does **not** prove
application startup after restore, transaction-consistent databases, or complete
disaster recovery.

See also [SECURITY.md](../SECURITY.md), [integrity-checks.md](integrity-checks.md),
and [restore-verification.md](restore-verification.md).
