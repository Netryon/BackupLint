# Security Policy

## Supported versions

Security fixes will target the latest published release once packaging begins.

## Reporting a vulnerability

If you discover a security issue in BackupLint, please open a private security advisory on the GitHub repository when available, or contact the repository maintainers through GitHub.

Do not include production secrets, live backup passwords, or personal data in public issues.

## Design expectations

BackupLint is a read-only diagnostic tool. It should never:

- delete user files
- delete Docker volumes
- modify Compose files
- modify backup repositories
- automatically restore or prune backups
- print secrets from environment files or backup tools

Please report any behavior that violates those expectations.
