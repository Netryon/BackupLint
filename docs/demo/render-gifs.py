#!/usr/bin/env python3
"""Render clean README demo GIFs/PNGs from real BackupLint output."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent.parent
VENV_BIN = REPO / ".venv" / "bin"
DEMO = Path(os.environ.get("BACKUPLINT_DEMO_DIR", "/srv/backuplint-demo"))

SCENES = [
    ("pass", "backup-audit-pass", "PASS — all persistent paths covered"),
    ("fail", "backup-audit-fail", "FAIL — vaultwarden data not backed up"),
    ("restic", "restic", "Restic — relevant snapshot coverage"),
]


def run_scan(name: str) -> tuple[str, int]:
    cwd = DEMO / name
    env = os.environ.copy()
    env["PATH"] = f"{VENV_BIN}:{env.get('PATH', '')}"
    env["NO_COLOR"] = "1"
    env["TERM"] = "dumb"
    # Avoid virtualenv prompt noise in any nested shells.
    env.pop("VIRTUAL_ENV", None)
    env.pop("VIRTUAL_ENV_PROMPT", None)
    proc = subprocess.run(
        [str(VENV_BIN / "backuplint"), "scan", "compose.yml"],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    body = (proc.stdout or "").rstrip()
    # Keep only the audit body — no shell chrome, no venv prompts.
    text = f"$ backuplint scan compose.yml\n{body}\n"
    return text, proc.returncode


def _fonts() -> tuple[ImageFont.ImageFont, ImageFont.ImageFont]:
    for fp in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    ):
        if Path(fp).exists():
            return ImageFont.truetype(fp, 15), ImageFont.truetype(fp, 16)
    default = ImageFont.load_default()
    return default, default


def render_image(text: str, title: str) -> Image.Image:
    font, title_font = _fonts()
    lines = [line for line in text.splitlines() if line.strip() != "(.venv)"]
    pad = 24
    line_h = 22
    width = 920
    height = pad * 2 + 40 + line_h * max(len(lines), 1)
    img = Image.new("RGB", (width, height), "#1e1e2e")
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, 0, width, 36), fill="#181825")
    draw.text((pad, 9), title, fill="#cba6f7", font=title_font)
    y = 48
    for line in lines:
        color = "#cdd6f4"
        if line.startswith("$"):
            color = "#a6e3a1"
        elif "Result: FAIL" in line or line.startswith("✗"):
            color = "#f38ba8"
        elif "Result: PASS" in line or line.startswith("✓"):
            color = "#a6e3a1"
        elif line.startswith("⚠") or "Result: WARN" in line:
            color = "#f9e2af"
        draw.text((pad, y), line[:110], fill=color, font=font)
        y += line_h
    return img


def save_gif(img: Image.Image, out: Path) -> None:
    # Two nearly identical frames: short, no blank-enter pauses.
    frame2 = img.copy()
    img.save(out, save_all=True, append_images=[frame2], duration=600, loop=0)
    print(f"wrote {out} ({out.stat().st_size} bytes)")


def main() -> int:
    if not DEMO.exists():
        print("Run docs/demo/prepare-fixtures.sh first", file=sys.stderr)
        return 2

    expected_exits = {"pass": 0, "fail": 1, "restic": 0}
    for name, stem, title in SCENES:
        text, code = run_scan(name)
        if code != expected_exits[name]:
            print(f"{name}: unexpected exit {code}\n{text}", file=sys.stderr)
            return 1
        if "(.venv)" in text or "sysadmin" in text or "backuplint-demo" in text:
            print(f"{name}: presentation leak in output:\n{text}", file=sys.stderr)
            return 1
        img = render_image(text, title)
        save_gif(img, ROOT / f"{stem}.gif")
        png = ROOT / f"{stem if stem != 'restic' else 'restic-audit'}.png"
        if stem == "restic":
            png = ROOT / "restic-audit.png"
        elif stem == "backup-audit-pass":
            png = ROOT / "backup-audit-pass.png"
        else:
            png = ROOT / "backup-audit-fail.png"
        img.save(png)
        print(f"wrote {png} ({png.stat().st_size} bytes)")
    # Remove obsolete restic filename if present
    old = ROOT / "backup-audit-restic.gif"
    if old.exists():
        old.unlink()
        print(f"removed {old}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
