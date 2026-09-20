#!/usr/bin/env python3
"""Snapshot stable v1-bound interfaces for accidental-change detection.

Writes a sorted JSON manifest of CLI commands, controller routes, protocol
window, policy/SIEM/install enums, and schema versions. Compare two manifests
with ``--diff``.

Does not rename or mutate product interfaces.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


def _sha(payload: object) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def _cli_commands() -> dict[str, Any]:
    # Import typer app without executing side effects beyond module load.
    sys.path.insert(0, str(ROOT / "src"))
    from backuplint.cli import app  # noqa: WPS433

    def walk(typer_app: object, prefix: str = "") -> list[str]:
        names: list[str] = []
        # Typer >=0.12: registered_commands / registered_groups
        for cmd in getattr(typer_app, "registered_commands", []) or []:
            name = getattr(cmd, "name", None) or getattr(cmd, "callback", lambda: None).__name__
            if name:
                names.append(f"{prefix}{name}" if prefix else str(name))
        for group in getattr(typer_app, "registered_groups", []) or []:
            gname = getattr(group, "name", None) or ""
            child = getattr(group, "typer_instance", None)
            if child is not None and gname:
                names.extend(walk(child, f"{prefix}{gname}."))
        return sorted(set(names))

    return {"commands": walk(app)}


def _controller_routes() -> list[str]:
    text = (ROOT / "src/backuplint/fleet/controller.py").read_text(encoding="utf-8")
    return sorted(set(re.findall(r'["\'](/v\d+/[A-Za-z0-9_./-]+)["\']', text)))


def _protocol() -> dict[str, Any]:
    sys.path.insert(0, str(ROOT / "src"))
    from backuplint.fleet import compat

    return {
        "current": compat.CURRENT_PROTOCOL_VERSION,
        "previous": compat.PREVIOUS_PROTOCOL_VERSION,
        "min_supported": compat.MIN_SUPPORTED_PROTOCOL_VERSION,
        "max_supported": compat.MAX_SUPPORTED_PROTOCOL_VERSION,
        "compat_codes": sorted(c.value for c in compat.ProtocolCompatCode),
    }


def _policy() -> dict[str, Any]:
    sys.path.insert(0, str(ROOT / "src"))
    from backuplint.fleet.controller_store import STORE_SCHEMA_VERSION
    from backuplint.policy.schema import POLICY_SCHEMA_VERSION

    drift = sorted(
        set(
            re.findall(
                r'"(PENDING|IN_SYNC|DRIFTED|APPLY_FAILED|UNSUPPORTED)"',
                (ROOT / "src/backuplint/fleet/policy_store.py").read_text(encoding="utf-8"),
            )
        )
    )
    return {
        "policy_schema_version": POLICY_SCHEMA_VERSION,
        "controller_store_schema_version": STORE_SCHEMA_VERSION,
        "drift_statuses_observed": drift,
    }


def _siem() -> dict[str, Any]:
    sys.path.insert(0, str(ROOT / "src"))
    from backuplint.siem.event import NON_EXPORTABLE_FAMILIES, SiemEventFamily

    return {
        "event_families": sorted(f.value for f in SiemEventFamily),
        "non_exportable": sorted(f.value for f in NON_EXPORTABLE_FAMILIES),
    }


def _install() -> dict[str, Any]:
    sys.path.insert(0, str(ROOT / "src"))
    from backuplint.install.features import FeatureId
    from backuplint.install.roles import InstallationRole

    return {
        "roles": sorted(r.value for r in InstallationRole),
        "features": sorted(f.value for f in FeatureId),
    }


def _audit_results() -> dict[str, Any]:
    sys.path.insert(0, str(ROOT / "src"))
    from backuplint.reporting import summarize_findings  # noqa: F401
    # Result labels used in JSON/text reports
    text = (ROOT / "src/backuplint/reporting.py").read_text(encoding="utf-8")
    labels = sorted(set(re.findall(r'"(PASS|WARN|FAIL|ERROR)"', text)))
    return {"result_labels": labels}


def build_manifest(*, source_commit: str) -> dict[str, Any]:
    body = {
        "schema": "backuplint.v1.contract_manifest.v1",
        "source_commit": source_commit,
        "cli": _cli_commands(),
        "controller_routes": _controller_routes(),
        "fleet_protocol": _protocol(),
        "policy": _policy(),
        "siem": _siem(),
        "installer": _install(),
        "audit": _audit_results(),
    }
    body["content_sha256"] = _sha(
        {k: v for k, v in body.items() if k not in {"source_commit", "content_sha256"}}
    )
    return body


def diff_manifests(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    ignore = {"source_commit", "content_sha256"}
    keys = sorted((set(a) | set(b)) - ignore)
    changes = []
    for key in keys:
        if a.get(key) != b.get(key):
            changes.append({"key": key, "a": a.get(key), "b": b.get(key)})
    return {
        "schema": "backuplint.v1.contract_diff.v1",
        "unchanged": len(changes) == 0,
        "a_sha": a.get("content_sha256"),
        "b_sha": b.get("content_sha256"),
        "changes": changes,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    snap = sub.add_parser("snapshot", help="write current contract manifest")
    snap.add_argument("--out", type=Path, required=True)
    snap.add_argument("--git-sha", default="")
    diff = sub.add_parser("diff", help="compare two manifests")
    diff.add_argument("--a", type=Path, required=True)
    diff.add_argument("--b", type=Path, required=True)
    diff.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    if args.cmd == "snapshot":
        import subprocess

        sha = args.git_sha or subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip()
        manifest = build_manifest(source_commit=sha)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        print(f"wrote {args.out} sha={manifest['content_sha256']}")
        return 0

    a = json.loads(args.a.read_text(encoding="utf-8"))
    b = json.loads(args.b.read_text(encoding="utf-8"))
    report = diff_manifests(a, b)
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if report["unchanged"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
