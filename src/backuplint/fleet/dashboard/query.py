"""Read/query service for the dashboard (no direct UI→SQLite coupling)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from backuplint.events import EventType
from backuplint.fleet.compat import (
    MAX_SUPPORTED_PROTOCOL_VERSION,
    MIN_SUPPORTED_PROTOCOL_VERSION,
)
from backuplint.fleet.controller_store import ControllerStore
from backuplint.fleet.dashboard.config import DashboardConfig
from backuplint.fleet.presence import OFFLINE, ONLINE, STALE, classify_presence
from backuplint.fleet.queue_policy import DATA_GAP_RESULT_MARKER, QUEUE_OVERFLOW_STATUS

__all__ = [
    "OFFLINE",
    "ONLINE",
    "STALE",
    "DashboardQueryService",
    "classify_presence",
]

FAILURE_STATUSES = frozenset({"FAIL", "ERROR", "QUEUE_OVERFLOW"})
ALERT_STATUSES = frozenset({"FAIL", "ERROR", "QUEUE_OVERFLOW"})
TREND_STATUSES = ("PASS", "WARN", "FAIL", "ERROR")
MAX_RECENT_ALERTS = 30
MAX_TREND_BUCKETS = 48
MAX_DASHBOARD_WINDOW = timedelta(days=7)


def _capability_summary(capabilities_json: str | None) -> dict[str, object]:
    if not capabilities_json:
        return {"schema_version": None, "families": {}, "available_count": 0}
    try:
        raw = json.loads(capabilities_json)
    except json.JSONDecodeError:
        return {
            "schema_version": None,
            "families": {},
            "available_count": 0,
            "parse_error": True,
        }
    if not isinstance(raw, dict):
        return {
            "schema_version": None,
            "families": {},
            "available_count": 0,
            "parse_error": True,
        }
    families = (
        raw.get("capabilities") if isinstance(raw.get("capabilities"), dict) else {}
    )
    available = 0
    compact: dict[str, object] = {}
    for key, value in families.items():
        if not isinstance(value, dict):
            continue
        installed = bool(value.get("installed"))
        supported = bool(value.get("supported"))
        avail = bool(value.get("available"))
        reason = str(value.get("reason") or "")
        compact[str(key)] = {
            "installed": installed,
            "supported": supported,
            "available": avail,
            "reason": reason,
        }
        if avail:
            available += 1
    return {
        "schema_version": raw.get("schema_version"),
        "deployment_form": raw.get("deployment_form"),
        "families": compact,
        "available_count": available,
    }


def _event_has_data_gap(status: str, payload_json: str) -> bool:
    if status == QUEUE_OVERFLOW_STATUS:
        return True
    if DATA_GAP_RESULT_MARKER in payload_json or QUEUE_OVERFLOW_STATUS in payload_json:
        return True
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, dict):
        return False
    result = payload.get("result")
    if isinstance(result, dict):
        if result.get("queue_marker") in (DATA_GAP_RESULT_MARKER, QUEUE_OVERFLOW_STATUS):
            return True
        if result.get("result") == DATA_GAP_RESULT_MARKER:
            return True
        summary = result.get("summary")
        if isinstance(summary, dict) and summary.get("result") == DATA_GAP_RESULT_MARKER:
            return True
    return False


def _safe_payload_summary(payload_json: str) -> str:
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, dict):
        return ""
    # Envelope-style: result.summary / result.queue_marker
    result = payload.get("result")
    if isinstance(result, dict):
        summary = result.get("summary")
        if isinstance(summary, dict):
            parts = [
                str(summary.get("result") or ""),
                str(summary.get("status") or ""),
            ]
            text = " ".join(p for p in parts if p).strip()
            if text:
                return text[:240]
        if "queue_marker" in result:
            return str(result.get("queue_marker"))[:240]
        if "result" in result:
            return str(result.get("result"))[:240]
    # Canonical event payload often *is* the audit result object.
    summary = payload.get("summary")
    if isinstance(summary, dict):
        parts = [
            str(summary.get("result") or ""),
            str(summary.get("status") or ""),
            str(summary.get("note") or ""),
        ]
        text = " ".join(p for p in parts if p).strip()
        if text:
            return text[:240]
    return str(payload.get("message") or "")[:240]


def _like_escape(value: str) -> str:
    return value.replace("\\", "").replace("%", "").replace("_", "").strip()


def _public_event(event: dict[str, object] | None) -> dict[str, object] | None:
    if event is None:
        return None
    payload_json = json.dumps(event.get("payload") or {})
    status = str(event.get("status") or "")
    return {
        "event_id": event.get("event_id"),
        "event_type": event.get("event_type"),
        "status": status,
        "occurred_at": event.get("occurred_at"),
        "received_at": event.get("received_at"),
        "summary": _safe_payload_summary(payload_json),
        "has_data_gap": _event_has_data_gap(status, payload_json),
    }


class DashboardQueryService:
    """Bounded read API over ControllerStore."""

    def __init__(
        self,
        store: ControllerStore,
        config: DashboardConfig,
        *,
        reader: object | None = None,
    ) -> None:
        self.store = store
        self.config = config
        # Optional dedicated RO connection so dashboard SELECTs do not hold the
        # write RLock (ingest/heartbeat stay responsive under load).
        self._reader = reader

    def _execute(self, sql: str, params: tuple[object, ...] | list[object] = ()) -> list[tuple]:
        if self._reader is not None:
            return list(self._reader.execute(sql, params).fetchall())  # type: ignore[union-attr]
        with self.store._lock:  # noqa: SLF001
            return list(self.store._conn.execute(sql, params).fetchall())  # noqa: SLF001

    def _fetchone(self, sql: str, params: tuple[object, ...] | list[object] = ()) -> tuple | None:
        rows = self._execute(sql, params)
        return rows[0] if rows else None

    def _clamp_limit(self, limit: int | None) -> int:
        if limit is None:
            return self.config.default_page_size
        return max(1, min(int(limit), self.config.max_page_size))

    def _clamp_offset(self, offset: int | None) -> int:
        if offset is None:
            return 0
        return max(0, min(int(offset), 1_000_000))

    def resolve_time_window(
        self,
        *,
        time_range: str | None = None,
        time_from: str | None = None,
        time_to: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, str]:
        """Bounded occurred_at window for trends/alerts/history (default last 24h)."""
        current = now or datetime.now(UTC)
        tr = (time_range or "24h").strip().lower()
        if tr not in {"1h", "24h", "7d", "custom"}:
            tr = "24h"
        if tr == "custom" and time_from and time_to:
            start = time_from[:64]
            end = time_to[:64]
        elif tr == "1h":
            start = (current - timedelta(hours=1)).isoformat()
            end = current.isoformat()
        elif tr == "7d":
            start = (current - timedelta(days=7)).isoformat()
            end = current.isoformat()
        else:
            tr = "24h"
            start = (current - timedelta(hours=24)).isoformat()
            end = current.isoformat()
        # Hard cap: never scan more than 7 days even if custom range is wider.
        earliest = current - MAX_DASHBOARD_WINDOW
        if start < earliest.isoformat():
            start = earliest.isoformat()
        if end > current.isoformat():
            end = current.isoformat()
        return {
            "time_range": tr,
            "time_from": start,
            "time_to": end,
        }

    def list_recent_alerts(
        self,
        *,
        time_from: str,
        time_to: str,
        limit: int | None = None,
    ) -> dict[str, object]:
        """Important fleet conditions from stored events (no invented alerts)."""
        page = min(MAX_RECENT_ALERTS, self._clamp_limit(limit or 20))
        gap_like = f"%{DATA_GAP_RESULT_MARKER}%"
        overflow_like = f"%{QUEUE_OVERFLOW_STATUS}%"
        rows = self._execute(
            """
            SELECT event_id, agent_id, event_type, status, occurred_at, received_at,
                   payload_json
            FROM events
            WHERE occurred_at >= ? AND occurred_at <= ?
              AND (
                status IN (?, ?, ?)
                OR payload_json LIKE ?
                OR payload_json LIKE ?
              )
            ORDER BY occurred_at DESC, received_at DESC, event_id DESC
            LIMIT ?
            """,
            (
                time_from[:64],
                time_to[:64],
                "FAIL",
                "ERROR",
                QUEUE_OVERFLOW_STATUS,
                gap_like,
                overflow_like,
                page,
            ),
        )
        items: list[dict[str, object]] = []
        for row in rows:
            payload_json = str(row[6])
            status = str(row[3])
            kind = status
            if _event_has_data_gap(status, payload_json) and status not in ALERT_STATUSES:
                kind = "DATA_GAP"
            items.append(
                {
                    "alert_kind": kind,
                    "event_id": row[0],
                    "agent_id": row[1],
                    "event_type": row[2],
                    "status": status,
                    "occurred_at": row[4],
                    "received_at": row[5],
                    "summary": _safe_payload_summary(payload_json),
                    "has_data_gap": _event_has_data_gap(status, payload_json),
                }
            )
        proto_rows = self._execute(
            """
            SELECT agent_id, label, protocol_version,
                   COALESCE(last_seen, first_seen) AS seen_at
            FROM agents
            WHERE status != 'revoked'
              AND (protocol_version < ? OR protocol_version > ?)
            ORDER BY agent_id
            LIMIT ?
            """,
            (
                MIN_SUPPORTED_PROTOCOL_VERSION,
                MAX_SUPPORTED_PROTOCOL_VERSION,
                max(0, page - len(items)),
            ),
        )
        for row in proto_rows:
            items.append(
                {
                    "alert_kind": "PROTOCOL",
                    "event_id": None,
                    "agent_id": row[0],
                    "event_type": "fleet.protocol",
                    "status": "PROTOCOL",
                    "occurred_at": row[3],
                    "received_at": row[3],
                    "summary": (
                        f"Protocol v{row[2]} outside supported "
                        f"{MIN_SUPPORTED_PROTOCOL_VERSION}–{MAX_SUPPORTED_PROTOCOL_VERSION}"
                    ),
                    "has_data_gap": False,
                }
            )
        items.sort(key=lambda x: str(x.get("occurred_at") or ""), reverse=True)
        items = items[:page]
        return {
            "schema_version": 1,
            "items": items,
            "limit": page,
            "returned": len(items),
            "time_from": time_from,
            "time_to": time_to,
        }

    def audit_status_trend(
        self,
        *,
        time_from: str,
        time_to: str,
        max_buckets: int | None = None,
    ) -> dict[str, object]:
        """PASS/WARN/FAIL/ERROR counts over time (occurred_at), bounded buckets."""
        cap = min(MAX_TREND_BUCKETS, max(4, int(max_buckets or 24)))
        try:
            start_dt = datetime.fromisoformat(time_from.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(time_to.replace("Z", "+00:00"))
        except ValueError:
            start_dt = datetime.now(UTC) - timedelta(hours=24)
            end_dt = datetime.now(UTC)
        span = end_dt - start_dt
        bucket_len = 13 if span <= timedelta(hours=48) else 10  # hour vs day prefix
        rows = self._execute(
            f"""
            SELECT substr(occurred_at, 1, {bucket_len}) AS bucket, status, COUNT(*) AS n
            FROM events
            WHERE occurred_at >= ? AND occurred_at <= ?
              AND status IN ('PASS', 'WARN', 'FAIL', 'ERROR')
            GROUP BY bucket, status
            ORDER BY bucket ASC
            LIMIT ?
            """,  # noqa: S608
            (time_from[:64], time_to[:64], cap * len(TREND_STATUSES)),
        )
        buckets: dict[str, dict[str, int]] = {}
        for bucket, status, count in rows:
            key = str(bucket)
            if key not in buckets:
                buckets[key] = {s: 0 for s in TREND_STATUSES}
            st = str(status)
            if st in buckets[key]:
                buckets[key][st] = int(count)
        ordered = sorted(buckets.items())[-cap:]
        series = [
            {
                "bucket": b,
                "counts": counts,
                "total": sum(counts.values()),
            }
            for b, counts in ordered
        ]
        totals = {s: 0 for s in TREND_STATUSES}
        for item in series:
            for s in TREND_STATUSES:
                totals[s] += int(item["counts"].get(s) or 0)
        return {
            "schema_version": 1,
            "time_from": time_from,
            "time_to": time_to,
            "bucket_unit": "hour" if bucket_len == 13 else "day",
            "buckets": series,
            "totals": totals,
            "truncated": len(buckets) > cap,
        }

    def dashboard_insights(
        self,
        *,
        time_range: str | None = None,
        time_from: str | None = None,
        time_to: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, object]:
        window = self.resolve_time_window(
            time_range=time_range,
            time_from=time_from,
            time_to=time_to,
            now=now,
        )
        tf = window["time_from"]
        tt = window["time_to"]
        return {
            "schema_version": 1,
            "window": window,
            "alerts": self.list_recent_alerts(time_from=tf, time_to=tt),
            "trend": self.audit_status_trend(time_from=tf, time_to=tt),
        }

    def fleet_overview(self, *, now: datetime | None = None) -> dict[str, object]:
        agents = self.list_agents(limit=self.config.max_page_size, offset=0, now=now)
        totals = self._presence_and_audit_counts(now=now)
        return {
            "schema_version": 1,
            "totals": totals,
            "agents_sample": agents["items"],
            "agents_sample_truncated": bool(agents.get("scan_truncated")),
            "protocol_window": {
                "min": MIN_SUPPORTED_PROTOCOL_VERSION,
                "max": MAX_SUPPORTED_PROTOCOL_VERSION,
            },
        }

    def _presence_and_audit_counts(self, *, now: datetime | None) -> dict[str, object]:
        current = now or datetime.now(UTC)
        online = stale = offline = 0
        revoked = 0
        audit_counts = {
            "PASS": 0,  # nosec B105 — status counter keys, not passwords
            "WARN": 0,
            "FAIL": 0,
            "ERROR": 0,
            "OTHER": 0,
            "NONE": 0,
        }
        data_gap_agents = 0
        protocol_warnings = 0
        rows = self._execute(
                """
                SELECT a.agent_id, a.status, a.protocol_version,
                       h.last_heartbeat,
                       (
                         SELECT e.status FROM events e
                         WHERE e.agent_id = a.agent_id AND e.event_type = ?
                         ORDER BY e.occurred_at DESC, e.received_at DESC, e.event_id DESC
                         LIMIT 1
                       ) AS audit_status,
                       (
                         SELECT e.payload_json FROM events e
                         WHERE e.agent_id = a.agent_id
                           AND (
                             e.status = ?
                             OR e.payload_json LIKE ?
                             OR e.payload_json LIKE ?
                           )
                         LIMIT 1
                       ) AS gap_payload
                FROM agents a
                LEFT JOIN heartbeats h ON h.agent_id = a.agent_id
                """,
                (
                    EventType.AUDIT_COMPLETED.value,
                    QUEUE_OVERFLOW_STATUS,
                    f"%{DATA_GAP_RESULT_MARKER}%",
                    f"%{QUEUE_OVERFLOW_STATUS}%",
                ),
            )
        for row in rows:
            if str(row[1]) == "revoked":
                revoked += 1
            presence = classify_presence(
                row[3],
                now=current,
                online_after_seconds=self.config.online_after_seconds,
                stale_after_seconds=self.config.stale_after_seconds,
            )
            if presence == ONLINE:
                online += 1
            elif presence == STALE:
                stale += 1
            else:
                offline += 1
            audit = row[4]
            if audit is None:
                audit_counts["NONE"] += 1
            elif str(audit) in audit_counts:
                audit_counts[str(audit)] += 1
            else:
                audit_counts["OTHER"] += 1
            if row[5] is not None:
                data_gap_agents += 1
            proto = int(row[2])
            if (
                proto < MIN_SUPPORTED_PROTOCOL_VERSION
                or proto > MAX_SUPPORTED_PROTOCOL_VERSION
            ):
                protocol_warnings += 1
        return {
            "agents": len(rows),
            "online": online,
            "stale": stale,
            "offline": offline,
            "revoked": revoked,
            "audit": audit_counts,
            "data_gap_or_overflow_agents": data_gap_agents,
            "protocol_compatibility_warnings": protocol_warnings,
        }

    def _fetch_agent_join_rows(
        self,
        *,
        q: str | None,
        software_version: str | None,
        protocol_version: int | None,
        registry_status: str | None,
        audit_status: str | None,
        capability: str | None,
        scan_cap: int,
    ) -> list[tuple[Any, ...]]:
        where = ["1=1"]
        params: list[object] = []
        if q:
            where.append("(a.agent_id LIKE ? OR a.label LIKE ? OR a.hostname LIKE ?)")
            like = f"%{_like_escape(q)[:120]}%"
            params.extend([like, like, like])
        if software_version:
            where.append("a.software_version = ?")
            params.append(software_version[:64])
        if protocol_version is not None:
            where.append("a.protocol_version = ?")
            params.append(int(protocol_version))
        if registry_status:
            where.append("a.status = ?")
            params.append(registry_status[:32])
        if capability:
            where.append("a.capabilities_json LIKE ?")
            params.append(f'%"{_like_escape(capability)[:64]}"%')
        audit_filter_sql = ""
        audit_params: list[object] = []
        if audit_status:
            audit_filter_sql = """
                AND (
                  SELECT e.status FROM events e
                  WHERE e.agent_id = a.agent_id AND e.event_type = ?
                  ORDER BY e.occurred_at DESC, e.received_at DESC, e.event_id DESC
                  LIMIT 1
                ) = ?
            """
            audit_params = [EventType.AUDIT_COMPLETED.value, audit_status[:32]]
        sql = f"""
            SELECT a.agent_id, a.label, a.hostname, a.status, a.first_seen,
                   a.last_seen, a.protocol_version, a.software_version,
                   a.capabilities_json, h.last_heartbeat,
                   (
                     SELECT e.status FROM events e
                     WHERE e.agent_id = a.agent_id AND e.event_type = ?
                     ORDER BY e.occurred_at DESC, e.received_at DESC, e.event_id DESC
                     LIMIT 1
                   ),
                   (
                     SELECT e.occurred_at FROM events e
                     WHERE e.agent_id = a.agent_id AND e.event_type = ?
                     ORDER BY e.occurred_at DESC, e.received_at DESC, e.event_id DESC
                     LIMIT 1
                   ),
                   (
                     SELECT MAX(e.received_at) FROM events e WHERE e.agent_id = a.agent_id
                   ),
                   (
                     SELECT e.payload_json FROM events e
                     WHERE e.agent_id = a.agent_id
                       AND (
                         e.status = ?
                         OR e.payload_json LIKE ?
                         OR e.payload_json LIKE ?
                       )
                     ORDER BY e.received_at DESC LIMIT 1
                   )
            FROM agents a
            LEFT JOIN heartbeats h ON h.agent_id = a.agent_id
            WHERE {" AND ".join(where)}
            {audit_filter_sql}
            ORDER BY a.agent_id
            LIMIT ?
        """  # noqa: S608  # nosec B608 — where fragments are fixed literals; values bound
        bind: list[object] = [
            EventType.AUDIT_COMPLETED.value,
            EventType.AUDIT_COMPLETED.value,
            QUEUE_OVERFLOW_STATUS,
            f"%{DATA_GAP_RESULT_MARKER}%",
            f"%{QUEUE_OVERFLOW_STATUS}%",
            *params,
            *audit_params,
            scan_cap,
        ]
        return self._execute(sql, bind)

    def list_agents(
        self,
        *,
        limit: int | None = None,
        offset: int | None = None,
        q: str | None = None,
        presence: str | None = None,
        audit_status: str | None = None,
        capability: str | None = None,
        software_version: str | None = None,
        protocol_version: int | None = None,
        registry_status: str | None = None,
        has_data_gap: bool | None = None,
        now: datetime | None = None,
    ) -> dict[str, object]:
        page = self._clamp_limit(limit)
        off = self._clamp_offset(offset)
        current = now or datetime.now(UTC)
        scan_cap = 5000
        rows = self._fetch_agent_join_rows(
            q=q,
            software_version=software_version,
            protocol_version=protocol_version,
            registry_status=registry_status,
            audit_status=audit_status,
            capability=capability,
            scan_cap=scan_cap,
        )
        items: list[dict[str, object]] = []
        for row in rows:
            caps = _capability_summary(row[8])
            presence_v = classify_presence(
                row[9],
                now=current,
                online_after_seconds=self.config.online_after_seconds,
                stale_after_seconds=self.config.stale_after_seconds,
            )
            gap = row[13] is not None
            proto = int(row[6])
            warning = (
                proto < MIN_SUPPORTED_PROTOCOL_VERSION
                or proto > MAX_SUPPORTED_PROTOCOL_VERSION
            )
            if presence is not None and presence_v != presence:
                continue
            if has_data_gap is not None and gap != has_data_gap:
                continue
            items.append(
                {
                    "agent_id": row[0],
                    "label": row[1],
                    "hostname": row[2],
                    "registry_status": row[3],
                    "presence": presence_v,
                    "last_heartbeat": row[9],
                    "last_received_at": row[12],
                    "current_audit_status": row[10],
                    "current_audit_occurred_at": row[11],
                    "software_version": row[7],
                    "protocol_version": proto,
                    "deployment_form": caps.get("deployment_form"),
                    "capability_summary": {
                        "available_count": caps.get("available_count"),
                        "families": caps.get("families"),
                    },
                    "has_data_gap": gap,
                    "protocol_warning": warning,
                }
            )
        truncated_scan = len(rows) >= scan_cap
        page_items = items[off : off + page]
        return {
            "schema_version": 1,
            "items": page_items,
            "limit": page,
            "offset": off,
            "returned": len(page_items),
            "matched": len(items),
            "has_more": off + page < len(items),
            "scan_truncated": truncated_scan,
            "truncated": truncated_scan or (off + page < len(items)),
        }

    def agent_detail(
        self, agent_id: str, *, now: datetime | None = None
    ) -> dict[str, object] | None:
        agent = self.store.get_agent(agent_id)
        if agent is None:
            return None
        current = now or datetime.now(UTC)
        hb = self.store.last_heartbeat(agent_id)
        current_event = self.store.current_event_by_occurred_at(agent_id)
        latest_event = self.store.latest_event(agent_id)
        caps_doc = self.store.get_agent_capabilities(agent_id)
        caps = _capability_summary(agent.capabilities_json)
        recent = self.list_events(agent_id=agent_id, limit=25, offset=0)
        failures = self.list_events(
            agent_id=agent_id, limit=25, offset=0, failures_only=True
        )
        presence = classify_presence(
            hb,
            now=current,
            online_after_seconds=self.config.online_after_seconds,
            stale_after_seconds=self.config.stale_after_seconds,
        )
        latest_by_type = self._latest_by_check_type(agent_id)
        gap = any(item.get("has_data_gap") for item in recent["items"])
        return {
            "schema_version": 1,
            "identity": {
                "agent_id": agent.agent_id,
                "label": agent.label,
                "hostname": agent.hostname,
                "registry_status": agent.status,
                "first_seen": agent.first_seen,
                "last_seen": agent.last_seen,
            },
            "presence": presence,
            "last_heartbeat": hb,
            "last_received_at": (
                latest_event.get("received_at") if latest_event else None
            ),
            "current_audit": _public_event(current_event),
            "latest_received_event": _public_event(latest_event),
            "latest_by_check_type": latest_by_type,
            "software_version": agent.software_version,
            "protocol_version": agent.protocol_version,
            "protocol_warning": (
                agent.protocol_version < MIN_SUPPORTED_PROTOCOL_VERSION
                or agent.protocol_version > MAX_SUPPORTED_PROTOCOL_VERSION
            ),
            "capabilities": caps_doc or caps,
            "capability_summary": caps,
            "has_data_gap": gap,
            "recent_history": recent["items"],
            "recent_failures": failures["items"],
        }

    def _latest_by_check_type(self, agent_id: str) -> dict[str, object]:
        wanted = (
            EventType.AUDIT_COMPLETED.value,
            EventType.COVERAGE_RESULT.value,
            EventType.INTEGRITY_RESULT.value,
            EventType.RESTORE_RESULT.value,
        )
        out: dict[str, object] = {}
        for event_type in wanted:
            row = self._fetchone(
                """
                SELECT event_id, event_type, status, occurred_at, received_at,
                       payload_json
                FROM events
                WHERE agent_id = ? AND event_type = ?
                ORDER BY occurred_at DESC, received_at DESC, event_id DESC
                LIMIT 1
                """,
                (agent_id, event_type),
            )
            if row is None:
                out[event_type] = None
            else:
                out[event_type] = {
                    "event_id": row[0],
                    "event_type": row[1],
                    "status": row[2],
                    "occurred_at": row[3],
                    "received_at": row[4],
                    "summary": _safe_payload_summary(str(row[5])),
                    "has_data_gap": _event_has_data_gap(str(row[2]), str(row[5])),
                }
        return out

    def list_events(
        self,
        *,
        agent_id: str | None = None,
        event_type: str | None = None,
        status: str | None = None,
        failures_only: bool = False,
        time_from: str | None = None,
        time_to: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict[str, object]:
        page = self._clamp_limit(limit)
        off = self._clamp_offset(offset)
        where = ["1=1"]
        params: list[object] = []
        if agent_id:
            where.append("agent_id = ?")
            params.append(agent_id[:128])
        if event_type:
            where.append("event_type = ?")
            params.append(event_type[:64])
        if status:
            where.append("status = ?")
            params.append(status[:32])
        if failures_only:
            placeholders = ",".join("?" for _ in FAILURE_STATUSES)
            where.append(f"status IN ({placeholders})")
            params.extend(sorted(FAILURE_STATUSES))
        if time_from:
            where.append("occurred_at >= ?")
            params.append(time_from[:64])
        if time_to:
            where.append("occurred_at <= ?")
            params.append(time_to[:64])
        where_sql = " AND ".join(where)
        total_row = self._fetchone(
                # where_sql is composed only from fixed fragments + bound params
                f"SELECT COUNT(*) FROM events WHERE {where_sql}",  # noqa: S608  # nosec B608
                params,
            )
        matched = int(total_row[0]) if total_row else 0
        rows = self._execute(
                f"""
                SELECT event_id, event_type, agent_id, status, occurred_at, received_at,
                       payload_json
                FROM events
                WHERE {where_sql}
                ORDER BY occurred_at DESC, received_at DESC, event_id DESC
                LIMIT ? OFFSET ?
                """,  # noqa: S608  # nosec B608
                [*params, page, off],
            )
        items = []
        for row in rows:
            payload_json = str(row[6])
            items.append(
                {
                    "event_id": row[0],
                    "event_type": row[1],
                    "agent_id": row[2],
                    "status": row[3],
                    "occurred_at": row[4],
                    "received_at": row[5],
                    "summary": _safe_payload_summary(payload_json),
                    "has_data_gap": _event_has_data_gap(str(row[3]), payload_json),
                }
            )
        return {
            "schema_version": 1,
            "items": items,
            "limit": page,
            "offset": off,
            "returned": len(items),
            "matched": matched,
            "has_more": off + page < matched,
            "truncated": False,
        }

    def siem_status(self) -> dict[str, object] | None:
        """Optional SIEM integration telemetry (v0.7 stub for future UI panel)."""
        try:
            from backuplint.siem.runtime import read_telemetry_tail
        except ImportError:
            return None
        path = self.store.path.parent / "siem" / "telemetry.jsonl"
        return read_telemetry_tail(path)

    def list_policies(self, *, limit: int | None = None) -> dict[str, object]:
        page = self._clamp_limit(limit)
        items = self.store.policy_list()[:page]
        return {
            "schema_version": 1,
            "items": items,
            "limit": page,
            "returned": len(items),
            "write_enabled": False,
        }

    def list_policy_drift(self, *, limit: int | None = None) -> dict[str, object]:
        page = self._clamp_limit(limit)
        items = self.store.policy_list_drift(limit=page)
        return {
            "schema_version": 1,
            "items": items,
            "limit": page,
            "returned": len(items),
            "write_enabled": False,
        }

    def list_policy_rollouts(self, *, limit: int | None = None) -> dict[str, object]:
        page = self._clamp_limit(limit)
        items = self.store.policy_rollout_list(limit=page)
        return {
            "schema_version": 1,
            "items": items,
            "limit": page,
            "returned": len(items),
            "write_enabled": False,
        }

    def list_policy_audit(self, *, limit: int | None = None) -> dict[str, object]:
        page = self._clamp_limit(limit)
        items = self.store.policy_list_audit(limit=page)
        return {
            "schema_version": 1,
            "items": items,
            "limit": page,
            "returned": len(items),
            "write_enabled": False,
        }

    def agent_policy_effective(self, agent_id: str) -> dict[str, object] | None:
        return self.store.policy_get_effective(agent_id)
