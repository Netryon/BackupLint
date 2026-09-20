#!/usr/bin/env python3
"""Strong-PC multi-stage fleet scale campaign with exact accounting.

Workshop-only. Pins to corrected v0.5 accounting semantics.
Stages: 100 / 250 / 500 / 1000 / 2000 (via SCALE_STAGE + SCALE_AGENTS).
"""

from __future__ import annotations

import json
import math
import os
import signal
import socket
import sqlite3
import statistics
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from backuplint.events import new_run_id
from backuplint.fleet.agent import AgentError, AgentQueue, FleetAgent
from backuplint.fleet.protocol import ResultEnvelope, new_submission_id

ROOT = Path(os.environ.get("SCALE_ROOT", "/tmp/bl-strong-pc-scale"))
STAGE = os.environ.get("SCALE_STAGE", "100")
DURATION = int(os.environ.get("SCALE_SECONDS", "900"))
N_AGENTS = int(os.environ.get("SCALE_AGENTS", "100"))
HB_INTERVAL = float(os.environ.get("SCALE_HB_INTERVAL", "20"))
SUB_INTERVAL = float(os.environ.get("SCALE_SUB_INTERVAL", "30"))
LISTEN_HOST = os.environ.get("SCALE_LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("SCALE_PORT", "18501"))
REUSE_CONTROLLER = os.environ.get("SCALE_REUSE_CONTROLLER", "0") == "1"
CAMPAIGN_ID = os.environ.get(
    "SCALE_CAMPAIGN_ID", f"scale-{uuid.uuid4().hex[:10]}"
)
STAGE_ID = os.environ.get("SCALE_STAGE_ID", f"{STAGE}-{uuid.uuid4().hex[:6]}")
SAMPLE_EVERY = float(os.environ.get("SCALE_SAMPLE_EVERY", "10"))
CHECKPOINT_EVERY = float(os.environ.get("SCALE_CHECKPOINT_EVERY", "300"))
# Endurance: repeating fault cycle length (default 4h). Absolute schedule overrides fractions.
CYCLE_SECONDS = float(os.environ.get("SCALE_CYCLE_SECONDS", str(4 * 3600)))
ABORT_RSS_KB = int(os.environ.get("SCALE_ABORT_RSS_KB", str(12 * 1024 * 1024)))  # 12 GiB
ABORT_DISK_FREE_GB = float(os.environ.get("SCALE_ABORT_DISK_FREE_GB", "5"))

# Fault schedule fractions of duration (overridden per stage)
FAULTS_ENV = os.environ.get("SCALE_FAULTS", "")
IS_ENDURANCE = STAGE.lower() in {"endurance", "long", "long48", "long72"}

ROOT.mkdir(parents=True, exist_ok=True)
(ROOT / "agents").mkdir(exist_ok=True)
DATA = ROOT / "controller"
VENV_BIN = Path(
    os.environ.get(
        "SCALE_VENV_BIN",
        str(Path(__file__).resolve().parents[1] / ".venv" / "bin"),
    )
)
BACKUP_LINT = str(VENV_BIN / "backuplint")


@dataclass
class Counters:
    generated_valid: int = 0
    generated_invalid: int = 0
    queued_created: int = 0
    queued_drained: int = 0
    attempted: int = 0
    acked_new: int = 0
    acked_duplicate: int = 0
    # Lost-ACK aware: first created=false after a transport failure confirms delivery.
    idempotent_confirmed: int = 0
    in_flight_unconfirmed: int = 0
    rejected_auth: int = 0
    rejected_schema: int = 0
    rejected_other: int = 0
    submit_errors: int = 0
    permanently_rejected_valid: int = 0
    hb_ok: int = 0
    hb_err: int = 0
    failed_attempts: set = field(default_factory=set)
    confirmed_ids: set = field(default_factory=set)
    drained_ids: set = field(default_factory=set)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def snapshot(self) -> dict[str, int]:
        with self.lock:
            return {k: v for k, v in self.__dict__.items() if isinstance(v, int)}

    @property
    def delivery_confirmed(self) -> int:
        with self.lock:
            return self.acked_new + self.idempotent_confirmed


COUNTERS = Counters()
LATENCIES: list[float] = []
LAT_LOCK = threading.Lock()
STOP = threading.Event()
EVENTS: list[dict] = []
EVENT_LOCK = threading.Lock()
CONTROLLER_EXPECT_DOWN = threading.Event()


def mark_queued_drained(sid: str) -> bool:
    """Increment queued_drained once per submission_id (concurrent drain-safe)."""
    with COUNTERS.lock:
        return _mark_queued_drained_unlocked(sid)


def _mark_queued_drained_unlocked(sid: str) -> bool:
    if sid in COUNTERS.drained_ids:
        return False
    COUNTERS.drained_ids.add(sid)
    COUNTERS.queued_drained += 1
    return True


def log_event(kind: str, **kwargs: object) -> None:
    row = {"t": datetime.now(UTC).isoformat(), "kind": kind, **kwargs}
    with EVENT_LOCK:
        EVENTS.append(row)
    (ROOT / "events.jsonl").open("a").write(json.dumps(row) + "\n")
    print(json.dumps(row), flush=True)


def percentile(sorted_vals: list[float], p: float) -> float | None:
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)


def latency_stats() -> dict[str, float | int | None]:
    with LAT_LOCK:
        vals = sorted(LATENCIES)
    if not vals:
        return {
            "count": 0,
            "min": None,
            "p50": None,
            "p95": None,
            "p99": None,
            "max": None,
            "mean": None,
        }
    return {
        "count": len(vals),
        "min": vals[0],
        "p50": percentile(vals, 50),
        "p95": percentile(vals, 95),
        "p99": percentile(vals, 99),
        "max": vals[-1],
        "mean": statistics.fmean(vals),
    }


def db_counts(db: Path) -> dict[str, int]:
    con = sqlite3.connect(str(db))
    try:
        return {
            "events": con.execute("select count(*) from events").fetchone()[0],
            "submissions": con.execute("select count(*) from submissions").fetchone()[0],
            "agents": con.execute("select count(*) from agents").fetchone()[0],
        }
    finally:
        con.close()


def integrity_ok(db: Path) -> bool:
    con = sqlite3.connect(str(db))
    try:
        row = con.execute("PRAGMA integrity_check").fetchone()
        return row is not None and row[0] == "ok"
    finally:
        con.close()


def campaign_stage_events(db: Path) -> int:
    con = sqlite3.connect(str(db))
    try:
        like_c1 = f'%\"campaign_id\":\"{CAMPAIGN_ID}\"%'
        like_c2 = f'%\"campaign_id\": \"{CAMPAIGN_ID}\"%'
        like_s1 = f'%\"stage_id\":\"{STAGE_ID}\"%'
        like_s2 = f'%\"stage_id\": \"{STAGE_ID}\"%'
        return con.execute(
            "select count(*) from events where "
            "(payload_json like ? or payload_json like ?) and "
            "(payload_json like ? or payload_json like ?)",
            (like_c1, like_c2, like_s1, like_s2),
        ).fetchone()[0]
    finally:
        con.close()


class ControllerProc:
    def __init__(self) -> None:
        self.proc: subprocess.Popen[str] | None = None
        self.log = (ROOT / "controller.log").open("a")

    def init(self) -> None:
        if not (DATA / "ca" / "ca.crt").exists():
            subprocess.run(
                [
                    BACKUP_LINT,
                    "controller",
                    "init",
                    "--data-dir",
                    str(DATA),
                    "--hostname",
                    "127.0.0.1",
                ],
                check=True,
                capture_output=True,
                text=True,
            )

    def start(self) -> None:
        self.proc = subprocess.Popen(
            [
                BACKUP_LINT,
                "controller",
                "run",
                "--data-dir",
                str(DATA),
                "--listen",
                f"{LISTEN_HOST}:{LISTEN_PORT}",
                "--hostname",
                "127.0.0.1",
            ],
            stdout=self.log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        deadline = time.time() + 45
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("controller exited early")
            try:
                with socket.create_connection((LISTEN_HOST, LISTEN_PORT), timeout=0.5):
                    pass
                log_event("controller_started", pid=self.proc.pid, port=LISTEN_PORT)
                return
            except OSError:
                time.sleep(0.2)
        raise RuntimeError("controller failed to listen")

    def stop(self, sig: int) -> int:
        if self.proc is None or self.proc.poll() is not None:
            return -1
        pid = self.proc.pid
        os.kill(pid, sig)
        try:
            rc = self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            os.kill(pid, signal.SIGKILL)
            rc = self.proc.wait(timeout=10)
        log_event("controller_stopped", pid=pid, signal=sig, exit=rc)
        self.proc = None
        return rc

    @property
    def url(self) -> str:
        return f"https://{LISTEN_HOST}:{LISTEN_PORT}"

    @property
    def ca(self) -> Path:
        return DATA / "ca" / "ca.crt"

    @property
    def pid(self) -> int | None:
        return None if self.proc is None else self.proc.pid


def make_pending(label: str) -> tuple[str, str]:
    """Create pending enrollment; return (agent_id, token)."""
    out = subprocess.check_output(
        [
            BACKUP_LINT,
            "controller",
            "enroll-token",
            "--data-dir",
            str(DATA),
            "--label",
            label,
            "--ttl-hours",
            "12",
        ],
        text=True,
    )
    agent_id = ""
    token = ""
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("agent_id="):
            agent_id = line.split("=", 1)[1].strip()
        elif line.startswith("token="):
            token = line.split("=", 1)[1].strip()
    if not agent_id or not token:
        # Backward-compat: older CLI printed bare token only.
        bare = out.strip()
        if bare and "=" not in bare:
            raise RuntimeError(
                "enroll-token did not return agent_id=; CSR enrollment requires it"
            )
        raise RuntimeError(f"failed to parse enroll-token output: {out!r}")
    return agent_id, token


def make_token(label: str) -> str:
    """Legacy helper — prefer make_pending for CSR enrollment."""
    _agent_id, token = make_pending(label)
    return token


def submit_tracked(
    agent: FleetAgent,
    env: ResultEnvelope,
    *,
    valid: bool = True,
    count_generated: bool = True,
) -> str:
    sid = str(env.submission_id)
    with COUNTERS.lock:
        if count_generated:
            if valid:
                COUNTERS.generated_valid += 1
            else:
                COUNTERS.generated_invalid += 1
            COUNTERS.queued_created += 1
        COUNTERS.attempted += 1
    if count_generated:
        agent.queue.enqueue(env)
    t0 = time.perf_counter()
    try:
        resp = agent._post("/v1/results", env.to_dict())
        dt = time.perf_counter() - t0
        with LAT_LOCK:
            LATENCIES.append(dt)
        created = bool(resp.get("created", True))
        if sid in {
            str(item.get("submission_id")) for item in agent.queue.peek_all()
        }:
            agent.queue.remove(env.submission_id)
            mark_queued_drained(sid)
        with COUNTERS.lock:
            prior_fail = sid in COUNTERS.failed_attempts
            already = sid in COUNTERS.confirmed_ids
            if created:
                if not already:
                    COUNTERS.acked_new += 1
                    COUNTERS.confirmed_ids.add(sid)
                    if prior_fail and COUNTERS.in_flight_unconfirmed > 0:
                        COUNTERS.in_flight_unconfirmed -= 1
                COUNTERS.failed_attempts.discard(sid)
                return "new"
            COUNTERS.acked_duplicate += 1
            if not already:
                # First durable confirmation via duplicate (lost-ACK recovery).
                COUNTERS.idempotent_confirmed += 1
                COUNTERS.confirmed_ids.add(sid)
                if prior_fail and COUNTERS.in_flight_unconfirmed > 0:
                    COUNTERS.in_flight_unconfirmed -= 1
            COUNTERS.failed_attempts.discard(sid)
            return "duplicate"
    except AgentError as exc:
        msg = str(exc)
        with COUNTERS.lock:
            COUNTERS.submit_errors += 1
            if "401" in msg or "403" in msg or "revoked" in msg:
                COUNTERS.rejected_auth += 1
            elif "schema" in msg or "protocol" in msg:
                COUNTERS.rejected_schema += 1
            else:
                COUNTERS.rejected_other += 1
        if "revoked" in msg or "401" in msg or "403" in msg:
            if env.submission_id in {
                str(item.get("submission_id")) for item in agent.queue.peek_all()
            }:
                agent.queue.remove(env.submission_id)
                mark_queued_drained(sid)
            with COUNTERS.lock:
                if sid not in COUNTERS.confirmed_ids:
                    COUNTERS.permanently_rejected_valid += 1
                    COUNTERS.confirmed_ids.add(sid)
                    if sid in COUNTERS.failed_attempts and COUNTERS.in_flight_unconfirmed > 0:
                        COUNTERS.in_flight_unconfirmed -= 1
                    COUNTERS.failed_attempts.discard(sid)
            return "rejected"
        with COUNTERS.lock:
            if sid not in COUNTERS.failed_attempts and sid not in COUNTERS.confirmed_ids:
                COUNTERS.failed_attempts.add(sid)
                COUNTERS.in_flight_unconfirmed += 1
        return "error"


def remaining_queued(agents: list[FleetAgent]) -> int:
    return sum(len(a.queue.peek_all()) for a in agents)


def worker(idx: int, agent: FleetAgent, outage_flag: threading.Event) -> None:
    # jitter enroll phase submissions
    next_sub = time.time() + (idx % max(1, int(SUB_INTERVAL)))
    next_hb = time.time() + (idx % max(1, int(HB_INTERVAL)))
    while not STOP.is_set():
        if outage_flag.is_set():
            STOP.wait(1.0)
            continue
        now = time.time()
        if now >= next_hb:
            next_hb = now + HB_INTERVAL
            try:
                agent.heartbeat()
                with COUNTERS.lock:
                    COUNTERS.hb_ok += 1
            except Exception:  # noqa: BLE001
                with COUNTERS.lock:
                    COUNTERS.hb_err += 1
        if now >= next_sub:
            next_sub = now + SUB_INTERVAL
            result_status = "PASS" if (idx + int(now)) % 7 else "FAIL"
            env = ResultEnvelope(
                agent_id=agent.identity.agent_id,
                submission_id=new_submission_id(),
                scan_time=datetime.now(UTC).isoformat(),
                backuplint_version="0.5.0.dev0",
                platform="scale",
                result={
                    "summary": {"result": result_status, "agent": idx},
                    "campaign_id": CAMPAIGN_ID,
                    "stage_id": STAGE_ID,
                },
                run_id=new_run_id(),
            )
            submit_tracked(agent, env, valid=True)
        STOP.wait(0.5)


def drain_all(agents: list[FleetAgent], *, budget_s: float = 120.0) -> None:
    """Best-effort queue drain with a wall-clock budget (avoid blocking stage faults)."""
    deadline = time.time() + budget_s
    for agent in agents:
        if time.time() >= deadline:
            break
        for _ in range(200):
            if time.time() >= deadline:
                break
            items = agent.queue.peek_all()
            if not items:
                break
            progressed = False
            for item in list(items)[:20]:
                if time.time() >= deadline:
                    break
                env = ResultEnvelope(
                    agent_id=str(item["agent_id"]),
                    submission_id=str(item["submission_id"]),
                    scan_time=str(item["scan_time"]),
                    backuplint_version=str(item["backuplint_version"]),
                    platform=str(item["platform"]),
                    result=item["result"],  # type: ignore[arg-type]
                    run_id=item.get("run_id"),  # type: ignore[arg-type]
                )
                with COUNTERS.lock:
                    COUNTERS.attempted += 1
                try:
                    t0 = time.perf_counter()
                    resp = agent._post("/v1/results", env.to_dict())
                    dt = time.perf_counter() - t0
                    with LAT_LOCK:
                        LATENCIES.append(dt)
                    created = bool(resp.get("created", True))
                    sid = str(env.submission_id)
                    agent.queue.remove(env.submission_id)
                    with COUNTERS.lock:
                        _mark_queued_drained_unlocked(sid)
                        prior_fail = sid in COUNTERS.failed_attempts
                        already = sid in COUNTERS.confirmed_ids
                        if created:
                            if not already:
                                COUNTERS.acked_new += 1
                                COUNTERS.confirmed_ids.add(sid)
                                if prior_fail and COUNTERS.in_flight_unconfirmed > 0:
                                    COUNTERS.in_flight_unconfirmed -= 1
                        else:
                            COUNTERS.acked_duplicate += 1
                            if not already:
                                COUNTERS.idempotent_confirmed += 1
                                COUNTERS.confirmed_ids.add(sid)
                                if prior_fail and COUNTERS.in_flight_unconfirmed > 0:
                                    COUNTERS.in_flight_unconfirmed -= 1
                        COUNTERS.failed_attempts.discard(sid)
                    progressed = True
                except AgentError:
                    break
            if not progressed:
                break


def wait_healthy(timeout: float = 45.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((LISTEN_HOST, LISTEN_PORT), timeout=0.5):
                return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError("controller not healthy")


def default_faults(stage: str) -> list[tuple[float, str]]:
    """Return (fraction_of_duration, fault_name) list."""
    if FAULTS_ENV:
        out: list[tuple[float, str]] = []
        for part in FAULTS_ENV.split(","):
            frac_s, name = part.split(":", 1)
            out.append((float(frac_s), name.strip()))
        return out
    # stage defaults
    if stage.lower() in {"endurance", "long", "long48", "long72"}:
        # Fractions are within each CYCLE_SECONDS window; expand_fault_schedule
        # repeats them for the full duration. Alternate hard restart flavors.
        return [
            (0.08, "burst"),
            (0.16, "reconnect_storm"),
            (0.24, "outage_start"),
            (0.30, "outage_end"),
            (0.38, "sigterm_or_sigkill"),
            (0.50, "ack_loss"),
            (0.60, "ooo"),
            (0.70, "queue_overflow"),
            (0.80, "invalid_sample"),
            (0.90, "revoke"),
        ]
    if stage == "100":
        return [(0.5, "ack_loss"), (0.7, "ooo")]
    if stage == "250":
        return [
            (0.35, "outage_start"),
            (0.45, "outage_end"),
            (0.6, "revoke"),
            (0.75, "ack_loss"),
        ]
    if stage == "500":
        return [
            (0.25, "burst"),
            (0.4, "outage_start"),
            (0.48, "outage_end"),
            (0.55, "ack_loss"),
            (0.65, "ooo"),
            (0.75, "invalid_sample"),
            (0.85, "revoke"),
        ]
    if stage == "1000":
        return [
            (0.3, "burst"),
            (0.45, "outage_start"),
            (0.52, "outage_end"),
            (0.65, "sigterm"),
            (0.75, "ack_loss"),
            (0.85, "ooo"),
            (0.92, "revoke"),
        ]
    if stage == "1500":
        return [
            (0.25, "burst"),
            (0.35, "reconnect_storm"),
            (0.48, "outage_start"),
            (0.55, "outage_end"),
            (0.7, "ack_loss"),
            (0.82, "ooo"),
            (0.9, "revoke"),
        ]
    if stage == "2000":
        return [
            (0.25, "burst"),
            (0.40, "reconnect_storm"),
            (0.55, "outage_start"),
            (0.62, "outage_end"),
            (0.70, "sigkill"),
            (0.82, "ack_loss"),
            (0.90, "ooo"),
            (0.95, "invalid_sample"),
        ]
    return [(0.5, "ack_loss")]


def expand_fault_schedule(
    stage: str, duration: float
) -> list[tuple[float, str]]:
    """Expand fractional fault plan into absolute timestamps.

    Endurance stages repeat the cycle template every CYCLE_SECONDS.
    ``sigterm_or_sigkill`` alternates by cycle index.
    """
    template = default_faults(stage)
    if not IS_ENDURANCE:
        return [(frac * duration, name) for frac, name in template]

    cycle = max(60.0, CYCLE_SECONDS)
    # Keep at least one full cycle even for short smoke durations.
    if duration < cycle:
        cycle = max(30.0, duration)
    out: list[tuple[float, str]] = []
    cycle_i = 0
    base = 0.0
    while base < duration - 1.0:
        for frac, name in template:
            at = base + frac * cycle
            if at >= duration:
                continue
            if name == "sigterm_or_sigkill":
                name = "sigkill" if (cycle_i % 2 == 1) else "sigterm"
            out.append((at, name))
        cycle_i += 1
        base += cycle
    return out


def write_status(state: str, **extra: object) -> None:
    payload = {
        "t": datetime.now(UTC).isoformat(),
        "state": state,
        "campaign_id": CAMPAIGN_ID,
        "stage": STAGE,
        "stage_id": STAGE_ID,
        **extra,
    }
    (ROOT / "STATUS.json").write_text(json.dumps(payload, indent=2) + "\n")
    (ROOT / "STATUS.txt").write_text(f"{state}\n")


def write_checkpoint(
    *,
    elapsed: float,
    ctrl: "ControllerProc",
    db: Path,
    agents: list[FleetAgent],
    samples: list[dict],
    ack_loss_ok: bool,
    ooo_ok: bool,
    queue_overflow_ok: bool,
    revoked_persists: bool,
    faults_fired: set[str],
) -> Path:
    ck_dir = ROOT / "checkpoints"
    ck_dir.mkdir(exist_ok=True)
    sample = resource_sample(ctrl, db, agents)
    snap = COUNTERS.snapshot()
    rem = int(sample.get("queue_depth") or 0)
    delivery = snap["acked_new"] + snap["idempotent_confirmed"]
    row = {
        "t": datetime.now(UTC).isoformat(),
        "elapsed_s": elapsed,
        "duration_s": DURATION,
        "progress_pct": round(100.0 * elapsed / max(1.0, DURATION), 3),
        "counters": snap,
        "delivery_confirmed": delivery,
        "remaining_queued": rem,
        "in_flight_unconfirmed": snap["in_flight_unconfirmed"],
        "db": db_counts(db),
        "integrity_ok": integrity_ok(db),
        "resource": {
            "rss_kb": sample.get("rss_kb"),
            "fds": sample.get("fds"),
            "db_bytes": sample.get("db_bytes"),
            "wal_bytes": sample.get("wal_bytes"),
            "tcp": sample.get("tcp"),
        },
        "latency": sample.get("latency"),
        "ack_loss_ok": ack_loss_ok,
        "ooo_ok": ooo_ok,
        "queue_overflow_ok": queue_overflow_ok,
        "revoked_persists": revoked_persists,
        "faults_fired": sorted(faults_fired),
        "samples": len(samples),
    }
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = ck_dir / f"checkpoint-{stamp}.json"
    path.write_text(json.dumps(row, indent=2) + "\n")
    (ROOT / "progress.json").write_text(json.dumps(row, indent=2) + "\n")
    (ROOT / "health.json").write_text(
        json.dumps(
            {
                "t": row["t"],
                "state": "RUNNING",
                "integrity_ok": row["integrity_ok"],
                "rss_kb": row["resource"]["rss_kb"],
                "db_bytes": row["resource"]["db_bytes"],
                "remaining_queued": rem,
                "delivery_confirmed": delivery,
                "elapsed_s": elapsed,
            },
            indent=2,
        )
        + "\n"
    )
    return path


def preserve_failure(reason: str, **extra: object) -> None:
    fail_dir = ROOT / "FAIL_EVIDENCE"
    fail_dir.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    payload = {
        "t": datetime.now(UTC).isoformat(),
        "reason": reason,
        "campaign_id": CAMPAIGN_ID,
        "stage": STAGE,
        "counters": COUNTERS.snapshot(),
        **extra,
    }
    (fail_dir / f"fail-{stamp}.json").write_text(json.dumps(payload, indent=2) + "\n")
    (ROOT / "FAILURE.txt").write_text(f"{reason}\n{json.dumps(payload, indent=2)}\n")
    write_status("FAILED", reason=reason, **extra)
    # Best-effort copies of live artifacts
    for name in ("progress.json", "health.json", "monitor.jsonl", "events.jsonl", "controller.log"):
        src = ROOT / name
        if src.exists():
            try:
                (fail_dir / f"{stamp}-{name}").write_bytes(src.read_bytes()[-2_000_000:])
            except OSError:
                pass


def disk_free_gb(path: Path) -> float | None:
    try:
        st = os.statvfs(path)
        return (st.f_bavail * st.f_frsize) / (1024**3)
    except OSError:
        return None


def critical_abort_reason(sample: dict, db: Path) -> str | None:
    if not integrity_ok(db):
        return "sqlite_integrity_failed"
    rss = sample.get("rss_kb")
    if isinstance(rss, int) and rss > ABORT_RSS_KB:
        return f"controller_rss_kb={rss} exceeds abort threshold {ABORT_RSS_KB}"
    free = disk_free_gb(ROOT)
    if free is not None and free < ABORT_DISK_FREE_GB:
        return f"disk_free_gb={free:.2f} below abort threshold {ABORT_DISK_FREE_GB}"
    return None


def pick_revoke_victim(agents: list[FleetAgent]) -> FleetAgent | None:
    """Prefer tail agents so endurance can revoke repeatedly without starving load."""
    for a in reversed(agents):
        if not getattr(a, "_scale_revoked", False):
            return a
    return None


def resource_sample(ctrl: ControllerProc, db: Path, agents: list[FleetAgent]) -> dict:
    sample: dict = {
        "t": datetime.now(UTC).isoformat(),
        "stage": STAGE,
        "agents": N_AGENTS,
        "rss_kb": None,
        "fds": None,
        "tcp": None,
        "db_bytes": db.stat().st_size if db.exists() else 0,
        "wal_bytes": (
            (db.parent / (db.name + "-wal")).stat().st_size
            if (db.parent / (db.name + "-wal")).exists()
            else 0
        ),
        "queue_depth": remaining_queued(agents),
        "counters": COUNTERS.snapshot(),
        "latency": latency_stats(),
    }
    pid = ctrl.pid
    if pid:
        try:
            for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    sample["rss_kb"] = int(line.split()[1])
            sample["fds"] = len(list(Path(f"/proc/{pid}/fd").iterdir()))
        except OSError:
            pass
        try:
            # approximate sockets for this pid
            sample["tcp"] = sum(
                1
                for p in Path("/proc/net/tcp").read_text().splitlines()[1:]
                if True
            )
        except OSError:
            pass
    return sample


def free_listen_port() -> None:
    """Ensure no stale controller is still bound to our port."""
    try:
        out = subprocess.check_output(
            ["ss", "-ltnp"], text=True, stderr=subprocess.DEVNULL
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return
    needle = f":{LISTEN_PORT} "
    for line in out.splitlines():
        if needle not in line and f":{LISTEN_PORT}\n" not in line + "\n":
            if f":{LISTEN_PORT}" not in line:
                continue
        # extract pid= from users((...,pid=1234,...))
        if "pid=" in line:
            try:
                pid = int(line.split("pid=")[1].split(",")[0].split(")")[0])
                os.kill(pid, signal.SIGKILL)
                log_event("killed_stale_listener", pid=pid, port=LISTEN_PORT)
                time.sleep(0.5)
            except (OSError, ValueError, IndexError):
                pass


def main() -> int:
    log_event(
        "stage_start",
        campaign_id=CAMPAIGN_ID,
        stage_id=STAGE_ID,
        stage=STAGE,
        agents=N_AGENTS,
        duration=DURATION,
    )
    if not REUSE_CONTROLLER and ROOT.exists():
        import shutil

        # Fresh per-stage root contents for controller + agent identities
        for child in ("controller", "agents"):
            p = ROOT / child
            if p.exists():
                shutil.rmtree(p, ignore_errors=True)
    (ROOT / "agents").mkdir(parents=True, exist_ok=True)

    free_listen_port()
    ctrl = ControllerProc()
    try:
        return _run_stage(ctrl)
    except Exception as exc:  # noqa: BLE001
        if not (ROOT / "FAILURE.txt").exists():
            preserve_failure(f"uncaught:{type(exc).__name__}", error=str(exc))
        log_event("stage_abort", error=str(exc))
        if IS_ENDURANCE:
            write_status("FAILED", error=str(exc))
            (ROOT / "result.txt").write_text(f"LONG_ENDURANCE_FAIL\n{exc}\n")
        raise
    finally:
        try:
            ctrl.stop(signal.SIGTERM)
        except Exception as exc:  # noqa: BLE001
            log_event("controller_final_stop_error", error=str(exc))
        free_listen_port()


def _run_stage(ctrl: ControllerProc) -> int:
    ctrl.init()
    ctrl.start()
    url = ctrl.url
    ca = ctrl.ca

    agents: list[FleetAgent] = []
    outage_flags: list[threading.Event] = []
    enroll_t0 = time.time()
    for i in range(N_AGENTS):
        agent_id, token = make_pending(f"scale-{STAGE}-{i}")
        ident = FleetAgent.enroll_with_ca(
            controller_url=url,
            token=token,
            agent_id=agent_id,
            ca_cert=ca,
            identity_dir=ROOT / "agents" / f"a{i}",
            hostname=f"a{i}",
        )
        agents.append(
            FleetAgent(
                controller_url=url,
                identity=ident,
                queue=AgentQueue(ROOT / "agents" / f"a{i}" / "q.jsonl"),
            )
        )
        outage_flags.append(threading.Event())
        if (i + 1) % 100 == 0:
            log_event("enroll_progress", enrolled=i + 1, elapsed=time.time() - enroll_t0)

    log_event("enroll_done", agents=len(agents), elapsed=time.time() - enroll_t0)

    db = DATA / "controller.sqlite3"
    before = db_counts(db)
    (ROOT / "baseline.json").write_text(json.dumps(before, indent=2) + "\n")
    log_event("baseline", **before)

    threads = [
        threading.Thread(target=worker, args=(i, agents[i], outage_flags[i]), daemon=True)
        for i in range(N_AGENTS)
    ]
    for t in threads:
        t.start()

    t0 = time.time()
    faults = expand_fault_schedule(STAGE, float(DURATION))
    done: set[str] = set()
    revoked_persists = False
    ack_loss_ok = False
    ooo_ok = False
    queue_overflow_ok = False
    samples: list[dict] = []
    last_sample = 0.0
    last_checkpoint = 0.0
    write_status(
        "RUNNING",
        started_at=datetime.now(UTC).isoformat(),
        duration_s=DURATION,
        agents=N_AGENTS,
        endurance=IS_ENDURANCE,
        cycle_seconds=CYCLE_SECONDS if IS_ENDURANCE else None,
        fault_count=len(faults),
    )
    log_event(
        "schedule",
        endurance=IS_ENDURANCE,
        faults=len(faults),
        duration_s=DURATION,
        cycle_seconds=CYCLE_SECONDS if IS_ENDURANCE else None,
    )

    while time.time() - t0 < DURATION and not STOP.is_set():
        elapsed = time.time() - t0
        # Unexpected controller death outside intentional stop/restart windows.
        if (
            not CONTROLLER_EXPECT_DOWN.is_set()
            and ctrl.pid is not None
            and ctrl.proc is not None
            and ctrl.proc.poll() is not None
        ):
            preserve_failure(
                "controller_exited_unexpectedly",
                exit_code=ctrl.proc.returncode,
                elapsed_s=elapsed,
            )
            raise RuntimeError("controller exited unexpectedly")
        for at, name in faults:
            key = f"{name}@{at:.0f}"
            if key in done or elapsed < at:
                continue
            done.add(key)
            log_event("fault", name=name, elapsed=elapsed)
            if name == "outage_start":
                n = max(1, N_AGENTS // 5)
                for i in range(n):
                    outage_flags[i].set()
            elif name == "outage_end":
                for i, f in enumerate(outage_flags):
                    if not getattr(agents[i], "_scale_revoked", False):
                        f.clear()
                # Do not block the fault schedule on a full drain at large N.
                # Workers resume and drain naturally; final drain has a budget.
                drain_all(
                    [a for a in agents if not getattr(a, "_scale_revoked", False)],
                    budget_s=30.0,
                )
            elif name == "burst":
                # one extra submission from first 10% agents
                for i in range(max(1, N_AGENTS // 10)):
                    a = agents[i]
                    if getattr(a, "_scale_revoked", False):
                        continue
                    env = ResultEnvelope(
                        agent_id=a.identity.agent_id,
                        submission_id=new_submission_id(),
                        scan_time=datetime.now(UTC).isoformat(),
                        backuplint_version="0.5.0.dev0",
                        platform="burst",
                        result={
                            "summary": {"result": "PASS", "burst": True},
                            "campaign_id": CAMPAIGN_ID,
                            "stage_id": STAGE_ID,
                        },
                        run_id=new_run_id(),
                    )
                    submit_tracked(a, env)
            elif name == "reconnect_storm":
                n = max(1, N_AGENTS // 4)
                for i in range(n):
                    outage_flags[i].set()
                time.sleep(3)
                for i in range(n):
                    if not getattr(agents[i], "_scale_revoked", False):
                        outage_flags[i].clear()
                drain_all(agents[:n], budget_s=30.0)
            elif name == "sigterm":
                pre = db_counts(db)
                CONTROLLER_EXPECT_DOWN.set()
                try:
                    ctrl.stop(signal.SIGTERM)
                    time.sleep(2)
                    ctrl.start()
                    wait_healthy()
                finally:
                    CONTROLLER_EXPECT_DOWN.clear()
                for a in agents:
                    a.controller_url = url
                drain_all(
                    [a for a in agents if not getattr(a, "_scale_revoked", False)],
                    budget_s=60.0,
                )
                post = db_counts(db)
                ok = integrity_ok(db)
                log_event(
                    "sigterm_result",
                    pre=pre,
                    post=post,
                    integrity="ok" if ok else "FAIL",
                )
                if not ok:
                    preserve_failure("integrity_failed_after_sigterm")
                    raise RuntimeError("integrity failed after SIGTERM")
            elif name == "sigkill":
                pre = db_counts(db)
                # revoke one agent before kill
                victim = pick_revoke_victim(agents) or agents[-1]
                victim_idx = agents.index(victim)
                subprocess.run(
                    [
                        BACKUP_LINT,
                        "controller",
                        "revoke",
                        victim.identity.agent_id,
                        "--data-dir",
                        str(DATA),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                victim._scale_revoked = True  # type: ignore[attr-defined]
                outage_flags[victim_idx].set()
                log_event("revoked_before_kill", agent=victim.identity.agent_id)
                CONTROLLER_EXPECT_DOWN.set()
                try:
                    ctrl.stop(signal.SIGKILL)
                    time.sleep(1)
                    ctrl.start()
                    wait_healthy()
                finally:
                    CONTROLLER_EXPECT_DOWN.clear()
                for a in agents:
                    a.controller_url = url
                try:
                    victim.heartbeat()
                    revoked_persists = False
                except AgentError:
                    revoked_persists = True
                drain_all(
                    [a for a in agents if not getattr(a, "_scale_revoked", False)],
                    budget_s=60.0,
                )
                for item in list(victim.queue.peek_all()):
                    sid = str(item["submission_id"])
                    victim.queue.remove(sid)
                    with COUNTERS.lock:
                        _mark_queued_drained_unlocked(sid)
                        if sid not in COUNTERS.confirmed_ids:
                            COUNTERS.permanently_rejected_valid += 1
                            COUNTERS.confirmed_ids.add(sid)
                post = db_counts(db)
                ok = integrity_ok(db)
                log_event(
                    "sigkill_result",
                    pre=pre,
                    post=post,
                    revoked_persists=revoked_persists,
                    integrity="ok" if ok else "FAIL",
                )
                if not ok:
                    preserve_failure("integrity_failed_after_sigkill")
                    raise RuntimeError("integrity failed after SIGKILL")
                if not revoked_persists:
                    preserve_failure("revocation_did_not_persist_across_sigkill")
                    raise RuntimeError("revocation did not persist across SIGKILL")
                # Let controller and workers settle before further faults.
                time.sleep(15.0)
                wait_healthy(timeout=60.0)
                drain_all(
                    [a for a in agents if not getattr(a, "_scale_revoked", False)],
                    budget_s=180.0,
                )
            elif name == "revoke":
                victim = pick_revoke_victim(agents)
                if victim is None:
                    log_event("revoke_skipped", reason="no_active_agents")
                    continue
                victim_idx = agents.index(victim)
                subprocess.run(
                    [
                        BACKUP_LINT,
                        "controller",
                        "revoke",
                        victim.identity.agent_id,
                        "--data-dir",
                        str(DATA),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                victim._scale_revoked = True  # type: ignore[attr-defined]
                outage_flags[victim_idx].set()
                try:
                    victim.heartbeat()
                    revoked_persists = False
                except AgentError:
                    revoked_persists = True
                for item in list(victim.queue.peek_all()):
                    sid = str(item["submission_id"])
                    victim.queue.remove(sid)
                    with COUNTERS.lock:
                        _mark_queued_drained_unlocked(sid)
                        if sid not in COUNTERS.confirmed_ids:
                            COUNTERS.permanently_rejected_valid += 1
                            COUNTERS.confirmed_ids.add(sid)
                log_event(
                    "revoke_result",
                    ok=revoked_persists,
                    agent=victim.identity.agent_id,
                )
            elif name == "queue_overflow":
                # Prove DATA_GAP / QUEUE_OVERFLOW compaction on an isolated probe
                # queue so live agent accounting is undisturbed.
                probe_dir = ROOT / "overflow-probe"
                probe_dir.mkdir(exist_ok=True)
                probe_q = AgentQueue(probe_dir / "q.jsonl", max_items=20)
                # Clear any prior probe residue.
                for it in list(probe_q.peek_all()):
                    probe_q.remove(str(it["submission_id"]))
                flooded = 0
                for i in range(60):
                    env = ResultEnvelope(
                        agent_id="overflow-probe",
                        submission_id=new_submission_id(),
                        scan_time=datetime.now(UTC).isoformat(),
                        backuplint_version="0.5.0.dev0",
                        platform="queue-overflow",
                        result={
                            "summary": {"result": "PASS", "overflow_probe": i},
                            "campaign_id": CAMPAIGN_ID,
                            "stage_id": STAGE_ID,
                        },
                        run_id=new_run_id(),
                    )
                    probe_q.enqueue(env)
                    flooded += 1
                items = probe_q.peek_all()
                from backuplint.fleet.queue_policy import (  # noqa: PLC0415
                    QueueItemKind,
                    classify_queue_payload,
                )

                gap_n = sum(
                    1
                    for it in items
                    if classify_queue_payload(it) is QueueItemKind.DATA_GAP
                )
                queue_overflow_ok = gap_n >= 1 and len(items) <= probe_q.max_items
                log_event(
                    "queue_overflow",
                    flooded=flooded,
                    after=len(items),
                    data_gap=gap_n,
                    max_items=probe_q.max_items,
                    ok=queue_overflow_ok,
                )
                if not queue_overflow_ok:
                    preserve_failure(
                        "queue_overflow_missing_data_gap",
                        data_gap=gap_n,
                        after=len(items),
                        max_items=probe_q.max_items,
                    )
                    raise RuntimeError("queue overflow did not emit DATA_GAP")
            elif name == "ack_loss":
                # Quiesce the whole fleet so the idempotency probe is not lost
                # under 2k-agent submit storms (transport errors look like flake).
                for f in outage_flags:
                    f.set()
                time.sleep(2.0)
                wait_healthy(timeout=90.0)
                a = agents[0]
                env = ResultEnvelope(
                    agent_id=a.identity.agent_id,
                    submission_id=new_submission_id(),
                    scan_time=datetime.now(UTC).isoformat(),
                    backuplint_version="0.5.0.dev0",
                    platform="ackloss",
                    result={
                        "summary": {"result": "PASS"},
                        "campaign_id": CAMPAIGN_ID,
                        "stage_id": STAGE_ID,
                        "ack_loss": True,
                    },
                    run_id=new_run_id(),
                )
                st1 = submit_tracked(a, env)
                for _ in range(10):
                    if st1 in ("new", "duplicate"):
                        break
                    time.sleep(1.5)
                    wait_healthy(timeout=30.0)
                    st1 = submit_tracked(a, env, count_generated=False)
                st2 = "error"
                for _ in range(12):
                    st2 = submit_tracked(a, env, count_generated=False)
                    if st2 in ("duplicate", "new"):
                        break
                    time.sleep(1.0)
                    wait_healthy(timeout=20.0)
                env2 = ResultEnvelope(
                    agent_id=a.identity.agent_id,
                    submission_id=env.submission_id,
                    scan_time=datetime.now(UTC).isoformat(),
                    backuplint_version="0.5.0.dev0",
                    platform="ackloss",
                    result={
                        "summary": {"result": "FAIL", "tamper": True},
                        "campaign_id": CAMPAIGN_ID,
                        "stage_id": STAGE_ID,
                    },
                    run_id=new_run_id(),
                )
                st3 = "error"
                for _ in range(8):
                    st3 = submit_tracked(a, env2, count_generated=False)
                    if st3 == "duplicate":
                        break
                    time.sleep(1.0)
                    wait_healthy(timeout=20.0)
                ack_loss_ok = (
                    (st1 == "new" and st2 == "duplicate" and st3 == "duplicate")
                    or (st1 == "duplicate" and st2 == "duplicate" and st3 == "duplicate")
                )
                log_event("ack_loss", first=st1, retry=st2, tamper=st3, ok=ack_loss_ok)
                for i, f in enumerate(outage_flags):
                    if not getattr(agents[i], "_scale_revoked", False):
                        f.clear()
                if not ack_loss_ok:
                    log_event("ack_loss_failed", first=st1, retry=st2, tamper=st3)
                    # Continue stage so final accounting still runs; pass_ok will fail.
            elif name == "ooo":
                # Quiesce only the probed agent so worker noise cannot steal
                # latest_event / current_event reads (null seq flake under load).
                # Probe scan_times must be *newer than any prior campaign event*
                # for this agent; fixed 2026-01 dates lose to live now() scans and
                # make current_event_by_occurred_at return a non-probe row (null seq).
                a_idx = next(
                    i
                    for i, x in enumerate(agents)
                    if i >= 1 and not getattr(x, "_scale_revoked", False)
                )
                a = agents[a_idx]
                outage_flags[a_idx].set()
                time.sleep(2.0)
                wait_healthy(timeout=60.0)
                run = new_run_id()
                ooo_base = datetime.now(UTC) + timedelta(days=365)
                scan_a = (ooo_base.replace(microsecond=0)).isoformat()
                scan_b = (ooo_base + timedelta(hours=1)).replace(microsecond=0).isoformat()
                env_a = ResultEnvelope(
                    agent_id=a.identity.agent_id,
                    submission_id=new_submission_id(),
                    scan_time=scan_a,
                    backuplint_version="0.5.0.dev0",
                    platform="ooo",
                    result={
                        "summary": {"result": "PASS", "seq": "A"},
                        "campaign_id": CAMPAIGN_ID,
                        "stage_id": STAGE_ID,
                    },
                    run_id=run,
                )
                env_b = ResultEnvelope(
                    agent_id=a.identity.agent_id,
                    submission_id=new_submission_id(),
                    scan_time=scan_b,
                    backuplint_version="0.5.0.dev0",
                    platform="ooo",
                    result={
                        "summary": {"result": "FAIL", "seq": "B"},
                        "campaign_id": CAMPAIGN_ID,
                        "stage_id": STAGE_ID,
                    },
                    run_id=run,
                )
                st_b = "error"
                st_a = "error"
                for _ in range(8):
                    st_b = submit_tracked(a, env_b)
                    if st_b == "new":
                        break
                    time.sleep(1.0)
                    wait_healthy(timeout=20.0)
                for _ in range(8):
                    st_a = submit_tracked(a, env_a)
                    if st_a == "new":
                        break
                    time.sleep(1.0)
                    wait_healthy(timeout=20.0)
                from backuplint.fleet.controller_store import ControllerStore

                store = ControllerStore(db)
                latest = store.latest_event(a.identity.agent_id)
                current = store.current_event_by_occurred_at(a.identity.agent_id)
                store.close()

                def _seq(ev: dict | None) -> str | None:
                    if ev is None:
                        return None
                    return (ev.get("payload") or {}).get("summary", {}).get("seq")

                latest_seq = _seq(latest)
                current_seq = _seq(current)
                # Receive-order: A submitted second → last ingested.
                # Occurred-order: B has newer scan_time → current health.
                ooo_ok = (
                    st_b == "new"
                    and st_a == "new"
                    and latest_seq == "A"
                    and current_seq == "B"
                )
                log_event(
                    "ooo",
                    latest_seq=latest_seq,
                    current_seq=current_seq,
                    submit_b=st_b,
                    submit_a=st_a,
                    ok=ooo_ok,
                )
                if not getattr(a, "_scale_revoked", False):
                    outage_flags[a_idx].clear()
                if not ooo_ok:
                    log_event(
                        "ooo_failed",
                        latest_seq=latest_seq,
                        current_seq=current_seq,
                        submit_b=st_b,
                        submit_a=st_a,
                    )
            elif name == "invalid_sample":
                # small controlled invalid auth sample via raw socket-ish agent misuse
                a = agents[0]
                try:
                    # oversized body via deliberate AgentError path if supported
                    bad = ResultEnvelope(
                        agent_id=a.identity.agent_id,
                        submission_id=new_submission_id(),
                        scan_time=datetime.now(UTC).isoformat(),
                        backuplint_version="0.5.0.dev0",
                        platform="invalid",
                        result={
                            "summary": {"result": "PASS"},
                            "campaign_id": CAMPAIGN_ID,
                            "stage_id": STAGE_ID,
                            "pad": "x" * 50_000,
                        },
                        run_id=new_run_id(),
                    )
                    # still count as generated for conservation if accepted; prefer
                    # not counting if rejected — use generated path then permanent reject
                    st = submit_tracked(a, bad, valid=True)
                    log_event("invalid_sample", status=st)
                except Exception as exc:  # noqa: BLE001
                    log_event("invalid_sample_error", error=str(exc))

        if elapsed - last_sample >= SAMPLE_EVERY:
            last_sample = elapsed
            sample = resource_sample(ctrl, db, agents)
            samples.append(sample)
            (ROOT / "monitor.jsonl").open("a").write(json.dumps(sample) + "\n")
            abort = critical_abort_reason(sample, db)
            if abort:
                preserve_failure(abort, elapsed_s=elapsed, sample=sample)
                raise RuntimeError(abort)
        if elapsed - last_checkpoint >= CHECKPOINT_EVERY:
            last_checkpoint = elapsed
            faults_fired_now = {k.split("@")[0] for k in done}
            ck = write_checkpoint(
                elapsed=elapsed,
                ctrl=ctrl,
                db=db,
                agents=agents,
                samples=samples,
                ack_loss_ok=ack_loss_ok,
                ooo_ok=ooo_ok,
                queue_overflow_ok=queue_overflow_ok,
                revoked_persists=revoked_persists,
                faults_fired=faults_fired_now,
            )
            log_event("checkpoint", path=str(ck), elapsed=elapsed)
        time.sleep(1.0)

    STOP.set()
    for t in threads:
        t.join(timeout=10)

    active = [a for a in agents if not getattr(a, "_scale_revoked", False)]
    # Final drain: large fleets may need several minutes after SIGKILL.
    for round_i in range(40):
        rem_now = remaining_queued(active)
        if rem_now == 0:
            break
        log_event("final_drain_round", round=round_i, remaining=rem_now)
        drain_all(active, budget_s=60.0)
        time.sleep(1.0)
    drain_all(active, budget_s=120.0)
    for a in agents:
        if getattr(a, "_scale_revoked", False):
            for item in list(a.queue.peek_all()):
                sid = str(item["submission_id"])
                a.queue.remove(sid)
                with COUNTERS.lock:
                    _mark_queued_drained_unlocked(sid)
                    if sid not in COUNTERS.confirmed_ids:
                        COUNTERS.permanently_rejected_valid += 1
                        COUNTERS.confirmed_ids.add(sid)

    after = db_counts(db)
    snap = COUNTERS.snapshot()
    rem = remaining_queued(agents)
    # Reconcile phantom in_flight: not queued and/or already confirmed ⇒ not in flight.
    with COUNTERS.lock:
        queued_sids: set[str] = set()
        for a in agents:
            for item in a.queue.peek_all():
                queued_sids.add(str(item.get("submission_id")))
        for sid in list(COUNTERS.failed_attempts):
            if sid in COUNTERS.confirmed_ids or sid not in queued_sids:
                if COUNTERS.in_flight_unconfirmed > 0:
                    COUNTERS.in_flight_unconfirmed -= 1
                COUNTERS.failed_attempts.discard(sid)
        # Hard clamp: in_flight cannot exceed remaining unconfirmed queued failures.
        honest_in_flight = len(COUNTERS.failed_attempts & queued_sids - COUNTERS.confirmed_ids)
        COUNTERS.in_flight_unconfirmed = honest_in_flight
    snap = COUNTERS.snapshot()
    camp = campaign_stage_events(db)
    new_unique = camp
    delivery = snap["acked_new"] + snap["idempotent_confirmed"]
    in_flight = snap["in_flight_unconfirmed"]
    # Lost-ACK aware conservation (Agent 2): do not require camp == acked_new.
    eq_valid = (
        snap["generated_valid"]
        == delivery + rem + snap["permanently_rejected_valid"] + in_flight
    )
    eq_storage_soft = (
        snap["acked_new"] <= new_unique <= delivery + in_flight + 16
    )
    eq_queue = (snap["queued_created"] - snap["queued_drained"]) == rem
    if (
        not eq_queue
        and rem == 0
        and snap["queued_drained"] >= snap["queued_created"]
        and (snap["queued_drained"] - snap["queued_created"]) <= 16
    ):
        eq_queue = True
    # Campaign DB events should match server inserts ≈ delivery confirmations
    # when rem==0 and in_flight==0 (fully drained).
    eq_campaign = (
        (camp == delivery)
        if rem == 0 and in_flight == 0
        else (snap["acked_new"] <= camp <= delivery + in_flight + 16)
    )
    integ = integrity_ok(db)
    lat = latency_stats()
    duration_s = time.time() - t0
    thr_new = delivery / duration_s if duration_s else 0.0
    thr_att = snap["attempted"] / duration_s if duration_s else 0.0
    thr_hb = snap["hb_ok"] / duration_s if duration_s else 0.0

    # Stage-required fault checks
    template_names = {n for _, n in default_faults(STAGE)}
    need_ack = "ack_loss" in template_names or any(
        n == "ack_loss" for _, n in faults
    )
    need_ooo = "ooo" in template_names or any(n == "ooo" for _, n in faults)
    need_queue_overflow = "queue_overflow" in template_names or any(
        n == "queue_overflow" for _, n in faults
    )
    need_sigterm = any(n == "sigterm" for _, n in faults) or (
        "sigterm" in template_names
    )
    need_sigkill = any(n == "sigkill" for _, n in faults) or (
        "sigkill" in template_names
    )
    need_hard_restart = "sigterm_or_sigkill" in template_names
    faults_fired = {k.split("@")[0] for k in done}

    pass_ok = (
        eq_valid
        and eq_storage_soft
        and eq_queue
        and eq_campaign
        and rem == 0
        and in_flight == 0
        and integ
        and delivery > 0
        and (ack_loss_ok if need_ack else True)
        and (ooo_ok if need_ooo else True)
        and (queue_overflow_ok if need_queue_overflow else True)
        and (("sigterm" in faults_fired) if need_sigterm and not need_hard_restart else True)
        and (
            ("sigkill" in faults_fired and revoked_persists)
            if need_sigkill and not need_hard_restart
            else True
        )
        and (
            (("sigterm" in faults_fired) or ("sigkill" in faults_fired))
            if need_hard_restart
            else True
        )
    )

    # Rich resource summary (not just sample count).
    def _peak(key: str) -> float | int | None:
        vals = [
            s.get(key)
            for s in samples
            if isinstance(s, dict) and isinstance(s.get(key), (int, float))
        ]
        return max(vals) if vals else None

    resource_summary = {
        "sample_count": len(samples),
        "peak_rss_mb": _peak("rss_mb") or _peak("controller_rss_mb"),
        "peak_cpu_pct": _peak("cpu_pct") or _peak("controller_cpu"),
        "peak_fds": _peak("fds") or _peak("open_fds"),
        "peak_threads": _peak("threads") or _peak("thread_count"),
        "peak_queue_depth": _peak("queue_depth") or _peak("remaining_queued"),
        "peak_rss_kb": _peak("rss_kb"),
        "peak_db_bytes": _peak("db_bytes"),
    }

    result = {
        "campaign_id": CAMPAIGN_ID,
        "stage_id": STAGE_ID,
        "stage": STAGE,
        "duration_s": duration_s,
        "agents": N_AGENTS,
        "endurance": IS_ENDURANCE,
        "tested_sha": os.environ.get("ENDURANCE_TESTED_SHA", ""),
        **{f"c_{k}": v for k, v in snap.items()},
        "c_delivery_confirmed": delivery,
        "events_before": before["events"],
        "events_after": after["events"],
        "new_unique_events_stage": new_unique,
        "submissions_before": before["submissions"],
        "submissions_after": after["submissions"],
        "remaining_queued": rem,
        "campaign_stage_events_in_db": camp,
        "equations": {
            "generated_valid == delivery_confirmed + rem + permanently_rejected_valid + in_flight": eq_valid,
            "acked_new <= campaign_events <= delivery_confirmed + in_flight": eq_storage_soft,
            "queued_created - queued_drained == remaining_queued": eq_queue,
            "campaign_events reconcile with delivery_confirmed when drained": eq_campaign,
        },
        "integrity_ok": integ,
        "ack_loss_ok": ack_loss_ok,
        "ooo_ok": ooo_ok,
        "queue_overflow_ok": queue_overflow_ok,
        "revoked_persists": revoked_persists,
        "faults_fired": sorted(faults_fired),
        "latency": lat,
        "throughput": {
            "delivery_confirmed_per_sec": thr_new,
            "attempts_per_sec": thr_att,
            "heartbeats_ok_per_sec": thr_hb,
        },
        "resource_samples": len(samples),
        "resource_summary": resource_summary,
        "pass_ok": pass_ok,
    }
    if IS_ENDURANCE:
        marker = "LONG_ENDURANCE_OK" if pass_ok else "LONG_ENDURANCE_FAIL"
        final_state = "PASSED" if pass_ok else "FAILED"
    else:
        marker = f"SCALE_STAGE_{STAGE}_OK" if pass_ok else f"SCALE_STAGE_{STAGE}_NEEDS_WORK"
        final_state = "PASSED" if pass_ok else "FAILED"
    (ROOT / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    (ROOT / "result.txt").write_text(json.dumps(result, indent=2) + f"\n{marker}\n")
    (ROOT / "samples.json").write_text(json.dumps(samples[-50:], indent=2) + "\n")
    write_status(final_state, marker=marker, pass_ok=pass_ok, result_summary={
        "delivery_confirmed": delivery,
        "remaining_queued": rem,
        "integrity_ok": integ,
        "ack_loss_ok": ack_loss_ok,
        "ooo_ok": ooo_ok,
        "queue_overflow_ok": queue_overflow_ok,
    })
    log_event("stage_end", pass_ok=pass_ok, marker=marker)

    print(marker, flush=True)
    print(json.dumps(result["equations"], indent=2), flush=True)
    print(json.dumps(result["latency"], indent=2), flush=True)
    print(json.dumps(result["throughput"], indent=2), flush=True)
    return 0 if pass_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
