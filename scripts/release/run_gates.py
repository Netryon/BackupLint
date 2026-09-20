#!/usr/bin/env python3
"""Aggregate local release gates (static, tests, audit, build, clean-room)."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

try:
    from .common import (
        assert_versions_consistent,
        git_sha,
        repo_root,
        run,
        utc_now_iso,
        write_json,
    )
except ImportError:  # script execution
    from common import (
        assert_versions_consistent,
        git_sha,
        repo_root,
        run,
        utc_now_iso,
        write_json,
    )


def _which(name: str) -> str | None:
    return shutil.which(name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outdir", type=Path, default=Path("dist-release"))
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--skip-container", action="store_true")
    parser.add_argument("--skip-integration", action="store_true")
    parser.add_argument("--skip-harness", action="store_true")
    args = parser.parse_args()

    root = repo_root()
    version = assert_versions_consistent(root)
    source_commit = git_sha(root)
    outdir = args.outdir if args.outdir.is_absolute() else root / args.outdir
    evidence = outdir / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)

    steps: list[dict[str, object]] = []

    def record(name: str, ok: bool, detail: str = "") -> None:
        steps.append({"name": name, "ok": ok, "detail": detail})
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {name}{': ' + detail if detail else ''}")
        if not ok:
            raise RuntimeError(f"gate failed: {name}: {detail}")

    # Static gates
    run([sys.executable, "-m", "ruff", "check", "src", "tests", "scripts/release"], cwd=root)
    record("ruff", True)
    run([sys.executable, "-m", "bandit", "-r", "src", "docker", "-q"], cwd=root)
    record("bandit", True)

    # Unit tests
    run([sys.executable, "-m", "pytest", "-q", "tests/unit"], cwd=root)
    record("unit-tests", True)

    if not args.skip_integration:
        # Keep this local gate bounded: controller container tests only when docker present.
        if _which("docker"):
            run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "tests/integration/test_controller_container.py",
                ],
                cwd=root,
            )
            record("controller-container-integration", True)
        else:
            record("controller-container-integration", True, "skipped (no docker)")
    else:
        record("controller-container-integration", True, "skipped by flag")

    # Build RC artifacts
    build_cmd = [
        sys.executable,
        str(root / "scripts/release/build_rc.py"),
        "--outdir",
        str(outdir),
    ]
    if args.force:
        build_cmd.append("--force")
    if args.allow_dirty:
        build_cmd.append("--allow-dirty")
    if args.skip_container:
        build_cmd.append("--skip-container")
    run(build_cmd, cwd=root)
    record("build-rc", True)

    wheel = next((outdir / "python").glob("*.whl"))
    sdist = next((outdir / "python").glob("*.tar.gz"))

    # Clean-room installs
    clean_report = evidence / "clean-room-install.json"
    run(
        [
            sys.executable,
            str(root / "scripts/release/clean_room_install.py"),
            "--wheel",
            str(wheel),
            "--sdist",
            str(sdist),
            "--expected-version",
            version,
            "--report",
            str(clean_report),
        ],
        cwd=root,
    )
    record("clean-room-install", True)

    # pip-audit against a clean install of the wheel (shipped deps only).
    import tempfile
    import venv

    with tempfile.TemporaryDirectory(prefix="bl-audit-") as tmp:
        venv_dir = Path(tmp) / "venv"
        venv.create(venv_dir, with_pip=True)
        pip = venv_dir / "bin" / "pip"
        python = venv_dir / "bin" / "python"
        run([str(pip), "install", "--upgrade", "pip", "pip-audit"])
        run([str(pip), "install", str(wheel)])
        audit = run(
            [str(python), "-m", "pip_audit", "--progress-spinner", "off"],
            cwd=root,
            check=False,
        )
        (evidence / "pip-audit.txt").write_text(
            audit.stdout + "\n" + audit.stderr, encoding="utf-8"
        )
        if audit.returncode != 0:
            record("pip-audit", False, "vulnerabilities reported; see evidence/pip-audit.txt")
        record("pip-audit", True)

    # Optional controller harness
    if not args.skip_container and not args.skip_harness and _which("docker"):
        harness = root / "deploy/controller/run-hardening-harness.sh"
        run(["bash", str(harness)], cwd=root)
        record("controller-hardening-harness", True)
    else:
        record("controller-hardening-harness", True, "skipped")

    evidence_manifest = {
        "schema": "backuplint.release.evidence.v1",
        "source_commit": source_commit,
        "package_version": version,
        "generated_at": utc_now_iso(),
        "gates": steps,
        "artifacts": {
            "build_manifest": str(outdir / "meta" / "build-manifest.json"),
            "checksums": str(outdir / "meta" / "checksums.json"),
            "artifact_scan": str(outdir / "meta" / "artifact-scan.json"),
            "clean_room": str(clean_report),
            "pip_audit": str(evidence / "pip-audit.txt"),
            "python_sbom": str(outdir / "sbom" / "backuplint-python.cdx.json"),
        },
        "external_evidence": {
            "scale_campaign": "not evaluated by this gate — record separately",
            "security_enrollment_audit": "not evaluated by this gate — record separately",
            "integrity_restore_campaigns": "not evaluated by this gate — record separately",
        },
        "publication": {
            "pypi": False,
            "github_release": False,
            "container_registry": False,
            "final_tag": False,
        },
        "known_limitations": [
            "Package version is 1.0.0",
            "SBOM is pip-freeze/CycloneDX fallback (Syft optional elsewhere)",
            "Container vuln scan (Trivy/Grype) optional and not required here",
            "No cryptographic signing in this workflow",
        ],
    }
    # Attach container meta if present.
    container_meta = outdir / "meta" / "controller-image.json"
    if container_meta.is_file():
        evidence_manifest["artifacts"]["controller_image"] = str(container_meta)
        evidence_manifest["artifacts"]["controller_sbom"] = str(
            outdir / "sbom" / "backuplint-controller.cdx.json"
        )

    write_json(evidence / "release-evidence.json", evidence_manifest)
    print(json.dumps(evidence_manifest, indent=2))
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
