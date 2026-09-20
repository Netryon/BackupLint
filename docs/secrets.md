# Secrets and credential sources

BackupLint resolves credentials through an explicit **SecretRef** model so the
same configuration style works for native hosts, systemd services, and
container-mounted secret files.

## Security invariants

BackupLint must never:

- log or print resolved secret values
- embed secrets in JSON status output
- put long-lived secrets on process argv when avoidable
- silently fall back across secret sources after a failed resolve
- accept shell/command secret providers
- read world-writable secret files
- resolve secrets during config parse (parse validates shape only)
- persist raw secret values into SQLite state or config exports

Secret **references** may be serialized. Resolved **values** must not.

## SecretRef shape

```yaml
# File (also used for Docker/Podman mounted secrets)
password:
  source: file
  path: /run/secrets/restic_password

# Environment variable (explicit name required)
password:
  source: env
  name: RESTIC_PASSWORD

# systemd credential (CREDENTIALS_DIRECTORY)
password:
  source: systemd
  name: restic_password

# Mounted secret convenience (defaults under /run/secrets)
password:
  source: mounted
  name: restic_password
```

Inline raw password strings in config are rejected.

## Resolution lifecycle

1. **Parse** reference mapping → `SecretRef`
2. **Validate** shape (no I/O except path normalization)
3. **Resolve** late → `SecretValue`
4. **Consume** via `SecretValue.get_secret_value()` at the boundary
5. **Discard** references promptly (do not store on long-lived objects)

## File permission requirements

For file/mounted/systemd credential files:

- must be a regular file (directories/FIFOs/devices rejected)
- world-writable rejected
- world-readable and group-accessible rejected (prefer `0600`)
- size capped (default 64 KiB)
- UTF-8 required
- exactly one trailing newline stripped

Symlinks are followed only when the final target is a safe regular file.

Platform note: permission checks use POSIX `stat` mode bits.

## systemd credentials

Uses the standard `CREDENTIALS_DIRECTORY` environment variable. BackupLint does
not call systemd over D-Bus. Credential names must be plain basenames (no `/`
or `..`).

Useful with native installer unit drop-ins such as:

```ini
LoadCredential=restic_password:/etc/backuplint/restic.pass
```

## Containers / mounted secrets

Docker Compose secrets, Podman secrets, and Kubernetes-style projected volumes
that mount files under `/run/secrets` work through `source: file` or
`source: mounted`. No Docker socket/API is required.

## Restic password handling

Precedence (first match wins; no silent rescue after a failure):

1. `restic.password` SecretRef in config
2. `restic.password_file` legacy path (treated as a file SecretRef)
3. internal `password=` argument (tests)
4. ambient `RESTIC_PASSWORD`
5. ambient `RESTIC_PASSWORD_FILE`

Configured credentials clear ambient `RESTIC_PASSWORD` / `RESTIC_PASSWORD_FILE`
before applying the configured source (prevents false PASS via leftover env).

Secrets are passed to restic through the subprocess environment
(`RESTIC_PASSWORD`), never on argv. Output scrubbing still redacts known secret
strings.

### Backend credentials inherited from the environment

Restic itself may consume standard backend env vars (examples):

- `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN`
- `AWS_DEFAULT_REGION` / `AWS_PROFILE`
- SFTP/`RESTIC_*` backend settings as documented by restic

BackupLint currently inherits these ambient variables for repository backends
and does **not** rewrite them into SecretRef yet. Prefer OS/container secret
injection for those values. Future work can make selected backend fields
explicit SecretRefs without inventing cloud SDKs inside BackupLint.

## Enrollment token CLI hygiene

`backuplint agent enroll` accepts exactly one of:

- `--token-file` (preferred)
- `--token-env NAME`
- `--token` (still supported; argv-visible — risk label in help text)

Token generation/storage/controller redeem semantics are unchanged.

## TLS private-key diagnostics

`diagnose_sensitive_file(path)` inspects existence, type, symlink status, and
permission exposure **without reading key contents**. Intended for a future
`backuplint doctor` path.

## SecretValue redaction

`SecretValue` renders as `***` under `str`, `repr`, formatting, and resists
pickle. Call `get_secret_value()` only at consumption boundaries.

## Extension point

Providers implement a narrow `SecretProvider` protocol (`supports` / `resolve`).
Default providers: file/mounted, env, systemd. Do not add Vault/AWS/Azure/GCP
connectors here without a deliberate product decision.
