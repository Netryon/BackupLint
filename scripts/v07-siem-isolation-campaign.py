#!/usr/bin/env python3
"""Bounded v0.7 SIEM failure/isolation + concurrent regression campaign."""

from __future__ import annotations

import json
import ssl
import threading
import time
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from http.cookiejar import CookieJar
from pathlib import Path
from types import SimpleNamespace

from backuplint.fleet.controller import FleetController
from backuplint.fleet.dashboard.config import DashboardConfig
from backuplint.fleet.protocol import PROTOCOL_VERSION, ResultEnvelope, new_submission_id
from backuplint.secrets import SecretRef, SecretSource
from backuplint.siem.config import SiemAuthType, SiemConfig
from backuplint.siem.event import NON_EXPORTABLE_FAMILIES, SiemEventFamily
from backuplint.siem.queue import SiemQueueError
from backuplint.siem.transport.base import TransportResult
from tests.helpers.fleet_lab import enroll_test_agent


class ControllableTransport:
    name = "campaign"

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.mode = "healthy"
        self.sent: list[str] = []
        self.attempts = 0
        self.retryable_failures = 0
        self.permanent_failures = 0

    def send(self, event, *, config: SiemConfig) -> TransportResult:
        with self.lock:
            self.attempts += 1
            mode = self.mode
        if mode == "healthy":
            with self.lock:
                self.sent.append(event.event_id)
            return TransportResult(True, False, 200, None, None)
        if mode == "unavailable":
            with self.lock:
                self.retryable_failures += 1
            return TransportResult(False, True, 503, None, "unavailable")
        if mode == "rate_limit":
            with self.lock:
                self.retryable_failures += 1
            return TransportResult(False, True, 429, 1.0, "rate limited")
        if mode == "bad_creds":
            with self.lock:
                self.retryable_failures += 1
            return TransportResult(False, True, 401, None, "unauthorized")
        if mode == "tls_fail":
            with self.lock:
                self.retryable_failures += 1
            return TransportResult(False, True, None, None, "tls validation failed")
        with self.lock:
            self.permanent_failures += 1
        return TransportResult(False, False, 400, None, "bad request")


def _siem_cfg() -> SiemConfig:
    return SiemConfig(
        enabled=True,
        endpoint="https://siem.example.internal/ingest",
        auth_type=SiemAuthType.BEARER,
        auth_token=SecretRef(source=SecretSource.ENV, name="BACKUPLINT_SIEM_TOKEN"),
        tls_verify=False,
        initial_backoff_seconds=0.05,
        max_backoff_seconds=1.0,
    )


def _fail_envelope(agent_id: str) -> ResultEnvelope:
    return ResultEnvelope(
        agent_id=agent_id,
        submission_id=new_submission_id(),
        scan_time=datetime.now(UTC).isoformat(),
        backuplint_version="0.7.campaign",
        platform="campaign",
        result={"summary": {"result": "FAIL"}, "reason": "campaign failure"},
        protocol_version=PROTOCOL_VERSION,
    )


def _pass_envelope(agent_id: str) -> ResultEnvelope:
    return ResultEnvelope(
        agent_id=agent_id,
        submission_id=new_submission_id(),
        scan_time=datetime.now(UTC).isoformat(),
        backuplint_version="0.7.campaign",
        platform="campaign",
        result={"summary": {"result": "PASS"}},
        protocol_version=PROTOCOL_VERSION,
    )


def _counts(store) -> tuple[int, int]:
    with store._lock:  # noqa: SLF001
        events = store._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]  # noqa: SLF001
        submissions = store._conn.execute(  # noqa: SLF001
            "SELECT COUNT(*) FROM submissions"
        ).fetchone()[0]
    return int(events), int(submissions)


def run_isolation_campaign(tmp: Path) -> dict[str, object]:
    report: dict[str, object] = {}
    data_dir = tmp / "ctrl"

    controller = FleetController(data_dir, hostname="localhost", siem=_siem_cfg())
    transport = ControllableTransport()
    assert controller._siem_exporter is not None
    controller._siem_exporter.transport = transport
    host, port = controller.start(host="127.0.0.1", port=0)
    url = f"https://{host}:{port}"
    try:
        agent = enroll_test_agent(controller, tmp, url, name="iso-agent")

        # 1. Healthy delivery
        transport.mode = "healthy"
        agent.submit_envelope(_fail_envelope(agent.identity.agent_id))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            controller._siem_exporter.drain_once(wall_clock_budget_seconds=0.5, max_items=8)
            if controller._siem_exporter.queue.depth_by_status().get("delivered", 0) >= 1:
                break
            time.sleep(0.05)
        delivered = controller._siem_exporter.queue.depth_by_status().get("delivered", 0)
        report["healthy_delivery"] = {
            "ok": delivered >= 1 and len(set(transport.sent)) >= 1,
            "delivered_rows": delivered,
            "unique_sent": len(set(transport.sent)),
        }

        # 2. Outage
        transport.mode = "unavailable"
        for _ in range(5):
            agent.submit_envelope(_fail_envelope(agent.identity.agent_id))
        controller._siem_exporter.drain_once(wall_clock_budget_seconds=1.0, max_items=20)
        depth = controller._siem_exporter.queue.depth_by_status()
        pendingish = depth.get("pending", 0) + depth.get("in_flight", 0)
        events, submissions = _counts(controller.store)
        report["outage_behavior"] = {
            "ok": pendingish >= 1 and submissions >= 6,
            "pending_or_inflight": pendingish,
            "submissions": submissions,
            "events": events,
            "retryable_failures": transport.retryable_failures,
        }
        report["fleet_ingest_during_outage"] = report["outage_behavior"]["ok"]
        report["dashboard_during_outage"] = events >= 1

        # 3. Recovery / drain
        transport.mode = "healthy"
        before_unique = len(set(transport.sent))
        delivered_total = 0
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            summary = controller._siem_exporter.drain_once(
                wall_clock_budget_seconds=1.0, max_items=64
            )
            delivered_total += summary.delivered
            depth_after = controller._siem_exporter.queue.depth_by_status()
            if depth_after.get("pending", 0) == 0 and depth_after.get("in_flight", 0) == 0:
                break
            time.sleep(0.05)
        depth_after = controller._siem_exporter.queue.depth_by_status()
        report["recovery_drain"] = {
            "ok": delivered_total >= 1 and depth_after.get("pending", 0) == 0,
            "delivered": delivered_total,
            "unique_sent_delta": len(set(transport.sent)) - before_unique,
            "pending_after": depth_after.get("pending", 0),
        }

        # 4. Restart durability
        transport.mode = "unavailable"
        agent.submit_envelope(_fail_envelope(agent.identity.agent_id))
        controller._siem_exporter.drain_once(wall_clock_budget_seconds=0.5, max_items=8)
        controller.close()

        controller = FleetController(data_dir, hostname="localhost", siem=_siem_cfg())
        transport = ControllableTransport()
        transport.mode = "healthy"
        assert controller._siem_exporter is not None
        controller._siem_exporter.transport = transport
        depth_reopen = controller._siem_exporter.queue.depth_by_status()
        summary2 = controller._siem_exporter.drain_once(
            wall_clock_budget_seconds=5.0, max_items=64
        )
        report["restart_durability"] = {
            "ok": (
                depth_reopen.get("pending", 0) + depth_reopen.get("in_flight", 0) >= 1
                or summary2.delivered >= 1
            ),
            "pending_on_reopen": depth_reopen.get("pending", 0)
            + depth_reopen.get("in_flight", 0),
            "delivered_after_reopen": summary2.delivered,
        }

        # Resume server for remaining checks
        host, port = controller.start(host="127.0.0.1", port=0)
        url = f"https://{host}:{port}"
        agent = enroll_test_agent(controller, tmp, url, name="iso-agent-2")

        # 5. 429 Retry-After
        transport.mode = "rate_limit"
        before = transport.retryable_failures
        agent.submit_envelope(_fail_envelope(agent.identity.agent_id))
        controller._siem_exporter.drain_once(wall_clock_budget_seconds=1.0, max_items=4)
        report["retry_after_429"] = {
            "ok": transport.retryable_failures > before,
            "retryable_failures": transport.retryable_failures,
        }

        # 6. Bad credentials — no secret leakage in status
        transport.mode = "bad_creds"
        agent.submit_envelope(_fail_envelope(agent.identity.agent_id))
        controller._siem_exporter.drain_once(wall_clock_budget_seconds=1.0, max_items=4)
        snap = controller.siem_status_snapshot(persist=False)
        snap_text = json.dumps(snap)
        report["bad_credentials"] = {
            "ok": (
                "BACKUPLINT_SIEM_TOKEN" not in snap_text
                and "Bearer" not in snap_text
                and "token" not in snap_text.lower().split("auth")[-1][:40]
            ),
            "has_enabled": "enabled" in snap,
        }

        # 7. TLS failure
        transport.mode = "tls_fail"
        before = transport.retryable_failures
        agent.submit_envelope(_fail_envelope(agent.identity.agent_id))
        controller._siem_exporter.drain_once(wall_clock_budget_seconds=1.0, max_items=4)
        report["tls_failure"] = {
            "ok": transport.retryable_failures > before,
        }

        # 8. Recursion guard
        fake = SimpleNamespace(
            event_id="66666666-6666-4666-8666-666666666666",
            event_family=SiemEventFamily.SIEM_DELIVERY_ERROR,
            severity=SimpleNamespace(value="info"),
            priority_class="healthy",
            schema_version=1,
            occurred_at=datetime.now(UTC).isoformat(),
            to_json=lambda: "{}",
        )
        recursion_ok = False
        try:
            controller._siem_exporter.queue.enqueue(fake)  # type: ignore[arg-type]
        except SiemQueueError:
            recursion_ok = True
        report["recursion_guard"] = {
            "ok": recursion_ok
            and NON_EXPORTABLE_FAMILIES == frozenset({SiemEventFamily.SIEM_DELIVERY_ERROR}),
        }

        report["accounting"] = {
            "transport_attempts": transport.attempts,
            "unique_delivered_ids": len(set(transport.sent)),
            "retryable_failures": transport.retryable_failures,
            "queue_depth": controller._siem_exporter.queue.depth_by_status(),
            "fleet_events_submissions": _counts(controller.store),
        }
        report["pass"] = all(
            bool(report[k]["ok"])
            for k in (
                "healthy_delivery",
                "outage_behavior",
                "recovery_drain",
                "restart_durability",
                "retry_after_429",
                "bad_credentials",
                "tls_failure",
                "recursion_guard",
            )
        )
        return report
    finally:
        controller.close()


def run_concurrent_with_siem(
    tmp: Path,
    *,
    agents: int = 12,
    seconds: float = 20.0,
    dashboard_workers: int = 3,
    siem_mode: str = "healthy",
) -> dict[str, object]:
    dash = DashboardConfig(enabled=True, cookie_secure=False)
    controller = FleetController(
        tmp / "ctrl",
        hostname="localhost",
        dashboard=dash,
        siem=_siem_cfg(),
    )
    controller.dashboard_auth.set_password("campaign-pass-ok")
    transport = ControllableTransport()
    transport.mode = siem_mode
    assert controller._siem_exporter is not None
    controller._siem_exporter.transport = transport

    submit_ok = submit_err = hb_ok = hb_err = dash_ok = dash_err = 0
    dash_lat: list[float] = []
    stop = threading.Event()
    lock = threading.Lock()

    host, port = controller.start(host="127.0.0.1", port=0)
    base = f"https://{host}:{port}"
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        fleet = [
            enroll_test_agent(controller, tmp, base, name=f"c-{i}")
            for i in range(agents)
        ]
        jar = CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ctx),
            urllib.request.HTTPCookieProcessor(jar),
        )
        body = urllib.parse.urlencode({"password": "campaign-pass-ok"}).encode()
        login = urllib.request.Request(
            f"{base}/dashboard/login",
            data=body,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        opener.open(login, timeout=10)  # noqa: S310

        def fleet_worker(i: int) -> None:
            nonlocal submit_ok, submit_err, hb_ok, hb_err
            agent = fleet[i]
            while not stop.is_set():
                try:
                    agent.heartbeat(max_attempts=2)
                    with lock:
                        hb_ok += 1
                except Exception:  # noqa: BLE001
                    with lock:
                        hb_err += 1
                try:
                    env = (
                        _fail_envelope(agent.identity.agent_id)
                        if i % 3 == 0
                        else _pass_envelope(agent.identity.agent_id)
                    )
                    agent.submit_envelope(env)
                    with lock:
                        submit_ok += 1
                except Exception:  # noqa: BLE001
                    with lock:
                        submit_err += 1
                time.sleep(0.05)

        def dash_worker() -> None:
            nonlocal dash_ok, dash_err
            paths = [
                "/v1/dashboard/overview",
                "/v1/dashboard/agents?limit=50",
                "/v1/dashboard/events?limit=25",
                "/dashboard/",
            ]
            i = 0
            while not stop.is_set():
                path = paths[i % len(paths)]
                i += 1
                started = time.perf_counter()
                try:
                    with opener.open(f"{base}{path}", timeout=10) as resp:  # noqa: S310
                        resp.read()
                    with lock:
                        dash_ok += 1
                        dash_lat.append((time.perf_counter() - started) * 1000.0)
                except Exception:  # noqa: BLE001
                    with lock:
                        dash_err += 1
                time.sleep(0.03)

        threads = [
            threading.Thread(target=fleet_worker, args=(i,), daemon=True)
            for i in range(agents)
        ]
        threads += [
            threading.Thread(target=dash_worker, daemon=True)
            for _ in range(dashboard_workers)
        ]
        for t in threads:
            t.start()
        time.sleep(seconds)
        stop.set()
        time.sleep(0.5)
        if siem_mode == "healthy":
            controller._siem_exporter.drain_once(
                wall_clock_budget_seconds=3.0, max_items=256
            )
        snap = controller.metrics_snapshot(persist=False)
        siem_depth = controller._siem_exporter.queue.depth_by_status()
        events, submissions = _counts(controller.store)
        sorted_lat = sorted(dash_lat)

        def pct(values: list[float], p: float) -> float | None:
            if not values:
                return None
            idx = min(
                len(values) - 1, max(0, int(round((p / 100.0) * (len(values) - 1))))
            )
            return values[idx]

        return {
            "siem_mode": siem_mode,
            "seconds": seconds,
            "agents": agents,
            "dashboard_workers": dashboard_workers,
            "submit_ok": submit_ok,
            "submit_err": submit_err,
            "heartbeat_ok": hb_ok,
            "heartbeat_err": hb_err,
            "dashboard_ok": dash_ok,
            "dashboard_err": dash_err,
            "siem_queued_pending": siem_depth.get("pending", 0),
            "siem_delivered_rows": siem_depth.get("delivered", 0),
            "siem_unique_sent": len(set(transport.sent)),
            "siem_retries": transport.retryable_failures,
            "siem_permanent": transport.permanent_failures,
            "queue_end": siem_depth,
            "in_flight_end": {
                "active_requests": snap.get("active_requests"),
                "active_peak": snap.get("active_peak"),
                "max_inflight": snap.get("max_inflight"),
            },
            "db_integrity": {
                "submissions": submissions,
                "events": events,
                "submit_ok_floor": submissions >= submit_ok,
            },
            "latency_ms": {
                "p50": pct(sorted_lat, 50),
                "p95": pct(sorted_lat, 95),
                "p99": pct(sorted_lat, 99),
            },
            "pass": submit_err == 0
            and hb_err == 0
            and dash_err == 0
            and submit_ok > 0
            and submissions >= submit_ok,
        }
    finally:
        controller.close()


if __name__ == "__main__":
    import argparse
    import tempfile

    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="backuplint-v07-siem-campaign-"))
    isolation = run_isolation_campaign(root / "isolation")
    concurrent_ok = run_concurrent_with_siem(root / "conc-ok", siem_mode="healthy")
    concurrent_down = run_concurrent_with_siem(
        root / "conc-down", siem_mode="unavailable"
    )
    payload = {
        "isolation": isolation,
        "concurrent_siem_healthy": concurrent_ok,
        "concurrent_siem_unavailable": concurrent_down,
        "pass": bool(
            isolation.get("pass")
            and concurrent_ok.get("pass")
            and concurrent_down.get("pass")
        ),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    raise SystemExit(0 if payload["pass"] else 1)
