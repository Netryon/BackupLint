#!/usr/bin/env python3
"""Generate CycloneDX-ish SBOMs for the Python package and controller image."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

try:
    from .common import read_project_version, repo_root, sha256_file, utc_now_iso, write_json
except ImportError:  # script execution
    from common import read_project_version, repo_root, sha256_file, utc_now_iso, write_json


def _component(name: str, version: str, *, purl: str | None = None) -> dict[str, object]:
    comp: dict[str, object] = {
        "type": "library",
        "name": name,
        "version": version,
    }
    if purl:
        comp["purl"] = purl
    return comp


def sbom_from_requirements(
    *,
    name: str,
    version: str,
    components: list[dict[str, object]],
    source_commit: str,
) -> dict[str, object]:
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {
            "timestamp": utc_now_iso(),
            "component": {
                "type": "application",
                "name": name,
                "version": version,
            },
            "properties": [
                {"name": "backuplint:source_commit", "value": source_commit},
                {
                    "name": "backuplint:sbom_generator",
                    "value": "scripts/release/generate_sbom.py",
                },
            ],
        },
        "components": sorted(components, key=lambda c: str(c.get("name", ""))),
    }


def python_package_sbom(
    wheel: Path, *, source_commit: str, out: Path
) -> dict[str, object]:
    root = repo_root()
    version = read_project_version(root)
    # Install wheel into an ephemeral venv for accurate resolved deps.
    import tempfile

    with tempfile.TemporaryDirectory(prefix="bl-sbom-") as tmp:
        venv = Path(tmp) / "venv"
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        pip = venv / "bin" / "pip"
        subprocess.run(
            [str(pip), "install", "--upgrade", "pip"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [str(pip), "install", str(wheel)],
            check=True,
            capture_output=True,
            text=True,
        )
        freeze = subprocess.run(
            [str(pip), "freeze"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    components: list[dict[str, object]] = []
    for line in freeze.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or " @ " in line:
            continue
        if "==" not in line:
            continue
        pkg, ver = line.split("==", 1)
        components.append(
            _component(pkg, ver, purl=f"pkg:pypi/{pkg.lower()}@{ver}")
        )
    components.append(
        _component(
            "backuplint",
            version,
            purl=f"pkg:pypi/backuplint@{version}",
        )
    )
    # Deduplicate by name
    by_name = {str(c["name"]).lower(): c for c in components}
    bom = sbom_from_requirements(
        name="backuplint",
        version=version,
        components=list(by_name.values()),
        source_commit=source_commit,
    )
    bom["metadata"]["properties"].append(
        {"name": "backuplint:wheel_sha256", "value": sha256_file(wheel)}
    )
    write_json(out, bom)
    return bom


def controller_image_sbom(
    image: str, *, source_commit: str, version: str, out: Path
) -> dict[str, object]:
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "pip",
            image,
            "freeze",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    components: list[dict[str, object]] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or "==" not in line:
            continue
        pkg, ver = line.split("==", 1)
        components.append(
            _component(pkg, ver, purl=f"pkg:pypi/{pkg.lower()}@{ver}")
        )
    bom = sbom_from_requirements(
        name="backuplint-controller",
        version=version,
        components=components,
        source_commit=source_commit,
    )
    bom["metadata"]["properties"].append(
        {"name": "backuplint:image_ref", "value": image}
    )
    bom["metadata"]["properties"].append(
        {
            "name": "backuplint:sbom_note",
            "value": "Fallback pip-freeze SBOM; Syft/Trivy not required",
        }
    )
    write_json(out, bom)
    return bom


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    py = sub.add_parser("python-wheel")
    py.add_argument("--wheel", type=Path, required=True)
    py.add_argument("--source-commit", required=True)
    py.add_argument("--out", type=Path, required=True)

    img = sub.add_parser("controller-image")
    img.add_argument("--image", required=True)
    img.add_argument("--source-commit", required=True)
    img.add_argument("--version", required=True)
    img.add_argument("--out", type=Path, required=True)

    args = parser.parse_args()
    if args.cmd == "python-wheel":
        python_package_sbom(
            args.wheel, source_commit=args.source_commit, out=args.out
        )
    else:
        controller_image_sbom(
            args.image,
            source_commit=args.source_commit,
            version=args.version,
            out=args.out,
        )
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
