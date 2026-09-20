"""CLI commands for policy management (operator-local)."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from backuplint.policy.errors import PolicyError
from backuplint.policy.gitops import (
    diff_policy_files,
    export_policy_file,
    import_policy_file,
    validate_policy_file,
)

policy_app = typer.Typer(
    name="policy",
    help="Policy GitOps utilities (v0.8).",
    add_completion=False,
)
controller_policy_app = typer.Typer(
    name="policy",
    help="Controller policy administration (v0.8).",
    add_completion=False,
)


def _default_settings() -> dict[str, object]:
    return {
        "schedule": {
            "coverage": {"enabled": True, "every": "30m"},
            "integrity": {"enabled": True, "every": "6h"},
        },
        "reporting": {"policy_poll_interval": "5m"},
    }


@policy_app.command("validate")
def policy_validate(path: Path = typer.Argument(..., exists=True, dir_okay=False)) -> None:
    try:
        result = validate_policy_file(path)
    except PolicyError as exc:
        typer.echo(exc.message, err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(json.dumps(result, indent=2, sort_keys=True))
    raise typer.Exit(code=0)


@policy_app.command("diff")
def policy_diff(
    left: Path = typer.Argument(..., exists=True, dir_okay=False),
    right: Path = typer.Argument(..., exists=True, dir_okay=False),
) -> None:
    try:
        result = diff_policy_files(left, right)
    except PolicyError as exc:
        typer.echo(exc.message, err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(json.dumps(result, indent=2, sort_keys=True))
    raise typer.Exit(code=0)


@controller_policy_app.command("create")
def controller_policy_create(
    data_dir: Path = typer.Option(Path("backuplint-controller"), "--data-dir"),
    display_name: str = typer.Option(..., "--display-name"),
    description: str = typer.Option("", "--description"),
    settings_file: Path | None = typer.Option(None, "--settings-file"),
    actor: str = typer.Option("operator", "--actor"),
) -> None:
    from backuplint.fleet.controller import FleetController
    from backuplint.fleet.controller_store import ControllerStoreError

    settings = _default_settings()
    if settings_file is not None:
        settings = json.loads(settings_file.read_text(encoding="utf-8"))
    controller = FleetController(data_dir)
    try:
        snap = controller.store.policy_create(
            display_name=display_name,
            description=description,
            settings=settings,
            created_by=actor,
        )
    except (ControllerStoreError, PolicyError) as exc:
        typer.echo(exc.message, err=True)
        raise typer.Exit(code=2) from exc
    finally:
        controller.close()
    typer.echo(json.dumps(snap.to_dict(), indent=2, sort_keys=True))
    raise typer.Exit(code=0)


@controller_policy_app.command("list")
def controller_policy_list(
    data_dir: Path = typer.Option(Path("backuplint-controller"), "--data-dir"),
) -> None:
    from backuplint.fleet.controller import FleetController

    controller = FleetController(data_dir)
    try:
        items = controller.store.policy_list()
    finally:
        controller.close()
    typer.echo(json.dumps({"items": items}, indent=2, sort_keys=True))
    raise typer.Exit(code=0)


@controller_policy_app.command("export")
def controller_policy_export(
    revision_id: str = typer.Argument(...),
    output: Path = typer.Argument(...),
    data_dir: Path = typer.Option(Path("backuplint-controller"), "--data-dir"),
) -> None:
    from backuplint.fleet.controller import FleetController
    from backuplint.fleet.controller_store import ControllerStoreError

    controller = FleetController(data_dir)
    try:
        snap = controller.store.policy_get_revision(revision_id)
        if snap is None:
            raise ControllerStoreError(f"unknown revision: {revision_id}")
        export_policy_file(snap, output)
    except (ControllerStoreError, PolicyError) as exc:
        typer.echo(exc.message, err=True)
        raise typer.Exit(code=2) from exc
    finally:
        controller.close()
    typer.echo(str(output))
    raise typer.Exit(code=0)


@controller_policy_app.command("import")
def controller_policy_import(
    path: Path = typer.Argument(..., exists=True, dir_okay=False),
    data_dir: Path = typer.Option(Path("backuplint-controller"), "--data-dir"),
    actor: str = typer.Option("operator", "--actor"),
    policy_id: str | None = typer.Option(None, "--policy-id"),
) -> None:
    from backuplint.fleet.controller import FleetController
    from backuplint.fleet.controller_store import ControllerStoreError

    controller = FleetController(data_dir)
    try:
        snap = import_policy_file(path, created_by=actor, policy_id=policy_id)
        existing = controller.store.policy_get_revision(snap.revision_id)
        if existing is None:
            try:
                controller.store.policy_create(
                    policy_id=snap.policy_id,
                    display_name=snap.display_name,
                    description=snap.description,
                    settings=snap.settings,
                    created_by=actor,
                )
            except ControllerStoreError:
                snap = controller.store.policy_create_revision(
                    snap.policy_id,
                    display_name=snap.display_name,
                    description=snap.description,
                    settings=snap.settings,
                    created_by=actor,
                )
    except (ControllerStoreError, PolicyError) as exc:
        typer.echo(exc.message, err=True)
        raise typer.Exit(code=2) from exc
    finally:
        controller.close()
    typer.echo(json.dumps(snap.to_dict(), indent=2, sort_keys=True))
    raise typer.Exit(code=0)


@controller_policy_app.command("assign")
def controller_policy_assign(
    revision_id: str = typer.Argument(...),
    data_dir: Path = typer.Option(Path("backuplint-controller"), "--data-dir"),
    agent_id: str | None = typer.Option(None, "--agent-id"),
    group_id: str | None = typer.Option(None, "--group-id"),
    default: bool = typer.Option(False, "--default"),
    actor: str = typer.Option("operator", "--actor"),
) -> None:
    from backuplint.fleet.controller import FleetController
    from backuplint.fleet.controller_store import ControllerStoreError

    controller = FleetController(data_dir)
    try:
        if default:
            controller.store.policy_set_default(revision_id, actor=actor)
            typer.echo(json.dumps({"assigned": "default", "revision_id": revision_id}))
        elif group_id:
            controller.store.policy_assign_group(group_id, revision_id, actor=actor)
            typer.echo(
                json.dumps({"assigned": "group", "group_id": group_id, "revision_id": revision_id})
            )
        elif agent_id:
            generation = controller.store.policy_assign_agent(
                agent_id, revision_id, actor=actor
            )
            typer.echo(
                json.dumps(
                    {
                        "assigned": "agent",
                        "agent_id": agent_id,
                        "revision_id": revision_id,
                        "assignment_generation": generation,
                    }
                )
            )
        else:
            raise ControllerStoreError("specify --agent-id, --group-id, or --default")
    except ControllerStoreError as exc:
        typer.echo(exc.message, err=True)
        raise typer.Exit(code=2) from exc
    finally:
        controller.close()
    raise typer.Exit(code=0)


@controller_policy_app.command("rollback")
def controller_policy_rollback(
    agent_id: str = typer.Argument(...),
    revision_id: str = typer.Argument(...),
    data_dir: Path = typer.Option(Path("backuplint-controller"), "--data-dir"),
    actor: str = typer.Option("operator", "--actor"),
) -> None:
    from backuplint.fleet.controller import FleetController
    from backuplint.fleet.controller_store import ControllerStoreError

    controller = FleetController(data_dir)
    try:
        generation = controller.store.policy_rollback_agent(
            agent_id, revision_id, actor=actor
        )
    except ControllerStoreError as exc:
        typer.echo(exc.message, err=True)
        raise typer.Exit(code=2) from exc
    finally:
        controller.close()
    typer.echo(
        json.dumps(
            {
                "agent_id": agent_id,
                "revision_id": revision_id,
                "assignment_generation": generation,
            },
            indent=2,
        )
    )
    raise typer.Exit(code=0)


@controller_policy_app.command("group-create")
def controller_policy_group_create(
    display_name: str = typer.Argument(...),
    data_dir: Path = typer.Option(Path("backuplint-controller"), "--data-dir"),
    description: str = typer.Option("", "--description"),
) -> None:
    from backuplint.fleet.controller import FleetController

    controller = FleetController(data_dir)
    try:
        group_id = controller.store.policy_group_create(
            display_name=display_name, description=description
        )
    finally:
        controller.close()
    typer.echo(json.dumps({"group_id": group_id}))
    raise typer.Exit(code=0)


@controller_policy_app.command("group-add")
def controller_policy_group_add(
    group_id: str = typer.Argument(...),
    agent_id: str = typer.Argument(...),
    data_dir: Path = typer.Option(Path("backuplint-controller"), "--data-dir"),
) -> None:
    from backuplint.fleet.controller import FleetController
    from backuplint.fleet.controller_store import ControllerStoreError

    controller = FleetController(data_dir)
    try:
        controller.store.policy_group_add_member(group_id, agent_id)
    except ControllerStoreError as exc:
        typer.echo(exc.message, err=True)
        raise typer.Exit(code=2) from exc
    finally:
        controller.close()
    raise typer.Exit(code=0)


@controller_policy_app.command("rollout-create")
def controller_policy_rollout_create(
    revision_id: str = typer.Argument(...),
    data_dir: Path = typer.Option(Path("backuplint-controller"), "--data-dir"),
    group_id: str | None = typer.Option(None, "--group-id"),
    batch_size: int = typer.Option(10, "--batch-size"),
    max_concurrent: int = typer.Option(5, "--max-concurrent"),
    pause_seconds: int = typer.Option(30, "--pause-seconds"),
    failure_threshold: int = typer.Option(3, "--failure-threshold"),
    actor: str = typer.Option("operator", "--actor"),
) -> None:
    from backuplint.fleet.controller import FleetController
    from backuplint.fleet.controller_store import ControllerStoreError

    controller = FleetController(data_dir)
    try:
        rollout_id = controller.store.policy_rollout_create(
            revision_id=revision_id,
            target_group_id=group_id,
            batch_size=batch_size,
            max_concurrent=max_concurrent,
            pause_between_batches_seconds=pause_seconds,
            failure_threshold=failure_threshold,
            created_by=actor,
        )
    except ControllerStoreError as exc:
        typer.echo(exc.message, err=True)
        raise typer.Exit(code=2) from exc
    finally:
        controller.close()
    typer.echo(json.dumps({"rollout_id": rollout_id}))
    raise typer.Exit(code=0)


@controller_policy_app.command("rollout-start")
def controller_policy_rollout_start(
    rollout_id: str = typer.Argument(...),
    data_dir: Path = typer.Option(Path("backuplint-controller"), "--data-dir"),
) -> None:
    from backuplint.fleet.controller import FleetController
    from backuplint.fleet.controller_store import ControllerStoreError

    controller = FleetController(data_dir)
    try:
        controller.store.policy_rollout_start(rollout_id)
    except ControllerStoreError as exc:
        typer.echo(exc.message, err=True)
        raise typer.Exit(code=2) from exc
    finally:
        controller.close()
    raise typer.Exit(code=0)


@controller_policy_app.command("rollout-pause")
def controller_policy_rollout_pause(
    rollout_id: str = typer.Argument(...),
    data_dir: Path = typer.Option(Path("backuplint-controller"), "--data-dir"),
) -> None:
    from backuplint.fleet.controller import FleetController
    from backuplint.fleet.controller_store import ControllerStoreError

    controller = FleetController(data_dir)
    try:
        controller.store.policy_rollout_pause(rollout_id)
    except ControllerStoreError as exc:
        typer.echo(exc.message, err=True)
        raise typer.Exit(code=2) from exc
    finally:
        controller.close()
    raise typer.Exit(code=0)


@controller_policy_app.command("rollout-resume")
def controller_policy_rollout_resume(
    rollout_id: str = typer.Argument(...),
    data_dir: Path = typer.Option(Path("backuplint-controller"), "--data-dir"),
) -> None:
    from backuplint.fleet.controller import FleetController
    from backuplint.fleet.controller_store import ControllerStoreError

    controller = FleetController(data_dir)
    try:
        controller.store.policy_rollout_resume(rollout_id)
    except ControllerStoreError as exc:
        typer.echo(exc.message, err=True)
        raise typer.Exit(code=2) from exc
    finally:
        controller.close()
    raise typer.Exit(code=0)


@controller_policy_app.command("rollout-abort")
def controller_policy_rollout_abort(
    rollout_id: str = typer.Argument(...),
    data_dir: Path = typer.Option(Path("backuplint-controller"), "--data-dir"),
) -> None:
    from backuplint.fleet.controller import FleetController
    from backuplint.fleet.controller_store import ControllerStoreError

    controller = FleetController(data_dir)
    try:
        controller.store.policy_rollout_abort(rollout_id)
    except ControllerStoreError as exc:
        typer.echo(exc.message, err=True)
        raise typer.Exit(code=2) from exc
    finally:
        controller.close()
    raise typer.Exit(code=0)
