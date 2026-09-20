"""Systemd unit generation for native BackupLint installs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from backuplint.install.features import FeatureId
from backuplint.install.layout import NativeLayout
from backuplint.install.roles import InstallationRole


@dataclass(frozen=True, slots=True)
class SystemdUnit:
    name: str
    content: str
    enable: bool
    reason: str

    @property
    def filename(self) -> str:
        return self.name if self.name.endswith(".service") else f"{self.name}.service"

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.filename,
            "enable": self.enable,
            "reason": self.reason,
            "bytes": len(self.content.encode("utf-8")),
        }


def generate_units(
    *,
    role: InstallationRole,
    features: tuple[FeatureId, ...],
    layout: NativeLayout,
    backuplint_bin: str = "/usr/bin/backuplint",
    user: str = "backuplint",
    group: str = "backuplint",
) -> tuple[SystemdUnit, ...]:
    """Generate role-appropriate units. Avoid duplicate controllers/schedulers."""
    feature_set = set(features)
    units: list[SystemdUnit] = []

    want_scheduler = FeatureId.SCHEDULER in feature_set
    want_controller = FeatureId.FLEET_CONTROLLER in feature_set

    # Rewrite hardening ReadWritePaths for prefixed layouts (tests/labs).
    rw_paths = f"{layout.state} {layout.log} {layout.run} {layout.etc}"
    # Sandbox directives compatible with controller TLS networking and
    # scheduler access to configured state/config paths. Docker socket (when
    # used by the scheduler) remains reachable under /var/run.
    harden = (
        "NoNewPrivileges=true\n"
        "PrivateTmp=true\n"
        "ProtectSystem=strict\n"
        "ProtectHome=true\n"
        "ProtectKernelTunables=true\n"
        "ProtectKernelModules=true\n"
        "ProtectControlGroups=true\n"
        "RestrictSUIDSGID=true\n"
        f"ReadWritePaths={rw_paths}\n"
    )

    if want_controller:
        unit = (
            "[Unit]\n"
            "Description=BackupLint fleet controller\n"
            "After=network-online.target\n"
            "Wants=network-online.target\n"
            "\n"
            "[Service]\n"
            "Type=simple\n"
            f"User={user}\n"
            f"Group={group}\n"
            f"WorkingDirectory={layout.etc}\n"
            f"ExecStart={backuplint_bin} controller run "
            f"--data-dir {layout.controller_state} "
            f"--listen 127.0.0.1:8443 "
            f"--hostname localhost\n"
            "Restart=on-failure\n"
            "RestartSec=5\n"
            f"{harden}"
            "\n"
            "[Install]\n"
            "WantedBy=multi-user.target\n"
        )
        units.append(
            SystemdUnit(
                name="backuplint-controller.service",
                content=unit,
                enable=True,
                reason="fleet_controller feature enabled",
            )
        )

    if want_scheduler:
        compose = layout.etc / "compose.yml"
        unit = (
            "[Unit]\n"
            "Description=BackupLint local scheduler\n"
            "After=network-online.target docker.service\n"
            "Wants=network-online.target\n"
            "\n"
            "[Service]\n"
            "Type=simple\n"
            f"User={user}\n"
            f"Group={group}\n"
            f"WorkingDirectory={layout.etc}\n"
            f"ExecStart={backuplint_bin} daemon {compose} "
            f"--config {layout.config_file}\n"
            "Restart=on-failure\n"
            "RestartSec=5\n"
            f"{harden}"
            "\n"
            "[Install]\n"
            "WantedBy=multi-user.target\n"
        )
        units.append(
            SystemdUnit(
                name="backuplint-scheduler.service",
                content=unit,
                enable=True,
                reason="scheduler feature enabled",
            )
        )

    # Agent enroll/submit is one-shot; no separate long-running agent unit.
    # Agent+scheduler uses scheduler unit only (no duplicate).
    _ = role
    return tuple(units)


def systemd_available(*, systemctl_path: Path | None = None) -> bool:
    from shutil import which

    return which("systemctl") is not None or (
        systemctl_path is not None and systemctl_path.exists()
    )


def plan_systemd_commands(units: tuple[SystemdUnit, ...]) -> tuple[tuple[str, ...], ...]:
    cmds: list[tuple[str, ...]] = [("systemctl", "daemon-reload")]
    for unit in units:
        if unit.enable:
            cmds.append(("systemctl", "enable", unit.filename))
    return tuple(cmds)
