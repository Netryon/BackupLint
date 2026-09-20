"""Controller shutdown and durable state tests (containerization slice)."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from backuplint.fleet.controller import FleetController
from backuplint.fleet.protocol import PROTOCOL_VERSION


def _health_ok(port: int) -> bool:
    import ssl
    import urllib.error
    import urllib.request

    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    url = f"https://127.0.0.1:{port}/v1/health"
    try:
        with urllib.request.urlopen(url, context=context, timeout=2) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def test_wait_until_stopped_honors_stop_event(tmp_path: Path) -> None:
    data_dir = tmp_path / "controller"
    controller = FleetController(data_dir, hostname="localhost")
    stop = threading.Event()
    host, port = controller.start(host="127.0.0.1", port=0)
    assert host == "127.0.0.1"
    assert _health_ok(port)

    def _stop_soon() -> None:
        time.sleep(0.2)
        stop.set()

    threading.Thread(target=_stop_soon, daemon=True).start()
    controller.wait_until_stopped(stop_event=stop, poll_seconds=0.05)
    assert not _health_ok(port)
    # Store still usable until close(); then durable files remain on disk.
    assert (data_dir / "controller.sqlite3").is_file()
    assert (data_dir / "ca" / "ca.crt").is_file()
    controller.close()


def test_controller_state_survives_process_recreation(tmp_path: Path) -> None:
    data_dir = tmp_path / "controller"
    first = FleetController(data_dir, hostname="localhost")
    try:
        token = first.create_enroll_token(label="persist-lab", ttl_hours=1)
        first.store.register_agent(
            agent_id="agent-persist01",
            label="persist-lab",
            hostname="host-a",
        )
        first.store.set_agent_status("agent-persist01", "revoked")
        agents = {a.agent_id: a.status for a in first.store.list_agents()}
        assert agents["agent-persist01"] == "revoked"
        assert token
    finally:
        first.close()

    second = FleetController(data_dir, hostname="localhost")
    try:
        agents = {a.agent_id: a for a in second.store.list_agents()}
        assert "agent-persist01" in agents
        assert agents["agent-persist01"].status == "revoked"
        assert agents["agent-persist01"].label == "persist-lab"
        assert (data_dir / "ca" / "ca.key").is_file()
        assert (data_dir / "server" / "server.crt").is_file()
    finally:
        second.close()


def test_controller_run_exits_cleanly_on_sigterm(tmp_path: Path) -> None:
    import socket

    data_dir = tmp_path / "controller-cli"
    env = os.environ.copy()
    src = Path(__file__).resolve().parents[2] / "src"
    env["PYTHONPATH"] = str(src) + os.pathsep + env.get("PYTHONPATH", "")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "backuplint",
            "controller",
            "run",
            "--data-dir",
            str(data_dir),
            "--listen",
            f"127.0.0.1:{port}",
            "--hostname",
            "localhost",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        deadline = time.time() + 15
        while time.time() < deadline:
            if _health_ok(port):
                break
            if proc.poll() is not None:
                stderr = proc.stderr.read() if proc.stderr else ""
                pytest.fail(f"controller exited early: {stderr!r}")
            time.sleep(0.1)
        else:
            stderr = proc.stderr.read() if proc.stderr else ""
            pytest.fail(f"controller never became healthy: {stderr!r}")

        proc.send_signal(signal.SIGTERM)
        try:
            returncode = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            pytest.fail("controller did not exit within timeout after SIGTERM")
        assert returncode == 0
        assert (data_dir / "controller.sqlite3").is_file()
        assert (data_dir / "ca" / "ca.crt").is_file()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_health_endpoint_has_no_side_effects(tmp_path: Path) -> None:
    data_dir = tmp_path / "controller"
    controller = FleetController(data_dir, hostname="localhost")
    _host, port = controller.start(host="127.0.0.1", port=0)
    try:
        before = list(controller.store.list_agents())
        assert _health_ok(port)
        assert _health_ok(port)
        after = list(controller.store.list_agents())
        assert before == after
        import json
        import ssl
        import urllib.request

        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(
            f"https://127.0.0.1:{port}/v1/health", context=context, timeout=2
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
        assert payload["ok"] is True
        assert payload["protocol_version"] == PROTOCOL_VERSION
        assert "software_version" in payload
        assert "supported_protocol_versions" in payload
    finally:
        controller.close()
