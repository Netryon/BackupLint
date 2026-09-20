"""Policy schema and hashing tests."""

from __future__ import annotations

import pytest

from backuplint.policy.schema import (
    POLICY_SCHEMA_VERSION,
    PolicyError,
    build_policy_snapshot,
    canonical_json,
    content_sha256,
    parse_policy_snapshot,
)


def _settings() -> dict[str, object]:
    return {
        "schedule": {"coverage": {"enabled": True, "every": "30m"}},
        "reporting": {"policy_poll_interval": "5m"},
    }


def test_build_and_verify_hash() -> None:
    snap = build_policy_snapshot(
        policy_id="pol-test0001",
        created_by="test",
        display_name="Test",
        settings=_settings(),
    )
    assert snap.schema_version == POLICY_SCHEMA_VERSION
    body = snap.canonical_body()
    assert content_sha256(body) == snap.content_sha256
    roundtrip = parse_policy_snapshot(snap.to_dict())
    assert roundtrip.content_sha256 == snap.content_sha256


def test_reject_unknown_schema_version() -> None:
    snap = build_policy_snapshot(
        policy_id="pol-test0002",
        created_by="test",
        display_name="Test",
        settings=_settings(),
    )
    data = snap.to_dict()
    data["schema_version"] = 99
    with pytest.raises(PolicyError):
        parse_policy_snapshot(data)


def test_reject_hash_mismatch() -> None:
    snap = build_policy_snapshot(
        policy_id="pol-test0003",
        created_by="test",
        display_name="Test",
        settings=_settings(),
    )
    data = snap.to_dict()
    data["content_sha256"] = "0" * 64
    with pytest.raises(PolicyError, match="mismatch"):
        parse_policy_snapshot(data)


def test_canonical_json_sorted() -> None:
    payload = {"b": 1, "a": 2}
    assert canonical_json(payload) == '{"a":2,"b":1}'
