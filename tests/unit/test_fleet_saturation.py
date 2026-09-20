"""Saturation admission, telemetry, and agent Retry-After backoff tests."""

from __future__ import annotations

import json
import ssl
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import pytest

from backuplint.fleet.agent import (
    AgentQueue,
    AgentTransientError,
    FleetAgent,
    compute_retry_delay,
    parse_retry_after,
    retry_backoff_seconds,
)
from backuplint.fleet.controller import FleetController
from backuplint.fleet.protocol import ResultEnvelope, new_submission_id
from backuplint.fleet.telemetry import ControllerTelemetry


def _enroll_agent(
    controller: FleetController, tmp_path: Path, url: str, name: str = "agent"
) -> FleetAgent:
    from tests.helpers.fleet_lab import enroll_test_agent

    return enroll_test_agent(controller, tmp_path, url, name=name)


def test_retry_backoff_and_retry_after_helpers() -> None:
    assert parse_retry_after("3") == 3.0
    assert parse_retry_after(None) is None
    # Deterministic rng → stable jittered values.
    d1 = retry_backoff_seconds(0, rng=lambda: 0.0)
    d2 = retry_backoff_seconds(1, rng=lambda: 0.0)
    assert d1 == 0.5
    assert d2 == 1.0
    honored = compute_retry_delay(0, 2.0, rng=lambda: 0.5)
    assert honored == pytest.approx(2.0)


def test_admission_returns_503_with_retry_after(tmp_path: Path) -> None:
    data_dir = tmp_path / "controller"
    controller = FleetController(
        data_dir,
        hostname="localhost",
        max_inflight=1,
        retry_after_seconds=2,
    )
    _host, port = controller.start(host="127.0.0.1", port=0)
    url = f"https://127.0.0.1:{port}"
    entered = threading.Event()
    release = threading.Event()
    original = controller.store.heartbeat

    def blocking_heartbeat(agent_id: str, **_kwargs: object) -> None:
        entered.set()
        assert release.wait(timeout=5)
        original(agent_id)

    controller.store.heartbeat = blocking_heartbeat  # type: ignore[method-assign]
    try:
        agent = _enroll_agent(controller, tmp_path, url)
        errors: list[BaseException] = []

        def hold_slot() -> None:
            try:
                agent.heartbeat(max_attempts=1)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        holder = threading.Thread(target=hold_slot, daemon=True)
        holder.start()
        assert entered.wait(timeout=5)

        # Second concurrent heartbeat must be rejected with 503 + Retry-After.
        context = agent._ssl_context()
        req = urllib.request.Request(
            url + "/v1/heartbeat",
            data=json.dumps({"agent_id": agent.identity.agent_id}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(req, context=context, timeout=5)  # nosec B310
        err = caught.value
        assert err.code == 503
        assert err.headers.get("Retry-After") == "2"
        body = json.loads(err.read().decode("utf-8"))
        assert body["error"] == "controller saturated"
        assert controller.telemetry.snapshot(persist=False)["rejected_503"] >= 1
        # Rejected request must not have completed a store write beyond the holder.
        release.set()
        holder.join(timeout=5)
        assert not errors
    finally:
        release.set()
        controller.close()


def test_saturated_results_do_not_write_store(tmp_path: Path) -> None:
    data_dir = tmp_path / "controller"
    controller = FleetController(
        data_dir, hostname="localhost", max_inflight=1, retry_after_seconds=1
    )
    _host, port = controller.start(host="127.0.0.1", port=0)
    url = f"https://127.0.0.1:{port}"
    entered = threading.Event()
    release = threading.Event()
    original = controller.store.heartbeat

    def blocking_heartbeat(agent_id: str, **_kwargs: object) -> None:
        entered.set()
        assert release.wait(timeout=5)
        original(agent_id)

    controller.store.heartbeat = blocking_heartbeat  # type: ignore[method-assign]
    try:
        agent = _enroll_agent(controller, tmp_path, url)
        holder = threading.Thread(
            target=lambda: agent.heartbeat(max_attempts=1), daemon=True
        )
        holder.start()
        assert entered.wait(timeout=5)

        envelope = ResultEnvelope(
            agent_id=agent.identity.agent_id,
            submission_id=new_submission_id(),
            scan_time=datetime.now(UTC).isoformat(),
            backuplint_version="0.5.0.dev0",
            platform="test",
            result={"summary": {"result": "PASS"}},
        )
        with pytest.raises(AgentTransientError) as caught:
            # Bypass flush retries: call _post directly once.
            agent._post("/v1/results", envelope.to_dict())
        assert caught.value.status == 503
        assert controller.store.latest_result(agent.identity.agent_id) is None
        release.set()
        holder.join(timeout=5)
    finally:
        release.set()
        controller.close()


def test_metrics_endpoint_requires_client_cert_and_persists(tmp_path: Path) -> None:
    data_dir = tmp_path / "controller"
    controller = FleetController(data_dir, hostname="localhost")
    _host, port = controller.start(host="127.0.0.1", port=0)
    url = f"https://127.0.0.1:{port}"
    try:
        # Anonymous health still works.
        anon = ssl.create_default_context()
        anon.check_hostname = False
        anon.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(  # nosec B310
            url + "/v1/health", context=anon, timeout=5
        ) as resp:
            assert resp.status == 200

        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(url + "/v1/metrics", context=anon, timeout=5)  # nosec
        assert caught.value.code == 401

        agent = _enroll_agent(controller, tmp_path, url)
        agent.heartbeat()
        req = urllib.request.Request(url + "/v1/metrics", method="GET")
        with urllib.request.urlopen(  # nosec B310
            req, context=agent._ssl_context(), timeout=5
        ) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        assert payload["ok"] is True
        metrics = payload["metrics"]
        assert "endpoint_latency_ms" in metrics
        assert metrics["max_inflight"] == controller.max_inflight
        assert "p50_ms" in metrics["endpoint_latency_ms"].get("/v1/heartbeat", {})
        assert controller.metrics_path.is_file()
        lines = controller.metrics_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) >= 1
        assert "rejected_503" in json.loads(lines[-1])
    finally:
        controller.close()


def test_agent_honors_retry_after_with_backoff(tmp_path: Path) -> None:
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    # Fixed rng → predictable delays from Retry-After.
    agent = FleetAgent(
        controller_url="https://example.invalid",
        identity=mock.Mock(),
        queue=AgentQueue(tmp_path / "q.jsonl"),
        sleep=fake_sleep,
        rng=lambda: 0.5,
    )
    calls = {"n": 0}

    def flaky_post(path: str, payload: dict[str, object]) -> dict[str, object]:
        calls["n"] += 1
        if calls["n"] < 3:
            raise AgentTransientError(
                "saturated", status=503, retry_after=1.5
            )
        return {"ok": True}

    agent._post = flaky_post  # type: ignore[method-assign]
    agent.identity.agent_id = "agent-test0001"
    agent.heartbeat(max_attempts=5)
    assert calls["n"] == 3
    assert sleeps == [pytest.approx(1.5), pytest.approx(1.5)]


def test_agent_flush_retries_then_keeps_queue(tmp_path: Path) -> None:
    from backuplint.fleet.agent import AgentIdentity

    sleeps: list[float] = []
    identity_dir = tmp_path / "agent"
    identity_dir.mkdir()
    (identity_dir / "agent_id").write_text("agent-abcdefgh\n", encoding="utf-8")
    for name in ("ca.crt", "client.crt", "client.key"):
        (identity_dir / name).write_text("x", encoding="utf-8")

    queue = AgentQueue(tmp_path / "q.jsonl")
    envelope = ResultEnvelope(
        agent_id="agent-abcdefgh",
        submission_id=new_submission_id(),
        scan_time=datetime.now(UTC).isoformat(),
        backuplint_version="0.5.0.dev0",
        platform="test",
        result={"summary": {"result": "FAIL"}},
    )
    queue.enqueue(envelope)

    agent = FleetAgent(
        controller_url="https://example.invalid",
        identity=AgentIdentity.load(identity_dir),
        queue=queue,
        sleep=lambda s: sleeps.append(s),
        rng=lambda: 0.0,
    )

    def always_503(path: str, payload: dict[str, object]) -> dict[str, object]:
        raise AgentTransientError("no", status=503, retry_after=1.0)

    agent._post = always_503  # type: ignore[method-assign]
    acked = agent.flush(max_attempts_per_item=3)
    assert acked == 0
    assert len(sleeps) == 2  # attempts 0 and 1, then give up
    assert any(i.get("submission_id") == envelope.submission_id for i in queue.peek_all())


def test_listen_backlog_raised(tmp_path: Path) -> None:
    controller = FleetController(
        tmp_path / "controller",
        hostname="localhost",
        request_queue_size=256,
    )
    try:
        _host, _port = controller.start(host="127.0.0.1", port=0)
        assert controller._httpd is not None
        assert controller._httpd.request_queue_size == 256
    finally:
        controller.close()


def test_default_inflight_still_allows_normal_fleet_flow(tmp_path: Path) -> None:
    """1000-style smoke: enroll, heartbeat, idempotent submit under default budget."""
    data_dir = tmp_path / "controller"
    controller = FleetController(data_dir, hostname="localhost")
    assert controller.max_inflight >= 64
    _host, port = controller.start(host="127.0.0.1", port=0)
    url = f"https://127.0.0.1:{port}"
    try:
        agent = _enroll_agent(controller, tmp_path, url)
        agent.heartbeat()
        envelope = ResultEnvelope(
            agent_id=agent.identity.agent_id,
            submission_id=new_submission_id(),
            scan_time=datetime.now(UTC).isoformat(),
            backuplint_version="0.5.0.dev0",
            platform="test",
            result={"summary": {"result": "PASS"}},
        )
        agent.submit_envelope(envelope)
        agent.submit_envelope(envelope)  # idempotent ACK
        latest = controller.store.latest_result(agent.identity.agent_id)
        assert latest is not None
        assert latest["submission_id"] == envelope.submission_id
        snap = controller.metrics_snapshot(persist=True)
        assert snap["rejected_503"] == 0
        assert snap["admitted"] >= 2
    finally:
        controller.close()


def test_telemetry_latency_summary_not_just_counts() -> None:
    tel = ControllerTelemetry(sample_window=100)
    for ms in (10.0, 20.0, 30.0, 40.0, 100.0):
        started = tel.begin_request("/v1/results")
        time.sleep(0)  # no-op; inject via end path by observing directly
        tel.end_request("/v1/results", started - (ms / 1000.0))
    snap = tel.snapshot(persist=True)
    lat = snap["endpoint_latency_ms"]["/v1/results"]
    assert lat["count"] == 5
    assert lat["p50_ms"] is not None
    assert lat["p95_ms"] is not None
    assert lat["max_ms"] is not None
    assert len(tel.recent_snapshots()) == 1
