"""Generic provisioning bundle tests."""

from __future__ import annotations

import json
from pathlib import Path

from backuplint.fleet.controller import FleetController
from backuplint.fleet.provisioning import (
    PROVISIONING_SCHEMA_VERSION,
    export_bundle_json,
    write_bundle_file,
)


def test_bulk_pending_and_bundle_export(tmp_path: Path) -> None:
    controller = FleetController(tmp_path / "c", hostname="localhost")
    try:
        pending = controller.create_pending_agents_bulk(
            count=5, label_prefix="web", ttl_hours=2
        )
        assert len(pending) == 5
        assert len({p["agent_id"] for p in pending}) == 5
        assert len({p["token"] for p in pending}) == 5
        # Raw tokens not in DB file.
        db = (tmp_path / "c" / "controller.sqlite3").read_bytes()
        for p in pending:
            assert p["token"].encode() not in db
        ca = (controller.ca_dir / "ca.crt").read_text(encoding="utf-8")
        records = controller.export_provisioning_records(
            controller_url="https://controller.example:8443",
            pending=pending,
            ca_cert_pem=ca,
            role="agent",
        )
        out = tmp_path / "bundle.json"
        write_bundle_file(out, records, include_tokens=True)  # type: ignore[arg-type]
        assert (out.stat().st_mode & 0o777) == 0o600
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["schema_version"] == PROVISIONING_SCHEMA_VERSION
        assert payload["sensitive"] is True
        assert payload["record_count"] == 5
        assert payload["records"][0]["enrollment_token"]
        assert "PRIVATE KEY" not in out.read_text(encoding="utf-8")
        redacted = export_bundle_json(records, include_tokens=False)  # type: ignore[arg-type]
        assert "enrollment_token" not in redacted or '"enrollment_token": null' in redacted
        listed = controller.store.list_pending_enrollments()
        assert len(listed) == 5
        controller.store.revoke_pending_enrollment(pending[0]["agent_id"])
        assert (
            controller.store.get_pending_enrollment(pending[0]["agent_id"])["status"]
            == "revoked"
        )
    finally:
        controller.close()
