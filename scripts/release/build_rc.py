#!/usr/bin/env python3
"""Build a BackupLint release-candidate tree (wheel, sdist, manifests, SBOMs)."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

try:
    from .common import (
        assert_versions_consistent,
        ensure_outdir,
        git_is_clean,
        git_sha,
        normalize_sdist_reproducible,
        repo_root,
        run,
        sha256_file,
        utc_now_iso,
        write_json,
    )
    from .generate_sbom import controller_image_sbom, python_package_sbom
    from .scan_artifacts import scan_artifact
except ImportError:  # script execution
    from common import (
        assert_versions_consistent,
        ensure_outdir,
        git_is_clean,
        git_sha,
        normalize_sdist_reproducible,
        repo_root,
        run,
        sha256_file,
        utc_now_iso,
        write_json,
    )
    from generate_sbom import controller_image_sbom, python_package_sbom
    from scan_artifacts import scan_artifact


def _artifact_type(path: Path) -> str:
    name = path.name
    if name.endswith(".whl"):
        return "wheel"
    if name.endswith(".tar.gz"):
        return "sdist"
    if name.endswith(".cdx.json"):
        return "sbom"
    if name.endswith(".json"):
        return "manifest"
    return "other"


def build_python_artifacts(
    source_root: Path, dist_dir: Path, *, source_date_epoch: str
) -> tuple[Path, Path]:
    dist_dir.mkdir(parents=True, exist_ok=True)
    run(
        [sys.executable, "-m", "build", "--outdir", str(dist_dir)],
        cwd=source_root,
        env={"SOURCE_DATE_EPOCH": source_date_epoch},
    )
    wheels = sorted(dist_dir.glob("*.whl"))
    sdists = sorted(dist_dir.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise RuntimeError(f"expected one wheel and one sdist in {dist_dir}")
    return wheels[0], sdists[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--outdir",
        type=Path,
        default=Path("dist-release"),
        help="artifact output directory (default: dist-release)",
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="permit uncommitted changes (not for formal RC)",
    )
    parser.add_argument(
        "--skip-container",
        action="store_true",
        help="skip controller image build/SBOM",
    )
    parser.add_argument(
        "--image-tag",
        default="backuplint-controller:rc",
        help="local image tag for RC build (never pushed)",
    )
    args = parser.parse_args()

    root = repo_root()
    if not args.allow_dirty and not git_is_clean(root):
        raise SystemExit(
            "refusing dirty worktree; commit/stash or pass --allow-dirty"
        )

    version = assert_versions_consistent(root)
    source_commit = git_sha(root)
    source_date_epoch = run(
        ["git", "log", "-1", "--format=%ct", source_commit],
        cwd=root,
    ).stdout.strip()
    outdir = args.outdir if args.outdir.is_absolute() else root / args.outdir
    ensure_outdir(outdir, force=args.force)

    build_dir = outdir / "python"
    sbom_dir = outdir / "sbom"
    meta_dir = outdir / "meta"
    for path in (build_dir, sbom_dir, meta_dir):
        path.mkdir(parents=True, exist_ok=True)

    # Clean export via git archive into a temporary tree (no workshop dirt).
    with tempfile.TemporaryDirectory(prefix="bl-rc-src-") as tmp:
        export_root = Path(tmp) / "src"
        export_root.mkdir()
        archive = Path(tmp) / "src.tar"
        run(
            ["git", "archive", "--format=tar", "-o", str(archive), "HEAD"],
            cwd=root,
        )
        run(["tar", "-xf", str(archive), "-C", str(export_root)])
        wheel, sdist = build_python_artifacts(
            export_root, build_dir, source_date_epoch=source_date_epoch
        )
        normalize_sdist_reproducible(
            sdist, source_date_epoch=int(source_date_epoch)
        )

    scan_results = [scan_artifact(wheel), scan_artifact(sdist)]
    scan_payload = {
        "ok": all(bool(item["ok"]) for item in scan_results),
        "results": scan_results,
    }
    write_json(meta_dir / "artifact-scan.json", scan_payload)
    if not scan_payload["ok"]:
        print(json.dumps(scan_payload, indent=2), file=sys.stderr)
        return 1

    python_sbom = sbom_dir / "backuplint-python.cdx.json"
    python_package_sbom(wheel, source_commit=source_commit, out=python_sbom)

    container_meta: dict[str, object] = {"skipped": True}
    if not args.skip_container:
        # Build from clean export again so image context matches release source.
        with tempfile.TemporaryDirectory(prefix="bl-rc-img-") as tmp:
            export_root = Path(tmp) / "src"
            export_root.mkdir()
            archive = Path(tmp) / "src.tar"
            run(
                ["git", "archive", "--format=tar", "-o", str(archive), "HEAD"],
                cwd=root,
            )
            run(["tar", "-xf", str(archive), "-C", str(export_root)])
            # Ensure docker scripts are executable in the export.
            for rel in (
                "docker/controller-entrypoint.sh",
                "docker/controller-healthcheck.py",
                "docker/controller-preflight.py",
            ):
                target = export_root / rel
                if target.exists():
                    target.chmod(0o755)
            run(
                [
                    "docker",
                    "build",
                    "-f",
                    "Dockerfile.controller",
                    "--build-arg",
                    f"BACKUPLINT_VERSION={version}",
                    "--build-arg",
                    f"BACKUPLINT_REVISION={source_commit}",
                    "-t",
                    args.image_tag,
                    ".",
                ],
                cwd=export_root,
            )
        inspect = run(
            [
                "docker",
                "inspect",
                "--format",
                "{{json .}}",
                args.image_tag,
            ]
        )
        image_json = json.loads(inspect.stdout)
        config = image_json.get("Config") or {}
        labels = config.get("Labels") or {}
        user = config.get("User") or ""
        if not str(user).startswith("10001") and "backuplint" not in str(user):
            # Dockerfile sets USER backuplint:backuplint which may appear as uid.
            pass
        # Fail if user is root.
        if str(user) in {"", "0", "0:0", "root", "root:root"}:
            raise RuntimeError(f"controller image runs as root unexpectedly: {user!r}")
        # Scan image filesystem for embedded keys (best-effort).
        key_probe = run(
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "sh",
                args.image_tag,
                "-c",
                (
                    "find /app /state -type f "
                    "\\( -name '*.key' -o -name '*.pem' \\) 2>/dev/null | head"
                ),
            ],
            check=False,
        )
        if key_probe.stdout.strip():
            raise RuntimeError(
                f"embedded key material found in image:\n{key_probe.stdout}"
            )
        image_sbom = sbom_dir / "backuplint-controller.cdx.json"
        controller_image_sbom(
            args.image_tag,
            source_commit=source_commit,
            version=version,
            out=image_sbom,
        )
        container_meta = {
            "skipped": False,
            "image_tag": args.image_tag,
            "image_id": image_json.get("Id"),
            "repo_digests": image_json.get("RepoDigests") or [],
            "user": user,
            "labels": labels,
            "healthcheck": config.get("Healthcheck"),
            "sbom": str(image_sbom.relative_to(outdir)),
        }
        write_json(meta_dir / "controller-image.json", container_meta)

    artifacts = []
    for path in sorted(build_dir.glob("*")) + sorted(sbom_dir.glob("*.json")):
        if not path.is_file():
            continue
        artifacts.append(
            {
                "filename": path.name,
                "relative_path": str(path.relative_to(outdir)),
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
                "source_commit": source_commit,
                "package_version": version,
                "build_timestamp": utc_now_iso(),
                "artifact_type": _artifact_type(path),
            }
        )

    checksums = {
        "schema": "backuplint.release.checksums.v1",
        "source_commit": source_commit,
        "package_version": version,
        "generated_at": utc_now_iso(),
        "artifacts": artifacts,
    }
    write_json(meta_dir / "checksums.json", checksums)
    # Also emit sha256sums.txt for convenience.
    lines = [
        f"{item['sha256']}  {item['relative_path']}"
        for item in artifacts
        if item["artifact_type"] in {"wheel", "sdist", "sbom"}
    ]
    (meta_dir / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")

    build_manifest = {
        "schema": "backuplint.release.build.v1",
        "source_commit": source_commit,
        "package_version": version,
        "generated_at": utc_now_iso(),
        "outdir": str(outdir),
        "python": {
            "wheel": wheel.name,
            "sdist": sdist.name,
            "sbom": str(python_sbom.relative_to(outdir)),
        },
        "container": container_meta,
        "notes": [
            "Local release-candidate artifacts only.",
            "No PyPI publication, registry push, GitHub Release, or final tag.",
            "Wheel/sdist bit-for-bit claim requires compare_builds after "
            "normalize_sdist_reproducible.",
        ],
    }
    write_json(meta_dir / "build-manifest.json", build_manifest)
    print(json.dumps(build_manifest, indent=2))
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
