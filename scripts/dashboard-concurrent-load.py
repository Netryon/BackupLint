"""Short concurrent fleet ingest + dashboard read load probe (minutes, not days)."""

from __future__ import annotations

import json
import ssl
import statistics
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

from backuplint.fleet.controller import FleetController
from backuplint.fleet.dashboard.config import DashboardConfig
from backuplint.fleet.protocol import PROTOCOL_VERSION, ResultEnvelope, new_submission_id
from tests.helpers.fleet_lab import enroll_test_agent


def _https_opener(ctx: ssl.SSLContext, jar: CookieJar | None = None):
    handlers: list[object] = [urllib.request.HTTPSHandler(context=ctx)]
    if jar is not None:
        handlers.append(urllib.request.HTTPCookieProcessor(jar))
    return urllib.request.build_opener(*handlers)


def run_probe(
    tmp: Path,
    *,
    agents: int = 20,
    seconds: float = 20.0,
    dashboard_workers: int = 4,
) -> dict[str, object]:
    cfg = DashboardConfig(
        enabled=True,
        cookie_secure=False,
        max_page_size=100,
        online_after_seconds=120,
        stale_after_seconds=600,
    )
    controller = FleetController(tmp / "ctrl", dashboard=cfg)
    controller.dashboard_auth.set_password("concurrent-load-password")
    submit_ok = 0
    submit_err = 0
    hb_ok = 0
    hb_err = 0
    dash_lat: list[float] = []
    dash_err = 0
    stop = threading.Event()
    lock = threading.Lock()

    try:
        host, port = controller.start(host="127.0.0.1", port=0)
        base = f"https://{host}:{port}"
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        fleet_agents = [
            enroll_test_agent(controller, tmp, base, name=f"load-{i}")
            for i in range(agents)
        ]

        jar = CookieJar()
        opener = _https_opener(ctx, jar)
        body = urllib.parse.urlencode(
            {"password": "concurrent-load-password"}
        ).encode("utf-8")
        login_req = urllib.request.Request(
            f"{base}/dashboard/login",
            data=body,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with opener.open(login_req, timeout=10):  # noqa: S310
            pass

        def fleet_worker(agent_index: int) -> None:
            nonlocal submit_ok, submit_err, hb_ok, hb_err
            agent = fleet_agents[agent_index]
            while not stop.is_set():
                try:
                    agent.heartbeat(max_attempts=2)
                    with lock:
                        hb_ok += 1
                except Exception:  # noqa: BLE001
                    with lock:
                        hb_err += 1
                try:
                    env = ResultEnvelope(
                        agent_id=agent.identity.agent_id,
                        submission_id=new_submission_id(),
                        scan_time=time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
                        backuplint_version="0.5.0.dev0",
                        platform="load",
                        result={"summary": {"result": "PASS"}},
                        protocol_version=PROTOCOL_VERSION,
                    )
                    agent.submit_envelope(env)
                    with lock:
                        submit_ok += 1
                except Exception:  # noqa: BLE001
                    with lock:
                        submit_err += 1
                time.sleep(0.05)

        def dash_worker() -> None:
            nonlocal dash_err
            paths = [
                "/v1/dashboard/overview",
                "/v1/dashboard/agents?limit=50",
                "/v1/dashboard/events?limit=25",
                "/dashboard/",
                "/dashboard/history?failures_only=1",
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
                        dash_lat.append((time.perf_counter() - started) * 1000.0)
                except Exception:  # noqa: BLE001
                    with lock:
                        dash_err += 1
                time.sleep(0.02)

        threads = [
            threading.Thread(target=fleet_worker, args=(i,), daemon=True)
            for i in range(agents)
        ]
        threads.extend(
            threading.Thread(target=dash_worker, daemon=True)
            for _ in range(dashboard_workers)
        )
        for t in threads:
            t.start()
        time.sleep(seconds)
        stop.set()
        time.sleep(0.5)
        snap = controller.metrics_snapshot(persist=False)
        sorted_lat = sorted(dash_lat)

        def _pct(values: list[float], pct: float) -> float | None:
            if not values:
                return None
            if len(values) == 1:
                return values[0]
            idx = min(len(values) - 1, max(0, int(round((pct / 100.0) * (len(values) - 1)))))
            return values[idx]

        # DB accounting sanity: submissions created during the probe.
        with controller.store._lock:  # noqa: SLF001
            submission_count = controller.store._conn.execute(  # noqa: SLF001
                "SELECT COUNT(*) FROM submissions"
            ).fetchone()[0]
            event_count = controller.store._conn.execute(  # noqa: SLF001
                "SELECT COUNT(*) FROM events"
            ).fetchone()[0]
        result = {
            "seconds": seconds,
            "agents": agents,
            "dashboard_workers": dashboard_workers,
            "submit_ok": submit_ok,
            "submit_err": submit_err,
            "heartbeat_ok": hb_ok,
            "heartbeat_err": hb_err,
            "dashboard_samples": len(dash_lat),
            "dashboard_ok": len(dash_lat),
            "dashboard_err": dash_err,
            "rejected_503": int(snap.get("rejected_503", 0) or 0),
            "dashboard_latency_ms": {
                "p50": _pct(sorted_lat, 50),
                "p95": _pct(sorted_lat, 95),
                "p99": _pct(sorted_lat, 99),
                "max": max(dash_lat) if dash_lat else None,
                "mean": statistics.fmean(dash_lat) if dash_lat else None,
            },
            "queue_end": {
                "request_queue_size": snap.get("request_queue_size"),
            },
            "in_flight_end": {
                "active_requests": snap.get("active_requests"),
                "active_peak": snap.get("active_peak"),
                "max_inflight": snap.get("max_inflight"),
            },
            "db_integrity": {
                "submissions": submission_count,
                "events": event_count,
                "submit_ok_matches_floor": submission_count >= submit_ok,
            },
            "controller_metrics": snap,
            "ingest_ok_ratio": (
                submit_ok / max(1, submit_ok + submit_err)
            ),
            "pass": submit_err == 0 and hb_err == 0 and dash_err == 0 and submit_ok > 0,
        }
        return result
    finally:
        controller.close()


if __name__ == "__main__":
    import argparse
    import tempfile

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agents", type=int, default=20)
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--dashboard-workers", type=int, default=4)
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Optional JSON output path",
    )
    args = parser.parse_args()
    base = Path(tempfile.mkdtemp(prefix="backuplint-dashboard-load-"))
    out = args.out or (base / "concurrent-load.json")
    result = run_probe(
        base / "data",
        seconds=args.seconds,
        agents=args.agents,
        dashboard_workers=args.dashboard_workers,
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    print(f"wrote {out}", flush=True)
    raise SystemExit(0 if result.get("pass") else 1)
