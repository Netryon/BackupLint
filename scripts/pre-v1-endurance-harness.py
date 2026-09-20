#!/usr/bin/env python3
"""Real pre-v1 mixed-workload endurance harness (not a monitor stub).

Generates and verifies:
  fleet submit/heartbeat, dashboard reads, policy poll/apply ack,
  SIEM export with planned outage/recovery, rollout/rollback, drift
  injection, controller restart, and DB integrity checks.

Survives terminal disconnect when launched via systemd-run / nohup / setsid.
Does not declare production-ready by itself.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from backuplint.events import new_run_id
from backuplint.fleet.agent import AgentQueue, FleetAgent
from backuplint.fleet.controller import FleetController
from backuplint.fleet.dashboard.config import DashboardConfig
from backuplint.fleet.protocol import ResultEnvelope, new_submission_id
from backuplint.secrets import SecretRef, SecretSource
from backuplint.siem.config import SiemAuthType, SiemConfig
from backuplint.siem.transport.base import TransportResult


class ControllableSiemTransport:
    name = "pre-v1-endurance"

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.mode = "healthy"
        self.sent: list[str] = []
        self.attempts = 0
        self.retryable_failures = 0
        self.permanent_failures = 0

    def send(self, event: object, *, config: SiemConfig) -> TransportResult:
        with self.lock:
            self.attempts += 1
            mode = self.mode
        if mode == "healthy":
            with self.lock:
                self.sent.append(str(getattr(event, "event_id", "")))
            return TransportResult(True, False, 200, None, None)
        if mode == "outage":
            with self.lock:
                self.retryable_failures += 1
            return TransportResult(False, True, 503, None, "planned outage")
        with self.lock:
            self.permanent_failures += 1
        return TransportResult(False, False, 400, None, "permanent reject")


def _pct(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    ordered = sorted(vals)
    idx = max(0, min(len(ordered) - 1, int(len(ordered) * p) - 1))
    return ordered[idx]


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


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _append_jsonl(path: Path, row: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def _queue_submission_ids(agent: FleetAgent) -> set[str]:
    return {
        str(item.get("submission_id") or "")
        for item in agent.queue.peek_all()
        if item.get("submission_id")
    }


def _flush_and_collect_acks(agent: FleetAgent) -> set[str]:
    """Flush the durable queue and return submission IDs newly ACK'd.

    ``FleetAgent.flush`` can ACK previously failed queue items as a side effect.
    Exact accounting must credit those late ACKs. Partial failures still return
    whichever IDs left the queue (durable ACK rule).
    """
    before = _queue_submission_ids(agent)
    try:
        agent.flush()
    except Exception:  # noqa: BLE001
        # Queue membership after the attempt is authoritative.
        pass
    after = _queue_submission_ids(agent)
    return before - after


def _credit_acks(
    *,
    newly_acked: set[str],
    agent_id: str,
    current_sid: str,
    stats_lock: threading.Lock,
    stats: dict[str, int],
    confirmed_ids: set[str],
    account_path: Path,
    lat_ms: float | None = None,
    lat_submit: list[float] | None = None,
) -> None:
    rows: list[dict[str, object]] = []
    with stats_lock:
        if lat_ms is not None and lat_submit is not None:
            lat_submit.append(lat_ms)
        for acked_sid in newly_acked:
            if acked_sid in confirmed_ids:
                continue
            confirmed_ids.add(acked_sid)
            stats["delivery_confirmed"] += 1
            rows.append(
                {
                    "ts": datetime.now(UTC).isoformat(),
                    "kind": "submit_ack",
                    "submission_id": acked_sid,
                    "agent_id": agent_id,
                    "late_ack": acked_sid != current_sid,
                }
            )
    for row in rows:
        _append_jsonl(account_path, row)

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=1200, help="campaign duration")
    parser.add_argument("--agents", type=int, default=20)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--campaign-id", default="")
    args = parser.parse_args()

    os.environ.setdefault("BACKUPLINT_SIEM_TOKEN", "pre-v1-endurance-token")

    evidence = args.evidence_dir
    evidence.mkdir(parents=True, exist_ok=True)
    data_dir = args.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)

    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    campaign_id = args.campaign_id or f"pre-v1-{uuid.uuid4().hex[:12]}"
    started = datetime.now(UTC).isoformat()
    hardware = {
        "hostname": os.uname().nodename,
        "machine": os.uname().machine,
        "sysname": os.uname().sysname,
        "release": os.uname().release,
    }

    meta = {
        "campaign_id": campaign_id,
        "candidate_sha": sha,
        "started_at": started,
        "duration_target_s": args.seconds,
        "agents": args.agents,
        "hardware": hardware,
        "harness": "scripts/pre-v1-endurance-harness.py",
        "placeholder": False,
    }
    _write_json(evidence / "campaign-meta.json", meta)

    transport = ControllableSiemTransport()
    dashboard = DashboardConfig(enabled=True, cookie_secure=False)
    controller = FleetController(
        data_dir,
        hostname="127.0.0.1",
        dashboard=dashboard,
        siem=_siem_cfg(),
        max_inflight=64,
    )
    controller.dashboard_auth.set_password("pre-v1-endurance-pass")
    if controller._siem_exporter is not None:  # noqa: SLF001
        controller._siem_exporter.transport = transport  # noqa: SLF001

    host, port = controller.start(host="127.0.0.1", port=0)
    base = f"https://{host}:{port}"
    ca = controller.ca_dir / "ca.crt"
    _write_json(evidence / "controller.json", {"url": base, "port": port, "pid": os.getpid()})

    policy_a = controller.store.policy_create(
        display_name="endurance-a",
        description="primary",
        settings={"reporting": {"policy_poll_interval": "1m"}},
        created_by="endurance",
    )
    policy_b = controller.store.policy_create(
        display_name="endurance-b",
        description="rollback-target",
        settings={"reporting": {"policy_poll_interval": "2m"}},
        created_by="endurance",
    )

    agents: list[FleetAgent] = []
    identities = []
    for i in range(args.agents):
        pending = controller.create_pending_agent(label=f"e-{i}", ttl_hours=24)
        identity = FleetAgent.enroll_with_ca(
            controller_url=base,
            token=pending["token"],
            agent_id=pending["agent_id"],
            ca_cert=ca,
            identity_dir=data_dir / "agents" / f"a{i}",
            hostname=f"endurance-{i}",
        )
        identities.append(identity)
        controller.store.policy_assign_agent(identity.agent_id, policy_a.revision_id)
        agents.append(
            FleetAgent(
                controller_url=base,
                identity=identity,
                queue=AgentQueue(data_dir / "agents" / f"a{i}" / "q.jsonl"),
                sleep=lambda _s: None,
            )
        )

    member_ids = [a.identity.agent_id for a in agents[: max(1, args.agents // 2)]]
    rollout_id = controller.store.policy_rollout_create(
        revision_id=policy_b.revision_id,
        target_group_id=None,
        batch_size=max(1, args.agents // 4),
        max_concurrent=max(1, args.agents // 4),
        pause_between_batches_seconds=0,
        failure_threshold=max(1, args.agents // 2),
        created_by="endurance",
        agent_ids=member_ids,
    )
    controller.store.policy_rollout_start(rollout_id, actor="endurance")

    stats_lock = threading.Lock()
    stats: dict[str, int] = {
        "generated": 0,
        "delivery_confirmed": 0,
        "permanent_rejects": 0,
        "submit_flush_failures": 0,
        "hb_ok": 0,
        "hb_err": 0,
        "policy_poll_ok": 0,
        "policy_poll_err": 0,
        "policy_apply_ok": 0,
        "policy_apply_err": 0,
        "dashboard_ok": 0,
        "dashboard_err": 0,
        "siem_outage_events": 0,
        "restart_events": 0,
        "drift_injections": 0,
        "fault_events": 0,
    }
    # Per-submission lifecycle: generated IDs must end in exactly one sink.
    generated_ids: set[str] = set()
    confirmed_ids: set[str] = set()
    permanent_reject_ids: set[str] = set()
    lat_submit: list[float] = []
    lat_hb: list[float] = []
    lat_policy: list[float] = []
    lat_dash: list[float] = []
    stop = threading.Event()
    account_path = evidence / "accounting.jsonl"
    monitor_path = evidence / "monitor.jsonl"
    url_lock = threading.Lock()
    current_base = base

    def bump(key: str, n: int = 1) -> None:
        with stats_lock:
            stats[key] += n

    def worker(idx: int, agent: FleetAgent) -> None:
        while not stop.is_set():
            with url_lock:
                agent.controller_url = current_base
            try:
                t0 = time.perf_counter()
                agent.heartbeat(max_attempts=3)
                with stats_lock:
                    lat_hb.append((time.perf_counter() - t0) * 1000)
                bump("hb_ok")
            except Exception:  # noqa: BLE001
                bump("hb_err")

            try:
                t0 = time.perf_counter()
                env = ResultEnvelope(
                    agent_id=agent.identity.agent_id,
                    submission_id=new_submission_id(),
                    scan_time=datetime.now(UTC).isoformat(),
                    backuplint_version="0.8.0.dev0",
                    platform="pre-v1-endurance",
                    result={"summary": {"result": "PASS" if idx % 7 else "FAIL"}},
                    run_id=new_run_id(),
                )
                sid = str(env.submission_id)
                # Durable first: an ID is only "generated" once it is on the queue.
                agent.queue.enqueue(env)
                with stats_lock:
                    generated_ids.add(sid)
                    stats["generated"] += 1
                _append_jsonl(
                    account_path,
                    {
                        "ts": datetime.now(UTC).isoformat(),
                        "kind": "generated",
                        "submission_id": sid,
                        "agent_id": agent.identity.agent_id,
                    },
                )
                # Flush may ACK this ID and any previously unacked queue peers.
                newly_acked = _flush_and_collect_acks(agent)
                _credit_acks(
                    newly_acked=newly_acked,
                    agent_id=agent.identity.agent_id,
                    current_sid=sid,
                    stats_lock=stats_lock,
                    stats=stats,
                    confirmed_ids=confirmed_ids,
                    account_path=account_path,
                    lat_ms=(time.perf_counter() - t0) * 1000,
                    lat_submit=lat_submit,
                )
                # Still-queued IDs are not lost; they remain in remaining_queue.
                if sid in _queue_submission_ids(agent):
                    bump("submit_flush_failures")
                    _append_jsonl(
                        account_path,
                        {
                            "ts": datetime.now(UTC).isoformat(),
                            "kind": "queued_unacked",
                            "submission_id": sid,
                            "agent_id": agent.identity.agent_id,
                        },
                    )
            except Exception as exc:  # noqa: BLE001
                bump("submit_flush_failures")
                _append_jsonl(
                    account_path,
                    {
                        "ts": datetime.now(UTC).isoformat(),
                        "kind": "submit_exception",
                        "error": str(exc)[:200],
                        "agent_id": agent.identity.agent_id,
                    },
                )

            try:
                t0 = time.perf_counter()
                agent.fetch_desired_policy(max_attempts=3)
                with stats_lock:
                    lat_policy.append((time.perf_counter() - t0) * 1000)
                bump("policy_poll_ok")
            except Exception:  # noqa: BLE001
                bump("policy_poll_err")

            try:
                agent.poll_and_apply_policy(max_attempts=2)
                bump("policy_apply_ok")
            except Exception:  # noqa: BLE001
                bump("policy_apply_err")

            stop.wait(2.0)

    def dashboard_loop() -> None:
        while not stop.is_set():
            try:
                t0 = time.perf_counter()
                ctrl = ctrl_holder[0]
                ctrl.dashboard_query.fleet_overview()
                ctrl.dashboard_query.list_policy_drift(limit=20)
                with stats_lock:
                    lat_dash.append((time.perf_counter() - t0) * 1000)
                bump("dashboard_ok")
            except Exception:  # noqa: BLE001
                bump("dashboard_err")
            stop.wait(3.0)

    def fault_loop() -> None:
        nonlocal current_base
        phase = 0
        interval = max(20, args.seconds // 8)
        while not stop.wait(interval):
            ctrl = ctrl_holder[0]
            phase += 1
            bump("fault_events")
            if phase % 4 == 1:
                transport.mode = "outage"
                bump("siem_outage_events")
                _append_jsonl(
                    monitor_path,
                    {"ts": datetime.now(UTC).isoformat(), "fault": "siem_outage_begin"},
                )
            elif phase % 4 == 2:
                transport.mode = "healthy"
                _append_jsonl(
                    monitor_path,
                    {"ts": datetime.now(UTC).isoformat(), "fault": "siem_outage_end"},
                )
            elif phase % 4 == 3:
                agent0 = agents[0]
                try:
                    desired = ctrl.store.policy_get_desired(agent0.identity.agent_id)
                    ctrl.store.policy_report_applied(
                        agent0.identity.agent_id,
                        assignment_generation=int(
                            desired.get("assignment_generation") or 0
                        ),
                        revision_id=str(
                            desired.get("revision_id") or policy_a.revision_id
                        ),
                        content_sha256="0" * 64,
                        apply_status="APPLIED",
                        drift_status="IN_SYNC",
                        reason="endurance-drift-inject",
                        applied_at=datetime.now(UTC).isoformat(),
                    )
                    bump("drift_injections")
                    _append_jsonl(
                        monitor_path,
                        {"ts": datetime.now(UTC).isoformat(), "fault": "drift_inject"},
                    )
                except Exception as exc:  # noqa: BLE001
                    _append_jsonl(
                        monitor_path,
                        {
                            "ts": datetime.now(UTC).isoformat(),
                            "fault": "drift_inject_error",
                            "error": str(exc)[:200],
                        },
                    )
            else:
                try:
                    old_port = port
                    ctrl.stop()
                    ctrl.close()
                    new_ctrl = FleetController(
                        data_dir,
                        hostname="127.0.0.1",
                        dashboard=dashboard,
                        siem=_siem_cfg(),
                        max_inflight=64,
                    )
                    new_ctrl.dashboard_auth.set_password("pre-v1-endurance-pass")
                    if new_ctrl._siem_exporter is not None:  # noqa: SLF001
                        new_ctrl._siem_exporter.transport = transport  # noqa: SLF001
                    new_host, new_port = new_ctrl.start(
                        host="127.0.0.1", port=old_port
                    )
                    ctrl_holder[0] = new_ctrl
                    with url_lock:
                        current_base = f"https://{new_host}:{new_port}"
                    bump("restart_events")
                    _append_jsonl(
                        monitor_path,
                        {
                            "ts": datetime.now(UTC).isoformat(),
                            "fault": "controller_restart",
                            "url_after": current_base,
                        },
                    )
                except Exception as exc:  # noqa: BLE001
                    _append_jsonl(
                        monitor_path,
                        {
                            "ts": datetime.now(UTC).isoformat(),
                            "fault": "restart_error",
                            "error": str(exc)[:200],
                        },
                    )

            if phase == 2:
                try:
                    ctrl.store.policy_rollout_abort(rollout_id, actor="endurance")
                    for ident in identities:
                        ctrl.store.policy_assign_agent(
                            ident.agent_id, policy_a.revision_id
                        )
                    _append_jsonl(
                        monitor_path,
                        {
                            "ts": datetime.now(UTC).isoformat(),
                            "fault": "rollout_abort_rollback",
                        },
                    )
                except Exception as exc:  # noqa: BLE001
                    _append_jsonl(
                        monitor_path,
                        {
                            "ts": datetime.now(UTC).isoformat(),
                            "fault": "rollout_abort_error",
                            "error": str(exc)[:200],
                        },
                    )

    ctrl_holder = [controller]
    threads = [
        threading.Thread(target=worker, args=(i, a), daemon=True)
        for i, a in enumerate(agents)
    ]
    for t in threads:
        t.start()
    dash_t = threading.Thread(target=dashboard_loop, daemon=True)
    dash_t.start()
    fault_t = threading.Thread(target=fault_loop, daemon=True)
    fault_t.start()

    t0 = time.time()
    while time.time() - t0 < args.seconds and not stop.is_set():
        time.sleep(10)
        ctrl = ctrl_holder[0]
        diag = ctrl.store.diagnostics()
        with stats_lock:
            snap = dict(stats)
            submit_lat = list(lat_submit)
            hb_lat = list(lat_hb)
            pol_lat = list(lat_policy)
            dash_lat = list(lat_dash)
        remaining_q = 0
        for i in range(args.agents):
            qpath = data_dir / "agents" / f"a{i}" / "q.jsonl"
            if qpath.exists():
                remaining_q += sum(
                    1 for line in qpath.read_text().splitlines() if line.strip()
                )
        siem_snap: dict[str, object] = {}
        try:
            siem_snap = ctrl.siem_status_snapshot(persist=False)
        except Exception:  # noqa: BLE001
            siem_snap = {}
        with transport.lock:
            siem_delivered = len(transport.sent)
            siem_attempts = transport.attempts
            siem_retry = transport.retryable_failures
            siem_perm = transport.permanent_failures
        progress = {
            "ts": datetime.now(UTC).isoformat(),
            "elapsed_s": round(time.time() - t0, 1),
            "remaining_s": max(0, int(args.seconds - (time.time() - t0))),
            "stats": snap,
            "remaining_queue": remaining_q,
            "db_integrity_ok": bool(diag.integrity_ok),
            "latency_ms": {
                "submit_p50": _pct(submit_lat, 0.5),
                "submit_p95": _pct(submit_lat, 0.95),
                "submit_p99": _pct(submit_lat, 0.99),
                "hb_p95": _pct(hb_lat, 0.95),
                "policy_p95": _pct(pol_lat, 0.95),
                "dashboard_p95": _pct(dash_lat, 0.95),
            },
            "siem": {
                "delivered": siem_delivered,
                "attempts": siem_attempts,
                "retryable": siem_retry,
                "permanent": siem_perm,
                "mode": transport.mode,
                "telemetry": siem_snap,
            },
            "candidate_sha": sha,
            "campaign_id": campaign_id,
        }
        _write_json(evidence / "progress.json", progress)
        _append_jsonl(monitor_path, progress)
        print(
            f"PROGRESS elapsed={progress['elapsed_s']}s "
            f"gen={snap['generated']} ack={snap['delivery_confirmed']} "
            f"hb={snap['hb_ok']} dash={snap['dashboard_ok']} "
            f"pol={snap['policy_poll_ok']} siem_del={siem_delivered} "
            f"integrity={diag.integrity_ok}",
            flush=True,
        )

    stop.set()
    for t in threads:
        t.join(timeout=5)
    dash_t.join(timeout=5)
    fault_t.join(timeout=5)

    # Final drain: credit any late ACKs still sitting in durable queues.
    for agent in agents:
        with url_lock:
            agent.controller_url = current_base
        newly_acked = _flush_and_collect_acks(agent)
        _credit_acks(
            newly_acked=newly_acked,
            agent_id=agent.identity.agent_id,
            current_sid="",
            stats_lock=stats_lock,
            stats=stats,
            confirmed_ids=confirmed_ids,
            account_path=account_path,
        )

    ctrl = ctrl_holder[0]
    diag = ctrl.store.diagnostics()
    remaining_ids: set[str] = set()
    for agent in agents:
        remaining_ids |= _queue_submission_ids(agent)
    remaining_q = len(remaining_ids)

    with stats_lock:
        final_stats = dict(stats)
        submit_lat = list(lat_submit)
        hb_lat = list(lat_hb)
        pol_lat = list(lat_policy)
        dash_lat = list(lat_dash)
        gen_ids = set(generated_ids)
        conf_ids = set(confirmed_ids)
        rej_ids = set(permanent_reject_ids)

    # Exact fleet-submit invariant:
    #   generated = confirmed ∪ remaining_queue ∪ permanent_rejects
    # with pairwise-disjoint sinks (no double count, no silent loss).
    accounted_ids = conf_ids | remaining_ids | rej_ids
    unaccounted_ids = sorted(gen_ids - accounted_ids)
    double_counted = sorted(
        (conf_ids & remaining_ids)
        | (conf_ids & rej_ids)
        | (remaining_ids & rej_ids)
    )
    unknown_queue_ids = sorted(remaining_ids - gen_ids)

    with transport.lock:
        siem_final = {
            "delivered": len(transport.sent),
            "attempts": transport.attempts,
            "retryable": transport.retryable_failures,
            "permanent": transport.permanent_failures,
        }

    accounting = {
        "invariant": (
            "generated == delivery_confirmed + remaining_queue + permanent_rejects"
        ),
        "generated": len(gen_ids),
        "delivery_confirmed": len(conf_ids),
        "remaining_queue": remaining_q,
        "permanent_rejects": len(rej_ids),
        "accounted": len(accounted_ids),
        "unaccounted": len(unaccounted_ids),
        "unaccounted_ids_sample": unaccounted_ids[:20],
        "double_counted": len(double_counted),
        "double_counted_ids_sample": double_counted[:20],
        "unknown_queue_ids": len(unknown_queue_ids),
        "balanced": (
            len(unaccounted_ids) == 0
            and len(double_counted) == 0
            and len(unknown_queue_ids) == 0
            and len(gen_ids)
            == len(conf_ids) + remaining_q + len(rej_ids)
        ),
    }

    summary = {
        "campaign_id": campaign_id,
        "candidate_sha": sha,
        "started_at": started,
        "ended_at": datetime.now(UTC).isoformat(),
        "duration_s": round(time.time() - t0, 1),
        "duration_target_s": args.seconds,
        "hardware": hardware,
        "agents": args.agents,
        "generated_events": final_stats["generated"],
        "delivery_confirmed": final_stats["delivery_confirmed"],
        "permanent_rejects": final_stats["permanent_rejects"],
        "submit_flush_failures": final_stats["submit_flush_failures"],
        "remaining_queue": remaining_q,
        "in_flight": 0,
        "accounting": accounting,
        "hb_ok": final_stats["hb_ok"],
        "hb_err": final_stats["hb_err"],
        "dashboard_ok": final_stats["dashboard_ok"],
        "dashboard_err": final_stats["dashboard_err"],
        "policy_poll_ok": final_stats["policy_poll_ok"],
        "policy_poll_err": final_stats["policy_poll_err"],
        "policy_apply_ok": final_stats["policy_apply_ok"],
        "policy_apply_err": final_stats["policy_apply_err"],
        "siem": siem_final,
        "restart_events": final_stats["restart_events"],
        "drift_injections": final_stats["drift_injections"],
        "fault_events": final_stats["fault_events"],
        "siem_outage_events": final_stats["siem_outage_events"],
        "db_integrity_ok": bool(diag.integrity_ok),
        "latency_ms": {
            "submit_p50": _pct(submit_lat, 0.5),
            "submit_p95": _pct(submit_lat, 0.95),
            "submit_p99": _pct(submit_lat, 0.99),
            "hb_p50": _pct(hb_lat, 0.5),
            "hb_p95": _pct(hb_lat, 0.95),
            "hb_p99": _pct(hb_lat, 0.99),
            "policy_p95": _pct(pol_lat, 0.95),
            "dashboard_p95": _pct(dash_lat, 0.95),
            "submit_median": statistics.median(submit_lat) if submit_lat else 0.0,
        },
        "workload_actually_active": (
            final_stats["generated"] > 0
            and final_stats["hb_ok"] > 0
            and final_stats["dashboard_ok"] > 0
        ),
        "placeholder": False,
    }
    _write_json(evidence / "summary.json", summary)
    _write_json(evidence / "accounting-reconcile.json", accounting)
    print("SUMMARY " + json.dumps(summary, sort_keys=True), flush=True)
    print("ACCOUNTING " + json.dumps(accounting, sort_keys=True), flush=True)

    try:
        ctrl.stop()
        ctrl.close()
    except Exception:  # noqa: BLE001
        pass

    ok = (
        summary["workload_actually_active"]
        and summary["db_integrity_ok"]
        and int(summary["delivery_confirmed"]) > 0
        and bool(accounting["balanced"])
    )
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
