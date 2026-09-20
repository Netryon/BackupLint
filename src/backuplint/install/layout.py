"""Native filesystem layout for BackupLint installer-managed paths."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from backuplint.install.features import FeatureId
from backuplint.install.roles import InstallationRole


class LayoutError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True, slots=True)
class ManagedPath:
    path: Path
    mode: int
    kind: str  # directory | file
    purpose: str

    def to_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "mode": oct(self.mode),
            "kind": self.kind,
            "purpose": self.purpose,
        }


@dataclass(frozen=True, slots=True)
class NativeLayout:
    """Logical native layout. Prefix overrides support tests/labs."""

    prefix: Path
    etc: Path
    state: Path
    log: Path
    run: Path
    config_file: Path
    schedule_state: Path
    controller_state: Path
    agent_identity: Path
    agent_queue: Path
    manifest_path: Path
    systemd_dir: Path

    def to_dict(self) -> dict[str, str]:
        return {
            "prefix": str(self.prefix),
            "etc": str(self.etc),
            "state": str(self.state),
            "log": str(self.log),
            "run": str(self.run),
            "config_file": str(self.config_file),
            "schedule_state": str(self.schedule_state),
            "controller_state": str(self.controller_state),
            "agent_identity": str(self.agent_identity),
            "agent_queue": str(self.agent_queue),
            "manifest_path": str(self.manifest_path),
            "systemd_dir": str(self.systemd_dir),
        }


def resolve_layout(*, prefix: Path | None = None) -> NativeLayout:
    """Resolve layout under an optional prefix (default: system roots)."""
    root = Path("/") if prefix is None else prefix
    if prefix is not None:
        # Lab/test prefix: keep relative structure under prefix.
        etc = root / "etc" / "backuplint"
        state = root / "var" / "lib" / "backuplint"
        log = root / "var" / "log" / "backuplint"
        run = root / "run" / "backuplint"
        systemd_dir = root / "etc" / "systemd" / "system"
    else:
        etc = Path("/etc/backuplint")
        state = Path("/var/lib/backuplint")
        log = Path("/var/log/backuplint")
        run = Path("/run/backuplint")
        systemd_dir = Path("/etc/systemd/system")
    return NativeLayout(
        prefix=root,
        etc=etc,
        state=state,
        log=log,
        run=run,
        config_file=etc / "backuplint.yml",
        schedule_state=state / "schedule",
        controller_state=state / "controller",
        agent_identity=state / "agent",
        agent_queue=state / "agent" / "queue.jsonl",
        manifest_path=state / "install-manifest.json",
        systemd_dir=systemd_dir,
    )


def planned_paths(
    layout: NativeLayout,
    *,
    role: InstallationRole,
    features: tuple[FeatureId, ...],
) -> tuple[ManagedPath, ...]:
    paths: list[ManagedPath] = [
        ManagedPath(layout.etc, 0o750, "directory", "configuration"),
        ManagedPath(layout.state, 0o750, "directory", "state root"),
        ManagedPath(layout.log, 0o750, "directory", "logs"),
        ManagedPath(layout.run, 0o750, "directory", "runtime"),
    ]
    feature_set = set(features)
    if FeatureId.SCHEDULER in feature_set:
        paths.append(
            ManagedPath(
                layout.schedule_state, 0o700, "directory", "scheduler state"
            )
        )
    if FeatureId.FLEET_CONTROLLER in feature_set:
        paths.append(
            ManagedPath(
                layout.controller_state, 0o700, "directory", "controller data"
            )
        )
    if FeatureId.FLEET_AGENT in feature_set:
        paths.append(
            ManagedPath(
                layout.agent_identity, 0o700, "directory", "agent identity"
            )
        )
    # Config stub path is always planned for native installs with local verification
    # or all-in-one/agent/standalone; controller-only may still want etc dir.
    if role is not InstallationRole.CONTROLLER or FeatureId.COVERAGE in feature_set:
        paths.append(
            ManagedPath(layout.config_file, 0o640, "file", "backuplint config stub")
        )
    paths.append(
        ManagedPath(layout.manifest_path, 0o640, "file", "installation manifest")
    )
    return tuple(paths)


def _ensure_not_symlink(path: Path) -> None:
    if path.is_symlink():
        raise LayoutError(f"refusing to use symlink path: {path}")


def create_directories(
    paths: tuple[ManagedPath, ...],
    *,
    dry_run: bool = False,
) -> list[Path]:
    """Create installer-owned directories with restrictive modes.

    Returns paths created in this call (for rollback). Never follows/creates via
    intermediate symlinks when the final path already exists as a symlink.
    """
    created: list[Path] = []
    if dry_run:
        return created
    for item in paths:
        if item.kind != "directory":
            continue
        path = item.path
        if path.exists():
            _ensure_not_symlink(path)
            if not path.is_dir():
                raise LayoutError(f"path exists and is not a directory: {path}")
            os.chmod(path, item.mode)
            continue
        # Create parents carefully.
        path.mkdir(parents=True, exist_ok=False, mode=item.mode)
        os.chmod(path, item.mode)  # apply without umask surprises
        _ensure_not_symlink(path)
        created.append(path)
    return created


def require_service_account(
    *, user: str = "backuplint", group: str = "backuplint"
) -> tuple[int, int]:
    """Fail closed if the systemd service account is missing. Returns (uid, gid)."""
    import grp
    import pwd

    try:
        pw = pwd.getpwnam(user)
    except KeyError as exc:
        raise LayoutError(
            f"service user {user!r} does not exist; create with e.g. "
            f"`useradd --system --home /var/lib/backuplint --shell /usr/sbin/nologin {user}` "
            "before enabling systemd units"
        ) from exc
    try:
        gr = grp.getgrnam(group)
    except KeyError as exc:
        raise LayoutError(
            f"service group {group!r} does not exist; create it before enabling units"
        ) from exc
    return int(pw.pw_uid), int(gr.gr_gid)


def apply_ownership(
    paths: tuple[ManagedPath, ...],
    *,
    uid: int,
    gid: int,
    dry_run: bool = False,
) -> None:
    """chown installer-managed paths to the service account (best-effort on files)."""
    if dry_run:
        return
    for item in paths:
        path = item.path
        if not path.exists():
            continue
        _ensure_not_symlink(path)
        os.chown(path, uid, gid)
    return None


def atomic_write_text(
    path: Path,
    content: str,
    *,
    mode: int = 0o640,
    dry_run: bool = False,
) -> bool:
    """Atomically write text via temp file + replace. Returns True if wrote."""
    if dry_run:
        return False
    if path.is_symlink() or path.exists():
        _ensure_not_symlink(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        fd = os.open(
            tmp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            mode,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        os.chmod(path, mode & 0o777)
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise
    return True


def default_config_stub(*, role: InstallationRole, schedule_state: Path) -> str:
    """Minimal non-secret config stub. Does not enable features silently beyond role."""
    lines = [
        "# Generated by BackupLint native installer (no secrets).",
        "# Review and adjust before enabling production checks.",
        "backup_paths: []",
        "",
    ]
    if role in {
        InstallationRole.STANDALONE,
        InstallationRole.AGENT,
        InstallationRole.ALL_IN_ONE,
    }:
        lines.extend(
            [
                "schedule:",
                f"  state_dir: {schedule_state}",
                "  coverage:",
                "    every: 30m",
                "  integrity:",
                "    enabled: false",
                "    every: 6h",
                "  restore_verification:",
                "    enabled: false",
                "    every: 24h",
                "  deep_integrity:",
                "    enabled: false",
                "    every: 7d",
                "",
            ]
        )
    return "\n".join(lines) + "\n"


def is_world_readable(mode: int) -> bool:
    return bool(mode & stat.S_IROTH)
