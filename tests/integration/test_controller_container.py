"""Docker runtime hardening checks for the controller image.

Skipped automatically when the Docker daemon is unavailable.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from backuplint.fleet.agent import AgentError, FleetAgent

REPO_ROOT = Path(__file__).resolve().parents[2]
IMAGE = "backuplint-controller:hardening-test"
SIMPLE_COMPOSE = REPO_ROOT / "deploy" / "controller" / "compose.yml"
HARDENED_COMPOSE = REPO_ROOT / "deploy" / "controller" / "compose.hardened.yml"


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    result = subprocess.run(
        ["docker", "info"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.returncode == 0


pytestmark = pytest.mark.skipif(
    not _docker_available(),
    reason="Docker daemon not available",
)


def _run(args: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(REPO_ROOT),
    )


@pytest.fixture(scope="module")
def controller_image() -> Iterator[str]:
    build = _run(
        ["docker", "build", "-f", "Dockerfile.controller", "-t", IMAGE, "."],
        timeout=300,
    )
    assert build.returncode == 0, build.stderr
    yield IMAGE


def _host_port(name: str) -> int:
    inspect = _run(
        ["docker", "inspect", "--format", "{{json .NetworkSettings.Ports}}", name]
    )
    assert inspect.returncode == 0, inspect.stderr
    ports = json.loads(inspect.stdout)
    return int(ports["8443/tcp"][0]["HostPort"])


def _wait_healthy(name: str, *, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        health = _run(
            [
                "docker",
                "inspect",
                "--format",
                "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}",
                name,
            ]
        )
        if health.returncode == 0 and health.stdout.strip() == "healthy":
            return
        probe = _run(
            [
                "docker",
                "exec",
                name,
                "python",
                "/app/docker/controller-healthcheck.py",
            ],
            timeout=30,
        )
        if probe.returncode == 0:
            return
        running = _run(
            ["docker", "inspect", "--format", "{{.State.Running}}", name]
        )
        if running.stdout.strip() != "true":
            break
        time.sleep(1)
    logs = _run(["docker", "logs", name], timeout=30)
    pytest.fail(f"container not healthy; logs:\n{logs.stdout}\n{logs.stderr}")


def _run_hardened(name: str, volume: str, *, hostname: str = "localhost") -> None:
    result = _run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--tmpfs",
            "/tmp:size=32m,mode=1777",
            "-p",
            "127.0.0.1::8443",
            "-e",
            f"BACKUPLINT_CONTROLLER_HOSTNAME={hostname}",
            "-e",
            "HOME=/state",
            "-e",
            "TMPDIR=/tmp",
            "-v",
            f"{volume}:/state",
            IMAGE,
        ]
    )
    assert result.returncode == 0, result.stderr


def test_compose_examples_controller_only() -> None:
    for path in (SIMPLE_COMPOSE, HARDENED_COMPOSE):
        text = path.read_text(encoding="utf-8")
        assert "backuplint-controller" in text
        assert "Dockerfile.controller" in text
        assert "/state" in text
        assert "docker.sock" not in text
        assert "backuplint-agent:" not in text
    hardened = HARDENED_COMPOSE.read_text(encoding="utf-8")
    assert "read_only: true" in hardened
    assert "cap_drop:" in hardened
    assert "no-new-privileges" in hardened
    assert "tmpfs:" in hardened


def test_hardened_runtime_matrix(controller_image: str, tmp_path: Path) -> None:
    volume = f"bl-ctrl-h-{uuid.uuid4().hex[:10]}"
    name1 = f"bl-h1-{uuid.uuid4().hex[:8]}"
    name2 = f"bl-h2-{uuid.uuid4().hex[:8]}"
    name3 = f"bl-h3-{uuid.uuid4().hex[:8]}"
    create_vol = _run(["docker", "volume", "create", volume])
    assert create_vol.returncode == 0, create_vol.stderr
    try:
        _run_hardened(name1, volume)
        _wait_healthy(name1)

        uid = _run(["docker", "exec", name1, "id", "-u"])
        gid = _run(["docker", "exec", name1, "id", "-g"])
        assert uid.stdout.strip() == "10001"
        assert gid.stdout.strip() == "10001"

        # Caps dropped — ambient capabilities should be empty.
        caps = _run(["docker", "exec", name1, "sh", "-c", "grep CapEff /proc/1/status"])
        assert caps.returncode == 0
        # CapEff 0000000000000000 means no effective capabilities.
        assert "0000000000000000" in caps.stdout.replace(" ", "")

        token = _run(
            [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{volume}:/state",
                "--entrypoint",
                "backuplint",
                IMAGE,
                "controller",
                "enroll-token",
                "--data-dir",
                "/state",
                "--label",
                "matrix",
            ]
        )
        assert token.returncode == 0, token.stderr
        token_lines = {
            line.split("=", 1)[0]: line.split("=", 1)[1]
            for line in token.stdout.strip().splitlines()
            if "=" in line
        }
        assert "agent_id" in token_lines and "token" in token_lines
        port = _host_port(name1)
        ca_path = tmp_path / "ca.crt"
        cp = _run(["docker", "cp", f"{name1}:/state/ca/ca.crt", str(ca_path)])
        assert cp.returncode == 0, cp.stderr
        identity = FleetAgent.enroll_with_ca(
            controller_url=f"https://127.0.0.1:{port}",
            token=token_lines["token"],
            agent_id=token_lines["agent_id"],
            ca_cert=ca_path,
            identity_dir=tmp_path / "agent",
            hostname="matrix-host",
        )
        revoke = _run(
            [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{volume}:/state",
                "--entrypoint",
                "backuplint",
                IMAGE,
                "controller",
                "revoke",
                identity.agent_id,
                "--data-dir",
                "/state",
            ]
        )
        assert revoke.returncode == 0, revoke.stderr

        stop = _run(["docker", "stop", "-t", "10", name1])
        assert stop.returncode == 0, stop.stderr
        _run(["docker", "rm", "-f", name1])

        # Recreate with same volume (simulates upgrade/recreate).
        _run_hardened(name2, volume)
        _wait_healthy(name2)
        agents = _run(
            [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{volume}:/state",
                "--entrypoint",
                "backuplint",
                IMAGE,
                "controller",
                "agents",
                "--data-dir",
                "/state",
            ]
        )
        assert agents.returncode == 0, agents.stderr
        assert identity.agent_id in agents.stdout
        assert "revoked" in agents.stdout

        integrity = _run(
            [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{volume}:/state",
                "--entrypoint",
                "python",
                IMAGE,
                "-c",
                "import sqlite3; r=sqlite3.connect('/state/controller.sqlite3')"
                ".execute('PRAGMA integrity_check').fetchone(); "
                "assert r and r[0]=='ok', r",
            ]
        )
        assert integrity.returncode == 0, integrity.stderr

        # Unclean kill + restart.
        kill = _run(["docker", "kill", name2])
        assert kill.returncode == 0, kill.stderr
        _run(["docker", "rm", "-f", name2])
        _run_hardened(name3, volume)
        _wait_healthy(name3)
        agents2 = _run(
            [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{volume}:/state",
                "--entrypoint",
                "backuplint",
                IMAGE,
                "controller",
                "agents",
                "--data-dir",
                "/state",
            ]
        )
        assert identity.agent_id in agents2.stdout
        assert "revoked" in agents2.stdout
    finally:
        for name in (name1, name2, name3):
            _run(["docker", "rm", "-f", name])
        _run(["docker", "volume", "rm", "-f", volume])


def test_tls_wrong_ca_and_plaintext_rejected(
    controller_image: str, tmp_path: Path
) -> None:
    from backuplint.fleet.controller import FleetController

    volume = f"bl-ctrl-tls-{uuid.uuid4().hex[:10]}"
    name = f"bl-tls-{uuid.uuid4().hex[:8]}"
    _run(["docker", "volume", "create", volume])
    try:
        _run_hardened(name, volume)
        _wait_healthy(name)
        port = _host_port(name)
        token = _run(
            [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{volume}:/state",
                "--entrypoint",
                "backuplint",
                IMAGE,
                "controller",
                "enroll-token",
                "--data-dir",
                "/state",
                "--label",
                "tls",
            ]
        )
        assert token.returncode == 0, token.stderr
        token_lines = {
            line.split("=", 1)[0]: line.split("=", 1)[1]
            for line in token.stdout.strip().splitlines()
            if "=" in line
        }
        # Unrelated CA from a second controller instance (valid PEM, wrong trust).
        other = FleetController(tmp_path / "other-ca", hostname="localhost")
        other.close()
        bad_ca = tmp_path / "other-ca" / "ca" / "ca.crt"
        assert bad_ca.is_file()
        with pytest.raises(AgentError, match="enrollment failed"):
            FleetAgent.enroll_with_ca(
                controller_url=f"https://127.0.0.1:{port}",
                token=token_lines["token"],
                agent_id=token_lines["agent_id"],
                ca_cert=bad_ca,
                identity_dir=tmp_path / "bad-agent",
            )

        # Plaintext HTTP must not speak the fleet protocol.
        with socket.create_connection(("127.0.0.1", port), timeout=3) as sock:
            sock.sendall(b"GET /v1/health HTTP/1.1\r\nHost: localhost\r\n\r\n")
            sock.settimeout(2)
            try:
                chunk = sock.recv(64)
            except TimeoutError:
                chunk = b""
        assert b"HTTP/1.1 200" not in chunk
    finally:
        _run(["docker", "rm", "-f", name])
        _run(["docker", "volume", "rm", "-f", volume])


def test_tls_hostname_san_mismatch(controller_image: str, tmp_path: Path) -> None:
    """Server cert always includes localhost/127.0.0.1; mismatch uses URL hostname."""
    import ssl as sslmod
    import urllib.error
    import urllib.request

    volume = f"bl-ctrl-san-{uuid.uuid4().hex[:10]}"
    name = f"bl-san-{uuid.uuid4().hex[:8]}"
    _run(["docker", "volume", "create", volume])
    try:
        _run_hardened(name, volume, hostname="controller.example.test")
        _wait_healthy(name)
        port = _host_port(name)
        ca_path = tmp_path / "ca.crt"
        _run(["docker", "cp", f"{name}:/state/ca/ca.crt", str(ca_path)])
        # Present a hostname that is not in the certificate SAN.
        context = sslmod.create_default_context(cafile=str(ca_path))
        context.minimum_version = sslmod.TLSVersion.TLSv1_2
        url = f"https://127.0.0.1:{port}/v1/health"
        req = urllib.request.Request(url)
        # Force TLS server_hostname to a name absent from SAN.
        try:
            with urllib.request.urlopen(  # nosec B310
                req, context=context, timeout=5
            ) as resp:
                # Default check uses URL host 127.0.0.1 which IS in SAN — must pass.
                assert resp.status == 200
        except urllib.error.URLError as exc:
            pytest.fail(f"127.0.0.1 should be in default SAN: {exc}")

        # Explicit hostname override not in SAN should fail certificate verify.
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            with pytest.raises(sslmod.SSLCertVerificationError):
                context.wrap_socket(sock, server_hostname="not-in-san.example.test")
    finally:
        _run(["docker", "rm", "-f", name])
        _run(["docker", "volume", "rm", "-f", volume])


def test_read_only_state_mount_fails_preflight(
    controller_image: str, tmp_path: Path
) -> None:
    state = tmp_path / "ro-state"
    state.mkdir()
    # Mount host dir read-only so non-root cannot write.
    name = f"bl-rofail-{uuid.uuid4().hex[:8]}"
    result = _run(
        [
            "docker",
            "run",
            "--name",
            name,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--tmpfs",
            "/tmp:size=8m,mode=1777",
            "-e",
            "BACKUPLINT_CONTROLLER_HOSTNAME=localhost",
            "-v",
            f"{state}:/state:ro",
            IMAGE,
        ],
        timeout=60,
    )
    logs = _run(["docker", "logs", name])
    _run(["docker", "rm", "-f", name])
    assert result.returncode != 0
    combined = (result.stderr or "") + (logs.stdout or "") + (logs.stderr or "")
    assert "ERROR" in combined or "not writable" in combined or "Read-only" in combined


def test_healthcheck_script_https_only() -> None:
    import sys

    env = os.environ.copy()
    env["BACKUPLINT_CONTROLLER_LISTEN"] = "127.0.0.1:1"
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "docker" / "controller-healthcheck.py")],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode != 0


def test_image_has_oci_labels_and_no_docker_cli(controller_image: str) -> None:
    labels = _run(
        ["docker", "inspect", "--format", "{{json .Config.Labels}}", IMAGE]
    )
    assert labels.returncode == 0
    data = json.loads(labels.stdout)
    assert data.get("org.opencontainers.image.title") == "BackupLint Controller"
    assert "org.opencontainers.image.version" in data
    which = _run(
        ["docker", "run", "--rm", "--entrypoint", "sh", IMAGE, "-c", "command -v docker || true"]
    )
    assert which.stdout.strip() == ""
    which_restic = _run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            IMAGE,
            "-c",
            "command -v restic || true",
        ]
    )
    assert which_restic.stdout.strip() == ""
