"""Deterministic lost-ACK reproduction and delivery accounting tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from backuplint.fleet.agent import AgentError, AgentQueue, FleetAgent
from backuplint.fleet.controller import FleetController
from backuplint.fleet.delivery import (
    DeliveryAccounting,
    DeliveryState,
    classify_http_result,
    conservation_equations,
    counts_as_delivery_confirmation,
)
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


def _enroll_agent(tmp_path: Path) -> tuple[FleetController, FleetAgent]:
    data_dir = tmp_path / "controller"
    controller = FleetController(data_dir, hostname="localhost")
    _host, port = controller.start(host="127.0.0.1", port=0)
    url = f"https://127.0.0.1:{port}"
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
    return controller, agent


def test_classify_delivery_states() -> None:
    assert (
        classify_http_result(created=True)
        is DeliveryState.NEWLY_CONFIRMED
    )
    assert (
        classify_http_result(created=False)
        is DeliveryState.IDEMPOTENTLY_CONFIRMED
    )
    assert (
        classify_http_result(created=False, prior_attempt_failed=True)
        is DeliveryState.LOST_ACK_CONFIRMED
    )
    assert (
        classify_http_result(
            created=False,
            prior_attempt_failed=True,
            already_delivery_confirmed=True,
        )
        is DeliveryState.IDEMPOTENTLY_CONFIRMED
    )
    assert (
        classify_http_result(error_message="request failed: timeout")
        is DeliveryState.RETRYABLE_FAILURE
    )
    assert (
        classify_http_result(error_message="request failed: HTTP 403 revoked")
        is DeliveryState.PERMANENT_REJECT
    )
    assert counts_as_delivery_confirmation(DeliveryState.NEWLY_CONFIRMED)
    assert counts_as_delivery_confirmation(DeliveryState.LOST_ACK_CONFIRMED)
    assert not counts_as_delivery_confirmation(DeliveryState.IDEMPOTENTLY_CONFIRMED)


def test_lost_ack_accounting_off_by_one_example() -> None:
    """Reproduce the 2000-stage signature with honest equations."""
    acct = DeliveryAccounting()
    # 164638 delivered + 21 permanent rejects; one lost ACK among deliveries.
    for i in range(164637):
        sid = f"ok-{i}"
        acct.record_generated(valid=True)
        acct.observe(sid, created=True)
        acct.note_drained()
    # Lost ACK path for one submission
    lost = "lost-1"
    acct.record_generated(valid=True)
    assert acct.observe(lost, error_message="request failed: timed out") is (
        DeliveryState.RETRYABLE_FAILURE
    )
    assert acct.observe(lost, created=False) is DeliveryState.LOST_ACK_CONFIRMED
    acct.note_drained()
    for i in range(21):
        sid = f"rej-{i}"
        acct.record_generated(valid=True)
        assert acct.observe(sid, error_message="HTTP 403 revoked") is (
            DeliveryState.PERMANENT_REJECT
        )
        acct.note_drained()

    snap = acct.snapshot()
    assert snap["generated_valid"] == 164659
    assert snap["acked_new"] == 164637
    assert snap["lost_ack_confirmed"] == 1
    assert snap["delivery_confirmed"] == 164638
    assert snap["permanently_rejected_valid"] == 21
    eqs = conservation_equations(
        generated_valid=snap["generated_valid"],
        delivery_confirmed=snap["delivery_confirmed"],
        remaining_queued=0,
        permanently_rejected_valid=snap["permanently_rejected_valid"],
        campaign_events_in_db=164638,
        newly_confirmed=snap["newly_confirmed"],
        queued_created=snap["queued_created"],
        queued_drained=snap["queued_drained"],
    )
    assert all(eqs.values())
    # Dishonest legacy gate fails by one — documenting why we revised it.
    assert snap["acked_new"] != 164638


def test_commit_succeeds_response_lost_retry_duplicate(tmp_path: Path) -> None:
    """Controller commits; client loses response; retry sees created=false; one event."""
    controller, agent = _enroll_agent(tmp_path)
    try:
        envelope = _envelope(agent.identity.agent_id)
        agent.queue.enqueue(envelope)
        acct = DeliveryAccounting()
        acct.record_generated(valid=True)

        real_post = agent._post
        calls = {"n": 0}

        def lost_ack_post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
            calls["n"] += 1
            if calls["n"] == 1:
                # Server-side commit happens; client never parses the body.
                created = controller.store.ingest_result(envelope)
                assert created is True
                raise AgentError("request failed: connection reset after commit")
            return real_post(path, payload)

        agent._post = lost_ack_post  # type: ignore[method-assign]
        assert agent.flush() == 0
        assert _queued_sid(agent, envelope.submission_id)
        assert _event_count(controller, envelope.submission_id) == 1
        assert (
            acct.observe(
                envelope.submission_id,
                error_message="request failed: connection reset after commit",
            )
            is DeliveryState.RETRYABLE_FAILURE
        )

        # Retry: same submission_id → created=false path, still one event, queue drains.
        assert agent.flush() == 1
        assert agent.queue.peek_all() == []
        assert _event_count(controller, envelope.submission_id) == 1
        assert calls["n"] == 2

        state = acct.observe(envelope.submission_id, created=False)
        assert state is DeliveryState.LOST_ACK_CONFIRMED
        acct.note_drained()
        snap = acct.snapshot()
        assert snap["delivery_confirmed"] == 1
        assert snap["acked_new"] == 0
        assert snap["lost_ack_confirmed"] == 1
        assert snap["acked_duplicate"] == 1
        eqs = conservation_equations(
            generated_valid=snap["generated_valid"],
            delivery_confirmed=snap["delivery_confirmed"],
            remaining_queued=0,
            permanently_rejected_valid=0,
            campaign_events_in_db=1,
            newly_confirmed=snap["newly_confirmed"],
            queued_created=snap["queued_created"],
            queued_drained=snap["queued_drained"],
        )
        assert all(eqs.values())
    finally:
        controller.close()


def _queued_sid(agent: FleetAgent, submission_id: str) -> bool:
    return any(i.get("submission_id") == submission_id for i in agent.queue.peek_all())

def test_lost_ack_via_submit_envelope_monkeypatch(tmp_path: Path) -> None:
    """Fault-inject inside _post after real HTTP success body would be returned."""
    controller, agent = _enroll_agent(tmp_path)
    try:
        envelope = _envelope(agent.identity.agent_id)
        real_post = agent._post
        phase = {"drop": True}

        def drop_body_post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
            # Perform the real POST (server commits), then lose the response.
            resp = real_post(path, payload)
            if phase["drop"]:
                phase["drop"] = False
                raise AgentError("request failed: response body lost before parse")
            return resp

        agent._post = drop_body_post  # type: ignore[method-assign]
        with pytest.raises(AgentError, match="not acknowledged|body lost"):
            agent.submit_envelope(envelope)

        assert _event_count(controller, envelope.submission_id) == 1
        assert any(
            i.get("submission_id") == envelope.submission_id
            for i in agent.queue.peek_all()
        )

        # Retry without drop: product flush treats HTTP 200 as durable ACK.
        acked = agent.flush()
        assert acked == 1
        assert agent.queue.peek_all() == []
        assert _event_count(controller, envelope.submission_id) == 1
        # Second ingest is idempotent (created=false on wire).
        assert controller.store.ingest_result(envelope) is False
    finally:
        controller.close()
