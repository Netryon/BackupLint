"""Generic, provider-neutral provisioning bundle schema and helpers.

BackupLint prepares machine-readable enrollment data for an administrator's
existing provisioning system. It does not become Ansible/AWX/cloud-init itself.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

PROVISIONING_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class ProvisioningRecord:
    """One pending agent provisioning record (may include a raw one-time token)."""

    schema_version: int
    agent_id: str
    label: str
    controller_url: str
    ca_cert_pem: str | None
    role: str
    feature_profile: str
    deployment_form: str
    enrollment_token: str | None
    token_expires_at: str | None
    metadata: dict[str, str]

    def to_dict(self, *, include_token: bool = True) -> dict[str, Any]:
        data = asdict(self)
        if not include_token:
            data["enrollment_token"] = None
            data.pop("enrollment_token", None)
            data["token_redacted"] = True
        else:
            data["token_redacted"] = False
            data["sensitive"] = True
        return data


def build_provisioning_record(
    *,
    agent_id: str,
    label: str,
    controller_url: str,
    role: str = "agent",
    feature_profile: str = "default",
    deployment_form: str = "native",
    enrollment_token: str | None = None,
    token_expires_at: str | None = None,
    ca_cert_pem: str | None = None,
    metadata: dict[str, str] | None = None,
) -> ProvisioningRecord:
    if not controller_url.lower().startswith("https://"):
        raise ValueError("controller_url must use https://")
    return ProvisioningRecord(
        schema_version=PROVISIONING_SCHEMA_VERSION,
        agent_id=agent_id,
        label=label,
        controller_url=controller_url.rstrip("/"),
        ca_cert_pem=ca_cert_pem,
        role=role,
        feature_profile=feature_profile,
        deployment_form=deployment_form,
        enrollment_token=enrollment_token,
        token_expires_at=token_expires_at,
        metadata=dict(metadata or {}),
    )


def export_bundle_json(
    records: list[ProvisioningRecord],
    *,
    include_tokens: bool = True,
) -> str:
    payload = {
        "schema_version": PROVISIONING_SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "record_count": len(records),
        "sensitive": include_tokens,
        "records": [r.to_dict(include_token=include_tokens) for r in records],
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def write_bundle_file(
    path: Path,
    records: list[ProvisioningRecord],
    *,
    include_tokens: bool = True,
) -> None:
    """Write bundle with owner-only permissions when tokens are included.

    Creates the file with mode 0600 from the start (no write-then-chmod race).
    """
    payload = export_bundle_json(records, include_tokens=include_tokens)
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = 0o600 if include_tokens else 0o644
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(payload)
    # Re-affirm mode in case umask interfered with O_CREAT.
    os.chmod(path, mode)


def default_token_expiry(*, ttl_hours: int = 24) -> str:
    return (datetime.now(UTC) + timedelta(hours=ttl_hours)).isoformat()
