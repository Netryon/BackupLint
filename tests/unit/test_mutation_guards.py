"""Mutation-oriented regression guards for high-risk pure logic."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from backuplint.paths import is_path_covered
from backuplint.restic import ResticSnapshot, latest_relevant_snapshot
from backuplint.restore_dest import (
    RestoreDestinationError,
    assert_safe_restore_target,
    expected_materialized_path,
)
from backuplint.restore_verify import _classify_restore_operational_failure


def test_mutation_prefix_boundary_requires_separator() -> None:
    """Mutating relative_to to startswith would break this."""
    assert is_path_covered("/srv/app/data", "/srv/app")
    assert not is_path_covered("/srv/app2", "/srv/app")
    assert not is_path_covered("/srv/app", "/srv/app2")


def test_mutation_forbidden_exact_tmp() -> None:
    with pytest.raises(RestoreDestinationError):
        assert_safe_restore_target(Path("/tmp"))


def test_mutation_materialize_strips_only_root_slash(tmp_path: Path) -> None:
    out = expected_materialized_path(tmp_path, "/a/b")
    assert out == tmp_path / "a" / "b"
    # Relative sources stay relative (no accidental absolute join).
    out2 = expected_materialized_path(tmp_path, "rel/x")
    assert out2 == tmp_path / "rel" / "x"


def test_mutation_auth_failure_not_generic_fail() -> None:
    msg = _classify_restore_operational_failure(
        returncode=12,
        output="wrong password or no key found",
    )
    assert msg is not None
    assert "authentication" in msg.lower()


def test_mutation_latest_relevant_ignores_unrelated_newer() -> None:
    snaps = [
        ResticSnapshot(
            "old", "old", datetime(2026, 1, 1, tzinfo=UTC), ("/data",)
        ),
        ResticSnapshot(
            "new", "new", datetime(2026, 6, 1, tzinfo=UTC), ("/other",)
        ),
    ]
    picked = latest_relevant_snapshot(snaps, "/data")
    assert picked is not None
    assert picked.snapshot_id == "old"
