#!/usr/bin/env python3
"""Short native dashboard smoke for a Linux guest (LXD-friendly)."""

from __future__ import annotations

import json
import ssl
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path


def main() -> int:
    from backuplint.fleet.controller import FleetController
    from backuplint.fleet.dashboard.config import DashboardConfig

    platform = Path("/etc/os-release").read_text(encoding="utf-8")
    pretty = next(
        (
            line.split("=", 1)[1].strip().strip('"')
            for line in platform.splitlines()
            if line.startswith("PRETTY_NAME=")
        ),
        "unknown",
    )
    with tempfile.TemporaryDirectory(prefix="bl-dash-smoke-") as raw:
        data = Path(raw)
        cfg = DashboardConfig(enabled=True, cookie_secure=True)
        controller = FleetController(data, hostname="localhost", dashboard=cfg)
        controller.dashboard_auth.set_password("dashboard-platform-smoke")
        try:
            host, port = controller.start(host="127.0.0.1", port=0)
            base = f"https://{host}:{port}"
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            jar = CookieJar()
            opener = urllib.request.build_opener(
                urllib.request.HTTPSHandler(context=ctx),
                urllib.request.HTTPCookieProcessor(jar),
            )
            # unauth
            try:
                opener.open(f"{base}/v1/dashboard/overview", timeout=10)
                return 2
            except urllib.error.HTTPError as exc:
                if exc.code != 401:
                    return 3
            body = urllib.parse.urlencode(
                {"password": "dashboard-platform-smoke"}
            ).encode()
            req = urllib.request.Request(
                f"{base}/dashboard/login",
                data=body,
                method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            with opener.open(req, timeout=10):
                pass
            checks = {}
            for path in (
                "/dashboard/",
                "/dashboard/history",
                "/v1/dashboard/overview",
                "/v1/dashboard/agents?limit=5",
                "/v1/dashboard/events?limit=5",
            ):
                with opener.open(f"{base}{path}", timeout=10) as resp:
                    checks[path] = resp.status
                    resp.read()
            out = {
                "platform": pretty,
                "python": sys.version.split()[0],
                "ok": all(code == 200 for code in checks.values()),
                "checks": checks,
            }
            print(json.dumps(out, indent=2, sort_keys=True))
            return 0 if out["ok"] else 1
        finally:
            controller.close()


if __name__ == "__main__":
    raise SystemExit(main())
