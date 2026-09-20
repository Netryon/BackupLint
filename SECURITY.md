# Security Policy

## Supported versions

Security fixes target the latest published stable release. For the initial public launch, that is **BackupLint v1.0.0**.

## Reporting a vulnerability

Please report suspected security vulnerabilities through a **private GitHub Security Advisory**:

https://github.com/Netryon/BackupLint/security/advisories/new

Do **not** file a public issue with vulnerability details, production secrets, live backup passwords, private keys, tokens, internal infrastructure details, or personal data.

Include enough sanitized information to reproduce and assess the issue: affected BackupLint version, deployment role, native/container form, platform, impact, and reproduction steps.

## Design expectations

BackupLint is a read-only diagnostic tool. It should never:

- delete user files
- delete Docker volumes
- modify Compose files
- modify backup repositories
- automatically restore or prune backups
- print secrets from environment files or backup tools

Please report any behavior that violates those expectations.
