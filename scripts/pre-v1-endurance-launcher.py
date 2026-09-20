#!/usr/bin/env python3
"""Detached launcher for the real pre-v1 endurance harness.

Replaces the previous placeholder monitor stub. Prefer:

  systemd-run --user --unit=backuplint-pre-v1-endurance \\
    .../python scripts/pre-v1-endurance-launcher.py --seconds 259200 ...

Or:

  nohup/setsid as below.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=72 * 3600)
    parser.add_argument("--agents", type=int, default=50)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--campaign-id", default="")
    parser.add_argument(
        "--foreground",
        action="store_true",
        help="run harness in-process (for smoke); default re-exec via setsid",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    harness = root / "scripts" / "pre-v1-endurance-harness.py"
    cmd = [
        sys.executable,
        str(harness),
        "--seconds",
        str(args.seconds),
        "--agents",
        str(args.agents),
        "--data-dir",
        str(args.data_dir),
        "--evidence-dir",
        str(args.evidence_dir),
    ]
    if args.campaign_id:
        cmd.extend(["--campaign-id", args.campaign_id])

    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    args.data_dir.mkdir(parents=True, exist_ok=True)
    log = args.evidence_dir / "harness.log"

    if args.foreground:
        return subprocess.call(cmd, cwd=str(root))

    # Detach so terminal disconnect does not kill the campaign.
    with log.open("a", encoding="utf-8") as handle:
        proc = subprocess.Popen(  # noqa: S603
            ["setsid", *cmd],
            cwd=str(root),
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=os.environ.copy(),
        )
    (args.evidence_dir / "launcher.pid").write_text(str(proc.pid) + "\n", encoding="utf-8")
    print(f"LAUNCHED pid={proc.pid} log={log} evidence={args.evidence_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
