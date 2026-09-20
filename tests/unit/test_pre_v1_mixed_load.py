"""Bounded mixed-load pre-v1 concurrency campaign (not multi-day endurance)."""

from __future__ import annotations

import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

from backuplint.fleet.agent import AgentQueue, FleetAgent
from backuplint.fleet.controller import FleetController
from backuplint.fleet.dashboard.config import DashboardConfig
from backuplint.fleet.protocol import ResultEnvelope, new_submission_id


def test_pre_v1_mixed_load_campaign(tmp_path: Path) -> None:
    agents_n = 40
    controller = FleetController(
        tmp_path / "controller",
        hostname="localhost",
        dashboard=DashboardConfig(enabled=False),
        max_inflight=32,
    )
    _host, port = controller.start(host="127.0.0.1", port=0)
    base = f"https://127.0.0.1:{port}"
    ca = controller.ca_dir / "ca.crt"
    identities = []
    try:
        snap = controller.store.policy_create(
            display_name="mixed",
            description="",
            settings={"reporting": {"policy_poll_interval": "5m"}},
            created_by="mixed",
        )
        for i in range(agents_n):
            pending = controller.create_pending_agent(label=f"m-{i}", ttl_hours=1)
            identity = FleetAgent.enroll_with_ca(
                controller_url=base,
                token=pending["token"],
                agent_id=pending["agent_id"],
                ca_cert=ca,
                identity_dir=tmp_path / f"a-{i}",
                hostname=f"h-{i}",
            )
            identities.append(identity)
            controller.store.policy_assign_agent(identity.agent_id, snap.revision_id)

        submit_lat: list[float] = []
        hb_lat: list[float] = []
        pol_lat: list[float] = []
        submit_ok = hb_ok = pol_ok = 0
        submit_err = hb_err = pol_err = 0

        def one(identity: object) -> None:
            nonlocal submit_ok, hb_ok, pol_ok, submit_err, hb_err, pol_err
            agent = FleetAgent(
                controller_url=base,
                identity=identity,  # type: ignore[arg-type]
                queue=AgentQueue(tmp_path / f"q-{identity.agent_id}.jsonl"),  # type: ignore[attr-defined]
                sleep=lambda _s: None,
            )
            try:
                t0 = time.perf_counter()
                agent.heartbeat(max_attempts=3)
                hb_lat.append((time.perf_counter() - t0) * 1000)
                hb_ok += 1
            except Exception:  # noqa: BLE001
                hb_err += 1
            try:
                t0 = time.perf_counter()
                agent.submit_envelope(
                    ResultEnvelope(
                        agent_id=identity.agent_id,  # type: ignore[attr-defined]
                        submission_id=new_submission_id(),
                        scan_time=datetime.now(UTC).isoformat(),
                        backuplint_version="0.8.0.dev0",
                        platform="mixed",
                        result={"summary": {"result": "PASS"}},
                    )
                )
                submit_lat.append((time.perf_counter() - t0) * 1000)
                submit_ok += 1
            except Exception:  # noqa: BLE001
                submit_err += 1
            try:
                t0 = time.perf_counter()
                agent.fetch_desired_policy(max_attempts=3)
                pol_lat.append((time.perf_counter() - t0) * 1000)
                pol_ok += 1
            except Exception:  # noqa: BLE001
                pol_err += 1

        with ThreadPoolExecutor(max_workers=20) as pool:
            futs = [pool.submit(one, ident) for ident in identities]
            for fut in as_completed(futs):
                fut.result()

        # Dashboard reads concurrent with a few more heartbeats
        dash_ok = dash_err = 0
        for _ in range(20):
            try:
                controller.dashboard_query.fleet_overview()
                dash_ok += 1
            except Exception:  # noqa: BLE001
                dash_err += 1

        # Soft restart
        controller.stop()
        controller.close()
        controller = FleetController(
            tmp_path / "controller",
            hostname="localhost",
            dashboard=DashboardConfig(enabled=False),
        )
        controller.start(host="127.0.0.1", port=0)
        diag = controller.store.diagnostics()
        assert diag.integrity_ok is True
        assert submit_ok >= agents_n * 0.8
        assert hb_ok >= agents_n * 0.8
        assert pol_ok >= agents_n * 0.8
        assert dash_ok >= 15

        def pct(vals: list[float], p: float) -> float:
            if not vals:
                return 0.0
            s = sorted(vals)
            return s[max(0, int(len(s) * p) - 1)]

        print(
            "MIXED_LOAD "
            f"submit={submit_ok}/{submit_err} hb={hb_ok}/{hb_err} "
            f"policy={pol_ok}/{pol_err} dash={dash_ok}/{dash_err} "
            f"submit_p50={pct(submit_lat,0.5):.2f} submit_p95={pct(submit_lat,0.95):.2f} "
            f"submit_p99={pct(submit_lat,0.99):.2f} "
            f"hb_p95={pct(hb_lat,0.95):.2f} pol_p95={pct(pol_lat,0.95):.2f}"
        )
        _ = statistics.median(submit_lat or [0.0])
    finally:
        controller.stop()
        controller.close()
