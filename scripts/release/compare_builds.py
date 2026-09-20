#!/usr/bin/env python3
"""Compare two release-candidate outdirs for reproducibility signals."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    from .common import write_json
except ImportError:  # script execution
    from common import write_json


def load_checksums(outdir: Path) -> dict[str, dict[str, object]]:
    payload = json.loads((outdir / "meta" / "checksums.json").read_text(encoding="utf-8"))
    return {item["filename"]: item for item in payload["artifacts"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--a", type=Path, required=True)
    parser.add_argument("--b", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()

    a = load_checksums(args.a)
    b = load_checksums(args.b)
    names = sorted(set(a) | set(b))
    comparisons = []
    wheel_match = None
    sdist_match = None
    for name in names:
        left = a.get(name)
        right = b.get(name)
        if left is None or right is None:
            comparisons.append(
                {
                    "filename": name,
                    "status": "missing-in-one-side",
                    "a": left,
                    "b": right,
                }
            )
            continue
        same_hash = left["sha256"] == right["sha256"]
        same_size = left["size"] == right["size"]
        entry = {
            "filename": name,
            "artifact_type": left.get("artifact_type"),
            "sha256_match": same_hash,
            "size_match": same_size,
            "a_sha256": left["sha256"],
            "b_sha256": right["sha256"],
        }
        comparisons.append(entry)
        if left.get("artifact_type") == "wheel":
            wheel_match = same_hash
        if left.get("artifact_type") == "sdist":
            sdist_match = same_hash

    reproducible_claim = False
    notes = [
        "Wheel and sdist bit-for-bit reproducibility requires SOURCE_DATE_EPOCH "
        "plus sdist tar/gzip metadata normalization (see normalize_sdist_reproducible).",
        "SBOM JSON may still differ across builds due to generation timestamps; "
        "SBOM hash match is informational, not required for the wheel/sdist claim.",
    ]
    if wheel_match and sdist_match:
        reproducible_claim = True
        notes.append("Wheel and sdist SHA-256 matched across both clean builds.")
    elif wheel_match is False or sdist_match is False:
        notes.append("Hashes differed; do not claim bit-for-bit reproducibility.")

    payload = {
        "schema": "backuplint.release.reproducibility.v1",
        "reproducible_bit_for_bit": reproducible_claim,
        "wheel_sha256_match": wheel_match,
        "sdist_sha256_match": sdist_match,
        "comparisons": comparisons,
        "notes": notes,
    }
    write_json(args.report, payload)
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
