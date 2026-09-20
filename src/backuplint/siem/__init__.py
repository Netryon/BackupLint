"""SIEM export pipeline: normalized events, durable queue, HTTPS transport."""

from backuplint.siem.config import SIEM_CONFIG_SCHEMA_VERSION, SiemConfig, parse_siem_config
from backuplint.siem.event import (
    NON_EXPORTABLE_FAMILIES,
    SIEM_EVENT_SCHEMA_VERSION,
    SiemEvent,
    SiemEventFamily,
    SiemSeverity,
    to_siem_event,
)
from backuplint.siem.exporter import SiemExporter
from backuplint.siem.queue import SIEM_QUEUE_SCHEMA_VERSION, SiemExportQueue, SiemQueueError
from backuplint.siem.telemetry import SiemExportTelemetry
from backuplint.siem.transport.base import TransportResult
from backuplint.siem.transport.https_json import HttpsJsonTransport

__all__ = [
    "NON_EXPORTABLE_FAMILIES",
    "SIEM_CONFIG_SCHEMA_VERSION",
    "SIEM_EVENT_SCHEMA_VERSION",
    "SIEM_QUEUE_SCHEMA_VERSION",
    "HttpsJsonTransport",
    "SiemConfig",
    "SiemEvent",
    "SiemEventFamily",
    "SiemExportQueue",
    "SiemExportTelemetry",
    "SiemExporter",
    "SiemQueueError",
    "SiemSeverity",
    "TransportResult",
    "parse_siem_config",
    "to_siem_event",
]
