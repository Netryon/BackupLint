#!/usr/bin/env python3
"""Clean-room install smoke tests for BackupLint wheel and sdist."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

try:
    from .common import write_json
except ImportError:  # script execution
    from common import write_json


def _venv_python(venv_dir: Path) -> Path:
    return venv_dir / "bin" / "python"


def _run(python: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(python), *args],
        check=False,
        capture_output=True,
        text=True,
    )


def smoke_install(artifact: Path, *, expected_version: str) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="bl-cleanroom-") as tmp:
        root = Path(tmp)
        venv_dir = root / "venv"
        venv.create(venv_dir, with_pip=True)
        python = _venv_python(venv_dir)
        upgrade = _run(python, ["-m", "pip", "install", "--upgrade", "pip"])
        if upgrade.returncode != 0:
            return {"ok": False, "stage": "pip-upgrade", "stderr": upgrade.stderr}
        install = _run(python, ["-m", "pip", "install", str(artifact)])
        if install.returncode != 0:
            return {"ok": False, "stage": "install", "stderr": install.stderr}
        help_r = _run(python, ["-m", "backuplint", "--help"])
        if help_r.returncode != 0:
            return {"ok": False, "stage": "help", "stderr": help_r.stderr}
        ver_r = _run(python, ["-m", "backuplint", "--version"])
        if ver_r.returncode != 0:
            return {"ok": False, "stage": "version", "stderr": ver_r.stderr}
        version_text = (ver_r.stdout or ver_r.stderr).strip()
        if expected_version not in version_text:
            return {
                "ok": False,
                "stage": "version-match",
                "detail": f"expected {expected_version!r} in {version_text!r}",
            }
        # Safe core command that does not require Docker/Restic.
        ctrl_help = _run(python, ["-m", "backuplint", "controller", "--help"])
        if ctrl_help.returncode != 0:
            return {"ok": False, "stage": "controller-help", "stderr": ctrl_help.stderr}
        # Uninstall / reinstall
        uninstall = _run(python, ["-m", "pip", "uninstall", "-y", "backuplint"])
        if uninstall.returncode != 0:
            return {"ok": False, "stage": "uninstall", "stderr": uninstall.stderr}
        reinstall = _run(python, ["-m", "pip", "install", str(artifact)])
        if reinstall.returncode != 0:
            return {"ok": False, "stage": "reinstall", "stderr": reinstall.stderr}
        ver2 = _run(python, ["-m", "backuplint", "--version"])
        if ver2.returncode != 0 or expected_version not in (ver2.stdout or ver2.stderr):
            return {"ok": False, "stage": "reinstall-version", "stderr": ver2.stderr}
        return {
            "ok": True,
            "artifact": str(artifact),
            "version_output": version_text,
            "controller_help_ok": True,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--sdist", type=Path, required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    wheel_result = smoke_install(args.wheel, expected_version=args.expected_version)
    sdist_result = smoke_install(args.sdist, expected_version=args.expected_version)
    payload = {
        "ok": bool(wheel_result.get("ok")) and bool(sdist_result.get("ok")),
        "wheel": wheel_result,
        "sdist": sdist_result,
    }
    write_json(args.report, payload)
    if not payload["ok"]:
        print(json.dumps(payload, indent=2), file=sys.stderr)
        return 1
    print("clean-room install OK (wheel + sdist)")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
