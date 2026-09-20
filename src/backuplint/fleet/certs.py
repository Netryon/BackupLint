"""TLS certificate helpers for the BackupLint fleet (openssl + ssl stdlib)."""

from __future__ import annotations

import os
from pathlib import Path

from backuplint.process import run_argv


class CertError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _run_openssl(args: list[str]) -> None:
    completed = run_argv(["openssl", *args], timeout=60)
    if completed.returncode != 0:
        err = (completed.stderr or completed.stdout or "").strip()
        raise CertError(f"openssl failed: {err or 'unknown error'}")


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def init_ca(ca_dir: Path, *, common_name: str = "BackupLint Fleet CA") -> None:
    ensure_dir(ca_dir)
    key = ca_dir / "ca.key"
    cert = ca_dir / "ca.crt"
    if key.exists() and cert.exists():
        return
    _run_openssl(
        [
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-sha256",
            "-days",
            "3650",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-config",
            str(_openssl_ca_config(ca_dir, common_name)),
        ]
    )
    os.chmod(key, 0o600)
    os.chmod(cert, 0o644)


def _openssl_ca_config(ca_dir: Path, common_name: str) -> Path:
    cfg = ca_dir / "ca.cnf"
    cfg.write_text(
        f"""[req]
distinguished_name=req_distinguished_name
x509_extensions=v3_ca
prompt=no

[req_distinguished_name]
CN={common_name}

[v3_ca]
basicConstraints=critical,CA:TRUE
keyUsage=critical,keyCertSign,cRLSign
subjectKeyIdentifier=hash
""",
        encoding="utf-8",
    )
    return cfg


def issue_server_cert(
    ca_dir: Path,
    out_dir: Path,
    *,
    common_name: str,
    days: int = 825,
) -> tuple[Path, Path]:
    ensure_dir(out_dir)
    key = out_dir / "server.key"
    csr = out_dir / "server.csr"
    cert = out_dir / "server.crt"
    if key.exists() and cert.exists():
        return key, cert
    _run_openssl(
        [
            "req",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(csr),
            "-subj",
            f"/CN={common_name}",
        ]
    )
    # SAN: always include localhost for local labs; also include the requested name.
    san_parts = ["DNS:localhost", "IP:127.0.0.1"]
    if common_name not in {"localhost", "127.0.0.1"}:
        if _looks_like_ip(common_name):
            san_parts.append(f"IP:{common_name}")
        else:
            san_parts.append(f"DNS:{common_name}")
    ext = out_dir / "server.ext"
    ext.write_text(
        f"subjectAltName={','.join(san_parts)}\n"
        "extendedKeyUsage=serverAuth\n",
        encoding="utf-8",
    )
    _run_openssl(
        [
            "x509",
            "-req",
            "-in",
            str(csr),
            "-CA",
            str(ca_dir / "ca.crt"),
            "-CAkey",
            str(ca_dir / "ca.key"),
            "-CAcreateserial",
            "-out",
            str(cert),
            "-days",
            str(days),
            "-sha256",
            "-extfile",
            str(ext),
        ]
    )
    os.chmod(key, 0o600)
    csr.unlink(missing_ok=True)
    ext.unlink(missing_ok=True)
    return key, cert


def _looks_like_ip(value: str) -> bool:
    parts = value.split(".")
    if len(parts) != 4:
        return False
    try:
        return all(0 <= int(p) <= 255 for p in parts)
    except ValueError:
        return False


def issue_client_cert(
    ca_dir: Path,
    out_dir: Path,
    *,
    agent_id: str,
    days: int = 825,
) -> tuple[Path, Path]:
    """Issue a client cert by generating a key on the controller host.

    Deprecated for fleet enrollment: prefer :func:`generate_agent_key_and_csr`
    on the agent and :func:`sign_client_csr` on the controller so the private
    key never leaves the agent. Retained for lab helpers that already expect
    a controller-written keypair.
    """
    ensure_dir(out_dir)
    key = out_dir / "client.key"
    csr = out_dir / "client.csr"
    cert = out_dir / "client.crt"
    _run_openssl(
        [
            "req",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(csr),
            "-subj",
            f"/CN={agent_id}",
        ]
    )
    ext = out_dir / "client.ext"
    ext.write_text(
        f"subjectAltName=DNS:{agent_id}\nextendedKeyUsage=clientAuth\n",
        encoding="utf-8",
    )
    _run_openssl(
        [
            "x509",
            "-req",
            "-in",
            str(csr),
            "-CA",
            str(ca_dir / "ca.crt"),
            "-CAkey",
            str(ca_dir / "ca.key"),
            "-CAcreateserial",
            "-out",
            str(cert),
            "-days",
            str(days),
            "-sha256",
            "-extfile",
            str(ext),
        ]
    )
    os.chmod(key, 0o600)
    csr.unlink(missing_ok=True)
    ext.unlink(missing_ok=True)
    return key, cert


def generate_agent_key_and_csr(
    identity_dir: Path,
    *,
    agent_id: str,
) -> str:
    """Generate agent private key locally and return CSR PEM.

    Private key is written only under ``identity_dir`` with mode 0600 and is
    never returned to callers as a string (avoid log/JSON leakage).
    """
    ensure_dir(identity_dir)
    key = identity_dir / "client.key"
    csr = identity_dir / "client.csr"
    if key.exists():
        raise CertError("client.key already exists; refusing to overwrite")
    _run_openssl(
        [
            "req",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(csr),
            "-subj",
            f"/CN={agent_id}",
        ]
    )
    os.chmod(key, 0o600)
    os.chmod(identity_dir, 0o700)
    csr_pem = csr.read_text(encoding="utf-8")
    csr.unlink(missing_ok=True)
    if "BEGIN CERTIFICATE REQUEST" not in csr_pem:
        raise CertError("openssl produced an invalid CSR")
    return csr_pem


def sign_client_csr(
    ca_dir: Path,
    *,
    agent_id: str,
    csr_pem: str,
    days: int = 825,
) -> tuple[str, str, str]:
    """Sign an agent CSR. Returns (cert_pem, serial_hex, sha256_fingerprint).

    Does not accept or persist any private key material.
    """
    if "PRIVATE KEY" in csr_pem.upper():
        raise CertError("CSR payload must not contain a private key")
    if "BEGIN CERTIFICATE REQUEST" not in csr_pem:
        raise CertError("malformed CSR")
    # Bind CN to intended agent identity.
    import tempfile

    with tempfile.TemporaryDirectory(prefix="bl-csr-") as tmp:
        tmp_path = Path(tmp)
        csr_path = tmp_path / "client.csr"
        cert_path = tmp_path / "client.crt"
        ext_path = tmp_path / "client.ext"
        csr_path.write_text(csr_pem, encoding="utf-8")
        # Verify subject CN matches agent_id before signing.
        completed = run_argv(
            ["openssl", "req", "-in", str(csr_path), "-noout", "-subject"],
            timeout=30,
        )
        if completed.returncode != 0:
            raise CertError("unable to parse CSR subject")
        subject = (completed.stdout or "").strip()
        # OpenSSL subject forms vary; extract CN as a discrete RDN and require
        # exact equality with agent_id (reject substring spoofs like agent-10
        # matching agent-1).
        import re

        body = subject
        if body.lower().startswith("subject="):
            body = body[len("subject=") :]
        body = re.sub(r"\s*=\s*", "=", body)
        match = re.search(r"(?:^|[,/])CN=([^,/]+)", body)
        if match is None or match.group(1) != agent_id:
            raise CertError("CSR CN does not match intended agent_id")
        ext_path.write_text(
            f"subjectAltName=DNS:{agent_id}\nextendedKeyUsage=clientAuth\n",
            encoding="utf-8",
        )
        _run_openssl(
            [
                "x509",
                "-req",
                "-in",
                str(csr_path),
                "-CA",
                str(ca_dir / "ca.crt"),
                "-CAkey",
                str(ca_dir / "ca.key"),
                "-CAcreateserial",
                "-out",
                str(cert_path),
                "-days",
                str(days),
                "-sha256",
                "-extfile",
                str(ext_path),
            ]
        )
        cert_pem = cert_path.read_text(encoding="utf-8")
        serial_out = run_argv(
            ["openssl", "x509", "-in", str(cert_path), "-noout", "-serial"],
            timeout=30,
        )
        fp_out = run_argv(
            [
                "openssl",
                "x509",
                "-in",
                str(cert_path),
                "-noout",
                "-fingerprint",
                "-sha256",
            ],
            timeout=30,
        )
        if serial_out.returncode != 0 or fp_out.returncode != 0:
            raise CertError("unable to read issued certificate metadata")
        serial = (serial_out.stdout or "").strip().removeprefix("serial=").strip()
        fingerprint = (
            (fp_out.stdout or "").strip().split("=", 1)[-1].replace(":", "").lower()
        )
        return cert_pem, serial, fingerprint
