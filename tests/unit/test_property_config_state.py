"""Property-based tests for config rejection and integrity state parsing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from backuplint.config import ConfigError, load_config
from backuplint.integrity_state import IntegrityStateError, load_integrity_state

_settings = settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)

_secretish = st.sampled_from(
    [
        "super-secret-password-value",
        "AKIAIOSFODNN7EXAMPLE",
        "s3cr3t-key-material",
    ]
)


@_settings
@given(
    unknown_key=st.from_regex(r"[a-z]{3,12}", fullmatch=True).filter(
        lambda k: k
        not in {
            "backup_paths",
            "restic",
            "max_backup_age",
            "mode",
            "timeout",
            "expected_paths",
            "state_file",
            "max_age",
            "repository",
            "password_file",
            "integrity",
            "restore_verification",
        }
    ),
    secret=_secretish,
)
def test_unknown_top_level_keys_rejected_without_secret_leak(
    tmp_path: Path, unknown_key: str, secret: str
) -> None:
    cfg = tmp_path / "cfg.yml"
    payload = {
        "backup_paths": [],
        unknown_key: secret,
    }
    cfg.write_text(yaml.safe_dump(payload), encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(cfg)
    msg = exc.value.message
    assert "Unknown configuration key" in msg
    assert secret not in msg


@_settings
@given(
    bad_type=st.one_of(
        st.integers(),
        st.booleans(),
        st.dictionaries(st.text(min_size=1, max_size=4), st.integers(), max_size=2),
    )
)
def test_backup_paths_wrong_type_deterministic(tmp_path: Path, bad_type: object) -> None:
    # None is intentionally accepted as empty backup_paths (YAML null).
    cfg = tmp_path / "cfg.yml"
    cfg.write_text(
        yaml.safe_dump({"backup_paths": bad_type}),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as exc:
        load_config(cfg)
    assert "backup_paths" in exc.value.message


@_settings
@given(
    nested=st.dictionaries(
        st.sampled_from(["mode", "timeout", "expected_paths", "bogus", "password"]),
        st.one_of(st.integers(), st.booleans(), st.none(), st.text(max_size=20)),
        min_size=1,
        max_size=4,
    ),
    secret=_secretish,
)
def test_malformed_restore_verification_no_crash_no_secret(
    tmp_path: Path, nested: dict, secret: str
) -> None:
    cfg = tmp_path / "cfg.yml"
    body = {
        "backup_paths": [],
        "restic": {
            "repository": "/tmp/repo",
            "password_file": str(tmp_path / "pass"),
            "restore_verification": {**nested, "leak": secret}
            if "bogus" in nested or "password" in nested
            else nested,
        },
    }
    # Ensure password file exists so only nested parsing fails.
    (tmp_path / "pass").write_text("x\n", encoding="utf-8")
    (tmp_path / "pass").chmod(0o600)
    cfg.write_text(yaml.safe_dump(body), encoding="utf-8")
    try:
        load_config(cfg)
    except ConfigError as exc:
        assert secret not in exc.message
        assert "password_file" not in exc.message or secret not in exc.message
    except Exception as exc:  # noqa: BLE001 — property: never unexpected crash types
        pytest.fail(f"Unexpected exception type: {type(exc).__name__}: {exc}")


@_settings
@given(
    junk=st.one_of(
        st.binary(min_size=0, max_size=200),
        st.text(max_size=200).map(str.encode),
        st.just(b"{"),
        st.just(b"[]"),
        st.just(b'{"repositories": null}'),
        st.just(b'{"repositories": {"k": "not-a-map"}}'),
        st.just(b'{"repositories": {"k": {"last_success": "nope", "mode": "standard"}}}'),
    )
)
def test_malformed_integrity_state_never_trusted(
    tmp_path: Path, junk: bytes
) -> None:
    path = tmp_path / "state.json"
    path.write_bytes(junk)
    # Empty file / missing handled separately; empty mapping is ok.
    if junk in (b"", b"{}", b'{"repositories": {}}'):
        try:
            records = load_integrity_state(path)
        except IntegrityStateError:
            return
        assert records == {}
        return
    try:
        records = load_integrity_state(path)
    except IntegrityStateError:
        return
    # If it somehow parses, it must not invent success timestamps from garbage.
    for record in records.values():
        assert record.last_success is not None


def test_truncated_json_state_raises(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text('{"repositories": {"abc": {"last_success": "2026-', encoding="utf-8")
    with pytest.raises(IntegrityStateError):
        load_integrity_state(path)


def test_valid_state_roundtrip_minimal(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    payload = {
        "repositories": {
            "deadbeef": {
                "last_success": "2026-01-01T00:00:00+00:00",
                "mode": "standard",
                "restic_version": "0.18.1",
            }
        }
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    records = load_integrity_state(path)
    assert "deadbeef" in records
    assert records["deadbeef"].mode.value == "standard"
