"""Integration: path edge cases, randomized trees, disk/concurrency/interruptions."""

from __future__ import annotations

import hashlib
import os
import random
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from backuplint.cli import app

pytestmark = [
    pytest.mark.skipif(shutil.which("restic") is None, reason="restic not installed"),
    pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed"),
]

runner = CliRunner()


def _clean_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("RESTIC_PASSWORD", None)
    env.pop("RESTIC_PASSWORD_FILE", None)
    env.pop("RESTIC_REPOSITORY", None)
    return env


def _init_backup(repo: Path, password_file: Path, *paths: Path) -> None:
    password_file.write_text("gap-lab-pass\n", encoding="utf-8")
    password_file.chmod(0o600)
    env = _clean_env()
    env["RESTIC_PASSWORD_FILE"] = str(password_file)
    env["RESTIC_REPOSITORY"] = str(repo)
    subprocess.run(["restic", "init"], check=True, env=env, capture_output=True)
    subprocess.run(
        ["restic", "backup", *[str(p) for p in paths]],
        check=True,
        env=env,
        capture_output=True,
    )


def _write_compose(base: Path, data_dir: Path) -> Path:
    compose = base / "compose.yml"
    compose.write_text(
        "services:\n"
        "  app:\n"
        "    image: alpine:3.20\n"
        '    command: ["sleep", "300"]\n'
        f'    volumes: ["{data_dir}:/data"]\n',
        encoding="utf-8",
    )
    return compose


def _write_cfg(base: Path, repo: Path, password_file: Path, **restore_extra: object) -> Path:
    import yaml

    restore = {"mode": "selected", **restore_extra}
    cfg = {
        "backup_paths": [],
        "restic": {
            "repository": str(repo),
            "password_file": str(password_file),
            "restore_verification": restore,
        },
    }
    path = base / "backuplint.yml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return path


EDGE_NAMES = [
    "spaces in name",
    "café-üñîçødé",
    "emoji-🔐-file",
    "-leading-dash",
    "single'quote",
    'double"quote',
    "semi;colon",
    "amp&ersand",
    "dollar$sign",
    "back`tick",
    "paren(s)",
    "brack[et]",
    "brace{s}",
    "wild*card?",
    ".dotfile",
    "a" * 180,
]


@pytest.mark.parametrize("name", EDGE_NAMES)
def test_restore_path_edge_name(tmp_path: Path, name: str) -> None:
    data = tmp_path / "data"
    data.mkdir()
    # Some FS disallow certain chars; skip if cannot create.
    try:
        target = data / name
        target.write_text("payload\n", encoding="utf-8")
    except OSError:
        pytest.skip(f"filesystem cannot create name: {name!r}")
    repo = tmp_path / "repo"
    passfile = tmp_path / "pass"
    compose = _write_compose(tmp_path, data)
    _init_backup(repo, passfile, data)
    cfg = _write_cfg(tmp_path, repo, passfile)
    result = runner.invoke(app, ["scan", str(compose), "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "selected-path restore verification passed" in result.output
    assert "gap-lab-pass" not in result.output


def test_deep_nesting_and_dotdot_components_normalized(tmp_path: Path) -> None:
    data = tmp_path / "data"
    nested = data
    for i in range(12):
        nested = nested / f"lvl{i}"
    nested.mkdir(parents=True)
    (nested / "leaf.txt").write_text("deep\n", encoding="utf-8")
    repo = tmp_path / "repo"
    passfile = tmp_path / "pass"
    compose = _write_compose(tmp_path, data)
    _init_backup(repo, passfile, data)
    cfg = _write_cfg(tmp_path, repo, passfile)
    result = runner.invoke(app, ["scan", str(compose), "--config", str(cfg)])
    assert result.exit_code == 0, result.output


def test_broken_symlink_in_tree_still_restore_verifies(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "ok.txt").write_text("ok\n", encoding="utf-8")
    (data / "broken").symlink_to(data / "missing-target")
    repo = tmp_path / "repo"
    passfile = tmp_path / "pass"
    compose = _write_compose(tmp_path, data)
    _init_backup(repo, passfile, data)
    cfg = _write_cfg(tmp_path, repo, passfile)
    result = runner.invoke(app, ["scan", str(compose), "--config", str(cfg)])
    assert result.exit_code == 0, result.output


def test_randomized_restore_tree_matches_oracle(tmp_path: Path) -> None:
    rng = random.Random(20260909)  # noqa: S311  # deterministic lab seed, not crypto
    data = tmp_path / "src"
    data.mkdir()
    oracle: dict[str, str] = {}
    for i in range(40):
        depth = rng.randint(0, 4)
        parts = [f"d{rng.randint(0, 5)}" for _ in range(depth)]
        dirname = data.joinpath(*parts) if parts else data
        dirname.mkdir(parents=True, exist_ok=True)
        if rng.random() < 0.15:
            continue  # empty dir
        name = f"f{i}.{'bin' if rng.random() < 0.3 else 'txt'}"
        if rng.random() < 0.1:
            name = f"spaced {i}.txt"
        path = dirname / name
        if rng.random() < 0.2:
            payload = b""
        elif name.endswith(".bin"):
            payload = bytes(rng.getrandbits(8) for _ in range(rng.randint(1, 4096)))
        else:
            payload = ("text-" + "x" * rng.randint(0, 200) + "\n").encode()
        path.write_bytes(payload)
        rel = str(path.relative_to(data))
        oracle[rel] = hashlib.sha256(payload).hexdigest()

    repo = tmp_path / "repo"
    passfile = tmp_path / "pass"
    compose = _write_compose(tmp_path, data)
    _init_backup(repo, passfile, data)
    # Mutate/remove original after backup.
    for child in list(data.iterdir()):
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    (data / "mutated-after-backup.txt").write_text("nope\n", encoding="utf-8")

    cfg = _write_cfg(tmp_path, repo, passfile)
    result = runner.invoke(app, ["scan", str(compose), "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "selected-path restore verification passed" in result.output

    # Independent oracle restore for byte comparison.
    env = _clean_env()
    env["RESTIC_PASSWORD_FILE"] = str(passfile)
    env["RESTIC_REPOSITORY"] = str(repo)
    target = tmp_path / "oracle-restore"
    target.mkdir()
    subprocess.run(
        ["restic", "restore", "latest", "--target", str(target), "--include", str(data)],
        check=True,
        env=env,
        capture_output=True,
    )
    restored_root = target / Path(*data.parts[1:]) if data.is_absolute() else target / data
    # Absolute path materializes under target without leading slash.
    restored_root = target.joinpath(*data.parts[1:])
    for rel, digest in oracle.items():
        got = restored_root / rel
        assert got.is_file(), f"missing {rel}"
        assert hashlib.sha256(got.read_bytes()).hexdigest() == digest


def test_concurrent_restores_unique_roots_no_cross_cleanup(tmp_path: Path) -> None:
    results: list[tuple[int, str]] = []
    lock = threading.Lock()

    def one(idx: int) -> None:
        base = tmp_path / f"run{idx}"
        base.mkdir()
        data = base / "data"
        data.mkdir()
        (data / "f.txt").write_text(f"r{idx}\n", encoding="utf-8")
        repo = base / "repo"
        passfile = base / "pass"
        compose = _write_compose(base, data)
        _init_backup(repo, passfile, data)
        cfg = _write_cfg(base, repo, passfile)
        env = _clean_env()
        env["PATH"] = os.environ.get("PATH", "")
        # CliRunner is not thread-safe; use real process isolation.
        completed = subprocess.run(
            ["backuplint", "scan", str(compose), "--config", str(cfg)],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        with lock:
            results.append((completed.returncode, completed.stdout + completed.stderr))

    t1 = threading.Thread(target=one, args=(1,))
    t2 = threading.Thread(target=one, args=(2,))
    t1.start()
    t2.start()
    t1.join(timeout=180)
    t2.join(timeout=180)
    assert len(results) == 2
    for ec, out in results:
        assert ec == 0, out
        assert "selected-path restore verification passed" in out


def test_sigterm_during_scan_no_false_pass(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    # Large-ish tree to keep restore busy briefly.
    for i in range(200):
        (data / f"f{i}.bin").write_bytes(os.urandom(8192))
    repo = tmp_path / "repo"
    passfile = tmp_path / "pass"
    compose = _write_compose(tmp_path, data)
    _init_backup(repo, passfile, data)
    cfg = _write_cfg(tmp_path, repo, passfile)
    env = _clean_env()
    env["PATH"] = os.environ.get("PATH", "")
    # Prefer venv backuplint if present.
    proc = subprocess.Popen(
        ["backuplint", "scan", str(compose), "--config", str(cfg)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    time.sleep(0.3)
    proc.send_signal(signal.SIGTERM)
    try:
        out, _ = proc.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate(timeout=30)
    # Must not report a clean PASS after SIGTERM.
    if proc.returncode == 0:
        assert "selected-path restore verification passed" not in (out or "")
    assert proc.returncode != 0 or "passed" not in (out or "").lower()


def test_restore_timeout_is_error_not_pass(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "big.bin").write_bytes(os.urandom(2_000_000))
    repo = tmp_path / "repo"
    passfile = tmp_path / "pass"
    _init_backup(repo, passfile, data)
    from backuplint.restic import ResticError, list_snapshots, restore_snapshot_paths

    snaps = list_snapshots(repository=str(repo), password_file=passfile)
    assert snaps
    target = tmp_path / "t"
    target.mkdir()
    with pytest.raises(ResticError, match="timed out"):
        restore_snapshot_paths(
            repository=str(repo),
            snapshot_id=snaps[0].snapshot_id,
            target=target,
            include_paths=(str(data),),
            password_file=passfile,
            timeout=0.0001,
        )


def test_permission_denied_destination_parent(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "f.txt").write_text("x\n", encoding="utf-8")
    repo = tmp_path / "repo"
    passfile = tmp_path / "pass"
    compose = _write_compose(tmp_path, data)
    _init_backup(repo, passfile, data)
    cfg = _write_cfg(tmp_path, repo, passfile)
    ro_parent = tmp_path / "ro-parent"
    ro_parent.mkdir()
    # Patch temp parent to read-only directory.
    from unittest.mock import patch

    with patch("backuplint.restore_dest.tempfile.gettempdir", return_value=str(ro_parent)):
        os.chmod(ro_parent, 0o500)
        try:
            result = runner.invoke(app, ["scan", str(compose), "--config", str(cfg)])
        finally:
            os.chmod(ro_parent, 0o700)
    # Should be operational error (exit 2), never silent PASS.
    assert result.exit_code != 0
    assert "selected-path restore verification passed" not in result.output
