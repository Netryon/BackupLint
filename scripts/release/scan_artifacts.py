#!/usr/bin/env python3
"""Scan release artifacts for hygiene / supply-chain policy violations."""

from __future__ import annotations

import argparse
import json
import sys
import tarfile
import zipfile
from pathlib import Path

try:
    from .common import (
        FORBIDDEN_CONTENT_PATTERNS,
        FORBIDDEN_NAME_PATTERNS,
        MAX_ARTIFACT_BYTES,
        write_json,
    )
except ImportError:  # script execution
    from common import (
        FORBIDDEN_CONTENT_PATTERNS,
        FORBIDDEN_NAME_PATTERNS,
        MAX_ARTIFACT_BYTES,
        write_json,
    )


def _iter_members(path: Path) -> list[tuple[str, int, bytes | None]]:
    items: list[tuple[str, int, bytes | None]] = []
    if path.suffix == ".whl" or path.name.endswith(".whl"):
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                data = None
                if not info.is_dir() and info.file_size <= 512 * 1024:
                    data = zf.read(info.filename)
                items.append((info.filename, info.file_size, data))
    elif path.name.endswith(".tar.gz") or path.suffixes[-2:] == [".tar", ".gz"]:
        with tarfile.open(path, "r:gz") as tf:
            for member in tf.getmembers():
                data = None
                if member.isfile() and member.size <= 512 * 1024:
                    extracted = tf.extractfile(member)
                    if extracted is not None:
                        data = extracted.read()
                items.append((member.name, int(member.size), data))
    else:
        raw = path.read_bytes() if path.stat().st_size <= 512 * 1024 else None
        items.append((path.name, path.stat().st_size, raw))
    return items


def scan_artifact(path: Path) -> dict[str, object]:
    findings: list[dict[str, str]] = []
    size = path.stat().st_size
    if size > MAX_ARTIFACT_BYTES:
        findings.append(
            {
                "severity": "HIGH",
                "code": "oversized-artifact",
                "detail": f"{path.name} is {size} bytes (limit {MAX_ARTIFACT_BYTES})",
            }
        )
    try:
        members = _iter_members(path)
    except (OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
        return {
            "path": str(path),
            "ok": False,
            "findings": [
                {
                    "severity": "HIGH",
                    "code": "unreadable-artifact",
                    "detail": str(exc),
                }
            ],
        }

    for name, member_size, data in members:
        for pattern in FORBIDDEN_NAME_PATTERNS:
            if pattern.search(name):
                findings.append(
                    {
                        "severity": "HIGH",
                        "code": "forbidden-name",
                        "detail": f"{path.name} contains {name}",
                    }
                )
                break
        if member_size > MAX_ARTIFACT_BYTES:
            findings.append(
                {
                    "severity": "HIGH",
                    "code": "oversized-member",
                    "detail": f"{path.name}:{name} is {member_size} bytes",
                }
            )
        if data is None:
            continue
        # Skip obvious binary blobs for content scans.
        if b"\x00" in data[:1024]:
            continue
        text = data.decode("utf-8", errors="ignore")
        for pattern in FORBIDDEN_CONTENT_PATTERNS:
            if pattern.search(text):
                findings.append(
                    {
                        "severity": "HIGH",
                        "code": "forbidden-content",
                        "detail": f"{path.name}:{name} matched {pattern.pattern}",
                    }
                )
                break
    return {"path": str(path), "ok": not findings, "findings": findings, "members": len(members)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifacts", nargs="+", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    results = [scan_artifact(path) for path in args.artifacts]
    payload = {
        "ok": all(bool(item["ok"]) for item in results),
        "results": results,
    }
    write_json(args.report, payload)
    if not payload["ok"]:
        print(json.dumps(payload, indent=2), file=sys.stderr)
        return 1
    print(f"artifact scan clean: {len(results)} artifact(s)")
    return 0


if __name__ == "__main__":
    # Allow running as a script from scripts/release/
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
