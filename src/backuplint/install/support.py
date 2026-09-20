"""Support status for profile role/deployment combinations."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from backuplint.install.deployment import DeploymentForm
from backuplint.install.roles import InstallationRole


class ImplementationStatus(StrEnum):
    """Whether BackupLint currently implements a valid architecture choice."""

    IMPLEMENTED = "implemented"
    LIMITED = "limited"
    PLANNED = "planned"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class SupportAssessment:
    """Separates architectural validity from current product support."""

    architecture_valid: bool
    implementation: ImplementationStatus
    detail: str

    @property
    def usable_now(self) -> bool:
        return self.architecture_valid and self.implementation in {
            ImplementationStatus.IMPLEMENTED,
            ImplementationStatus.LIMITED,
        }


def assess_support(
    role: InstallationRole, deployment: DeploymentForm
) -> SupportAssessment:
    """Return support status without performing any deployment action."""
    if deployment is DeploymentForm.NATIVE:
        return SupportAssessment(
            architecture_valid=True,
            implementation=ImplementationStatus.IMPLEMENTED,
            detail=f"Native {role.value} installation is supported.",
        )

    # deployment == container
    if role is InstallationRole.CONTROLLER:
        return SupportAssessment(
            architecture_valid=True,
            implementation=ImplementationStatus.PLANNED,
            detail=(
                "Controller + container is a valid architecture; container runtime "
                "artifacts are owned by the controller-containerization track and "
                "are not assumed present in every tree yet."
            ),
        )
    if role is InstallationRole.AGENT:
        return SupportAssessment(
            architecture_valid=True,
            implementation=ImplementationStatus.LIMITED,
            detail=(
                "Agent + container is representable for future mixed fleets, but "
                "containerized-agent host-access/least-privilege is not ready."
            ),
        )
    if role is InstallationRole.STANDALONE:
        return SupportAssessment(
            architecture_valid=True,
            implementation=ImplementationStatus.UNSUPPORTED,
            detail=(
                "Standalone + container is architecturally conceivable but not "
                "supported by the current product."
            ),
        )
    # ALL_IN_ONE
    return SupportAssessment(
        architecture_valid=False,
        implementation=ImplementationStatus.UNSUPPORTED,
        detail=(
            "All-in-one + container is not a supported deployment: co-locating "
            "local scan host access and controller runtime in one container is "
            "unsafe/undefined in the current model."
        ),
    )
