"""Queue deletion correctness under lost ACK, duplicate retry, and restart."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backuplint.fleet.agent import AgentError, AgentQueue, FleetAgent
from backuplint.fleet.controller import FleetController
from backuplint.fleet.protocol import ResultEnvelope, new_submission_id


def _envelope(agent_id: str, submission_id: str | None = None) -> ResultEnvelope:
    return ResultEnvelope(
        agent_id=agent_id,
        submission_id=submission_id or new_submission_id(),
        scan_time=datetime.now(UTC).isoformat(),
        backuplint_version="0.5.0.dev0",
        platform="test",
        result={"summary": {"result": "PASS"}},
    )


def _event_count(controller: FleetController, submission_id: str) -> int:
    row = controller.store._conn.execute(
        "SELECT COUNT(*) FROM events WHERE submission_id = ?",
        (submission_id,),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _queued(agent: FleetAgent, submission_id: str) -> bool:
    return any(
        i.get("submission_id") == submission_id for i in agent.queue.peek_all()
    )


def _enroll(tmp_path: Path, *, port: int = 0) -> tuple[FleetController, FleetAgent, str]:
    data_dir = tmp_path / "controller"
    controller = FleetController(data_dir, hostname="localhost")
    _host, bound = controller.start(host="127.0.0.1", port=port)
    url = f"https://127.0.0.1:{bound}"
    pending = controller.create_pending_agent(label="lab", ttl_hours=1)
    identity = FleetAgent.enroll_with_ca(
        controller_url=url,
        token=pending["token"],
        agent_id=pending["agent_id"],
        ca_cert=data_dir / "ca" / "ca.crt",
        identity_dir=tmp_path / "agent",
    )
    agent = FleetAgent(
        controller_url=url,
        identity=identity,
        queue=AgentQueue(tmp_path / "agent" / "queue.jsonl"),
    )
    return controller, agent, url


def test_lost_before_commit_keeps_queue_then_creates(tmp_path: Path) -> None:
    controller, agent, _url = _enroll(tmp_path)
    try:
        envelope = _envelope(agent.identity.agent_id)
        agent.queue.enqueue(envelope)

        def fail_before_commit(path: str, payload: dict[str, Any]) -> dict[str, Any]:
            raise AgentError("request failed: connection refused before commit")

        agent._post = fail_before_commit  # type: ignore[method-assign]
        assert agent.flush() == 0
        assert _queued(agent, envelope.submission_id)
        assert _event_count(controller, envelope.submission_id) == 0

        # Restore real post and drain.
        del agent._post
        assert agent.flush() == 1
        assert not _queued(agent, envelope.submission_id)
        assert _event_count(controller, envelope.submission_id) == 1
    finally:
        controller.close()


def test_lost_after_commit_keeps_queue_until_http_200(tmp_path: Path) -> None:
    controller, agent, _url = _enroll(tmp_path)
    try:
        envelope = _envelope(agent.identity.agent_id)
        agent.queue.enqueue(envelope)
        real_post = agent._post

        def commit_then_lose(path: str, payload: dict[str, Any]) -> dict[str, Any]:
            resp = real_post(path, payload)
            raise AgentError("request failed: reset after commit") from None
            return resp  # pragma: no cover

        agent._post = commit_then_lose  # type: ignore[method-assign]
        assert agent.flush() == 0
        assert _queued(agent, envelope.submission_id)
        assert _event_count(controller, envelope.submission_id) == 1

        agent._post = real_post  # type: ignore[method-assign]
        assert agent.flush() == 1
        assert not _queued(agent, envelope.submission_id)
        assert _event_count(controller, envelope.submission_id) == 1
    finally:
        controller.close()


def test_retry_duplicate_deletes_on_http_200(tmp_path: Path) -> None:
    """Product flush deletes on HTTP 200 even when created=false."""
    controller, agent, _url = _enroll(tmp_path)
    try:
        envelope = _envelope(agent.identity.agent_id)
        agent.submit_envelope(envelope)
        assert not _queued(agent, envelope.submission_id)
        agent.queue.enqueue(envelope)  # simulate client that still had it
        assert agent.flush() == 1
        assert not _queued(agent, envelope.submission_id)
        assert _event_count(controller, envelope.submission_id) == 1
        assert controller.store.ingest_result(envelope) is False
    finally:
        controller.close()


def test_repeated_response_loss_then_success(tmp_path: Path) -> None:
    controller, agent, _url = _enroll(tmp_path)
    try:
        envelope = _envelope(agent.identity.agent_id)
        agent.queue.enqueue(envelope)
        real_post = agent._post
        losses = {"n": 0}

        def lose_twice(path: str, payload: dict[str, Any]) -> dict[str, Any]:
            resp = real_post(path, payload)
            losses["n"] += 1
            if losses["n"] <= 2:
                raise AgentError(f"request failed: loss #{losses['n']}")
            return resp

        agent._post = lose_twice  # type: ignore[method-assign]
        assert agent.flush() == 0
        assert agent.flush() == 0
        assert _queued(agent, envelope.submission_id)
        assert _event_count(controller, envelope.submission_id) == 1
        assert agent.flush() == 1
        assert not _queued(agent, envelope.submission_id)
        assert _event_count(controller, envelope.submission_id) == 1
        assert losses["n"] == 3
    finally:
        controller.close()


def test_controller_restart_between_commit_and_retry(tmp_path: Path) -> None:
    controller, agent, _url = _enroll(tmp_path)
    try:
        envelope = _envelope(agent.identity.agent_id)
        agent.queue.enqueue(envelope)
        real_post = agent._post

        def commit_then_lose(path: str, payload: dict[str, Any]) -> dict[str, Any]:
            real_post(path, payload)
            raise AgentError("request failed: lost after commit")

        agent._post = commit_then_lose  # type: ignore[method-assign]
        assert agent.flush() == 0
        assert _event_count(controller, envelope.submission_id) == 1

        data_dir = tmp_path / "controller"
        controller.close()
        # New controller process/object on same durable store.
        controller2 = FleetController(data_dir, hostname="localhost")
        _h, port2 = controller2.start(host="127.0.0.1", port=0)
        agent.controller_url = f"https://127.0.0.1:{port2}"
        del agent._post  # restore real _post
        assert agent.flush() == 1
        assert not _queued(agent, envelope.submission_id)
        assert _event_count(controller2, envelope.submission_id) == 1
        assert controller2.store.ingest_result(envelope) is False
        controller2.close()
        controller = None  # already closed
    finally:
        if controller is not None:
            controller.close()


def test_agent_restart_before_retry_preserves_queue_file(tmp_path: Path) -> None:
    controller, agent, url = _enroll(tmp_path)
    try:
        envelope = _envelope(agent.identity.agent_id)
        agent.queue.enqueue(envelope)

        def fail_post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
            raise AgentError("request failed: offline")

        agent._post = fail_post  # type: ignore[method-assign]
        assert agent.flush() == 0
        queue_path = agent.queue.path
        assert _queued(agent, envelope.submission_id)

        # Simulate agent process restart: new FleetAgent, same queue file + identity.
        identity = agent.identity
        agent2 = FleetAgent(
            controller_url=url,
            identity=identity,
            queue=AgentQueue(queue_path),
        )
        assert _queued(agent2, envelope.submission_id)
        assert agent2.flush() == 1
        assert agent2.queue.peek_all() == []
        assert _event_count(controller, envelope.submission_id) == 1
    finally:
        controller.close()
