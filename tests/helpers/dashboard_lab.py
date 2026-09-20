"""Deterministic disposable dashboard dataset for browser / visual QA."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from backuplint.events import EVENT_SCHEMA_VERSION, EventType, new_event_id, new_run_id
from backuplint.fleet.controller import FleetController
from backuplint.fleet.controller_store import ControllerStore
from backuplint.fleet.dashboard.config import DashboardConfig
from backuplint.fleet.protocol import PROTOCOL_VERSION
from backuplint.fleet.queue_policy import DATA_GAP_RESULT_MARKER, QUEUE_OVERFLOW_STATUS

PASSWORD = "dashboard-qa-password-ok"


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _insert_event(
    store: ControllerStore,
    *,
    agent_id: str,
    event_type: str,
    status: str,
    occurred_at: str,
    received_at: str | None = None,
    payload: dict | None = None,
) -> None:
    payload = payload or {"summary": {"result": status}}
    with store._lock:  # noqa: SLF001
        store._conn.execute(  # noqa: SLF001
            """
            INSERT INTO events (
              event_id, schema_version, event_type, agent_id, run_id,
              submission_id, occurred_at, received_at, status, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_event_id(),
                EVENT_SCHEMA_VERSION,
                event_type,
                agent_id,
                new_run_id(),
                new_event_id(),
                occurred_at,
                received_at or occurred_at,
                status,
                json.dumps(payload, separators=(",", ":")),
            ),
        )


def _set_heartbeat(store: ControllerStore, agent_id: str, when: datetime) -> None:
    with store._lock:  # noqa: SLF001
        store._conn.execute(  # noqa: SLF001
            """
            INSERT INTO heartbeats (agent_id, last_heartbeat) VALUES (?, ?)
            ON CONFLICT(agent_id) DO UPDATE SET last_heartbeat=excluded.last_heartbeat
            """,
            (agent_id, _iso(when)),
        )


def _set_agent_meta(
    store: ControllerStore,
    agent_id: str,
    *,
    protocol_version: int | None = None,
    software_version: str | None = None,
    capabilities: dict | None = None,
    status: str | None = None,
    label: str | None = None,
    hostname: str | None = None,
) -> None:
    with store._lock:  # noqa: SLF001
        if protocol_version is not None:
            store._conn.execute(  # noqa: SLF001
                "UPDATE agents SET protocol_version = ? WHERE agent_id = ?",
                (protocol_version, agent_id),
            )
        if software_version is not None:
            store._conn.execute(  # noqa: SLF001
                "UPDATE agents SET software_version = ? WHERE agent_id = ?",
                (software_version, agent_id),
            )
        if capabilities is not None:
            caps_json = json.dumps(capabilities, separators=(",", ":"), sort_keys=True)
            store._conn.execute(  # noqa: SLF001
                """
                UPDATE agents SET capabilities_json = ?, capabilities_updated_at = ?
                WHERE agent_id = ?
                """,
                (caps_json, _iso(_now()), agent_id),
            )
        if status is not None:
            store._conn.execute(  # noqa: SLF001
                "UPDATE agents SET status = ? WHERE agent_id = ?",
                (status, agent_id),
            )
        if label is not None:
            store._conn.execute(  # noqa: SLF001
                "UPDATE agents SET label = ? WHERE agent_id = ?",
                (label, agent_id),
            )
        if hostname is not None:
            store._conn.execute(  # noqa: SLF001
                "UPDATE agents SET hostname = ? WHERE agent_id = ?",
                (hostname, agent_id),
            )


def _caps(
    *,
    restore_installed: bool = True,
    restore_supported: bool = True,
    restore_available: bool = True,
    restore_reason: str = "OK",
    coverage_available: bool = True,
) -> dict:
    return {
        "schema_version": 1,
        "deployment_form": "native",
        "capabilities": {
            "coverage_scanning": {
                "installed": True,
                "supported": True,
                "available": coverage_available,
                "reason": "OK" if coverage_available else "TEMPORARILY_UNAVAILABLE",
            },
            "restore_verification": {
                "installed": restore_installed,
                "supported": restore_supported,
                "available": restore_available and restore_installed and restore_supported,
                "reason": restore_reason,
            },
            "standard_integrity": {
                "installed": True,
                "supported": True,
                "available": True,
                "reason": "OK",
            },
        },
    }


def populate_populated_fleet(store: ControllerStore) -> dict[str, str]:
    """Seed a rich fleet. Returns mapping of scenario → agent_id."""
    now = _now()
    ids: dict[str, str] = {}

    scenarios = [
        ("online_pass", "ONLINE PASS", "host-online-pass", "PASS", 30, PROTOCOL_VERSION),
        ("online_warn", "ONLINE WARN", "host-online-warn", "WARN", 20, PROTOCOL_VERSION),
        ("online_fail", "ONLINE FAIL", "host-online-fail", "FAIL", 15, PROTOCOL_VERSION),
        ("online_error", "ONLINE ERROR", "host-online-error", "ERROR", 10, PROTOCOL_VERSION),
        ("offline_pass", "OFFLINE old PASS", "host-offline", "PASS", 7200, PROTOCOL_VERSION),
        ("stale_pass", "STALE old PASS", "host-stale", "PASS", 300, PROTOCOL_VERSION),
        ("proto_v1", "Protocol v1", "host-v1", "PASS", 40, 1),
        ("proto_warn", "Protocol warn", "host-badproto", "PASS", 25, 99),
        ("data_gap", "DATA_GAP agent", "host-gap", "PASS", 35, PROTOCOL_VERSION),
        ("caps_missing", "Caps NOT_INSTALLED", "host-caps", "PASS", 45, PROTOCOL_VERSION),
        ("no_history", "No history agent", "host-empty", None, 50, PROTOCOL_VERSION),
        (
            "xss",
            '<script>alert(1)</script>',
            '<img src=x onerror=alert(1)>',
            "PASS",
            55,
            PROTOCOL_VERSION,
        ),
        (
            "unicode",
            "エージェント 日本語",
            "host-ünicode-🚀",
            "WARN",
            60,
            PROTOCOL_VERSION,
        ),
        (
            "long_label",
            "L" * 180,
            "H" * 120,
            "PASS",
            65,
            PROTOCOL_VERSION,
        ),
    ]

    for key, label, hostname, audit, hb_age, proto in scenarios:
        agent_id = f"agent-{key.replace('_', '')[:20]}0001"
        if len(agent_id) < 8:
            agent_id = f"agent-{key}-00000001"
        store.register_agent(agent_id=agent_id, label=label[:200], hostname=hostname[:200])
        ids[key] = agent_id
        _set_agent_meta(
            store,
            agent_id,
            protocol_version=proto,
            software_version="0.5.0.dev0" if proto != 99 else "0.4.9",
            capabilities=_caps(
                restore_installed=(key != "caps_missing"),
                restore_supported=(key != "caps_missing"),
                restore_available=(key != "caps_missing"),
                restore_reason="NOT_CONFIGURED" if key == "caps_missing" else "OK",
                coverage_available=(key != "caps_missing"),
            )
            if key != "no_history"
            else None,
        )
        if key == "caps_missing":
            _set_agent_meta(
                store,
                agent_id,
                capabilities=_caps(
                    restore_installed=False,
                    restore_supported=False,
                    restore_available=False,
                    restore_reason="BINARY_MISSING",
                    coverage_available=False,
                ),
            )
            # also add unsupported + unavailable families via raw update
            caps = _caps(
                restore_installed=False,
                restore_supported=False,
                restore_available=False,
                restore_reason="BINARY_MISSING",
            )
            caps["capabilities"]["deep_integrity"] = {
                "installed": True,
                "supported": False,
                "available": False,
                "reason": "PLATFORM_UNSUPPORTED",
            }
            caps["capabilities"]["docker_compose"] = {
                "installed": True,
                "supported": True,
                "available": False,
                "reason": "TEMPORARILY_UNAVAILABLE",
            }
            _set_agent_meta(store, agent_id, capabilities=caps)

        if hb_age is not None and key != "offline_pass":
            _set_heartbeat(store, agent_id, now - timedelta(seconds=hb_age))
        elif key == "offline_pass":
            _set_heartbeat(store, agent_id, now - timedelta(seconds=hb_age))

        if audit is not None:
            old = key in {"offline_pass", "stale_pass"}
            occurred = _iso(
                now - (timedelta(hours=2) if old else timedelta(minutes=5))
            )
            # OOO example: older occurred_at received later for online_pass
            if key == "online_pass":
                _insert_event(
                    store,
                    agent_id=agent_id,
                    event_type=EventType.AUDIT_COMPLETED.value,
                    status="WARN",
                    occurred_at=_iso(now - timedelta(hours=5)),
                    received_at=_iso(now - timedelta(minutes=1)),
                    payload={"summary": {"result": "WARN", "note": "older-scan"}},
                )
            _insert_event(
                store,
                agent_id=agent_id,
                event_type=EventType.AUDIT_COMPLETED.value,
                status=audit,
                occurred_at=occurred,
                received_at=_iso(now - timedelta(minutes=2)),
                payload={"summary": {"result": audit}},
            )
            if key in {"online_pass", "online_fail", "caps_missing"}:
                _insert_event(
                    store,
                    agent_id=agent_id,
                    event_type=EventType.COVERAGE_RESULT.value,
                    status=audit if audit != "ERROR" else "FAIL",
                    occurred_at=occurred,
                )
                _insert_event(
                    store,
                    agent_id=agent_id,
                    event_type=EventType.INTEGRITY_RESULT.value,
                    status="PASS",
                    occurred_at=occurred,
                )
                _insert_event(
                    store,
                    agent_id=agent_id,
                    event_type=EventType.RESTORE_RESULT.value,
                    status="PASS" if key != "caps_missing" else "UNKNOWN",
                    occurred_at=occurred,
                    payload={
                        "summary": {
                            "result": "NOT_INSTALLED"
                            if key == "caps_missing"
                            else "PASS"
                        }
                    },
                )

        if key == "data_gap":
            _insert_event(
                store,
                agent_id=agent_id,
                event_type=EventType.AUDIT_COMPLETED.value,
                status=QUEUE_OVERFLOW_STATUS,
                occurred_at=_iso(now - timedelta(minutes=3)),
                payload={
                    "result": {
                        "queue_marker": DATA_GAP_RESULT_MARKER,
                        "summary": {
                            "result": DATA_GAP_RESULT_MARKER,
                            "status": QUEUE_OVERFLOW_STATUS,
                        },
                    }
                },
            )

    # revoked
    revoked = "agent-revoked00000001"
    store.register_agent(agent_id=revoked, label="Revoked agent", hostname="host-revoked")
    store.set_agent_status(revoked, "revoked")
    _set_heartbeat(store, revoked, now - timedelta(seconds=10))
    _insert_event(
        store,
        agent_id=revoked,
        event_type=EventType.AUDIT_COMPLETED.value,
        status="PASS",
        occurred_at=_iso(now - timedelta(hours=1)),
    )
    ids["revoked"] = revoked

    # large history on online_pass
    hist_agent = ids["online_pass"]
    for i in range(80):
        _insert_event(
            store,
            agent_id=hist_agent,
            event_type=EventType.SCHEDULE_RUN.value,
            status="PASS" if i % 7 else "FAIL",
            occurred_at=_iso(now - timedelta(minutes=i + 10)),
            payload={"summary": {"result": "PASS" if i % 7 else "FAIL", "i": i}},
        )

    return ids


def start_dashboard_lab(
    data_dir: Path,
    *,
    populated: bool = True,
    password: str = PASSWORD,
    online_after_seconds: int = 120,
    stale_after_seconds: int = 600,
) -> tuple[FleetController, str, dict[str, str]]:
    """Start HTTPS controller+dashboard. Returns (controller, base_url, agent_ids)."""
    cfg = DashboardConfig(
        enabled=True,
        cookie_secure=True,
        online_after_seconds=online_after_seconds,
        stale_after_seconds=stale_after_seconds,
        max_page_size=25,
        default_page_size=10,
        login_rate_limit=30,
        login_rate_window_seconds=60,
    )
    controller = FleetController(data_dir, hostname="localhost", dashboard=cfg)
    controller.dashboard_auth.set_password(password)
    ids: dict[str, str] = {}
    if populated:
        ids = populate_populated_fleet(controller.store)
    host, port = controller.start(host="127.0.0.1", port=0)
    return controller, f"https://{host}:{port}", ids
