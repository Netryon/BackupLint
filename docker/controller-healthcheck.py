#!/usr/bin/env python3
"""Liveness/readiness probe for the controller container (no agent credentials).

Proves the HTTPS listener answers GET /v1/health. This is process/service
readiness, not a full fleet correctness check (it does not verify enrollment,
SQLite integrity, or agent connectivity).
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.error
import urllib.request


def main() -> int:
    listen = os.environ.get("BACKUPLINT_CONTROLLER_LISTEN", "0.0.0.0:8443")
    _host, _, port = listen.partition(":")
    if not port.isdigit():
        print("invalid BACKUPLINT_CONTROLLER_LISTEN", file=sys.stderr)
        return 1
    # HTTPS only — never probe plaintext.
    url = f"https://127.0.0.1:{port}/v1/health"
    if not url.startswith("https://"):
        print("healthcheck refuses non-HTTPS URL", file=sys.stderr)
        return 1
    # Loopback probe skips hostname/SAN matching; production agents must still
    # verify the real controller hostname against the CA.
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        # Localhost HTTPS probe only; URL scheme is fixed to https above.
        with urllib.request.urlopen(  # nosec B310
            url, context=context, timeout=3
        ) as response:
            if response.status != 200:
                print(f"unexpected status {response.status}", file=sys.stderr)
                return 1
            raw = response.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"healthcheck failed: {exc}", file=sys.stderr)
        return 1
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        print(f"healthcheck invalid JSON: {exc}", file=sys.stderr)
        return 1
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        print(f"healthcheck unexpected payload: {payload!r}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
