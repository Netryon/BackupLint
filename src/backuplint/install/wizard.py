"""Interactive native installer wizard (testable IO)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from backuplint.install.deployment import DeploymentForm
from backuplint.install.features import (
    FEATURE_REGISTRY,
    FeatureId,
    default_feature_map,
)
from backuplint.install.profile import InstallProfile, normalize_and_validate
from backuplint.install.roles import ROLE_SEMANTICS, InstallationRole


class WizardCancelled(Exception):
    """User cancelled before mutation."""


PromptFn = Callable[[str], str]
PrintFn = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class WizardResult:
    profile: InstallProfile
    confirmed: bool


def run_interactive_wizard(
    *,
    prompt: PromptFn | None = None,
    echo: PrintFn | None = None,
) -> WizardResult:
    """Deterministic interactive role/feature selection. No mutations."""
    read = prompt or input
    write = echo or print

    write("BackupLint native installer")
    write("Roles: standalone, agent, controller, all_in_one")
    write("Enter q to cancel (no changes).")

    role_values = {r.value for r in InstallationRole}
    role_raw = None
    while role_raw is None:
        try:
            candidate = read("Select role [standalone]: ").strip() or "standalone"
        except EOFError as exc:
            raise WizardCancelled("cancelled (EOF)") from exc
        if candidate.lower() in {"q", "quit", "cancel"}:
            raise WizardCancelled("cancelled by user")
        normalized = candidate.lower().replace("-", "_").replace(" ", "_")
        if normalized in role_values:
            role_raw = normalized
        else:
            write(f"Unknown role. Choose one of: {', '.join(sorted(role_values))}")

    role = InstallationRole(role_raw)
    write(ROLE_SEMANTICS[role].summary)

    defaults = default_feature_map(role)
    features: dict[str, bool] = {}
    for fid, definition in FEATURE_REGISTRY.items():
        if role not in definition.applicable_roles:
            features[fid.value] = False
            continue
        # Required flags are forced.
        if role is InstallationRole.AGENT and fid is FeatureId.FLEET_AGENT:
            features[fid.value] = True
            write(f"feature {fid.value}: required for agent (enabled)")
            continue
        if role in {
            InstallationRole.CONTROLLER,
            InstallationRole.ALL_IN_ONE,
        } and fid is FeatureId.FLEET_CONTROLLER:
            features[fid.value] = True
            write(f"feature {fid.value}: required for {role.value} (enabled)")
            continue
        if role is InstallationRole.STANDALONE and fid in {
            FeatureId.FLEET_AGENT,
            FeatureId.FLEET_CONTROLLER,
        }:
            features[fid.value] = False
            continue

        default_enabled = defaults[fid]
        # Conservative: only default-on features start as yes; restore stays no.
        default_ans = "y" if default_enabled else "n"
        while True:
            try:
                ans = read(
                    f"Enable {fid.value}? ({definition.summary}) [{default_ans}]: "
                ).strip().lower()
            except EOFError as exc:
                raise WizardCancelled("cancelled (EOF)") from exc
            if not ans:
                ans = default_ans
            if ans in {"q", "quit", "cancel"}:
                raise WizardCancelled("cancelled by user")
            if ans in {"y", "yes"}:
                features[fid.value] = True
                break
            if ans in {"n", "no"}:
                features[fid.value] = False
                break
            write("Please answer y or n (or q to cancel).")

    # Scheduler requires a local check — auto-fix if user enabled scheduler alone.
    if features.get("scheduler") and not any(
        features.get(k)
        for k in ("coverage", "integrity", "restore_verification")
    ):
        write("scheduler requires coverage; enabling coverage.")
        features["coverage"] = True

    profile = normalize_and_validate(
        {
            "schema_version": 1,
            "role": role.value,
            "deployment": DeploymentForm.NATIVE.value,
            "features": features,
        }
    )

    write("")
    write("Summary (no changes yet):")
    write(f"  role: {profile.role.value}")
    write(f"  deployment: {profile.deployment.value}")
    enabled = ", ".join(f.value for f in profile.enabled_features()) or "(none)"
    write(f"  features: {enabled}")
    write("Confirm package/service changes? [n]:")
    try:
        confirm = read("").strip().lower()
    except EOFError as exc:
        raise WizardCancelled("cancelled (EOF)") from exc
    if confirm in {"q", "quit", "cancel"}:
        raise WizardCancelled("cancelled by user")
    confirmed = confirm in {"y", "yes"}
    if not confirmed:
        raise WizardCancelled("not confirmed; no changes made")
    return WizardResult(profile=profile, confirmed=True)
