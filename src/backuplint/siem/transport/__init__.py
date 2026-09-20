"""SIEM transport implementations."""

from backuplint.siem.transport.base import Transport, TransportResult
from backuplint.siem.transport.https_json import HttpsJsonTransport

__all__ = ["HttpsJsonTransport", "Transport", "TransportResult"]
