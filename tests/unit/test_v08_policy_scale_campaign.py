"""v0.8 bounded policy scale campaign (control-plane, not endurance)."""

from __future__ import annotations

import statistics
import time
from datetime import UTC, datetime
from pathlib import Path

from backuplint.fleet.agent import AgentQueue, FleetAgent
from backuplint.fleet.controller import FleetController
from backuplint.fleet.policy_apply import ManagedPolicyDir, apply_policy_response
from backuplint.fleet.protocol import ResultEnvelope, new_submission_id


def _settings(rev: int = 0) -> dict[str, object]:
    return {
        "reporting": {"policy_poll_interval": "5m"},
        "queue": {"max_items": 50 + rev},
    }


def test_policy_scale_campaign_100_agents(tmp_path: Path) -> None:
    agent_count = 100
    controller = FleetController(tmp_path / "controller", hostname="localhost")
    _host, port = controller.start(host="127.0.0.1", port=0)
    base = f"https://127.0.0.1:{port}"
    ca = controller.ca_dir / "ca.crt"
    latencies: list[float] = []
    poll_ok = 0
    poll_err = 0
    no_change = 0
    downloads = 0
    apply_ok = 0
    apply_fail = 0
    identities: list[tuple[object, Path]] = []
    try:
        group_ids = [
            controller.store.policy_group_create(display_name=f"g-{i}")
            for i in range(4)
        ]
        snap_a = controller.store.policy_create(
            display_name="base",
            description="",
            settings=_settings(0),
            created_by="scale",
        )
        snap_b = controller.store.policy_create_revision(
            snap_a.policy_id,
            display_name="canary",
            description="",
            settings=_settings(1),
            created_by="scale",
        )
        for i in range(agent_count):
            pending = controller.create_pending_agent(label=f"p-{i}", ttl_hours=1)
            identity = FleetAgent.enroll_with_ca(
                controller_url=base,
                token=pending["token"],
                agent_id=pending["agent_id"],
                ca_cert=ca,
                identity_dir=tmp_path / f"agent-{i}",
                hostname=f"h-{i}",
            )
            identities.append((identity, tmp_path / f"agent-{i}"))
            controller.store.policy_group_add_member(group_ids[i % 4], identity.agent_id)
            # Also submit a heartbeat-style result so fleet path stays warm.
            agent = FleetAgent(
                controller_url=base,
                identity=identity,
                queue=AgentQueue(tmp_path / f"agent-{i}" / "q.jsonl"),
            )
            agent.submit_envelope(
                ResultEnvelope(
                    agent_id=identity.agent_id,
                    submission_id=new_submission_id(),
                    scan_time=datetime.now(UTC).isoformat(),
                    backuplint_version="0.8.0.dev0",
                    platform="policy-scale",
                    result={"summary": {"result": "PASS"}},
                )
            )

        for gid in group_ids[:2]:
            controller.store.policy_assign_group(gid, snap_a.revision_id)
        for gid in group_ids[2:]:
            controller.store.policy_assign_group(gid, snap_b.revision_id)

        rollout_id = controller.store.policy_rollout_create(
            revision_id=snap_b.revision_id,
            target_group_id=group_ids[0],
            batch_size=10,
            max_concurrent=10,
            pause_between_batches_seconds=0,
            failure_threshold=20,
            created_by="scale",
        )
        controller.store.policy_rollout_start(rollout_id)

        # Simulate restart mid-rollout.
        controller.stop()
        controller.close()
        controller = FleetController(tmp_path / "controller", hostname="localhost")
        _host, port = controller.start(host="127.0.0.1", port=0)
        base = f"https://127.0.0.1:{port}"
        rollout = controller.store.policy_rollout_get(rollout_id)
        assert rollout["status"] in {"running", "completed", "paused", "pending"}

        for identity, root in identities:
            agent = FleetAgent(
                controller_url=base,
                identity=identity,
                queue=AgentQueue(root / "q.jsonl"),
                sleep=lambda _s: None,
            )
            started = time.perf_counter()
            try:
                desired = agent.fetch_desired_policy()
                poll_ok += 1
            except Exception:  # noqa: BLE001
                poll_err += 1
                continue
            latencies.append((time.perf_counter() - started) * 1000.0)
            status = str(desired.get("status") or "")
            if status == "NO_CHANGE":
                no_change += 1
                continue
            if status == "NO_ASSIGNMENT":
                continue
            downloads += 1
            managed = ManagedPolicyDir(root / "managed-policy")
            try:
                applied = apply_policy_response(
                    managed,
                    desired,
                    applied_at=datetime.now(UTC).isoformat(),
                )
                if applied.apply_status == "success":
                    apply_ok += 1
                    agent._post(  # noqa: SLF001
                        "/v1/policy/applied",
                        {
                            "assignment_generation": applied.assignment_generation,
                            "revision_id": applied.revision_id,
                            "content_sha256": applied.content_sha256,
                            "apply_status": applied.apply_status,
                            "drift_status": applied.drift_status,
                            "reason": applied.reason,
                            "applied_at": applied.applied_at,
                            "local_config_sha256": applied.local_config_sha256,
                        },
                    )
                else:
                    apply_fail += 1
            except Exception:  # noqa: BLE001
                apply_fail += 1

        # Second poll should mostly NO_CHANGE for applied agents.
        for identity, root in identities[:20]:
            agent = FleetAgent(
                controller_url=base,
                identity=identity,
                queue=AgentQueue(tmp_path / "noop.jsonl"),
                sleep=lambda _s: None,
            )
            managed = ManagedPolicyDir(root / "managed-policy")
            state = managed.load_state()
            if state.applied is None:
                continue
            desired = agent.fetch_desired_policy(
                current_assignment_generation=state.applied.assignment_generation,
                current_revision_id=state.applied.revision_id,
                current_content_sha256=state.applied.content_sha256,
            )
            if desired.get("status") == "NO_CHANGE":
                no_change += 1

        integrity = controller.store.diagnostics()
        assert poll_ok >= agent_count * 0.9
        assert apply_ok >= 1
        assert integrity.integrity_ok is True
        assert integrity.schema_version == 7
        if latencies:
            p50 = statistics.median(latencies)
            sorted_l = sorted(latencies)
            p95 = sorted_l[max(0, int(len(sorted_l) * 0.95) - 1)]
            p99 = sorted_l[max(0, int(len(sorted_l) * 0.99) - 1)]
            assert p95 < 5000.0
            assert p50 >= 0 and p99 >= p95
            # Surfaced for completion report via pytest -s if needed.
            print(
                f"SCALE_METRICS poll_ok={poll_ok} poll_err={poll_err} "
                f"no_change={no_change} downloads={downloads} "
                f"apply_ok={apply_ok} apply_fail={apply_fail} "
                f"p50={p50:.2f} p95={p95:.2f} p99={p99:.2f}"
            )

        # Drift subset: reassign without apply.
        controller.store.policy_assign_agent(
            identities[0][0].agent_id, snap_a.revision_id
        )
        drift = controller.store.policy_list_drift(limit=10)
        assert drift

        # Rollback subset.
        gen = controller.store.policy_rollback_agent(
            identities[1][0].agent_id, snap_a.revision_id
        )
        assert gen >= 1
    finally:
        controller.stop()
        controller.close()
