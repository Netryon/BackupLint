# Security Notes

BackupLint is a **read-only** diagnostic CLI. It reports whether Docker Compose
persistent mounts appear covered by backup configuration and, optionally, whether a
configured Restic repository passes integrity verification. It must not delete,
modify, or restore user data, Compose files, Docker volumes, or backup repositories.

## Trust model

BackupLint inherits the privileges of the account that runs it:

- Access to the Docker API (typically via membership in the `docker` group) allows
  inspection of Compose configuration and volume mountpoints.
- Access to a Restic repository (local path or remote URL plus credentials) allows
  read-only snapshot listing and optional integrity checks.

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
```

BackupLint does **not** run Restic `backup`, `restore`, `prune`, `forget`, `repair`,
or similar mutating commands.

Application code does not use `shell=True`, `os.system`, `eval`, or `exec`.
Subprocess helpers resolve executables with `shutil.which` and apply timeouts
(Compose 60s, volume inspect 30s, Restic snapshots 120s, standard integrity 300s,
deep integrity 3600s) so a hung helper cannot block indefinitely.

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

## Secret handling

- Configured `restic.password_file` always overrides ambient `RESTIC_PASSWORD` /
  `RESTIC_PASSWORD_FILE`.
- Secrets are scrubbed from error messages before display.
- Lock-error host/user identity strings are redacted.
- Text and JSON reports must not include password values.

## Residual risks

- Docker API and Restic credential access remain powerful on the operator host.
- Standard integrity is not a full data-read proof; deep mode is slower and still
  does not prove restore success.
- Integrity checks take an exclusive Restic lock while running (Restic behavior).

## What BackupLint does not guarantee

A PASS result means coverage and (when requested) repository integrity checks
succeeded at the level of the Restic operation used. It does **not** prove restore
success, application startup after restore, or transaction-consistent databases.

See also [SECURITY.md](../SECURITY.md) and [integrity-checks.md](integrity-checks.md).
