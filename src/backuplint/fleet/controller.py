"""HTTPS controller for BackupLint fleet result ingestion."""

from __future__ import annotations

import json
import signal
import ssl
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from backuplint import __version__ as SOFTWARE_VERSION
from backuplint.fleet.certs import (
    init_ca,
    issue_server_cert,
)
from backuplint.fleet.compat import health_protocol_payload
from backuplint.fleet.controller_store import ControllerStore, ControllerStoreError
from backuplint.fleet.dashboard.auth import DashboardAuth
from backuplint.fleet.dashboard.config import DashboardConfig
from backuplint.fleet.dashboard.http import DashboardHttp
from backuplint.fleet.dashboard.query import DashboardQueryService
from backuplint.fleet.protocol import (
    MAX_BODY_BYTES,
    MAX_CSR_PEM_BYTES,
    PROTOCOL_VERSION,
    ProtocolError,
    is_valid_agent_id,
    parse_envelope,
    validate_enroll_hostname,
)
from backuplint.fleet.telemetry import ControllerTelemetry
from backuplint.siem.config import SiemConfig
from backuplint.siem.runtime import build_exporter, submit_canonical_event, telemetry_snapshot

# Default admission budget: above steady ~1000-agent concurrency, below unbounded.
DEFAULT_MAX_INFLIGHT = 128
DEFAULT_REQUEST_QUEUE_SIZE = 256
DEFAULT_RETRY_AFTER_SECONDS = 1
# Extra workers for health/metrics/enroll and quick 503 responses.
_WORKER_HEADROOM = 16


class ControllerError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class FleetHTTPServer(ThreadingHTTPServer):
    """Capped worker pool + large listen backlog (vs unbounded thread-per-request)."""

    daemon_threads = True
    allow_reuse_address = True
    block_on_close = True

    def __init__(
        self,
        server_address: tuple[str, int],
        RequestHandlerClass: type[BaseHTTPRequestHandler],
        *,
        max_workers: int,
        request_queue_size: int,
    ) -> None:
        self.request_queue_size = max(5, int(request_queue_size))
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, int(max_workers)),
            thread_name_prefix="fleet-http",
        )
        super().__init__(server_address, RequestHandlerClass)

    def process_request(
        self, request: object, client_address: tuple[str, int] | str
    ) -> None:
        self._executor.submit(self.process_request_thread, request, client_address)

    def server_close(self) -> None:
        super().server_close()
        self._executor.shutdown(wait=True, cancel_futures=False)


class FleetController:
    def __init__(
        self,
        data_dir: Path,
        *,
        hostname: str = "localhost",
        max_inflight: int = DEFAULT_MAX_INFLIGHT,
        request_queue_size: int = DEFAULT_REQUEST_QUEUE_SIZE,
        retry_after_seconds: int = DEFAULT_RETRY_AFTER_SECONDS,
        dashboard: DashboardConfig | None = None,
        siem: SiemConfig | None = None,
    ) -> None:
        self.data_dir = data_dir
        self.hostname = hostname
        self.ca_dir = data_dir / "ca"
        self.server_dir = data_dir / "server"
        self.agents_dir = data_dir / "agents"
        self.max_inflight = max(1, int(max_inflight))
        self.request_queue_size = max(5, int(request_queue_size))
        self.retry_after_seconds = max(1, int(retry_after_seconds))
        self.telemetry = ControllerTelemetry()
        self.metrics_path = data_dir / "metrics.jsonl"
        self._admission = threading.BoundedSemaphore(self.max_inflight)
        self.store = ControllerStore(
            data_dir / "controller.sqlite3", telemetry=self.telemetry
        )
        self.dashboard_config = dashboard or DashboardConfig(enabled=False)
        self.dashboard_auth = DashboardAuth(self.dashboard_config, data_dir)
        self._dashboard_reader = self.store.open_reader()
        self.dashboard_query = DashboardQueryService(
            self.store, self.dashboard_config, reader=self._dashboard_reader
        )
        self.dashboard_http = DashboardHttp(self.dashboard_auth, self.dashboard_query)
        self.siem_config = siem or SiemConfig()
        self.siem_telemetry_path = data_dir / "siem" / "telemetry.jsonl"
        self._siem_exporter = build_exporter(data_dir, self.siem_config)
        init_ca(self.ca_dir)
        issue_server_cert(self.ca_dir, self.server_dir, common_name=hostname)
        self._httpd: FleetHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def close(self) -> None:
        self.stop()
        try:
            self._dashboard_reader.close()
        except OSError:
            pass
        self.store.close()

    def submit_siem_for_submission(self, submission_id: str) -> None:
        """Best-effort SIEM enqueue after fleet ingest; never raises to HTTP callers."""
        if self._siem_exporter is None:
            return
        try:
            canonical = self.store.event_for_submission(submission_id)
            if canonical is None:
                return
            submit_canonical_event(
                self._siem_exporter,
                canonical,
                source_role="controller",
            )
        except Exception:  # noqa: BLE001
            import logging

            logging.getLogger(__name__).warning(
                "SIEM enqueue failed for submission %s",
                submission_id,
                exc_info=False,
            )

    def siem_status_snapshot(self, *, persist: bool = True) -> dict[str, object]:
        snap = telemetry_snapshot(
            self._siem_exporter,
            telemetry_path=self.siem_telemetry_path,
            persist=persist,
        )
        if snap is None:
            return {"enabled": False}
        safe = dict(snap)
        safe["enabled"] = self.siem_config.enabled
        return safe

    def create_enroll_token(self, *, label: str, ttl_hours: int = 1) -> str:
        from datetime import timedelta

        return self.store.create_enroll_token(
            label=label, ttl=timedelta(hours=ttl_hours)
        )

    def create_pending_agent(
        self, *, label: str, ttl_hours: int = 1, agent_id: str | None = None
    ) -> dict[str, str]:
        """Create pending identity + one-time token (raw token returned once)."""
        from datetime import timedelta

        resolved_id, token = self.store.create_pending_enrollment(
            label=label, ttl=timedelta(hours=ttl_hours), agent_id=agent_id
        )
        return {"agent_id": resolved_id, "token": token, "label": label}

    def create_pending_agents_bulk(
        self,
        *,
        count: int,
        label_prefix: str = "agent",
        ttl_hours: int = 24,
    ) -> list[dict[str, str]]:
        if count < 1 or count > 10_000:
            raise ControllerError("count must be between 1 and 10000")
        out: list[dict[str, str]] = []
        for i in range(count):
            out.append(
                self.create_pending_agent(
                    label=f"{label_prefix}-{i+1:04d}", ttl_hours=ttl_hours
                )
            )
        return out

    def export_provisioning_records(
        self,
        *,
        controller_url: str,
        pending: list[dict[str, str]],
        ca_cert_pem: str | None = None,
        role: str = "agent",
        feature_profile: str = "default",
        deployment_form: str = "native",
    ) -> list[object]:
        from backuplint.fleet.provisioning import build_provisioning_record

        records = []
        for item in pending:
            expires = None
            row = self.store.get_pending_enrollment(item["agent_id"])
            if row is not None:
                expires = str(row.get("expires_at")) if row.get("expires_at") else None
            records.append(
                build_provisioning_record(
                    agent_id=item["agent_id"],
                    label=item["label"],
                    controller_url=controller_url,
                    enrollment_token=item.get("token"),
                    token_expires_at=expires,
                    ca_cert_pem=ca_cert_pem,
                    role=role,
                    feature_profile=feature_profile,
                    deployment_form=deployment_form,
                )
            )
        return records

    def start(self, host: str = "127.0.0.1", port: int = 8443) -> tuple[str, int]:
        if self._httpd is not None:
            raise ControllerError("controller already running")
        handler = _make_handler(self)
        max_workers = self.max_inflight + _WORKER_HEADROOM
        httpd = FleetHTTPServer(
            (host, port),
            handler,
            max_workers=max_workers,
            request_queue_size=self.request_queue_size,
        )
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(
            certfile=str(self.server_dir / "server.crt"),
            keyfile=str(self.server_dir / "server.key"),
        )
        context.load_verify_locations(cafile=str(self.ca_dir / "ca.crt"))
        # Optional so /v1/enroll works before client cert exists.
        context.verify_mode = ssl.CERT_OPTIONAL
        httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
        self._httpd = httpd
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self._thread = thread
        if self._siem_exporter is not None:
            self._siem_exporter.start()
        bound_host, bound_port = httpd.server_address[:2]
        return str(bound_host), int(bound_port)

    def stop(self) -> None:
        if self._siem_exporter is not None:
            self._siem_exporter.stop()
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def wait_until_stopped(
        self,
        *,
        stop_event: threading.Event | None = None,
        poll_seconds: float = 0.25,
    ) -> None:
        """Block until SIGTERM/SIGINT or an injected stop_event is set.

        Does not close the store; callers should invoke :meth:`close` afterward.
        Used by ``backuplint controller run`` so container orchestrators can stop
        the process cleanly with SIGTERM.
        """
        event = stop_event if stop_event is not None else threading.Event()

        def _request_stop(*_args: object) -> None:
            event.set()

        previous_term = signal.signal(signal.SIGTERM, _request_stop)
        previous_int = signal.signal(signal.SIGINT, _request_stop)
        try:
            while not event.is_set():
                event.wait(timeout=poll_seconds)
        finally:
            signal.signal(signal.SIGTERM, previous_term)
            signal.signal(signal.SIGINT, previous_int)
            self.stop()

    def try_admit(self) -> bool:
        """Non-blocking admission for heavy store work. False → caller should 503."""
        acquired = self._admission.acquire(blocking=False)
        self.telemetry.record_admission(admitted=acquired)
        return acquired

    def release_admit(self) -> None:
        self._admission.release()

    def metrics_snapshot(self, *, persist: bool = True) -> dict[str, object]:
        snap = self.telemetry.snapshot(persist=persist)
        snap["max_inflight"] = self.max_inflight
        snap["request_queue_size"] = self.request_queue_size
        if persist:
            self.telemetry.persist_to_jsonl(self.metrics_path, snap)
        return snap

    def enroll(
        self,
        *,
        token: str,
        hostname: str,
        agent_id: str,
        csr_pem: str,
    ) -> dict[str, str]:
        """CSR enrollment: sign agent-generated CSR; never handle private keys.

        Cheap token/agent validation runs *before* CA signing so invalid,
        expired, replayed, or mismatched tokens cannot trigger openssl work.
        Token consumption remains atomic inside ``complete_csr_enrollment``.
        """
        from backuplint.fleet.certs import CertError, sign_client_csr

        if not is_valid_agent_id(agent_id):
            raise ControllerError("invalid agent_id")
        try:
            hostname = validate_enroll_hostname(hostname)
        except ProtocolError as exc:
            raise ControllerError(exc.message) from exc
        if not isinstance(token, str) or not token.strip():
            raise ControllerError("token required")
        if not isinstance(csr_pem, str) or not csr_pem.strip():
            raise ControllerError("csr required")
        if len(csr_pem.encode("utf-8")) > MAX_CSR_PEM_BYTES:
            raise ControllerError("csr too large")
        if "PRIVATE KEY" in csr_pem.upper():
            raise ControllerError("CSR must not include a private key")
        # Validate/authenticate the enrollment token before any CA signing.
        try:
            self.store.assert_enroll_token_usable(
                token=token.strip(), agent_id=agent_id
            )
        except ControllerStoreError as exc:
            raise ControllerError(exc.message) from exc
        try:
            cert_pem, serial, fingerprint = sign_client_csr(
                self.ca_dir, agent_id=agent_id, csr_pem=csr_pem
            )
        except CertError as exc:
            raise ControllerError(exc.message) from exc
        try:
            label = self.store.complete_csr_enrollment(
                token=token.strip(),
                agent_id=agent_id,
                hostname=hostname,
                cert_serial=serial,
                cert_fingerprint=fingerprint,
            )
        except ControllerStoreError as exc:
            raise ControllerError(exc.message) from exc
        # Never include client_key in the response.
        return {
            "agent_id": agent_id,
            "label": label,
            "ca_cert": (self.ca_dir / "ca.crt").read_text(encoding="utf-8"),
            "client_cert": cert_pem,
            "protocol_version": str(PROTOCOL_VERSION),
            "software_version": SOFTWARE_VERSION,
            "cert_serial": serial,
            "cert_fingerprint": fingerprint,
        }


def _make_handler(controller: FleetController) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: object) -> None:
            # Avoid logging tokens/bodies.
            return

        def _read_json(self) -> object:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > MAX_BODY_BYTES:
                raise ProtocolError("body too large")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                return json.loads(raw.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise ProtocolError("invalid JSON body") from exc

        def _send(
            self,
            code: int,
            payload: dict[str, object],
            *,
            extra_headers: dict[str, str] | None = None,
        ) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if extra_headers:
                for key, value in extra_headers.items():
                    self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _send_saturated(self) -> None:
            retry_after = str(controller.retry_after_seconds)
            self._send(
                503,
                {
                    "error": "controller saturated",
                    "retry_after": controller.retry_after_seconds,
                },
                extra_headers={"Retry-After": retry_after},
            )

        def _peer_agent_id(self) -> str | None:
            try:
                peer = self.request.getpeercert()
            except Exception:  # noqa: BLE001
                peer = None
            if not peer:
                return None
            subject = peer.get("subject", ())
            for rdn in subject:
                for key, value in rdn:
                    if key == "commonName":
                        return str(value)
            return None

        def _send_dashboard(self, response: object) -> None:
            from backuplint.fleet.dashboard.http import DashboardResponse

            if not isinstance(response, DashboardResponse):
                self._send(500, {"error": "internal error"})
                return
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(response.body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
                "frame-ancestors 'none'; base-uri 'none'",
            )
            for key, value in response.headers:
                self.send_header(key, value)
            self.end_headers()
            if response.body:
                self.wfile.write(response.body)

        def _dashboard_headers(self) -> dict[str, str]:
            return {k: v for k, v in self.headers.items()}

        def _try_dashboard(self, method: str) -> bool:
            parsed = urlparse(self.path)
            path = parsed.path
            if not (
                path.startswith("/dashboard") or path.startswith("/v1/dashboard")
            ):
                return False
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length < 0 or length > MAX_BODY_BYTES:
                self._send(400, {"error": "body too large"})
                return True
            body = self.rfile.read(length) if length and method == "POST" else b""
            client = self.client_address[0] if self.client_address else "unknown"
            response = controller.dashboard_http.handle(
                method=method,
                path=path,
                query_string=parsed.query,
                headers=self._dashboard_headers(),
                body=body,
                client_addr=str(client),
            )
            if response is None:
                return False
            self._send_dashboard(response)
            return True

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            endpoint = path
            started = controller.telemetry.begin_request(endpoint)
            http_error = False
            try:
                if self._try_dashboard("GET"):
                    return
                if path == "/v1/health":
                    self._send(
                        200,
                        health_protocol_payload(software_version=SOFTWARE_VERSION),
                    )
                    return
                if path == "/v1/metrics":
                    # Require mTLS so metrics are not world-readable on the port.
                    if not self._peer_agent_id():
                        http_error = True
                        self._send(401, {"error": "client certificate required"})
                        return
                    snap = controller.metrics_snapshot(persist=True)
                    self._send(200, {"ok": True, "metrics": snap})
                    return
                if path == "/v1/siem/status":
                    if not self._peer_agent_id():
                        http_error = True
                        self._send(401, {"error": "client certificate required"})
                        return
                    snap = controller.siem_status_snapshot(persist=True)
                    self._send(200, {"ok": True, "siem": snap})
                    return
                if path == "/v1/policy/desired":
                    agent_id = self._peer_agent_id()
                    if not agent_id:
                        http_error = True
                        self._send(401, {"error": "client certificate required"})
                        return
                    if not controller.try_admit():
                        http_error = True
                        self._send_saturated()
                        return
                    try:
                        qs = parse_qs(urlparse(self.path).query)

                        def _q_int(name: str) -> int | None:
                            vals = qs.get(name)
                            if not vals:
                                return None
                            try:
                                return int(vals[0])
                            except ValueError:
                                return None

                        def _q_str(name: str) -> str | None:
                            vals = qs.get(name)
                            return vals[0] if vals else None

                        desired = controller.store.policy_get_desired(
                            agent_id,
                            current_assignment_generation=_q_int(
                                "current_assignment_generation"
                            ),
                            current_revision_id=_q_str("current_revision_id"),
                            current_content_sha256=_q_str("current_content_sha256"),
                        )
                        self._send(200, desired)
                    except ControllerStoreError as exc:
                        http_error = True
                        code = 403 if "revoked" in exc.message else 400
                        self._send(code, {"error": exc.message})
                    finally:
                        controller.release_admit()
                    return
                http_error = True
                self._send(404, {"error": "not found"})
            finally:
                controller.telemetry.end_request(
                    endpoint, started, http_error=http_error
                )

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            endpoint = path
            started = controller.telemetry.begin_request(endpoint)
            http_error = False
            admitted = False
            try:
                if self._try_dashboard("POST"):
                    return
                if path == "/v1/enroll":
                    # Enrollment is unauthenticated (pre-cert) and must share the
                    # same admission budget as heartbeat/results so CSR signing
                    # floods cannot starve legitimate agent traffic.
                    if not controller.try_admit():
                        http_error = True
                        self._send_saturated()
                        return
                    admitted = True
                    data = self._read_json()
                    if not isinstance(data, dict):
                        raise ProtocolError("enroll body must be object")
                    token = data.get("token")
                    hostname = data.get("hostname") or "unknown"
                    agent_id = data.get("agent_id")
                    csr_pem = data.get("csr")
                    if not isinstance(token, str) or not token.strip():
                        raise ProtocolError("token required")
                    if not isinstance(hostname, str) or not hostname.strip():
                        raise ProtocolError("hostname required")
                    if not isinstance(agent_id, str) or not agent_id.strip():
                        raise ProtocolError("agent_id required")
                    if not isinstance(csr_pem, str) or not csr_pem.strip():
                        raise ProtocolError("csr required")
                    # Do not log token or CSR body.
                    result = controller.enroll(
                        token=token.strip(),
                        hostname=hostname.strip(),
                        agent_id=agent_id.strip(),
                        csr_pem=csr_pem,
                    )
                    self._send(200, result)
                    return

                agent_id = self._peer_agent_id()
                if not agent_id:
                    http_error = True
                    self._send(401, {"error": "client certificate required"})
                    return

                if path in ("/v1/heartbeat", "/v1/results", "/v1/policy/applied"):
                    if not controller.try_admit():
                        http_error = True
                        self._send_saturated()
                        return
                    admitted = True

                if path == "/v1/heartbeat":
                    data = self._read_json()
                    if not isinstance(data, dict):
                        raise ProtocolError("heartbeat body must be object")
                    protocol_version = data.get("protocol_version")
                    software_version = data.get("software_version")
                    capabilities = data.get("capabilities")
                    if protocol_version is not None:
                        from backuplint.fleet.compat import check_protocol_version

                        compat = check_protocol_version(protocol_version)
                        if not compat.ok:
                            raise ProtocolError(
                                compat.message, code=str(compat.code)
                            )
                    if software_version is not None and (
                        not isinstance(software_version, str)
                        or not software_version.strip()
                    ):
                        raise ProtocolError("invalid software_version")
                    if capabilities is not None and not isinstance(capabilities, dict):
                        raise ProtocolError("capabilities must be an object")
                    controller.store.heartbeat(
                        agent_id,
                        protocol_version=(
                            int(protocol_version)
                            if isinstance(protocol_version, int)
                            else None
                        ),
                        software_version=(
                            software_version.strip()
                            if isinstance(software_version, str)
                            else None
                        ),
                        capabilities=(
                            capabilities if isinstance(capabilities, dict) else None
                        ),
                    )
                    self._send(200, {"ok": True})
                    return

                if path == "/v1/results":
                    data = self._read_json()
                    envelope = parse_envelope(data)
                    if envelope.agent_id != agent_id:
                        http_error = True
                        self._send(
                            403, {"error": "agent_id does not match certificate"}
                        )
                        return
                    created = controller.store.ingest_result(envelope)
                    if created:
                        controller.submit_siem_for_submission(envelope.submission_id)
                    self._send(200, {"ok": True, "created": created})
                    return

                if path == "/v1/policy/applied":
                    data = self._read_json()
                    if not isinstance(data, dict):
                        raise ProtocolError("policy applied body must be object")
                    # Identity comes from mTLS only — ignore any body agent_id.
                    body_agent = data.get("agent_id")
                    if body_agent is not None and str(body_agent) != agent_id:
                        http_error = True
                        self._send(
                            403, {"error": "agent_id does not match certificate"}
                        )
                        return
                    required = (
                        "assignment_generation",
                        "revision_id",
                        "content_sha256",
                        "apply_status",
                        "drift_status",
                    )
                    missing = [k for k in required if k not in data]
                    if missing:
                        raise ProtocolError(
                            f"missing fields: {', '.join(missing)}"
                        )
                    generation = data["assignment_generation"]
                    if not isinstance(generation, int) or generation < 1:
                        raise ProtocolError("assignment_generation must be int >= 1")
                    for key in ("revision_id", "content_sha256", "apply_status", "drift_status"):
                        if not isinstance(data[key], str) or not str(data[key]).strip():
                            raise ProtocolError(f"{key} required")
                    reason = data.get("reason", "")
                    if reason is not None and not isinstance(reason, str):
                        raise ProtocolError("reason must be a string")
                    if isinstance(reason, str) and len(reason) > 2048:
                        raise ProtocolError("reason too long")
                    applied_at = data.get("applied_at")
                    if applied_at is not None and not isinstance(applied_at, str):
                        raise ProtocolError("applied_at must be a string")
                    local_hash = data.get("local_config_sha256")
                    if local_hash is not None and not isinstance(local_hash, str):
                        raise ProtocolError("local_config_sha256 must be a string")
                    controller.store.policy_report_applied(
                        agent_id,
                        assignment_generation=generation,
                        revision_id=str(data["revision_id"]).strip(),
                        content_sha256=str(data["content_sha256"]).strip().lower(),
                        apply_status=str(data["apply_status"]).strip(),
                        drift_status=str(data["drift_status"]).strip(),
                        reason=str(reason or ""),
                        applied_at=applied_at if isinstance(applied_at, str) else None,
                        local_config_sha256=(
                            local_hash if isinstance(local_hash, str) else None
                        ),
                    )
                    self._send(200, {"ok": True})
                    return

                http_error = True
                self._send(404, {"error": "not found"})
            except ProtocolError as exc:
                http_error = True
                body: dict[str, object] = {"error": exc.message}
                if exc.code:
                    body["error_code"] = exc.code
                self._send(400, body)
            except (ControllerStoreError, ControllerError) as exc:
                http_error = True
                self._send(400, {"error": exc.message})
            except Exception:  # noqa: BLE001
                http_error = True
                self._send(500, {"error": "internal error"})
            finally:
                if admitted:
                    controller.release_admit()
                controller.telemetry.end_request(
                    endpoint, started, http_error=http_error
                )

    return Handler


def write_agent_identity(dir_path: Path, enrollment: dict[str, str]) -> None:
    """Write identity files from enrollment. Controller must not supply client_key."""
    if "client_key" in enrollment:
        raise ControllerError("refusing to write controller-supplied client_key")
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / "agent_id").write_text(enrollment["agent_id"] + "\n", encoding="utf-8")
    (dir_path / "ca.crt").write_text(enrollment["ca_cert"], encoding="utf-8")
    (dir_path / "client.crt").write_text(enrollment["client_cert"], encoding="utf-8")
    dir_path.chmod(0o700)
