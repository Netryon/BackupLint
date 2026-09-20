"""GitOps import/export/diff tests."""

from __future__ import annotations

from pathlib import Path

from backuplint.policy.gitops import (
    diff_policy_files,
    export_policy_file,
    import_policy_file,
    load_policy_file,
)
from backuplint.policy.schema import build_policy_snapshot


def _snap() -> object:
    return build_policy_snapshot(
        policy_id="pol-gitops01",
        created_by="test",
        display_name="GitOps",
        settings={"reporting": {"policy_poll_interval": "5m"}},
    )


def test_export_import_roundtrip(tmp_path: Path) -> None:
    snap = _snap()
    path = tmp_path / "policy.json"
    export_policy_file(snap, path)
    loaded = load_policy_file(path)
    assert loaded.policy_id == snap.policy_id
    assert loaded.content_sha256 == snap.content_sha256
    imported = import_policy_file(path, created_by="operator")
    assert imported.settings == snap.settings
    assert imported.revision_id != snap.revision_id


def test_diff_detects_changes(tmp_path: Path) -> None:
    left = _snap()
    right = build_policy_snapshot(
        policy_id=left.policy_id,
        created_by="test",
        display_name="Changed",
        settings=left.settings,
    )
    left_path = tmp_path / "left.json"
    right_path = tmp_path / "right.json"
    export_policy_file(left, left_path)
    export_policy_file(right, right_path)
    diff = diff_policy_files(left_path, right_path)
    assert diff["equal"] is False
    assert "display_name" in diff["changed"]
