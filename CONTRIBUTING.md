# Contributing

BackupLint is one product. Please keep changes scoped, tested, and consistent with the public security model: **read-only diagnostics**, no secret printing, no modification of Compose files, volumes, or backup repositories.

## Ways to participate

- Found a bug? Use the **Bug report** issue form.
- Have an improvement or feature idea? Use the **Feature request** issue form.
- Need help installing, configuring, or using BackupLint? Use the **Question / support** issue form.
- Found a security vulnerability? Follow [SECURITY.md](SECURITY.md) and use a private GitHub Security Advisory instead of a public issue.
- Want to contribute code or documentation? Open a pull request.

When sharing logs, configuration, screenshots, or reproduction data, remove passwords, tokens, private keys, repository credentials, internal infrastructure details, and personal data.

## Development setup

```bash
git clone https://github.com/Netryon/BackupLint.git
cd BackupLint
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
ruff check src tests
pytest -q tests/unit
```

Integration tests need Docker and Restic:

```bash
sg docker -c 'source .venv/bin/activate && pytest -q tests/integration'
```

## Before you open a pull request

- Prefer small, reviewable diffs.
- Update docs when behavior or flags change.
- Do not add private lab addresses, credentials, SQLite databases, or raw campaign logs.
- Do not weaken CSR enrollment, mTLS, or token handling.
- Do not introduce Borg/Kopia as supported v1 engines.

## Security issues

See [SECURITY.md](SECURITY.md). Do not file public issues that include production secrets.

## License

MIT. See [LICENSE](LICENSE).
