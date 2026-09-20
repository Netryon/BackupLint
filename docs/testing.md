# Testing

## Philosophy

Each feature should be covered at three levels where applicable:

1. Unit tests for individual functions
2. Integration tests across modules
3. Realistic Docker labs under `test-lab/`

Every meaningful bug should become a regression test.

## Current status

- Unit and integration suites under `tests/`
- Labs:
  - `test-lab/run-milestone6.sh` (scenarios A–E)
  - `test-lab/production/run-production-validation.sh` (real apps + Restic + scale)
  - individual labs under `test-lab/*/verify-coverage.sh`
- Production matrix: `docs/production-test-matrix.md`
- CI (`.github/workflows/ci.yml`) runs lint, Bandit, pip-audit, unit tests, and integration tests on pull requests and pushes to `main`

## Local commands matching CI

```bash
source .venv/bin/activate
ruff check src tests
bandit -r src -q
pip-audit
pytest -q tests/unit
# Docker group required for integration tests:
sg docker -c 'source .venv/bin/activate && pytest -q tests/integration'
```

## Labs

| Lab directory | Scenario |
|---------------|----------|
| `test-lab/complete-backup/` | All persistent paths covered |
| `test-lab/missing-bind-mount/` | Bind mount omitted from backup config |
| `test-lab/missing-volume/` | Named volume omitted |
| `test-lab/nested-paths/` | Parent/child path coverage |
| `test-lab/database-container/` | Database workload warning |
| `test-lab/edge-cases/` | Similar names (`app` vs `app2`) |
| `test-lab/production/` | Real application / Restic / scale harness |
