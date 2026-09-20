"""Shared helpers for BackupLint release-candidate tooling."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

FORBIDDEN_NAME_PATTERNS = (
    re.compile(r"(^|/)\.env($|\.)", re.I),
    re.compile(r"\.(pem|key)$", re.I),
    re.compile(r"(^|/)id_rsa", re.I),
    re.compile(r"credentials\.json$", re.I),
    re.compile(r"secrets?\.(ya?ml|json)$", re.I),
    re.compile(r"restic-password", re.I),
    re.compile(r"\.(pcap|cap)$", re.I),
    re.compile(r"\.(sqlite3?)(-wal|-shm)?$", re.I),
    re.compile(r"(^|/)AGENT_.*\.md$", re.I),
    re.compile(r"(^|/)docs/internal", re.I),
    re.compile(r"(^|/)docs/platform-reports/", re.I),
    re.compile(r"(^|/)docs/internal-", re.I),
    re.compile(r"(^|/)\.git/", re.I),
)

FORBIDDEN_CONTENT_PATTERNS = (
    re.compile(r"BEGIN (RSA |OPENSSH |EC )?PRIVATE KEY"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"Co-authored-by:\s*Cursor", re.I),
    re.compile(r"cursoragent@cursor\.com", re.I),
    re.compile(r"/home/" r"sysadmin/", re.I),
)

MAX_ARTIFACT_BYTES = 25 * 1024 * 1024


def repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file() and (parent / "src" / "backuplint").is_dir():
            return parent
    raise RuntimeError("unable to locate repository root")


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def run(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    result = subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        env=merged,
        check=False,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(args)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_sdist_reproducible(sdist_path: Path, *, source_date_epoch: int) -> Path:
    """Rewrite an sdist so tar/gzip metadata is deterministic.

    setuptools honors SOURCE_DATE_EPOCH for wheels but still embeds wall-clock
    mtimes (and a gzip original filename) into ``.tar.gz`` sdists. File
    *contents* already match across builds; this normalizes archive metadata
    only:

    - tar member mtime / uid / gid / uname / gname
    - gzip header mtime
    - empty gzip original filename
    - member order preserved (already sorted by setuptools)
    """
    import gzip
    import io
    import tarfile

    epoch = int(source_date_epoch)
    raw = sdist_path.read_bytes()
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(raw)), mode="r:") as src:
        members = src.getmembers()
        payloads: list[tuple[tarfile.TarInfo, bytes | None]] = []
        for member in members:
            data = None
            if member.isfile():
                handle = src.extractfile(member)
                if handle is None:
                    raise RuntimeError(f"unable to read sdist member {member.name}")
                data = handle.read()
            info = tarfile.TarInfo(name=member.name)
            info.type = member.type
            info.size = member.size if member.isfile() else 0
            info.mode = member.mode
            info.mtime = epoch
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            if member.isfile() and data is not None:
                info.size = len(data)
            payloads.append((info, data))

    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w") as dst:
        for info, data in payloads:
            if data is None:
                dst.addfile(info)
            else:
                dst.addfile(info, io.BytesIO(data))
    tar_bytes = tar_buf.getvalue()

    out_buf = io.BytesIO()
    with gzip.GzipFile(
        filename="",
        mode="wb",
        fileobj=out_buf,
        mtime=epoch,
        compresslevel=9,
    ) as gz:
        gz.write(tar_bytes)
    sdist_path.write_bytes(out_buf.getvalue())
    return sdist_path


def read_project_version(root: Path) -> str:
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    version = data["project"]["version"]
    if not isinstance(version, str) or not version.strip():
        raise RuntimeError("pyproject.toml project.version missing")
    return version.strip()


def read_runtime_version(root: Path) -> str:
    init = (root / "src" / "backuplint" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', init, re.M)
    if not match:
        raise RuntimeError("src/backuplint/__init__.py __version__ missing")
    return match.group(1)


def assert_versions_consistent(root: Path) -> str:
    project = read_project_version(root)
    runtime = read_runtime_version(root)
    if project != runtime:
        raise RuntimeError(
            f"version mismatch: pyproject={project!r} __init__={runtime!r}"
        )
    return project


def git_sha(root: Path) -> str:
    return run(["git", "rev-parse", "HEAD"], cwd=root).stdout.strip()


def git_is_clean(root: Path) -> bool:
    status = run(["git", "status", "--porcelain"], cwd=root).stdout.strip()
    return status == ""


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def ensure_outdir(path: Path, *, force: bool) -> None:
    if path.exists():
        if any(path.iterdir()):
            if not force:
                raise RuntimeError(
                    f"output directory {path} is not empty; pass --force to overwrite"
                )
            for child in path.iterdir():
                if child.is_file() or child.is_symlink():
                    child.unlink()
                else:
                    # Only remove known release subdirs; refuse deep workshop trees.
                    import shutil

                    shutil.rmtree(child)
    path.mkdir(parents=True, exist_ok=True)
