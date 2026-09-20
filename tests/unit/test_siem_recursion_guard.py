"""SIEM recursion guard: non-exportable families must never enter the queue."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backuplint.siem.event import NON_EXPORTABLE_FAMILIES, SiemEventFamily
from backuplint.siem.queue import SiemExportQueue, SiemQueueError


def test_non_exportable_family_enqueue_raises(tmp_path) -> None:
    queue = SiemExportQueue(tmp_path / "siem_export.sqlite3")
    fake_event = SimpleNamespace(
        event_id="66666666-6666-4666-8666-666666666666",
        event_family=SiemEventFamily.SIEM_DELIVERY_ERROR,
        severity=SimpleNamespace(value="info"),
        priority_class="healthy",
        schema_version=1,
        occurred_at="2026-09-15T12:00:00+00:00",
        to_json=lambda: "{}",
    )
    try:
        with pytest.raises(SiemQueueError, match="non-exportable"):
            queue.enqueue(fake_event)  # type: ignore[arg-type]
    finally:
        queue.close()


def test_non_exportable_set_is_explicit() -> None:
    assert NON_EXPORTABLE_FAMILIES == frozenset({SiemEventFamily.SIEM_DELIVERY_ERROR})
