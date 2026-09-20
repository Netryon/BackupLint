"""Deployment form (native vs container) for install profiles."""

from __future__ import annotations

from enum import StrEnum


class DeploymentForm(StrEnum):
    """How BackupLint is deployed on a node.

    Mixed fleets combine native and container nodes; deployment form must not
    change fleet protocol semantics.
    """

    NATIVE = "native"
    CONTAINER = "container"


def parse_deployment(raw: object) -> DeploymentForm:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("deployment must be a non-empty string")
    key = raw.strip().lower()
    try:
        return DeploymentForm(key)
    except ValueError as exc:
        allowed = ", ".join(d.value for d in DeploymentForm)
        raise ValueError(
            f"unknown deployment form {raw!r}; expected one of: {allowed}"
        ) from exc
