"""Unit tests for release artifact hygiene scanning."""

from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

from scripts.release.scan_artifacts import scan_artifact


def _write_wheel(path: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)


def test_scan_accepts_clean_wheel(tmp_path: Path) -> None:
    wheel = tmp_path / "backuplint-0.5.0.dev0-py3-none-any.whl"
    _write_wheel(
        wheel,
        {
            "backuplint/__init__.py": b'__version__ = "0.5.0.dev0"\n',
            "backuplint-0.5.0.dev0.dist-info/METADATA": b"Name: backuplint\n",
        },
    )
    result = scan_artifact(wheel)
    assert result["ok"] is True
    assert result["findings"] == []


def test_scan_rejects_private_key_member(tmp_path: Path) -> None:
    wheel = tmp_path / "backuplint-bad-py3-none-any.whl"
    _write_wheel(
        wheel,
        {
            "backuplint/ca.key": b"-----BEGIN PRIVATE KEY-----\nAABB\n-----END PRIVATE KEY-----\n",
        },
    )
    result = scan_artifact(wheel)
    assert result["ok"] is False
    codes = {f["code"] for f in result["findings"]}
    assert "forbidden-name" in codes or "forbidden-content" in codes


def test_scan_rejects_workshop_path_content(tmp_path: Path) -> None:
    sdist = tmp_path / "backuplint-0.5.0.dev0.tar.gz"
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tf:
        data = b"path=/tmp/example-secret-stuff\n"
        info = tarfile.TarInfo(name="backuplint-0.5.0.dev0/README.md")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    sdist.write_bytes(buffer.getvalue())
    result = scan_artifact(sdist)
    assert result["ok"] is False
    assert any(f["code"] == "forbidden-content" for f in result["findings"])
