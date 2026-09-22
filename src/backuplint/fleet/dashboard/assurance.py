"""Project stored audit findings into agent/service/mount assurance views.

This is a dashboard read model over payloads agents already submit. It does not
invent Docker health, repository-level integrity as per-mount PASS, or restore
success from ``restic check``. Missing fields degrade to UNKNOWN / NOT RUN /
NOT APPLICABLE instead of PASS.
"""

from __future__ import annotations

from typing import Any

ASSURANCE_SCHEMA_VERSION = 1
_MAX_FINDINGS = 200
_MAX_STR = 512
_MAX_DETAIL = 240

_COVERAGE_TO_STATE = {
    "protected": "PASS",
    "not protected": "FAIL",
    "stale": "WARN",
    "unsupported": "WARN",
    "skipped": "NOT_APPLICABLE",
}

_RANK = {  # nosec B105 — status ranks, not passwords
    "ERROR": 5,
    "FAIL": 4,
    "WARN": 3,
    "UNKNOWN": 2,
    "PASS": 1,
    "NOT_RUN": 0,
    "NOT_APPLICABLE": 0,
}


def _clip(value: object, *, limit: int = _MAX_STR) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) > limit:
        return text[:limit]
    return text


def _audit_payload(event: dict[str, object] | None) -> dict[str, Any]:
    if not event:
        return {}
    payload = event.get("payload")
    if isinstance(payload, dict):
        nested = payload.get("result")
        if isinstance(nested, dict) and (
            "findings" in nested or "summary" in nested or "operational_error" in nested
        ):
            return nested
        return payload
    return {}


def _coverage_state(status: str | None) -> str:
    if not status:
        return "UNKNOWN"
    return _COVERAGE_TO_STATE.get(status.strip().lower(), "UNKNOWN")


def _check_state(block: object, *, failed_values: frozenset[str]) -> str:
    if not isinstance(block, dict):
        return "NOT_RUN"
    status = str(block.get("status") or "").strip().lower()
    if not status:
        return "NOT_RUN"
    if status in {"not_requested", "not requested"}:
        return "NOT_RUN"
    if status in failed_values:
        if status in {"error", "unavailable", "timeout", "auth"}:
            return "ERROR"
        return "FAIL"
    if status in {"stale", "skipped"}:
        return "WARN"
    if status in {"passed", "pass", "ok"}:
        return "PASS"
    return "UNKNOWN"


def _worse(*states: str) -> str:
    worst = "NOT_APPLICABLE"
    worst_rank = -1
    for state in states:
        rank = _RANK.get(state, 2)
        if rank > worst_rank:
            worst = state
            worst_rank = rank
    return worst


def _freshness_from_finding(finding: dict[str, Any]) -> dict[str, object]:
    status = str(finding.get("status") or "").strip().lower()
    detail = _clip(finding.get("detail"), limit=_MAX_DETAIL) or ""
    snapshot = _clip(finding.get("snapshot_time"))
    if status == "stale":
        return {
            "state": "WARN",
            "detail": detail,
            "last_snapshot": snapshot,
            "age_attribution": "mount",
        }
    if snapshot and status == "protected":
        return {
            "state": "PASS",
            "detail": detail,
            "last_snapshot": snapshot,
            "age_attribution": "mount",
        }
    if status == "protected" and "backed up" in detail.lower():
        return {
            "state": "PASS",
            "detail": detail,
            "last_snapshot": snapshot,
            "age_attribution": "mount",
        }
    if status == "not protected":
        return {
            "state": "NOT_APPLICABLE",
            "detail": "no matching backup evidence until coverage exists",
            "last_snapshot": None,
            "age_attribution": "none",
        }
    if status in {"skipped"}:
        return {
            "state": "NOT_APPLICABLE",
            "detail": detail,
            "last_snapshot": snapshot,
            "age_attribution": "none",
        }
    return {
        "state": "UNKNOWN",
        "detail": detail or "freshness not separately recorded for this mount",
        "last_snapshot": snapshot,
        "age_attribution": "none",
    }


def _mount_id(service: str, finding: dict[str, Any], index: int) -> str:
    path = (
        _clip(finding.get("path"))
        or _clip(finding.get("target"))
        or _clip(finding.get("type"))
        or f"mount-{index}"
    )
    return f"{service}:{path}"[:128]


def _persistent(finding: dict[str, Any]) -> bool:
    status = str(finding.get("status") or "").strip().lower()
    storage = str(finding.get("storage_class") or "").strip().lower()
    if status == "skipped" or storage in {"cache", "temporary"}:
        return False
    return True


def project_assurance(
    payload: dict[str, Any] | None,
    *,
    agent_id: str,
    occurred_at: str | None = None,
    audit_status: str | None = None,
) -> dict[str, object]:
    """Build a bounded Agent → project → service → mount view."""
    data = payload if isinstance(payload, dict) else {}
    op_error = data.get("operational_error")
    findings_raw = data.get("findings")
    findings: list[dict[str, Any]] = []
    if isinstance(findings_raw, list):
        for item in findings_raw[:_MAX_FINDINGS]:
            if isinstance(item, dict):
                findings.append(item)

    integrity = data.get("integrity")
    restore = data.get("restore_verification")
    integrity_state = _check_state(
        integrity, failed_values=frozenset({"failed", "fail", "error"})
    )
    restore_state = _check_state(
        restore, failed_values=frozenset({"failed", "fail", "error"})
    )
    if isinstance(integrity, dict) and str(integrity.get("status") or "").lower() in {
        "error",
        "unavailable",
    }:
        integrity_state = "ERROR"
    if isinstance(restore, dict) and str(restore.get("status") or "").lower() in {
        "error",
        "unavailable",
    }:
        restore_state = "ERROR"

    repo_error = isinstance(op_error, dict)
    repo_scope = {
        "state": "ERROR" if repo_error else ("NOT_RUN" if not findings else "PASS"),
        "kind": _clip(op_error.get("kind") if isinstance(op_error, dict) else None),
        "message": _clip(
            op_error.get("message") if isinstance(op_error, dict) else None,
            limit=_MAX_DETAIL,
        ),
        "attribution": "repository",
        "note": (
            "Repository/operational ERROR is not a coverage FAIL."
            if repo_error
            else "Integrity and restore are repository-scoped unless a finding names a path."
        ),
    }
    if repo_error:
        integrity_display = "ERROR"
        restore_display = "ERROR"
    else:
        integrity_display = integrity_state
        restore_display = restore_state

    services: dict[str, dict[str, Any]] = {}
    for index, finding in enumerate(findings):
        service = _clip(finding.get("service")) or "unknown-service"
        project_name = _clip(finding.get("project")) or "compose"
        key = f"{project_name}::{service}"
        bucket = services.setdefault(
            key,
            {
                "service": service,
                "project": project_name,
                "image": _clip(finding.get("image")),
                "persistent": False,
                "mounts": [],
                "coverage": "NOT_APPLICABLE",
                "freshness": "NOT_APPLICABLE",
                "integrity": integrity_display,
                "restore": restore_display,
                "overall": "NOT_APPLICABLE",
            },
        )
        coverage = _coverage_state(_clip(finding.get("status")))
        freshness = _freshness_from_finding(finding)
        persistent = _persistent(finding)
        mount = {
            "id": _mount_id(service, finding, index),
            "persistent": persistent,
            "type": _clip(finding.get("type")) or "unknown",
            "storage_class": _clip(finding.get("storage_class")),
            "host_path": _clip(finding.get("path")),
            "target": _clip(finding.get("target")),
            "covered_by": _clip(finding.get("covered_by")),
            "expected_backed_up": persistent,
            "coverage": coverage,
            "freshness": freshness["state"],
            "freshness_detail": freshness["detail"],
            "last_snapshot": freshness.get("last_snapshot"),
            "integrity": integrity_display,
            "integrity_scope": "repository",
            "restore": restore_display,
            "restore_scope": "repository",
            "detail": _clip(finding.get("detail"), limit=_MAX_DETAIL),
            "critical": bool(finding.get("critical")),
        }
        if repo_error:
            mount["coverage"] = "UNKNOWN"
            mount["freshness"] = "UNKNOWN"
            mount["detail"] = repo_scope["message"]
        bucket["mounts"].append(mount)
        if persistent:
            bucket["persistent"] = True
            bucket["coverage"] = _worse(str(bucket["coverage"]), coverage)
            bucket["freshness"] = _worse(
                str(bucket["freshness"]), str(freshness["state"])
            )

    projects: dict[str, dict[str, Any]] = {}
    persistent_services = 0
    healthy = warning = failing = erroring = 0
    for service in sorted(services.values(), key=lambda item: str(item["service"])):
        overall = _worse(
            str(service["coverage"]),
            str(service["freshness"]),
            str(service["integrity"]),
            str(service["restore"]),
        )
        if repo_error:
            overall = "ERROR"
        service["overall"] = overall
        if service["persistent"]:
            persistent_services += 1
            if overall == "ERROR":
                erroring += 1
            elif overall == "FAIL":
                failing += 1
            elif overall in {"WARN", "UNKNOWN"}:
                warning += 1
            elif overall == "PASS":
                healthy += 1
        project_name = str(service["project"])
        project = projects.setdefault(
            project_name,
            {"name": project_name, "services": []},
        )
        project["services"].append(service)

    alerts: list[dict[str, object]] = []
    if repo_error:
        alerts.append(
            {
                "agent_id": agent_id,
                "project": None,
                "service": None,
                "mount": None,
                "check": "repository",
                "state": "ERROR",
                "reason": repo_scope["message"],
                "occurred_at": occurred_at,
            }
        )
    for service in services.values():
        if repo_error:
            break
        for mount in service["mounts"]:
            state = str(mount["coverage"])
            if state in {"FAIL", "WARN", "ERROR", "UNKNOWN"} and mount["persistent"]:
                alerts.append(
                    {
                        "agent_id": agent_id,
                        "project": service["project"],
                        "service": service["service"],
                        "mount": mount.get("host_path") or mount.get("target"),
                        "check": "coverage",
                        "state": state,
                        "reason": mount.get("detail"),
                        "occurred_at": occurred_at,
                    }
                )
            elif str(mount["freshness"]) == "WARN" and mount["persistent"]:
                alerts.append(
                    {
                        "agent_id": agent_id,
                        "project": service["project"],
                        "service": service["service"],
                        "mount": mount.get("host_path") or mount.get("target"),
                        "check": "freshness",
                        "state": "WARN",
                        "reason": mount.get("freshness_detail"),
                        "occurred_at": occurred_at,
                    }
                )
    if integrity_display in {"FAIL", "ERROR", "WARN"}:
        alerts.append(
            {
                "agent_id": agent_id,
                "project": None,
                "service": None,
                "mount": None,
                "check": "integrity",
                "state": integrity_display,
                "reason": _clip(
                    integrity.get("message") if isinstance(integrity, dict) else None,
                    limit=_MAX_DETAIL,
                )
                or "repository integrity",
                "occurred_at": occurred_at,
            }
        )
    if restore_display in {"FAIL", "ERROR", "WARN"}:
        alerts.append(
            {
                "agent_id": agent_id,
                "project": None,
                "service": None,
                "mount": None,
                "check": "restore_verification",
                "state": restore_display,
                "reason": _clip(
                    restore.get("message") if isinstance(restore, dict) else None,
                    limit=_MAX_DETAIL,
                )
                or "isolated restore verification",
                "occurred_at": occurred_at,
            }
        )

    empty_reason = None
    if repo_error:
        empty_reason = None
    elif not findings:
        empty_reason = "no Compose discovery findings in this audit payload"

    return {
        "schema_version": ASSURANCE_SCHEMA_VERSION,
        "agent_id": agent_id,
        "occurred_at": occurred_at,
        "audit_status": audit_status,
        "persistent_service_count": persistent_services,
        "healthy_count": healthy,
        "warning_count": warning,
        "failing_count": failing,
        "error_count": erroring,
        "integrity": {
            "state": integrity_display,
            "scope": "repository",
            "mode": _clip(integrity.get("mode") if isinstance(integrity, dict) else None),
            "message": _clip(
                integrity.get("message") if isinstance(integrity, dict) else None,
                limit=_MAX_DETAIL,
            ),
            "checked_at": _clip(
                integrity.get("checked_at") if isinstance(integrity, dict) else None
            ),
        },
        "restore_verification": {
            "state": restore_display,
            "scope": "repository",
            "mode": _clip(restore.get("mode") if isinstance(restore, dict) else None),
            "message": _clip(
                restore.get("message") if isinstance(restore, dict) else None,
                limit=_MAX_DETAIL,
            ),
            "snapshot_id": _clip(
                restore.get("snapshot_id") if isinstance(restore, dict) else None
            ),
            "checked_at": _clip(
                restore.get("checked_at") if isinstance(restore, dict) else None
            ),
        },
        "repository": repo_scope,
        "projects": [projects[name] for name in sorted(projects)],
        "alerts": alerts[:50],
        "empty_reason": empty_reason,
        "truncated_findings": isinstance(findings_raw, list)
        and len(findings_raw) > _MAX_FINDINGS,
    }


def service_from_assurance(
    assurance: dict[str, object],
    service_name: str,
    *,
    project: str | None = None,
) -> dict[str, object] | None:
    wanted = service_name.strip()
    wanted_project = project.strip() if isinstance(project, str) and project.strip() else None
    matches: list[dict[str, object]] = []
    for item in assurance.get("projects") or []:
        if not isinstance(item, dict):
            continue
        for service in item.get("services") or []:
            if not isinstance(service, dict):
                continue
            if str(service.get("service")) != wanted:
                continue
            if wanted_project and str(service.get("project") or "") != wanted_project:
                continue
            matches.append(service)
    if not matches:
        return None
    return matches[0]


def empty_assurance(agent_id: str) -> dict[str, object]:
    return project_assurance(
        None, agent_id=agent_id, occurred_at=None, audit_status=None
    )


def assurance_from_event(
    event: dict[str, object] | None, *, agent_id: str
) -> dict[str, object]:
    payload = _audit_payload(event)
    return project_assurance(
        payload,
        agent_id=agent_id,
        occurred_at=str(event.get("occurred_at") or "") if event else None,
        audit_status=str(event.get("status") or "") if event else None,
    )
