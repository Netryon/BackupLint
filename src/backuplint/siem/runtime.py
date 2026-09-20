"""Runtime wiring for SIEM export (controller and standalone drivers)."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from backuplint import __version__ as SOFTWARE_VERSION
from backuplint.events import CanonicalEvent, EventType, parse_event
from backuplint.siem.config import SiemConfig, SiemConfigError, parse_siem_config
from backuplint.siem.event import audit_result_to_siem_event, to_siem_event
from backuplint.siem.exporter import DrainSummary, SiemExporter
from backuplint.siem.queue import SiemExportQueue
from backuplint.siem.telemetry import SiemExportTelemetry
from backuplint.siem.transport.https_json import HttpsJsonTransport

_logger = logging.getLogger(__name__)


def siem_dir_for_data(data_dir: Path) -> Path:
    return data_dir / "siem"


def load_siem_config_file(path: Path) -> SiemConfig:
    text = path.expanduser().read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix in {".yaml", ".yml"}:
        raw = yaml.safe_load(text)
    else:
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            raw = yaml.safe_load(text)
    return parse_siem_config(raw, config_dir=path.parent.resolve())


def resolve_controller_siem_config(
    *,
    siem_config_path: Path | None = None,
) -> SiemConfig | None:
    """Resolve controller SIEM config from CLI path or env vars."""
    env_file = os.environ.get("BACKUPLINT_SIEM_CONFIG_FILE", "").strip()
    env_flag = os.environ.get("BACKUPLINT_CONTROLLER_SIEM", "").strip().lower()
    path = siem_config_path
    if path is None and env_file:
        path = Path(env_file)
    if path is not None:
        try:
            return load_siem_config_file(path)
        except (OSError, SiemConfigError, yaml.YAMLError) as exc:
            raise SiemConfigError(str(exc)) from exc
    if env_flag in {"1", "true", "yes", "on"}:
        raise SiemConfigError(
            "BACKUPLINT_CONTROLLER_SIEM is set but no SIEM config file was provided; "
            "use --siem-config or BACKUPLINT_SIEM_CONFIG_FILE"
        )
    return None


def build_exporter(data_dir: Path, config: SiemConfig) -> SiemExporter | None:
    if not config.enabled:
        return None
    siem_dir = siem_dir_for_data(data_dir)
    siem_dir.mkdir(parents=True, exist_ok=True)
    queue = SiemExportQueue(siem_dir / "siem_export.sqlite3", limits=config.queue)
    telemetry = SiemExportTelemetry()
    transport = HttpsJsonTransport()
    return SiemExporter(queue, transport, config, telemetry)


def read_telemetry_tail(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    lines = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not lines:
        return None
    try:
        payload = json.loads(lines[-1])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def telemetry_snapshot(
    exporter: SiemExporter | None,
    *,
    telemetry_path: Path | None = None,
    persist: bool = False,
) -> dict[str, Any] | None:
    if exporter is None:
        return None
    snap = exporter.telemetry.snapshot(exporter.queue, persist=persist)
    if persist and telemetry_path is not None:
        exporter.telemetry.persist_to_jsonl(telemetry_path, snap)
    return snap


def submit_canonical_event(
    exporter: SiemExporter | None,
    canonical: CanonicalEvent,
    *,
    source_role: str,
) -> None:
    if exporter is None:
        return
    try:
        siem_event = to_siem_event(
            canonical,
            source_role=source_role,
            received_at=canonical.received_at,
            software_version=SOFTWARE_VERSION,
        )
        if siem_event is None:
            return
        exporter.submit(siem_event)
    except Exception:  # noqa: BLE001 - must not affect caller exit paths
        _logger.warning("SIEM enqueue failed", exc_info=False)


def submit_audit_outcome(
    exporter: SiemExporter | None,
    outcome: object,
    *,
    source_role: str = "standalone",
    occurred_at: str | None = None,
) -> None:
    if exporter is None:
        return
    try:
        import json

        from backuplint.reporting import format_audit_json

        findings = getattr(outcome, "findings", None)
        if findings is None:
            return
        integrity = getattr(outcome, "integrity", None)
        restore = getattr(outcome, "restore", None)
        result_obj = json.loads(
            format_audit_json(findings, integrity=integrity, restore=restore)
        )
        if not isinstance(result_obj, dict):
            return
        when = occurred_at or datetime.now(UTC).isoformat()
        siem_event = audit_result_to_siem_event(
            result_obj,
            occurred_at=when,
            source_role=source_role,
            received_at=when,
            software_version=SOFTWARE_VERSION,
            event_type=EventType.AUDIT_COMPLETED,
        )
        if siem_event is None:
            return
        exporter.submit(siem_event)
    except Exception:  # noqa: BLE001
        _logger.warning("SIEM local submit failed", exc_info=False)


def should_use_local_siem_feed(config: SiemConfig | None, *, has_fleet: bool) -> bool:
    if config is None or not config.enabled:
        return False
    if has_fleet:
        return False
    return True


@dataclass
class LocalSiemFeed:
    exporter: SiemExporter | None
    telemetry_path: Path | None

    @classmethod
    def open(cls, state_dir: Path, config: SiemConfig | None) -> LocalSiemFeed:
        if config is None or not config.enabled:
            return cls(exporter=None, telemetry_path=None)
        exporter = build_exporter(state_dir, config)
        telemetry_path = siem_dir_for_data(state_dir) / "telemetry.jsonl"
        return cls(exporter=exporter, telemetry_path=telemetry_path)

    def submit_outcome(self, outcome: object, *, occurred_at: str | None = None) -> None:
        submit_audit_outcome(
            self.exporter,
            outcome,
            source_role="standalone",
            occurred_at=occurred_at,
        )

    def flush(self) -> DrainSummary | None:
        if self.exporter is None:
            return None
        summary = self.exporter.drain_once()
        telemetry_snapshot(
            self.exporter,
            telemetry_path=self.telemetry_path,
            persist=True,
        )
        return summary

    def status(self) -> dict[str, Any] | None:
        if self.exporter is not None:
            return telemetry_snapshot(
                self.exporter,
                telemetry_path=self.telemetry_path,
                persist=False,
            )
        if self.telemetry_path is not None:
            return read_telemetry_tail(self.telemetry_path)
        return None

    def close(self) -> None:
        if self.exporter is not None:
            self.exporter.stop()


def event_dict_to_canonical(data: dict[str, object]) -> CanonicalEvent:
    return parse_event(
        {
            "schema_version": data["schema_version"],
            "event_id": data["event_id"],
            "event_type": data["event_type"],
            "run_id": data["run_id"],
            "occurred_at": data["occurred_at"],
            "status": data["status"],
            "payload": data["payload"],
            "agent_id": data.get("agent_id"),
            "received_at": data.get("received_at"),
            "submission_id": data.get("submission_id"),
        }
    )
