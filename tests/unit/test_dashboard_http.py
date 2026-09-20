"""Dashboard auth, CSRF, session, and HTTP API security tests."""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

import pytest

from backuplint.fleet.controller import FleetController
from backuplint.fleet.dashboard.auth import DashboardAuth, DashboardAuthError
from backuplint.fleet.dashboard.config import DashboardConfig
from tests.helpers.fleet_lab import enroll_test_agent


def test_password_hash_roundtrip(tmp_path: Path) -> None:
    auth = DashboardAuth(
        DashboardConfig(enabled=True, cookie_secure=False), tmp_path
    )
    auth.set_password("correct-horse-battery-staple")
    assert auth.verify_password("correct-horse-battery-staple")
    assert not auth.verify_password("wrong-password-here")
    assert auth.hash_path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(DashboardAuthError):
        auth.set_password("short")


def test_dashboard_requires_auth_and_escapes_xss(tmp_path: Path) -> None:
    cfg = DashboardConfig(enabled=True, cookie_secure=False, session_ttl_seconds=3600)
    controller = FleetController(tmp_path / "data", dashboard=cfg)
    controller.dashboard_auth.set_password("dashboard-pass-ok")
    try:
        host, port = controller.start(host="127.0.0.1", port=0)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        base = f"https://{host}:{port}"

        # Unauthenticated API
        req = urllib.request.Request(f"{base}/v1/dashboard/overview")
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(req, context=ctx, timeout=5)  # noqa: S310
        assert err.value.code == 401

        # Login
        jar = CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ctx),
            urllib.request.HTTPCookieProcessor(jar),
        )
        body = urllib.parse.urlencode(
            {"password": "dashboard-pass-ok"}
        ).encode("utf-8")
        login_req = urllib.request.Request(
            f"{base}/dashboard/login",
            data=body,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with opener.open(login_req, timeout=5) as resp:  # noqa: S310
            assert resp.status in (200, 302) or resp.geturl().endswith("/dashboard/")

        with opener.open(f"{base}/v1/dashboard/overview", timeout=5) as resp:  # noqa: S310
            payload = json.loads(resp.read().decode("utf-8"))
            assert payload["schema_version"] == 1
            assert "totals" in payload

        # Enroll agent with XSS-ish label; UI must escape
        url = f"https://{host}:{port}"
        agent = enroll_test_agent(controller, tmp_path, url, name="xssagent")
        controller.store.set_agent_label(
            agent.identity.agent_id, '<script>alert(1)</script>'
        )
        with opener.open(f"{base}/dashboard/", timeout=5) as resp:  # noqa: S310
            html = resp.read().decode("utf-8")
            assert "<script>alert(1)</script>" not in html
            assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html

        # Pagination clamp via API
        with opener.open(  # noqa: S310
            f"{base}/v1/dashboard/events?limit=99999&offset=-5", timeout=5
        ) as resp:
            events = json.loads(resp.read().decode("utf-8"))
            assert events["limit"] == cfg.max_page_size
            assert events["offset"] == 0

        # Malformed filter values must not 500
        with opener.open(  # noqa: S310
            f"{base}/v1/dashboard/agents?protocol_version=not-int&q=" + ("%2e%2e/" * 40),
            timeout=5,
        ) as resp:
            assert resp.status == 200

        with opener.open(  # noqa: S310
            f"{base}/v1/dashboard/insights?time_range=24h", timeout=5
        ) as resp:
            insights = json.loads(resp.read().decode("utf-8"))
            assert insights["schema_version"] == 1
            assert "window" in insights and "alerts" in insights and "trend" in insights
    finally:
        controller.close()


def test_dashboard_disabled_by_default(tmp_path: Path) -> None:
    controller = FleetController(tmp_path / "data")
    try:
        host, port = controller.start(host="127.0.0.1", port=0)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(f"https://{host}:{port}/v1/dashboard/overview")
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(req, context=ctx, timeout=5)  # noqa: S310
        assert err.value.code == 404
    finally:
        controller.close()
