#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
EVIDENCE="${BACKUPLINT_AT_EVIDENCE:-${TMPDIR:-/tmp}/backuplint-advanced-test/native-controller}"
mkdir -p "$EVIDENCE"
command -v python3 >/dev/null || { echo "PREREQ_MISSING: python3" >&2; exit 3; }
[[ -d "$ROOT/src/backuplint" ]] || { echo "PREREQ_MISSING: repo checkout" >&2; exit 3; }
SHA="$(git -C "$ROOT" rev-parse HEAD)"
DATA="$EVIDENCE/data"
rm -rf "$DATA"
mkdir -p "$DATA"
export PYTHONPATH="$ROOT/src"
python3 - <<PY
from pathlib import Path
from backuplint.fleet.controller import FleetController
from backuplint.fleet.dashboard.config import DashboardConfig
root = Path("$DATA")
ctrl = FleetController(root, hostname="127.0.0.1", dashboard=DashboardConfig(enabled=True, cookie_secure=False))
ctrl.dashboard_auth.set_password("advanced-test-pass")
host, port = ctrl.start(host="127.0.0.1", port=0)
(root / "controller.url").write_text(f"https://{host}:{port}\n")
(root / "ca.crt").write_bytes((ctrl.ca_dir / "ca.crt").read_bytes())
print(f"NATIVE_CONTROLLER_UP url=https://{host}:{port} sha=$SHA")
print(f"pid evidence under $EVIDENCE — stop with ctrl.stop() in interactive use")
# Keep brief smoke then stop (pack smoke, not long service)
ctrl.stop(); ctrl.close()
print("NATIVE_CONTROLLER_SMOKE_OK")
PY
echo "$SHA" >"$EVIDENCE/sha.txt"
