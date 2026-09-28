"""Live HTTPS SIEM transport TLS verify behavior."""

from __future__ import annotations

import json
import ssl
import subprocess
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from backuplint.siem.config import SiemAuthType, SiemConfig
from backuplint.siem.event import SiemCategory, SiemEvent, SiemEventFamily, SiemSeverity
from backuplint.siem.exporter import SiemExporter
from backuplint.siem.queue import SiemExportQueue
from backuplint.siem.telemetry import SiemExportTelemetry
from backuplint.siem.transport.https_json import HttpsJsonTransport, tls_verify_enabled


def _event(event_id: str = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa") -> SiemEvent:
    now = datetime.now(UTC).isoformat()
    return SiemEvent(
        schema_version=1,
        event_id=event_id,
        event_family=SiemEventFamily.AGENT_OFFLINE,
        severity=SiemSeverity.MEDIUM,
        category=SiemCategory.FLEET_TRANSPORT,
        occurred_at=now,
        received_at=now,
        source_role="controller",
        summary="Agent offline",
        agent_id="agent-1",
    )


def _make_self_signed(tmp: Path) -> tuple[Path, Path]:
    cert = tmp / "server.crt"
    key = tmp / "server.key"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return cert, key


def _make_ca_and_server(tmp: Path) -> tuple[Path, Path, Path]:
    ca_key = tmp / "ca.key"
    ca_crt = tmp / "ca.crt"
    srv_key = tmp / "trusted.key"
    srv_csr = tmp / "trusted.csr"
    srv_crt = tmp / "trusted.crt"
    ext = tmp / "san.ext"
    ext.write_text("subjectAltName=DNS:localhost,IP:127.0.0.1\n", encoding="utf-8")
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(ca_key),
            "-out",
            str(ca_crt),
            "-days",
            "1",
            "-subj",
            "/CN=BackupLint-Test-CA",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
            "-addext",
            "keyUsage=critical,keyCertSign,cRLSign",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "openssl",
            "req",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(srv_key),
            "-out",
            str(srv_csr),
            "-subj",
            "/CN=localhost",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "openssl",
            "x509",
            "-req",
            "-in",
            str(srv_csr),
            "-CA",
            str(ca_crt),
            "-CAkey",
            str(ca_key),
            "-CAcreateserial",
            "-out",
            str(srv_crt),
            "-days",
            "1",
            "-sha256",
            "-extfile",
            str(ext),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return ca_crt, srv_crt, srv_key


class _Receiver(BaseHTTPRequestHandler):
    received: list[dict[str, Any]]
    ids: list[str]
    alive: bool

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        return

    def do_POST(self) -> None:  # noqa: N802
        if not type(self).alive:
            self.send_error(503)
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            payload = {}
        type(self).received.append(payload)
        event_id = str(payload.get("event_id") or self.headers.get("Idempotency-Key") or "")
        type(self).ids.append(event_id)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")


def _serve(cert: Path, key: Path) -> tuple[ThreadingHTTPServer, str]:
    _Receiver.received = []
    _Receiver.ids = []
    _Receiver.alive = True
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Receiver)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(str(cert), str(key))
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    return httpd, f"https://127.0.0.1:{port}/ingest"


def test_tls_verify_config_is_honored() -> None:
    assert tls_verify_enabled(config_verify=True) is True
    assert tls_verify_enabled(config_verify=False) is False


def test_verify_false_accepts_self_signed(tmp_path: Path) -> None:
    cert, key = _make_self_signed(tmp_path)
    httpd, endpoint = _serve(cert, key)
    try:
        cfg = SiemConfig(
            enabled=True,
            endpoint=endpoint,
            auth_type=SiemAuthType.NONE,
            tls_verify=False,
        )
        result = HttpsJsonTransport().send(_event(), config=cfg)
        assert result.delivered is True, result.error
        assert result.error is None or "secret" not in (result.error or "").lower()
        assert len(_Receiver.ids) == 1
    finally:
        httpd.shutdown()


def test_verify_true_rejects_self_signed(tmp_path: Path) -> None:
    cert, key = _make_self_signed(tmp_path)
    httpd, endpoint = _serve(cert, key)
    try:
        cfg = SiemConfig(
            enabled=True,
            endpoint=endpoint,
            auth_type=SiemAuthType.NONE,
            tls_verify=True,
        )
        result = HttpsJsonTransport().send(_event(), config=cfg)
        assert result.delivered is False
        assert result.retryable is True
        err = (result.error or "").lower()
        assert "certificate" in err or "ssl" in err or "verify" in err
        assert "begin certificate" not in err
        assert len(_Receiver.ids) == 0
    finally:
        httpd.shutdown()


def test_verify_true_with_ca_file_succeeds(tmp_path: Path) -> None:
    ca, cert, key = _make_ca_and_server(tmp_path)
    httpd, endpoint = _serve(cert, key)
    try:
        cfg = SiemConfig(
            enabled=True,
            endpoint=endpoint,
            auth_type=SiemAuthType.NONE,
            tls_verify=True,
            tls_ca_file=ca,
        )
        result = HttpsJsonTransport().send(_event(), config=cfg)
        assert result.delivered is True, result.error
        assert len(_Receiver.ids) == 1
    finally:
        httpd.shutdown()


def test_outage_queues_then_drains_without_duplicates(tmp_path: Path) -> None:
    cert, key = _make_self_signed(tmp_path)
    httpd, endpoint = _serve(cert, key)
    cfg = SiemConfig(
        enabled=True,
        endpoint=endpoint,
        auth_type=SiemAuthType.NONE,
        tls_verify=False,
        initial_backoff_seconds=0.05,
        max_backoff_seconds=0.2,
    )
    queue = SiemExportQueue(tmp_path / "q.sqlite3")
    transport = HttpsJsonTransport()
    exporter = SiemExporter(queue, transport, cfg, SiemExportTelemetry())
    try:
        exporter.submit(_event("11111111-1111-4111-8111-111111111111"))
        exporter.drain_once(wall_clock_budget_seconds=2.0)
        assert queue.depth_by_status().get("delivered", 0) >= 1
        _Receiver.alive = False
        exporter.submit(_event("22222222-2222-4222-8222-222222222222"))
        exporter.drain_once(wall_clock_budget_seconds=1.0)
        pending = queue.depth_by_status().get("pending", 0) + queue.depth_by_status().get(
            "in_flight", 0
        )
        assert pending >= 1
        _Receiver.alive = True
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            exporter.drain_once(wall_clock_budget_seconds=1.0)
            if queue.depth_by_status().get("pending", 0) == 0:
                break
            time.sleep(0.05)
        depth = queue.depth_by_status()
        assert depth.get("pending", 0) == 0
        assert depth.get("delivered", 0) >= 2
        ids = [i for i in _Receiver.ids if i]
        assert ids.count("11111111-1111-4111-8111-111111111111") == 1
        assert ids.count("22222222-2222-4222-8222-222222222222") == 1
        assert len(ids) == len(set(ids))
    finally:
        exporter.stop()
        httpd.shutdown()
