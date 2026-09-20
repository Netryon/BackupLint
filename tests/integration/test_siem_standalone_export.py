"""Integration tests for standalone SIEM export paths."""

from __future__ import annotations

from pathlib import Path

from backuplint.audit import AuditOutcome
from backuplint.secrets import SecretRef, SecretSource
from backuplint.siem.config import SiemAuthType, SiemConfig
from backuplint.siem.runtime import LocalSiemFeed, should_use_local_siem_feed
from backuplint.siem.transport.base import TransportResult


class RecordingTransport:
    name = "recording"

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[str] = []
        self.fail = fail

    def send(self, event, *, config: SiemConfig) -> TransportResult:
        self.sent.append(event.event_id)
        if self.fail:
            return TransportResult(False, True, 503, None, "down")
        return TransportResult(True, False, 200, None, None)


def _siem_config() -> SiemConfig:
    return SiemConfig(
        enabled=True,
        endpoint="https://siem.example.internal/ingest",
        auth_type=SiemAuthType.BEARER,
        auth_token=SecretRef(source=SecretSource.ENV, name="BACKUPLINT_SIEM_TOKEN"),
        tls_verify=False,
    )


def _failure_outcome() -> AuditOutcome:
    from backuplint.coverage import CoverageFinding, CoverageStatus
    from backuplint.reporting import AuditResult, AuditSummary

    finding = CoverageFinding(
        service="web",
        mount=None,
        storage_class=None,
        status=CoverageStatus.NOT_PROTECTED,
        detail="missing backup",
        host_path="/data",
    )
    summary = AuditSummary(
        protected=0,
        warning=0,
        critical=1,
        skipped=0,
        result=AuditResult.FAIL,
    )
    return AuditOutcome(findings=[finding], summary=summary, integrity=None, restore=None)


def test_standalone_disabled_is_noop(tmp_path: Path) -> None:
    feed = LocalSiemFeed.open(tmp_path / "state", SiemConfig(enabled=False))
    try:
        feed.submit_outcome(_failure_outcome())
        summary = feed.flush()
    finally:
        feed.close()
    assert summary is None
    assert not (tmp_path / "state" / "siem" / "siem_export.sqlite3").exists()


def test_standalone_enqueue_and_flush_delivers(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BACKUPLINT_SIEM_TOKEN", "test-token")
    feed = LocalSiemFeed.open(tmp_path / "state", _siem_config())
    transport = RecordingTransport()
    assert feed.exporter is not None
    feed.exporter.transport = transport
    try:
        feed.submit_outcome(_failure_outcome())
        summary = feed.flush()
        assert summary is not None
        assert summary.delivered == 1
        assert transport.sent
        snap = feed.status()
        assert snap is not None
        assert snap["delivered_total"] >= 1
    finally:
        feed.close()


def test_standalone_outage_does_not_raise(tmp_path: Path) -> None:
    feed = LocalSiemFeed.open(tmp_path / "state", _siem_config())
    transport = RecordingTransport(fail=True)
    assert feed.exporter is not None
    feed.exporter.transport = transport
    try:
        feed.submit_outcome(_failure_outcome())
        summary = feed.flush()
        assert summary is not None
        assert summary.delivered == 0
        depth = feed.exporter.queue.depth_by_status()
        assert depth.get("pending", 0) >= 1
    finally:
        feed.close()


def test_should_use_local_siem_feed_rules() -> None:
    assert should_use_local_siem_feed(None, has_fleet=False) is False
    assert should_use_local_siem_feed(_siem_config(), has_fleet=False) is True
    assert should_use_local_siem_feed(_siem_config(), has_fleet=True) is False
