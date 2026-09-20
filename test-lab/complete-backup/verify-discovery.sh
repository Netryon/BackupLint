#!/usr/bin/env bash
# Realistic Docker comparison for Milestone 2 mount discovery.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LAB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$LAB"

mkdir -p data config

echo "==> Starting lab stack"
sg docker -c "docker compose -f compose.yml up -d --quiet-pull"

cleanup() {
  sg docker -c "docker compose -f compose.yml down -v --remove-orphans" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "==> Comparing BackupLint output with docker compose config"
export PYTHONPATH="${ROOT}/src${PYTHONPATH:+:$PYTHONPATH}"
REPORT="$(sg docker -c "python3 - <<'PY'
from pathlib import Path
import json
import subprocess

from backuplint.compose import discover_mounts

compose = Path('compose.yml').resolve()
services = discover_mounts(compose)
discovered = {
    (s.name, m.type.value, m.source, m.target, m.read_only)
    for s in services
    for m in s.mounts
}

cfg = json.loads(
    subprocess.check_output(
        ['docker', 'compose', '-f', str(compose), 'config', '--format', 'json'],
        text=True,
    )
)
expected = set()
for name, svc in (cfg.get('services') or {}).items():
    for entry in svc.get('volumes') or []:
        expected.add(
            (
                name,
                entry.get('type', 'unknown'),
                entry.get('source') or '',
                entry.get('target') or '',
                bool(entry.get('read_only', False)),
            )
        )

missing = expected - discovered
extra = discovered - expected
if missing or extra:
    print('MISMATCH')
    print('missing', sorted(missing))
    print('extra', sorted(extra))
    raise SystemExit(1)

print('OK: BackupLint mounts match docker compose config')
for s in services:
    print(s.name)
    for m in s.mounts:
        print(m.summary_line())
PY
")"

echo "$REPORT"
echo "==> Lab passed"
