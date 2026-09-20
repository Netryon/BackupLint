"""Deterministic secrets / credential-source tests."""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from backuplint.config import ConfigError, parse_config_data
from backuplint.secrets import (
    InvalidSecretRefError,
    SecretMissingError,
    SecretPermissionError,
    SecretRef,
    SecretSource,
    SecretSourceUnavailableError,
    SecretTooLargeError,
    SecretValue,
    diagnose_sensitive_file,
    parse_secret_ref,
    resolve_secret,
    secret_ref_from_file_path,
    secret_ref_roundtrip,
)
from backuplint.secrets.file_checks import DEFAULT_MAX_SECRET_BYTES


def _write_secret(path: Path, text: str, mode: int = 0o600) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return path


def test_secret_value_redacts_str_repr_format_json_logging() -> None:
    secret = SecretValue("super-secret-value-xyz")
    assert str(secret) == "***"
    assert "super-secret" not in repr(secret)
    assert f"{secret}" == "***"
    assert "super-secret" not in json.dumps({"pw": str(secret)})
    with pytest.raises(TypeError):
        json.dumps(secret)  # type: ignore[arg-type]
    log = logging.getLogger("backuplint.secrets.test")
    records: list[str] = []

    class Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(self.format(record))

    handler = Handler()
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    try:
        log.info("credential=%s", secret)
        log.info("credential=%r", secret)
    finally:
        log.removeHandler(handler)
    joined = "\n".join(records)
    assert "super-secret" not in joined
    assert secret.get_secret_value() == "super-secret-value-xyz"
    with pytest.raises(TypeError):
        secret.__reduce__()


def test_parse_secret_ref_file_env_systemd_mounted(tmp_path: Path) -> None:
    file_ref = parse_secret_ref(
        {"source": "file", "path": "pw.txt"},
        config_dir=tmp_path,
        field_name="password",
    )
    assert file_ref.source is SecretSource.FILE
    assert file_ref.path == str(tmp_path / "pw.txt")

    env_ref = parse_secret_ref({"source": "env", "name": "RESTIC_PASSWORD"})
    assert env_ref.source is SecretSource.ENV
    assert env_ref.name == "RESTIC_PASSWORD"

    systemd_ref = parse_secret_ref({"source": "systemd", "name": "restic_password"})
    assert systemd_ref.source is SecretSource.SYSTEMD

    mounted = parse_secret_ref({"source": "mounted", "name": "restic_password"})
    assert mounted.source is SecretSource.MOUNTED
    assert mounted.path == "/run/secrets/restic_password"

    with pytest.raises(InvalidSecretRefError):
        parse_secret_ref("raw-secret")
    with pytest.raises(InvalidSecretRefError):
        parse_secret_ref({"source": "file"})
    with pytest.raises(InvalidSecretRefError):
        parse_secret_ref({"source": "env", "name": "X", "path": "/x"})
    with pytest.raises(InvalidSecretRefError):
        parse_secret_ref({"source": "systemd", "name": "../escape"})
    with pytest.raises(InvalidSecretRefError):
        parse_secret_ref({"source": "command", "name": "x"})


def test_secret_ref_roundtrip_and_config_password_mapping(tmp_path: Path) -> None:
    ref = parse_secret_ref({"source": "env", "name": "TOKEN"})
    assert secret_ref_roundtrip(ref) == ref
    cfg = parse_config_data(
        {
            "backup_paths": ["/data"],
            "restic": {
                "repository": "/repo",
                "password": {"source": "env", "name": "RESTIC_PASSWORD"},
            },
        }
    )
    assert cfg.restic is not None
    assert isinstance(cfg.restic.password, SecretRef)
    assert cfg.restic.password.name == "RESTIC_PASSWORD"
    with pytest.raises(ConfigError, match="only one"):
        parse_config_data(
            {
                "backup_paths": ["/data"],
                "restic": {
                    "repository": "/repo",
                    "password_file": "pw",
                    "password": {"source": "env", "name": "X"},
                },
            },
            source_file=tmp_path / "backuplint.yml",
        )


def test_file_source_success_and_newline(tmp_path: Path) -> None:
    path = _write_secret(tmp_path / "pw", "secret-line\n")
    value = resolve_secret(secret_ref_from_file_path(path))
    assert value.get_secret_value() == "secret-line"


def test_file_missing_directory_fifo_symlink_permissions(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    with pytest.raises(SecretMissingError):
        resolve_secret(secret_ref_from_file_path(missing))

    directory = tmp_path / "dir"
    directory.mkdir()
    with pytest.raises(SecretPermissionError, match="directory"):
        resolve_secret(secret_ref_from_file_path(directory))

    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(SecretPermissionError, match="regular file"):
        resolve_secret(secret_ref_from_file_path(fifo))

    target = _write_secret(tmp_path / "target", "x")
    link = tmp_path / "link"
    link.symlink_to(target)
    assert resolve_secret(secret_ref_from_file_path(link)).get_secret_value() == "x"

    broken = tmp_path / "broken"
    broken.symlink_to(tmp_path / "missing-target")
    with pytest.raises(SecretMissingError):
        resolve_secret(secret_ref_from_file_path(broken))

    world = _write_secret(tmp_path / "world", "x", mode=0o644)
    with pytest.raises(SecretPermissionError, match="world-readable"):
        resolve_secret(secret_ref_from_file_path(world))

    writable = _write_secret(tmp_path / "ww", "x", mode=0o666)
    with pytest.raises(SecretPermissionError, match="world-writable"):
        resolve_secret(secret_ref_from_file_path(writable))


def test_file_oversized(tmp_path: Path) -> None:
    path = tmp_path / "big"
    path.write_bytes(b"a" * (DEFAULT_MAX_SECRET_BYTES + 1))
    path.chmod(0o600)
    with pytest.raises(SecretTooLargeError):
        resolve_secret(secret_ref_from_file_path(path))


def test_env_present_missing_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BL_TEST_SECRET", "value")
    ref = SecretRef(source=SecretSource.ENV, name="BL_TEST_SECRET")
    assert resolve_secret(ref).get_secret_value() == "value"
    monkeypatch.delenv("BL_TEST_SECRET", raising=False)
    with pytest.raises(SecretMissingError, match="unset"):
        resolve_secret(ref)
    monkeypatch.setenv("BL_TEST_SECRET", "")
    with pytest.raises(SecretMissingError, match="empty"):
        resolve_secret(ref)


def test_systemd_credential_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cred_dir = tmp_path / "creds"
    cred_dir.mkdir()
    _write_secret(cred_dir / "restic_password", "from-systemd")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(cred_dir))
    ref = SecretRef(source=SecretSource.SYSTEMD, name="restic_password")
    assert resolve_secret(ref).get_secret_value() == "from-systemd"

    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    with pytest.raises(SecretSourceUnavailableError):
        resolve_secret(ref)


def test_mounted_secret_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulate /run/secrets via absolute path under tmp (no Docker API).
    secret_path = _write_secret(tmp_path / "restic_password", "mounted-secret")
    ref = parse_secret_ref({"source": "mounted", "path": str(secret_path)})
    assert resolve_secret(ref).get_secret_value() == "mounted-secret"


def test_diagnose_sensitive_file(tmp_path: Path) -> None:
    path = _write_secret(tmp_path / "key.pem", "BEGIN")
    report = diagnose_sensitive_file(path)
    assert report.ok
    bad = _write_secret(tmp_path / "bad.pem", "BEGIN", mode=0o644)
    report2 = diagnose_sensitive_file(bad)
    assert not report2.ok
    assert "world-readable" in report2.issues
    # Never reads contents into issues.
    assert "BEGIN" not in " ".join(report2.issues)


def test_concurrent_resolution(tmp_path: Path) -> None:
    path = _write_secret(tmp_path / "pw", "concurrent-secret")
    ref = secret_ref_from_file_path(path)
    errors: list[BaseException] = []
    values: list[str] = []

    def worker() -> None:
        try:
            for _ in range(20):
                values.append(resolve_secret(ref).get_secret_value())
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert errors == []
    assert values
    assert set(values) == {"concurrent-secret"}


@given(
    st.one_of(
        st.integers(),
        st.booleans(),
        st.none(),
        st.text(min_size=0, max_size=40),
        st.dictionaries(
            st.sampled_from(["source", "path", "name", "cmd", "extra"]),
            st.one_of(
                st.text(min_size=0, max_size=80),
                st.integers(),
                st.none(),
                st.lists(st.text(max_size=10), max_size=3),
            ),
            max_size=5,
        ),
    )
)
@settings(max_examples=80, deadline=None)
def test_property_parse_rejects_or_accepts_safely(raw: object) -> None:
    try:
        ref = parse_secret_ref(raw, field_name="password")
    except InvalidSecretRefError as exc:
        assert "password" in exc.message or "source" in exc.message
        assert "super-secret" not in exc.message
        return
    # Accepted refs must serialize without secret values.
    mapping = ref.to_mapping()
    assert "source" in mapping
    assert secret_ref_roundtrip(ref).source is ref.source


def test_exception_messages_never_include_secret(tmp_path: Path) -> None:
    path = _write_secret(tmp_path / "pw", "do-not-leak-me", mode=0o644)
    with pytest.raises(SecretPermissionError) as exc:
        resolve_secret(secret_ref_from_file_path(path))
    assert "do-not-leak-me" not in exc.value.message
