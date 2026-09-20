"""Regression tests for post-expert targeted hardening."""

from __future__ import annotations

import json
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from http.cookiejar import CookieJar
from pathlib import Path
from unittest.mock import patch

import pytest

from backuplint.fleet.certs import generate_agent_key_and_csr, sign_client_csr
from backuplint.fleet.controller import FleetController
from backuplint.fleet.controller_store import STORE_SCHEMA_VERSION, ControllerStore
from backuplint.fleet.dashboard.auth import CSRF_COOKIE, DashboardAuth
from backuplint.fleet.dashboard.config import DashboardConfig
from backuplint.fleet.protocol import MAX_CSR_PEM_BYTES
from backuplint.fleet.provisioning import (
    build_provisioning_record,
    write_bundle_file,
)
from backuplint.install.features import FeatureId
from backuplint.install.layout import resolve_layout
from backuplint.install.roles import InstallationRole
from backuplint.install.systemd_units import generate_units


def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def test_invalid_token_never_reaches_signing(tmp_path: Path) -> None:
    controller = FleetController(tmp_path / "c", hostname="localhost")
    pending = controller.create_pending_agent(label="good", ttl_hours=1)
    csr = generate_agent_key_and_csr(tmp_path / "id", agent_id=pending["agent_id"])
    try:
        with patch(
            "backuplint.fleet.certs.sign_client_csr",
            side_effect=AssertionError("sign_client_csr must not run"),
        ):
            with pytest.raises(Exception, match="token|pending|enrollment|unknown"):
                controller.enroll(
                    token="totally-invalid-token",
                    hostname="host1",
                    agent_id=pending["agent_id"],
                    csr_pem=csr,
                )
    finally:
        controller.close()


def test_wrong_agent_never_reaches_signing(tmp_path: Path) -> None:
    controller = FleetController(tmp_path / "c", hostname="localhost")
    a = controller.create_pending_agent(label="a", ttl_hours=1)
    b = controller.create_pending_agent(label="b", ttl_hours=1)
    csr = generate_agent_key_and_csr(tmp_path / "id", agent_id=b["agent_id"])
    try:
        with patch(
            "backuplint.fleet.certs.sign_client_csr",
            side_effect=AssertionError("sign_client_csr must not run"),
        ):
            with pytest.raises(Exception, match="token|pending|match|enrollment"):
                controller.enroll(
                    token=a["token"],
                    hostname="host1",
                    agent_id=b["agent_id"],
                    csr_pem=csr,
                )
    finally:
        controller.close()


def test_expired_token_never_reaches_signing(tmp_path: Path) -> None:
    controller = FleetController(tmp_path / "c", hostname="localhost")
    pending = controller.create_pending_agent(label="exp", ttl_hours=1)
    expired = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    with controller.store._lock:  # noqa: SLF001
        controller.store._conn.execute(  # noqa: SLF001
            """
            UPDATE pending_enrollments
            SET expires_at = ?
            WHERE agent_id = ?
            """,
            (expired, pending["agent_id"]),
        )
    csr = generate_agent_key_and_csr(tmp_path / "id", agent_id=pending["agent_id"])
    try:
        with patch(
            "backuplint.fleet.certs.sign_client_csr",
            side_effect=AssertionError("sign_client_csr must not run"),
        ):
            with pytest.raises(Exception, match="expired|enrollment"):
                controller.enroll(
                    token=pending["token"],
                    hostname="host1",
                    agent_id=pending["agent_id"],
                    csr_pem=csr,
                )
    finally:
        controller.close()


def test_replayed_token_never_reaches_signing(tmp_path: Path) -> None:
    controller = FleetController(tmp_path / "c", hostname="localhost")
    pending = controller.create_pending_agent(label="once", ttl_hours=1)
    csr = generate_agent_key_and_csr(tmp_path / "id1", agent_id=pending["agent_id"])
    try:
        result = controller.enroll(
            token=pending["token"],
            hostname="host1",
            agent_id=pending["agent_id"],
            csr_pem=csr,
        )
        assert "client_cert" in result
        assert "client_key" not in result
        with patch(
            "backuplint.fleet.certs.sign_client_csr",
            side_effect=AssertionError("sign_client_csr must not run"),
        ):
            with pytest.raises(Exception, match="already used|enrollment|consumed"):
                controller.enroll(
                    token=pending["token"],
                    hostname="host1",
                    agent_id=pending["agent_id"],
                    csr_pem=csr,
                )
    finally:
        controller.close()


def test_valid_enrollment_still_succeeds(tmp_path: Path) -> None:
    controller = FleetController(tmp_path / "c", hostname="localhost")
    host, port = controller.start(host="127.0.0.1", port=0)
    url = f"https://127.0.0.1:{port}"
    try:
        pending = controller.create_pending_agent(label="ok", ttl_hours=1)
        from backuplint.fleet.agent import FleetAgent

        identity = FleetAgent.enroll_with_ca(
            controller_url=url,
            token=pending["token"],
            agent_id=pending["agent_id"],
            ca_cert=controller.ca_dir / "ca.crt",
            identity_dir=tmp_path / "agent",
            hostname="ok-host",
        )
        assert identity.agent_id == pending["agent_id"]
        assert (tmp_path / "agent" / "client.key").stat().st_mode & 0o777 == 0o600
    finally:
        controller.close()


def test_enroll_admission_returns_503_when_saturated(tmp_path: Path) -> None:
    controller = FleetController(
        tmp_path / "c",
        hostname="localhost",
        max_inflight=1,
        retry_after_seconds=1,
    )
    host, port = controller.start(host="127.0.0.1", port=0)
    url = f"https://127.0.0.1:{port}"
    pending = controller.create_pending_agent(label="sat", ttl_hours=1)
    csr = generate_agent_key_and_csr(tmp_path / "id", agent_id=pending["agent_id"])
    hold = threading.Event()
    released = threading.Event()

    def _blocking_enroll(*_args: object, **_kwargs: object) -> dict[str, str]:
        hold.set()
        assert released.wait(timeout=5)
        return {
            "agent_id": pending["agent_id"],
            "label": "sat",
            "ca_cert": "x",
            "client_cert": "x",
            "protocol_version": "1",
            "software_version": "0",
            "cert_serial": "1",
            "cert_fingerprint": "f",
        }

    ctx = _ssl_ctx()
    try:
        with patch.object(controller, "enroll", side_effect=_blocking_enroll):

            def _first() -> None:
                body = json.dumps(
                    {
                        "token": pending["token"],
                        "hostname": "h",
                        "agent_id": pending["agent_id"],
                        "csr": csr,
                    }
                ).encode()
                req = urllib.request.Request(
                    url + "/v1/enroll",
                    data=body,
                    method="POST",
                    headers={"Content-Type": "application/json"},
                )
                urllib.request.urlopen(req, context=ctx, timeout=5)  # noqa: S310

            t = threading.Thread(target=_first, daemon=True)
            t.start()
            assert hold.wait(timeout=5)
            body = json.dumps(
                {
                    "token": "other",
                    "hostname": "h2",
                    "agent_id": "agent-abcdefgh",
                    "csr": csr,
                }
            ).encode()
            req = urllib.request.Request(
                url + "/v1/enroll",
                data=body,
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with pytest.raises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(req, context=ctx, timeout=5)  # noqa: S310
            assert err.value.code == 503
            released.set()
            t.join(timeout=5)
    finally:
        released.set()
        controller.close()


def test_pathological_inputs_rejected_before_signing(tmp_path: Path) -> None:
    controller = FleetController(tmp_path / "c", hostname="localhost")
    try:
        with patch(
            "backuplint.fleet.certs.sign_client_csr",
            side_effect=AssertionError("sign_client_csr must not run"),
        ):
            with pytest.raises(Exception, match="agent_id"):
                controller.enroll(
                    token="x",
                    hostname="host",
                    agent_id="bad id\nwith\tcontrols",
                    csr_pem=(
                        "-----BEGIN CERTIFICATE REQUEST-----\nMIIB\n"
                        "-----END CERTIFICATE REQUEST-----\n"
                    ),
                )
            with pytest.raises(Exception, match="hostname"):
                controller.enroll(
                    token="x",
                    hostname="bad host\n",
                    agent_id="agent-abcdefgh",
                    csr_pem=(
                        "-----BEGIN CERTIFICATE REQUEST-----\nMIIB\n"
                        "-----END CERTIFICATE REQUEST-----\n"
                    ),
                )
            with pytest.raises(Exception, match="csr"):
                controller.enroll(
                    token="x",
                    hostname="host.example",
                    agent_id="agent-abcdefgh",
                    csr_pem="A" * (MAX_CSR_PEM_BYTES + 1),
                )
    finally:
        controller.close()


def test_csr_cn_substring_spoof_rejected(tmp_path: Path) -> None:
    controller = FleetController(tmp_path / "c", hostname="localhost")
    try:
        csr = generate_agent_key_and_csr(tmp_path / "spoof", agent_id="agent-10")
        with pytest.raises(Exception, match="CN|agent_id|CSR"):
            sign_client_csr(controller.ca_dir, agent_id="agent-1", csr_pem=csr)
    finally:
        controller.close()


def test_provisioning_bundle_created_0600(tmp_path: Path) -> None:
    out = tmp_path / "bundle.json"
    record = build_provisioning_record(
        agent_id="agent-abcdefgh",
        label="web",
        controller_url="https://controller.example:8443",
        enrollment_token="secret-token-value",
    )
    write_bundle_file(out, [record], include_tokens=True)
    assert (out.stat().st_mode & 0o777) == 0o600
    text = out.read_text(encoding="utf-8")
    assert "secret-token-value" in text
    assert "PRIVATE KEY" not in text


def test_dashboard_logout_rejects_cookie_only_csrf(tmp_path: Path) -> None:
    cfg = DashboardConfig(enabled=True, cookie_secure=False, session_ttl_seconds=3600)
    controller = FleetController(tmp_path / "data", dashboard=cfg)
    controller.dashboard_auth.set_password("dashboard-pass-ok")
    try:
        host, port = controller.start(host="127.0.0.1", port=0)
        base = f"https://{host}:{port}"
        jar = CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=_ssl_ctx()),
            urllib.request.HTTPCookieProcessor(jar),
        )
        body = urllib.parse.urlencode({"password": "dashboard-pass-ok"}).encode()
        login_req = urllib.request.Request(
            f"{base}/dashboard/login",
            data=body,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        opener.open(login_req, timeout=5)  # noqa: S310

        logout_req = urllib.request.Request(
            f"{base}/dashboard/logout",
            data=b"",
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with pytest.raises(urllib.error.HTTPError) as err:
            opener.open(logout_req, timeout=5)  # noqa: S310
        assert err.value.code == 403

        csrf = None
        for cookie in jar:
            if cookie.name == CSRF_COOKIE:
                csrf = cookie.value
        assert csrf
        ok_body = urllib.parse.urlencode({"csrf": csrf}).encode()
        ok_req = urllib.request.Request(
            f"{base}/dashboard/logout",
            data=ok_body,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with opener.open(ok_req, timeout=5) as resp:  # noqa: S310
            assert resp.status in (200, 302) or "login" in resp.geturl()
    finally:
        controller.close()


def test_login_limiter_prunes_stale_buckets(tmp_path: Path) -> None:
    auth = DashboardAuth(
        DashboardConfig(
            enabled=True,
            cookie_secure=False,
            login_rate_limit=5,
            login_rate_window_seconds=1,
        ),
        tmp_path,
    )
    for i in range(20):
        key = f"attacker-{i}"
        assert auth.allow_login_attempt(key)
        auth.record_login_failure(key)
    assert len(auth._login_buckets) == 20  # noqa: SLF001
    time.sleep(1.1)
    assert auth.allow_login_attempt("fresh-client")
    assert "attacker-0" not in auth._login_buckets  # noqa: SLF001


def test_events_occurred_index_fresh_and_upgrade(tmp_path: Path) -> None:
    fresh = ControllerStore(tmp_path / "fresh.sqlite3")
    try:
        assert fresh.schema_version() == STORE_SCHEMA_VERSION
        rows = fresh._conn.execute(  # noqa: SLF001
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_events_occurred'"
        ).fetchall()
        assert rows
    finally:
        fresh.close()

    old_path = tmp_path / "old.sqlite3"
    old = ControllerStore(old_path)
    with old._lock:  # noqa: SLF001
        old._conn.execute("DROP INDEX IF EXISTS idx_events_occurred")  # noqa: SLF001
        old._meta_set("schema_version", "5")  # noqa: SLF001
    old.close()

    upgraded = ControllerStore(old_path)
    try:
        assert upgraded.schema_version() == STORE_SCHEMA_VERSION
        rows = upgraded._conn.execute(  # noqa: SLF001
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_events_occurred'"
        ).fetchall()
        assert rows
    finally:
        upgraded.close()


def test_systemd_units_include_protect_directives(tmp_path: Path) -> None:
    layout = resolve_layout(prefix=tmp_path)
    units = generate_units(
        role=InstallationRole.ALL_IN_ONE,
        features=(FeatureId.SCHEDULER, FeatureId.FLEET_CONTROLLER),
        layout=layout,
    )
    assert units
    for unit in units:
        content = unit.content
        assert "NoNewPrivileges=true" in content
        assert "PrivateTmp=true" in content
        assert "ProtectSystem=strict" in content
        assert "ProtectHome=true" in content
        assert "ProtectKernelTunables=true" in content
        assert "ProtectKernelModules=true" in content
        assert "ProtectControlGroups=true" in content
        assert "RestrictSUIDSGID=true" in content
        assert "ReadWritePaths=" in content
