#!/usr/bin/env bash
set -euo pipefail
EVIDENCE="${BACKUPLINT_AT_EVIDENCE:-${TMPDIR:-/tmp}/backuplint-advanced-test/siem-receiver}"
mkdir -p "$EVIDENCE"
PORT="${1:-8443}"
# Ephemeral self-signed HTTPS sink for SIEM export tests.
python3 - <<PY
from http.server import BaseHTTPRequestHandler, HTTPServer
import ssl, json, time
from pathlib import Path
ev = Path("$EVIDENCE")
received = ev / "received.jsonl"
class H(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(n)
        with received.open("a") as f:
            f.write(json.dumps({"ts": time.time(), "path": self.path, "bytes": len(body)}) + "\n")
        self.send_response(200); self.end_headers(); self.wfile.write(b'{"ok":true}')
    def log_message(self, *a):
        return
# Expect operator-provided cert/key or generate for workshop only
cert, key = ev / "recv.crt", ev / "recv.key"
if not cert.exists():
    import subprocess
    subprocess.check_call([
        "openssl","req","-x509","-newkey","rsa:2048","-keyout",str(key),"-out",str(cert),
        "-days","1","-nodes","-subj","/CN=siem-lab"
    ])
httpd = HTTPServer(("127.0.0.1", int("$PORT")), H)
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
print(f"SIEM_RECEIVER_UP https://127.0.0.1:$PORT evidence=$EVIDENCE")
print("Run until Ctrl-C; point controller SIEM endpoint here for advanced proof.")
httpd.serve_forever()
PY
