"""Portable GitOps import/export/validate/diff for policy snapshots."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from backuplint.policy.errors import PolicyError
from backuplint.policy.schema import (
    MAX_POLICY_BYTES,
    PolicySnapshot,
    build_policy_snapshot,
    diff_snapshots,
    parse_policy_snapshot,
    snapshot_to_dict,
)

GITOPS_FILE_VERSION = 1


def export_policy_file(snapshot: PolicySnapshot, path: Path) -> None:
    payload = {
        "gitops_version": GITOPS_FILE_VERSION,
        "policy": snapshot_to_dict(snapshot),
    }
    text = json.dumps(payload, indent=2, sort_keys=True)
    if len(text.encode("utf-8")) > MAX_POLICY_BYTES:
        raise PolicyError("export file exceeds size limit")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")
    path.chmod(0o600)


def load_policy_file(path: Path) -> PolicySnapshot:
    raw_text = path.read_text(encoding="utf-8")
    if len(raw_text.encode("utf-8")) > MAX_POLICY_BYTES:
        raise PolicyError("import file exceeds size limit")
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise PolicyError(f"invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise PolicyError("policy file must be an object")
    gitops_version = data.get("gitops_version")
    if gitops_version != GITOPS_FILE_VERSION:
        raise PolicyError(f"unsupported gitops_version {gitops_version!r}")
    policy_raw = data.get("policy")
    return parse_policy_snapshot(policy_raw)


def import_policy_file(
    path: Path,
    *,
    created_by: str,
    policy_id: str | None = None,
) -> PolicySnapshot:
    loaded = load_policy_file(path)
    return build_policy_snapshot(
        policy_id=policy_id or loaded.policy_id,
        revision_id=None,
        created_by=created_by,
        display_name=loaded.display_name,
        description=loaded.description,
        settings=loaded.settings,
    )


def validate_policy_file(path: Path) -> dict[str, object]:
    snapshot = load_policy_file(path)
    return {
        "valid": True,
        "policy_id": snapshot.policy_id,
        "revision_id": snapshot.revision_id,
        "content_sha256": snapshot.content_sha256,
    }


def diff_policy_files(left_path: Path, right_path: Path) -> dict[str, object]:
    left = load_policy_file(left_path)
    right = load_policy_file(right_path)
    return diff_snapshots(left, right)


def diff_policy_dicts(
    left: dict[str, Any] | PolicySnapshot,
    right: dict[str, Any] | PolicySnapshot,
) -> dict[str, object]:
    return diff_snapshots(left, right)
