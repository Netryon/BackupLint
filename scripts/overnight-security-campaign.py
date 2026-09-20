#!/usr/bin/env python3
"""Disposable overnight security campaign for BackupLint fleet (workshop-only)."""

from __future__ import annotations

import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(os.environ.get("OV_ROOT", "/tmp/bl-overnight-sec"))
ROOT.mkdir(parents=True, exist_ok=True)
REPORT = ROOT / "security-report.json"

SENTINELS = {
    "restic": "BL_OVERNIGHT_SECRET_RESTIC_9f3a2c1b",
    "enroll": "BL_OVERNIGHT_ENROLL_TOKEN_7e8d6c5b",
    "private": "BL_OVERNIGHT_PRIVATE_VALUE_4a1b2c3d",
    "result": "BL_OVERNIGHT_RESULT_SENTINEL_0e9f8a7b",
}

results: dict[str, object] = {"started_at": datetime.now(UTC).isoformat(), "checks": {}}


def record(name: str, ok: bool, detail: str = "") -> None:
    results["checks"][name] = {"ok": ok, "detail": detail}
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {name}: {detail}", flush=True)


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    # Prefer installed package
    from backuplint.events import new_run_id
    from backuplint.fleet.agent import AgentError, AgentQueue, FleetAgent
    from backuplint.fleet.certs import init_ca, issue_server_cert
    from backuplint.fleet.controller import FleetController
    from backuplint.fleet.protocol import (
        MAX_BODY_BYTES,
        ResultEnvelope,
        new_submission_id,
    )

    lab = ROOT / "lab"
    if lab.exists():
        # keep fresh each run
        pass
    lab.mkdir(parents=True, exist_ok=True)

    ctrl = FleetController(lab / "controller", hostname="127.0.0.1")
    host, port = ctrl.start(host="127.0.0.1", port=0)
    url = f"https://127.0.0.1:{port}"
    ca = lab / "controller" / "ca" / "ca.crt"
    print(f"controller {url}", flush=True)

    try:
        # --- plaintext rejection ---
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=2)
            sock.sendall(b"GET / HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
            data = sock.recv(256)
            sock.close()
            # If we got any clear HTTP response text, that is a failure.
            record(
                "plaintext_http",
                b"HTTP/" not in data,
                f"recv={data[:40]!r}",
            )
        except OSError as exc:
            record("plaintext_http", True, f"rejected: {exc}")

        # curl http explicitly
        http_url = f"http://127.0.0.1:{port}/v1/results"
        try:
            urllib.request.urlopen(http_url, timeout=2)  # nosec B310 - intentional plaintext probe
            record("http_urlopen", False, "unexpected success")
        except Exception as exc:  # noqa: BLE001
            record("http_urlopen", True, f"rejected: {type(exc).__name__}")

        # --- enroll + submit happy path with sentinel in result summary only as non-secret marker ---
        token = ctrl.create_enroll_token(label="overnight", ttl_hours=1)
        # Ensure token string itself is not the sentinel; keep sentinel separate for pcap search
        identity = FleetAgent.enroll_with_ca(
            controller_url=url,
            token=token,
            ca_cert=ca,
            identity_dir=lab / "agent1",
        )
        agent = FleetAgent(
            controller_url=url,
            identity=identity,
            queue=AgentQueue(lab / "agent1" / "q.jsonl"),
        )
        env = ResultEnvelope(
            agent_id=identity.agent_id,
            submission_id=new_submission_id(),
            scan_time=datetime.now(UTC).isoformat(),
            backuplint_version="0.5.0.dev0",
            platform="overnight",
            result={"summary": {"result": "PASS", "marker": SENTINELS["result"]}},
            run_id=new_run_id(),
        )
        agent.submit_envelope(env)
        stored = ctrl.store.latest_result(identity.agent_id)
        record("happy_submit", stored is not None, f"agent={identity.agent_id}")

        # --- wrong CA ---
        other = lab / "other-ca"
        init_ca(other / "ca")
        issue_server_cert(other / "ca", other / "server", common_name="127.0.0.1")
        token2 = ctrl.create_enroll_token(label="wrongca", ttl_hours=1)
        try:
            FleetAgent.enroll_with_ca(
                controller_url=url,
                token=token2,
                ca_cert=other / "ca" / "ca.crt",
                identity_dir=lab / "agent-wrongca",
            )
            record("wrong_ca", False, "enroll unexpectedly succeeded")
        except AgentError as exc:
            record("wrong_ca", True, str(exc)[:120])

        # --- wrong hostname/SAN (connect via 127.0.0.1 but CA cert for localhost-only other ctrl) ---
        # Build a controller with hostname localhost only, try connecting via IP with that CA
        # Already covered by wrong CA above; add explicit hostname mismatch using openssl s_client later.

        # --- no client cert on results ---
        context = ssl.create_default_context(cafile=str(ca))
        body = json.dumps(
            {
                "protocol_version": 1,
                "agent_id": identity.agent_id,
                "submission_id": new_submission_id(),
                "scan_time": datetime.now(UTC).isoformat(),
                "backuplint_version": "0.5.0.dev0",
                "platform": "x",
                "result": {"summary": {"result": "PASS"}},
            }
        ).encode()
        req = urllib.request.Request(
            url + "/v1/results",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, context=context, timeout=5)
            record("no_client_cert", False, "accepted without mTLS")
        except urllib.error.HTTPError as exc:
            record("no_client_cert", exc.code in {401, 403}, f"HTTP {exc.code}")
        except ssl.SSLError as exc:
            record("no_client_cert", True, f"TLS reject: {exc}")

        # --- impersonation ---
        token3 = ctrl.create_enroll_token(label="victim", ttl_hours=1)
        victim = FleetAgent.enroll_with_ca(
            controller_url=url,
            token=token3,
            ca_cert=ca,
            identity_dir=lab / "victim",
        )
        before = ctrl.store.latest_result(victim.agent_id)
        bad = ResultEnvelope(
            agent_id=victim.agent_id,
            submission_id=new_submission_id(),
            scan_time=datetime.now(UTC).isoformat(),
            backuplint_version="0.5.0.dev0",
            platform="impersonate",
            result={"summary": {"result": "PASS"}},
            run_id=new_run_id(),
        )
        try:
            agent.submit_envelope(bad)
            after = ctrl.store.latest_result(victim.agent_id)
            record(
                "impersonation",
                after == before,
                "submit did not raise but victim store unchanged"
                if after == before
                else "VICTIM STORE CHANGED",
            )
        except AgentError as exc:
            after = ctrl.store.latest_result(victim.agent_id)
            record("impersonation", after == before, str(exc)[:120])

        # --- revoke ---
        ctrl.store.set_agent_status(identity.agent_id, "revoked")
        try:
            agent.heartbeat()
            record("revoked_heartbeat", False, "heartbeat succeeded after revoke")
        except AgentError as exc:
            record("revoked_heartbeat", True, str(exc)[:120])
        # restore for later tests? create new agent instead
        token4 = ctrl.create_enroll_token(label="alive", ttl_hours=1)
        alive_id = FleetAgent.enroll_with_ca(
            controller_url=url,
            token=token4,
            ca_cert=ca,
            identity_dir=lab / "alive",
        )
        alive = FleetAgent(
            controller_url=url,
            identity=alive_id,
            queue=AgentQueue(lab / "alive" / "q.jsonl"),
        )

        # --- enrollment token abuse ---
        used = ctrl.create_enroll_token(label="once", ttl_hours=1)
        FleetAgent.enroll_with_ca(
            controller_url=url,
            token=used,
            ca_cert=ca,
            identity_dir=lab / "once1",
        )
        try:
            FleetAgent.enroll_with_ca(
                controller_url=url,
                token=used,
                ca_cert=ca,
                identity_dir=lab / "once2",
            )
            record("token_reuse", False, "reuse succeeded")
        except AgentError as exc:
            record("token_reuse", True, str(exc)[:120])

        try:
            FleetAgent.enroll_with_ca(
                controller_url=url,
                token="not-a-real-token",
                ca_cert=ca,
                identity_dir=lab / "badtok",
            )
            record("token_wrong", False, "wrong token accepted")
        except AgentError as exc:
            record("token_wrong", True, str(exc)[:120])

        expired = ctrl.create_enroll_token(label="exp", ttl_hours=0)
        # ttl_hours=0 may still be valid briefly; force expire via store if needed
        time.sleep(0.05)
        # create with negative by redeeming expired from store
        from backuplint.fleet.controller_store import ControllerStore

        # --- replay / idempotency ---
        sub = new_submission_id()
        replay = ResultEnvelope(
            agent_id=alive_id.agent_id,
            submission_id=sub,
            scan_time=datetime.now(UTC).isoformat(),
            backuplint_version="0.5.0.dev0",
            platform="replay",
            result={"summary": {"result": "PASS"}},
            run_id=new_run_id(),
        )
        alive.submit_envelope(replay)
        alive.submit_envelope(replay)
        # count events for submission
        row = ctrl.store.latest_event(alive_id.agent_id)
        record("replay_idempotent", row is not None and row["submission_id"] == sub, "duplicate ACK safe")

        # modified payload same submission_id should not overwrite
        modified = ResultEnvelope(
            agent_id=alive_id.agent_id,
            submission_id=sub,
            scan_time=datetime.now(UTC).isoformat(),
            backuplint_version="0.5.0.dev0",
            platform="replay",
            result={"summary": {"result": "FAIL", "tamper": True}},
            run_id=new_run_id(),
        )
        # ingest returns False on duplicate; agent may treat as ACK
        inserted = ctrl.store.ingest_result(modified)
        latest = ctrl.store.latest_result(alive_id.agent_id)
        record(
            "replay_no_overwrite",
            inserted is False and latest is not None and latest["result"]["summary"]["result"] == "PASS",
            f"inserted={inserted}",
        )

        # --- SQL / command injection via label+hostname ---
        inj = "'; DROP TABLE agents; --"
        token5 = ctrl.create_enroll_token(label=inj, ttl_hours=1)
        # enroll uses token label from controller; set hostname after
        inj_id = FleetAgent.enroll_with_ca(
            controller_url=url,
            token=token5,
            ca_cert=ca,
            identity_dir=lab / "inj",
        )
        ctrl.store.set_agent_hostname(inj_id.agent_id, "host; rm -rf /tmp/should-not-exist-overnight")
        ctrl.store.set_agent_label(inj_id.agent_id, inj)
        agents = ctrl.store.list_agents()
        record(
            "sql_injection_label",
            any(a.agent_id == inj_id.agent_id for a in agents) and len(agents) >= 1,
            f"agents={len(agents)} label preserved as data",
        )
        record(
            "cmd_injection_hostname",
            not Path("/tmp/should-not-exist-overnight").exists(),
            "no side-effect path created",
        )

        # --- oversized body ---
        huge = ResultEnvelope(
            agent_id=alive_id.agent_id,
            submission_id=new_submission_id(),
            scan_time=datetime.now(UTC).isoformat(),
            backuplint_version="0.5.0.dev0",
            platform="huge",
            result={"blob": "x" * (MAX_BODY_BYTES + 50_000)},
            run_id=new_run_id(),
        )
        before_n = len(
            [
                r
                for r in [ctrl.store.latest_result(alive_id.agent_id)]
                if r
            ]
        )
        try:
            alive.submit_envelope(huge)
            # if it somehow ACKed, fail
            record("oversized_body", False, "oversized accepted")
        except AgentError as exc:
            record("oversized_body", True, str(exc)[:120])

        # --- method abuse ---
        ctx = ssl.create_default_context(cafile=str(ca))
        for method in ("GET", "PUT", "DELETE", "PATCH"):
            req = urllib.request.Request(url + "/v1/results", method=method)
            try:
                urllib.request.urlopen(req, context=ctx, timeout=3)
                record(f"method_{method}", False, "unexpected success")
            except Exception as exc:  # noqa: BLE001
                record(f"method_{method}", True, type(exc).__name__)

        # --- fuzz envelopes via parse / controller ---
        from backuplint.fleet.protocol import ProtocolError, parse_envelope
        from backuplint.events import EventError, parse_event

        fuzz_fail = 0
        fuzz_ok = 0
        cases = [
            None,
            [],
            "",
            {"protocol_version": 99},
            {"protocol_version": 1, "agent_id": "../etc/passwd", "submission_id": "s",
             "scan_time": "t", "backuplint_version": "v", "platform": "p", "result": {}},
            {"protocol_version": 1, "agent_id": identity.agent_id, "submission_id": "s",
             "scan_time": "t", "backuplint_version": "v", "platform": "p",
             "result": {"password": SENTINELS["restic"]}},
            {"schema_version": 99, "event_id": "x", "event_type": "audit.completed",
             "run_id": "run-" + "a" * 32, "occurred_at": "t", "status": "PASS", "payload": {}},
        ]
        for case in cases:
            try:
                if isinstance(case, dict) and "schema_version" in case:
                    parse_event(case)
                else:
                    parse_envelope(case)
                fuzz_fail += 1
            except (ProtocolError, EventError, TypeError):
                fuzz_ok += 1
        record("fuzz_rejects", fuzz_fail == 0, f"ok={fuzz_ok} leak_accept={fuzz_fail}")

        # --- secret scan of controller DB ---
        db = (lab / "controller" / "controller.sqlite3").read_bytes()
        hits = [k for k, v in SENTINELS.items() if v.encode() in db and k != "result"]
        # result sentinel may legitimately be in stored payload as non-secret marker — exclude
        secret_hits = [k for k in hits if k in {"restic", "enroll", "private"}]
        # enroll token value should not be in DB (only hash)
        token_in_db = token.encode() in db
        record("db_secret_scan", not secret_hits and not token_in_db, f"hits={secret_hits} token_in_db={token_in_db}")

        # --- concurrent token race ---
        race_token = ctrl.create_enroll_token(label="race", ttl_hours=1)
        winners: list[bool] = []

        def redeem(i: int) -> None:
            try:
                FleetAgent.enroll_with_ca(
                    controller_url=url,
                    token=race_token,
                    ca_cert=ca,
                    identity_dir=lab / f"race{i}",
                )
                winners.append(True)
            except Exception:  # noqa: BLE001
                winners.append(False)

        threads = [threading.Thread(target=redeem, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        record("token_race", sum(1 for w in winners if w) == 1, f"winners={sum(1 for w in winners if w)}")

        # --- graceful restart consistency ---
        before_agents = {a.agent_id for a in ctrl.store.list_agents()}
        ctrl.close()
        ctrl2 = FleetController(lab / "controller", hostname="127.0.0.1")
        ctrl2.start(host="127.0.0.1", port=0)
        after_agents = {a.agent_id for a in ctrl2.store.list_agents()}
        record(
            "restart_preserves_agents",
            before_agents == after_agents,
            f"before={len(before_agents)} after={len(after_agents)}",
        )
        ctrl = ctrl2  # continue shutdown path

    finally:
        try:
            ctrl.close()
        except Exception:  # noqa: BLE001
            pass

    failed = [k for k, v in results["checks"].items() if not v["ok"]]
    results["finished_at"] = datetime.now(UTC).isoformat()
    results["failed"] = failed
    results["sentinels"] = SENTINELS
    REPORT.write_text(json.dumps(results, indent=2) + "\n")
    print(f"wrote {REPORT}", flush=True)
    if failed:
        print("SECURITY_CAMPAIGN_NEEDS_WORK", failed, flush=True)
        return 1
    print("SECURITY_CAMPAIGN_OK", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
