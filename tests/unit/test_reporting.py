"""Unit tests for PASS / WARN / FAIL audit reporting."""

from __future__ import annotations

import json
from unittest.mock import patch

from backuplint.coverage import evaluate_mount
from backuplint.models import Mount, MountType
from backuplint.reporting import (
    AuditResult,
    format_audit_json,
    format_audit_report,
    summarize_findings,
)


def _bind(service: str, source: str) -> Mount:
    return Mount(service=service, type=MountType.BIND, source=source, target="/config")


def test_pass_summary_and_report() -> None:
    findings = [
        evaluate_mount(_bind("sonarr", "/srv/docker/sonarr"), ("/srv/docker",)),
        evaluate_mount(_bind("radarr", "/srv/docker/radarr"), ("/srv/docker",)),
    ]
    summary = summarize_findings(findings)
    assert summary.result is AuditResult.PASS
    assert summary.exit_code == 0
    report = format_audit_report(findings)
    assert "BackupLint Audit" in report
    assert "Result: PASS" in report
    assert "2 protected" in report
    assert "0 warning" in report
    assert "0 critical" in report


def test_fail_summary_for_unprotected_bind() -> None:
    findings = [
        evaluate_mount(_bind("sonarr", "/srv/docker/sonarr"), ("/srv/docker",)),
        evaluate_mount(_bind("vaultwarden", "/srv/vaultwarden"), ("/srv/docker",)),
    ]
    summary = summarize_findings(findings)
    assert summary.result is AuditResult.FAIL
    assert summary.exit_code == 1
    assert summary.critical == 1
    report = format_audit_report(findings)
    assert "Result: FAIL" in report
    assert "✗" in report
    assert "not protected" in report


def test_warn_summary_for_unsupported_volume() -> None:
    volume = Mount(
        service="postgres",
        type=MountType.VOLUME,
        source="pgdata",
        target="/var/lib/postgresql/data",
    )
    with patch("backuplint.coverage.resolve_volume_mountpoint", return_value=None):
        findings = [evaluate_mount(volume, ("/srv/docker",))]
    summary = summarize_findings(findings)
    assert summary.result is AuditResult.WARN
    assert summary.exit_code == 0
    assert summary.warning == 1
    report = format_audit_report(findings)
    assert "Result: WARN" in report
    assert "⚠" in report
    assert "review required" in report


def test_json_output_shape() -> None:
    findings = [
        evaluate_mount(_bind("vaultwarden", "/srv/vaultwarden"), ("/srv/docker",)),
    ]
    payload = json.loads(format_audit_json(findings))
    assert payload["result"] == "FAIL"
    assert payload["summary"]["critical"] == 1
    assert payload["findings"][0]["service"] == "vaultwarden"
    assert payload["findings"][0]["critical"] is True
