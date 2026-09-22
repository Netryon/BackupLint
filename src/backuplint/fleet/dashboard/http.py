"""HTTP dispatch for dashboard API and HTML UI."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, unquote

from backuplint.fleet.dashboard import ui
from backuplint.fleet.dashboard.auth import (
    CSRF_COOKIE,
    CSRF_HEADER,
    SESSION_COOKIE,
    DashboardAuth,
)
from backuplint.fleet.dashboard.query import DashboardQueryService

_AGENT_PATH = re.compile(r"^/v1/dashboard/agents/([^/]+)$")
_AGENT_SERVICE_NESTED = re.compile(
    r"^/v1/dashboard/agents/([^/]+)/services/([^/]+)/([^/]+)$"
)
_AGENT_SERVICE_PATH = re.compile(r"^/v1/dashboard/agents/([^/]+)/services/([^/]+)$")
_UI_AGENT_PATH = re.compile(r"^/dashboard/agents/([^/]+)$")
_UI_AGENT_SERVICE_NESTED = re.compile(
    r"^/dashboard/agents/([^/]+)/services/([^/]+)/([^/]+)$"
)
_UI_AGENT_SERVICE_PATH = re.compile(r"^/dashboard/agents/([^/]+)/services/([^/]+)$")

MAX_QUERY_LEN = 256
MAX_BODY = 4096


@dataclass(frozen=True)
class DashboardResponse:
    status: int
    body: bytes
    content_type: str
    headers: tuple[tuple[str, str], ...] = ()


def _json(
    status: int,
    payload: dict[str, object],
    *,
    headers: list[tuple[str, str]] | None = None,
) -> DashboardResponse:
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return DashboardResponse(
        status=status,
        body=body,
        content_type="application/json; charset=utf-8",
        headers=tuple(headers or ()),
    )


def _html(
    status: int, text: str, *, headers: list[tuple[str, str]] | None = None
) -> DashboardResponse:
    return DashboardResponse(
        status=status,
        body=text.encode("utf-8"),
        content_type="text/html; charset=utf-8",
        headers=tuple(headers or ()),
    )


def _redirect(
    location: str, *, headers: list[tuple[str, str]] | None = None
) -> DashboardResponse:
    hdrs = [("Location", location)]
    if headers:
        hdrs.extend(headers)
    return DashboardResponse(
        status=302,
        body=b"",
        content_type="text/plain; charset=utf-8",
        headers=tuple(hdrs),
    )


def _parse_cookies(header: str | None) -> dict[str, str]:
    if not header:
        return {}
    jar = SimpleCookie()
    try:
        jar.load(header)
    except Exception:  # noqa: BLE001
        return {}
    return {k: morsel.value for k, morsel in jar.items()}


def _q_get(qs: dict[str, list[str]], key: str) -> str | None:
    values = qs.get(key)
    if not values:
        return None
    value = values[0]
    if len(value) > MAX_QUERY_LEN:
        return value[:MAX_QUERY_LEN]
    return value


def _q_int(qs: dict[str, list[str]], key: str) -> int | None:
    raw = _q_get(qs, key)
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _q_bool(qs: dict[str, list[str]], key: str) -> bool | None:
    raw = _q_get(qs, key)
    if raw is None:
        return None
    lowered = raw.strip().lower()
    if lowered in {"1", "true", "yes"}:
        return True
    if lowered in {"0", "false", "no"}:
        return False
    return None


class DashboardHttp:
    def __init__(self, auth: DashboardAuth, query: DashboardQueryService) -> None:
        self.auth = auth
        self.query = query

    def handle(
        self,
        *,
        method: str,
        path: str,
        query_string: str,
        headers: dict[str, str],
        body: bytes,
        client_addr: str,
    ) -> DashboardResponse | None:
        if not (
            path.startswith("/v1/dashboard") or path.startswith("/dashboard")
        ):
            return None

        if not self.auth.config.enabled:
            if path.startswith("/v1/dashboard"):
                return _json(404, {"error": "dashboard disabled"})
            return _html(
                404,
                ui.page_error("Dashboard disabled", "Enable with --dashboard."),
            )

        cookies = _parse_cookies(headers.get("Cookie") or headers.get("cookie"))
        session = self.auth.get_session(cookies.get(SESSION_COOKIE))
        qs = parse_qs(query_string, keep_blank_values=False)

        if method == "GET" and path == "/dashboard/login":
            return self._login_page(session)
        if method == "POST" and path == "/dashboard/login":
            return self._login_post(body, client_addr)
        if method == "POST" and path == "/dashboard/logout":
            return self._logout(session, cookies, headers, body)

        if path.startswith("/v1/dashboard"):
            return self._api(method, path, qs, session)
        if path.startswith("/dashboard"):
            return self._ui(method, path, qs, session)
        return None

    def _login_page(self, session: object) -> DashboardResponse:
        if session is not None:
            return _redirect("/dashboard/")
        if not self.auth.password_configured():
            return _html(
                503,
                ui.page_error(
                    "Dashboard password not set",
                    "Run: backuplint controller dashboard-password --data-dir ...",
                ),
            )
        return _html(200, ui.page_login())

    def _login_post(self, body: bytes, client_addr: str) -> DashboardResponse:
        if len(body) > MAX_BODY:
            return _html(413, ui.page_error("Request too large", "Login body rejected."))
        if not self.auth.allow_login_attempt(client_addr):
            return _html(429, ui.page_error("Too many attempts", "Try again later."))
        form = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
        password = (form.get("password") or [""])[0]
        if not self.auth.verify_password(password):
            self.auth.record_login_failure(client_addr)
            return _html(401, ui.page_login(error="Invalid password"))
        self.auth.clear_login_failures(client_addr)
        token, csrf, max_age = self.auth.create_session()
        headers = [
            (
                "Set-Cookie",
                self.auth.cookie_header(
                    SESSION_COOKIE, token, max_age=max_age, http_only=True
                ),
            ),
            (
                "Set-Cookie",
                self.auth.cookie_header(
                    CSRF_COOKIE, csrf, max_age=max_age, http_only=False
                ),
            ),
        ]
        return _redirect("/dashboard/", headers=headers)

    def _logout(
        self,
        session: object,
        cookies: dict[str, str],
        headers: dict[str, str],
        body: bytes,
    ) -> DashboardResponse:
        if session is None:
            return _redirect("/dashboard/login")
        form = {}
        if body and len(body) <= MAX_BODY:
            form = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
        # Require header or form field — never accept the CSRF cookie alone
        # (browser auto-attaches cookies, which defeats double-submit CSRF).
        provided = (
            headers.get(CSRF_HEADER)
            or headers.get(CSRF_HEADER.lower())
            or (form.get("csrf") or form.get("csrf_token") or [None])[0]
        )
        sess = self.auth.get_session(cookies.get(SESSION_COOKIE))
        csrf_ok = isinstance(provided, str) and sess is not None
        if not csrf_ok or not self.auth.check_csrf(sess, provided):  # type: ignore[arg-type]
            return _html(403, ui.page_error("CSRF rejected", "Reload and try again."))
        self.auth.destroy_session(cookies.get(SESSION_COOKIE))
        hdrs = [
            (
                "Set-Cookie",
                self.auth.clear_cookie_header(SESSION_COOKIE, http_only=True),
            ),
            (
                "Set-Cookie",
                self.auth.clear_cookie_header(CSRF_COOKIE, http_only=False),
            ),
        ]
        return _redirect("/dashboard/login", headers=hdrs)

    def _api(
        self,
        method: str,
        path: str,
        qs: dict[str, list[str]],
        session: object,
    ) -> DashboardResponse:
        if session is None:
            return _json(401, {"error": "authentication required"})
        if method != "GET":
            return _json(405, {"error": "method not allowed"})
        if path == "/v1/dashboard/overview":
            return _json(200, self.query.fleet_overview())
        if path == "/v1/dashboard/insights":
            return _json(
                200,
                self.query.dashboard_insights(
                    time_range=_q_get(qs, "time_range"),
                    time_from=_q_get(qs, "time_from"),
                    time_to=_q_get(qs, "time_to"),
                ),
            )
        if path == "/v1/dashboard/agents":
            return _json(
                200,
                self.query.list_agents(
                    limit=_q_int(qs, "limit"),
                    offset=_q_int(qs, "offset"),
                    q=_q_get(qs, "q"),
                    presence=_q_get(qs, "presence"),
                    audit_status=_q_get(qs, "audit_status"),
                    capability=_q_get(qs, "capability"),
                    software_version=_q_get(qs, "software_version"),
                    protocol_version=_q_int(qs, "protocol_version"),
                    registry_status=_q_get(qs, "registry_status"),
                    has_data_gap=_q_bool(qs, "has_data_gap"),
                ),
            )
        nested = _AGENT_SERVICE_NESTED.match(path)
        if nested:
            detail = self.query.agent_service(
                unquote(nested.group(1)),
                unquote(nested.group(3)),
                project=unquote(nested.group(2)),
            )
            if detail is None:
                return _json(404, {"error": "service not found"})
            return _json(200, detail)
        svc = _AGENT_SERVICE_PATH.match(path)
        if svc:
            detail = self.query.agent_service(
                unquote(svc.group(1)),
                unquote(svc.group(2)),
                project=_q_get(qs, "project"),
            )
            if detail is None:
                return _json(404, {"error": "service not found"})
            return _json(200, detail)
        m = _AGENT_PATH.match(path)
        if m:
            detail = self.query.agent_detail(m.group(1))
            if detail is None:
                return _json(404, {"error": "agent not found"})
            return _json(200, detail)
        if path == "/v1/dashboard/events":
            return _json(
                200,
                self.query.list_events(
                    agent_id=_q_get(qs, "agent_id"),
                    event_type=_q_get(qs, "event_type"),
                    status=_q_get(qs, "status"),
                    failures_only=bool(_q_bool(qs, "failures_only")),
                    time_from=_q_get(qs, "time_from"),
                    time_to=_q_get(qs, "time_to"),
                    limit=_q_int(qs, "limit"),
                    offset=_q_int(qs, "offset"),
                ),
            )
        if path == "/v1/dashboard/policies":
            return _json(200, self.query.list_policies(limit=_q_int(qs, "limit")))
        if path == "/v1/dashboard/policy/drift":
            return _json(200, self.query.list_policy_drift(limit=_q_int(qs, "limit")))
        if path == "/v1/dashboard/policy/rollouts":
            return _json(
                200, self.query.list_policy_rollouts(limit=_q_int(qs, "limit"))
            )
        if path == "/v1/dashboard/policy/audit":
            return _json(200, self.query.list_policy_audit(limit=_q_int(qs, "limit")))
        if path.startswith("/v1/dashboard/policy/effective/"):
            agent_id = path.rsplit("/", 1)[-1]
            effective = self.query.agent_policy_effective(agent_id)
            if effective is None:
                return _json(404, {"error": "no effective policy"})
            return _json(200, {"agent_id": agent_id, **effective, "write_enabled": False})
        return _json(404, {"error": "not found"})

    def _ui(
        self,
        method: str,
        path: str,
        qs: dict[str, list[str]],
        session: object,
    ) -> DashboardResponse:
        if method != "GET":
            return _html(405, ui.page_error("Method not allowed", ""))
        if session is None:
            return _redirect("/dashboard/login")
        csrf = getattr(session, "csrf", "")
        if path in ("/dashboard", "/dashboard/"):
            overview = self.query.fleet_overview()
            window = self.query.resolve_time_window(
                time_range=_q_get(qs, "time_range"),
                time_from=_q_get(qs, "time_from"),
                time_to=_q_get(qs, "time_to"),
            )
            tf = window["time_from"]
            tt = window["time_to"]
            agents = self.query.list_agents(
                limit=_q_int(qs, "limit") or 10,
                offset=_q_int(qs, "offset") or 0,
                q=_q_get(qs, "q"),
                presence=_q_get(qs, "presence"),
                audit_status=_q_get(qs, "audit_status"),
                capability=_q_get(qs, "capability"),
                software_version=_q_get(qs, "software_version"),
                protocol_version=_q_int(qs, "protocol_version"),
                has_data_gap=_q_bool(qs, "has_data_gap"),
            )
            alerts = self.query.list_recent_alerts(time_from=tf, time_to=tt)
            trend = self.query.audit_status_trend(time_from=tf, time_to=tt)
            return _html(
                200,
                ui.page_fleet(
                    overview,
                    agents,
                    alerts=alerts,
                    trend=trend,
                    window=window,
                    csrf=csrf,
                    qs=qs,
                ),
            )
        nested = _UI_AGENT_SERVICE_NESTED.match(path)
        if nested:
            detail = self.query.agent_service(
                unquote(nested.group(1)),
                unquote(nested.group(3)),
                project=unquote(nested.group(2)),
            )
            if detail is None:
                return _html(
                    404,
                    ui.page_error("Service not found", unquote(nested.group(3))),
                )
            return _html(
                200,
                ui.page_service(
                    detail,
                    csrf=csrf,
                    highlight_mount=_q_get(qs, "mount"),
                ),
            )
        svc = _UI_AGENT_SERVICE_PATH.match(path)
        if svc:
            detail = self.query.agent_service(
                unquote(svc.group(1)),
                unquote(svc.group(2)),
                project=_q_get(qs, "project"),
            )
            if detail is None:
                return _html(
                    404,
                    ui.page_error("Service not found", unquote(svc.group(2))),
                )
            return _html(
                200,
                ui.page_service(
                    detail,
                    csrf=csrf,
                    highlight_mount=_q_get(qs, "mount"),
                ),
            )
        m = _UI_AGENT_PATH.match(path)
        if m:
            detail = self.query.agent_detail(m.group(1))
            if detail is None:
                return _html(404, ui.page_error("Agent not found", m.group(1)))
            return _html(200, ui.page_agent(detail, csrf=csrf))
        if path == "/dashboard/history":
            time_from = _q_get(qs, "time_from")
            time_to = _q_get(qs, "time_to")
            time_range = _q_get(qs, "time_range")
            if time_from and time_to and not time_range:
                time_range = "custom"
            if time_range:
                window = self.query.resolve_time_window(
                    time_range=time_range,
                    time_from=time_from,
                    time_to=time_to,
                )
                time_from = window["time_from"]
                time_to = window["time_to"]
            else:
                window = {
                    "time_range": "",
                    "time_from": time_from or "",
                    "time_to": time_to or "",
                }
            events = self.query.list_events(
                agent_id=_q_get(qs, "agent_id"),
                event_type=_q_get(qs, "event_type"),
                status=_q_get(qs, "status"),
                failures_only=bool(_q_bool(qs, "failures_only")),
                time_from=time_from,
                time_to=time_to,
                limit=_q_int(qs, "limit") or 50,
                offset=_q_int(qs, "offset") or 0,
            )
            if window.get("time_from") and window.get("time_to"):
                trend = self.query.audit_status_trend(
                    time_from=str(window["time_from"]),
                    time_to=str(window["time_to"]),
                )
            else:
                trend = {
                    "schema_version": 1,
                    "buckets": [],
                    "totals": {"PASS": 0, "WARN": 0, "FAIL": 0, "ERROR": 0},  # nosec B105
                    "bucket_unit": "hour",
                }
            return _html(
                200,
                ui.page_history(
                    events,
                    trend=trend,
                    window=window,
                    csrf=csrf,
                    qs=qs,
                ),
            )
        if path == "/dashboard/policy":
            return _html(
                200,
                ui.page_policy(
                    self.query.list_policies(limit=_q_int(qs, "limit") or 50),
                    self.query.list_policy_drift(limit=_q_int(qs, "limit") or 50),
                    self.query.list_policy_rollouts(limit=_q_int(qs, "limit") or 50),
                    self.query.list_policy_audit(limit=_q_int(qs, "limit") or 50),
                    csrf=csrf,
                ),
            )
        return _html(404, ui.page_error("Not found", path))
