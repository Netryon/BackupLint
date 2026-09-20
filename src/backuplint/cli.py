"""Command-line entry point for BackupLint."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from backuplint import __version__
from backuplint.config import ConfigError, default_config_path, load_config

# Heavy scan/Restic/Docker and fleet modules are imported lazily inside commands so
# controller-oriented entry paths do not eagerly load local verification code.

app = typer.Typer(
    name="backuplint",
    help="Check whether Docker Compose persistent data is covered by backup configuration.",
    add_completion=False,
    invoke_without_command=True,
)

schedule_app = typer.Typer(
    name="schedule",
    help="Local scheduled verification (v0.4).",
    add_completion=False,
)
app.add_typer(schedule_app, name="schedule")

controller_app = typer.Typer(
    name="controller",
    help="Fleet controller administration (v0.5).",
    add_completion=False,
)
app.add_typer(controller_app, name="controller")

agent_app = typer.Typer(
    name="agent",
    help="Fleet agent (v0.5).",
    add_completion=False,
)
app.add_typer(agent_app, name="agent")

profile_app = typer.Typer(
    name="profile",
    help="Installation profile validation and dry-run planning (foundation).",
    add_completion=False,
)
app.add_typer(profile_app, name="profile")

install_app = typer.Typer(
    name="install",
    help="Native installer (profile execution, dry-run, interactive).",
    add_completion=False,
)
app.add_typer(install_app, name="install")

siem_app = typer.Typer(
    name="siem",
    help="SIEM export queue and telemetry (v0.7).",
    add_completion=False,
)
app.add_typer(siem_app, name="siem")

# Policy GitOps / controller-policy CLIs (v0.8).
from backuplint.policy.cli import (  # noqa: E402
    controller_policy_app,
    policy_app,
)

app.add_typer(policy_app, name="policy")
controller_app.add_typer(controller_policy_app, name="policy")

EXIT_OK = 0
EXIT_COVERAGE = 1
EXIT_ERROR = 2
EXIT_CANCEL = 3
EXIT_NOT_CONFIRMED = 4

ComposeFileArg = Annotated[
    Path,
    typer.Argument(
        exists=False,
        dir_okay=False,
        writable=False,
        readable=False,
        resolve_path=False,
        help="Path to a Docker Compose file.",
    ),
]


def _resolve_local_state_dir(backup_config: object) -> Path:
    from backuplint.scheduler import default_state_dir, resolve_state_dir

    schedule = getattr(backup_config, "schedule", None)
    if schedule is not None:
        return resolve_state_dir(schedule)
    return default_state_dir()


def _maybe_submit_local_siem(
    backup_config: object,
    outcome: object,
    *,
    occurred_at: str | None = None,
) -> None:
    from backuplint.siem.runtime import LocalSiemFeed, should_use_local_siem_feed

    siem_cfg = getattr(backup_config, "siem", None)
    has_fleet = getattr(backup_config, "fleet", None) is not None
    if not should_use_local_siem_feed(siem_cfg, has_fleet=has_fleet):
        return
    state_dir = _resolve_local_state_dir(backup_config)
    feed = LocalSiemFeed.open(state_dir, siem_cfg)
    try:
        feed.submit_outcome(outcome, occurred_at=occurred_at)
        feed.flush()
    finally:
        feed.close()


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"backuplint {__version__}")
        raise typer.Exit(code=0)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        help="Show the BackupLint version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """BackupLint command-line interface."""
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit(code=0)


@app.command("scan")
def scan(
    compose_file: ComposeFileArg,
    config: Path | None = typer.Option(
        None,
        "--config",
        "-c",
        help="Path to backuplint.yml (defaults to beside Compose file or ./backuplint.yml).",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit machine-readable JSON instead of the text audit report.",
    ),
    integrity: str | None = typer.Option(
        None,
        "--integrity",
        help="Integrity mode override: off, standard, or deep "
        "(overrides restic.integrity.mode; default remains off).",
    ),
    restore_verify: str | None = typer.Option(
        None,
        "--restore-verify",
        help="Restore verification mode override: off, selected, or full "
        "(overrides restic.restore_verification.mode; default remains off).",
    ),
) -> None:
    """Audit Compose mounts against configured backup paths or Restic snapshots."""
    from backuplint.audit import run_audit
    from backuplint.compose import ComposeError
    from backuplint.reporting import format_audit_json, format_audit_report
    from backuplint.restic import ResticError

    try:
        outcome = run_audit(
            compose_file,
            config_path=config,
            integrity_cli=integrity,
            restore_verify_cli=restore_verify,
        )
    except (ComposeError, ConfigError, ResticError) as exc:
        typer.echo(exc.message, err=True)
        raise typer.Exit(code=EXIT_ERROR) from exc

    if json_output:
        typer.echo(
            format_audit_json(
                outcome.findings,
                integrity=outcome.integrity,
                restore=outcome.restore,
            ),
            nl=False,
        )
    else:
        typer.echo(
            format_audit_report(
                outcome.findings,
                integrity=outcome.integrity,
                restore=outcome.restore,
            )
        )
    try:
        backup_config = load_config(
            config if config is not None else default_config_path(compose_file)
        )
        _maybe_submit_local_siem(backup_config, outcome)
    except ConfigError:
        pass
    raise typer.Exit(code=outcome.summary.exit_code)


def _load_schedule_context(
    compose_file: Path,
    config: Path | None,
) -> tuple[Path, object, object, object]:
    from backuplint.schedule_store import ScheduleStore
    from backuplint.scheduler import resolve_state_dir

    config_path = config if config is not None else default_config_path(compose_file)
    try:
        backup_config = load_config(config_path)
    except ConfigError as exc:
        typer.echo(exc.message, err=True)
        raise typer.Exit(code=EXIT_ERROR) from exc
    if backup_config.schedule is None:
        typer.echo("Configuration has no 'schedule' section.", err=True)
        raise typer.Exit(code=EXIT_ERROR)
    state_dir = resolve_state_dir(backup_config.schedule)
    store = ScheduleStore(
        state_dir / "history.sqlite3",
        history_limit=backup_config.schedule.history_limit,
    )
    return config_path, backup_config, backup_config.schedule, store


@schedule_app.command("status")
def schedule_status(
    compose_file: ComposeFileArg,
    config: Path | None = typer.Option(None, "--config", "-c"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show last/next scheduled check status (not backup truth)."""
    from backuplint.scheduler import format_schedule_status, schedule_status_payload

    _config_path, _backup, schedule, store = _load_schedule_context(compose_file, config)
    try:
        if json_output:
            typer.echo(
                json.dumps(
                    schedule_status_payload(schedule=schedule, store=store),
                    indent=2,
                )
            )
        else:
            typer.echo(format_schedule_status(schedule=schedule, store=store))
    finally:
        store.close()
    raise typer.Exit(code=EXIT_OK)


@schedule_app.command("next")
def schedule_next(
    compose_file: ComposeFileArg,
    config: Path | None = typer.Option(None, "--config", "-c"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show the next expected run times for each enabled check."""
    from backuplint.scheduler import schedule_status_payload

    _config_path, _backup, schedule, store = _load_schedule_context(compose_file, config)
    try:
        payload = schedule_status_payload(schedule=schedule, store=store)
        if json_output:
            typer.echo(json.dumps(payload["schedule"]["jobs"], indent=2))
        else:
            for job in payload["schedule"]["jobs"]:  # type: ignore[index]
                if not job["enabled"]:
                    continue
                nxt = job["next_run"] or "unscheduled"
                typer.echo(f"{job['check_type']}: {nxt}")
    finally:
        store.close()
    raise typer.Exit(code=EXIT_OK)


@schedule_app.command("history")
def schedule_history(
    compose_file: ComposeFileArg,
    config: Path | None = typer.Option(None, "--config", "-c"),
    limit: int = typer.Option(20, "--limit", min=1, max=500),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show recent scheduled check history."""
    _config_path, _backup, _schedule, store = _load_schedule_context(compose_file, config)
    try:
        rows = store.history(limit=limit)
        if json_output:
            typer.echo(
                json.dumps(
                    [
                        {
                            "id": r.id,
                            "check_type": r.check_type.value,
                            "started_at": r.started_at.isoformat(),
                            "finished_at": r.finished_at.isoformat(),
                            "result": r.result,
                            "exit_code": r.exit_code,
                            "duration_seconds": r.duration_seconds,
                            "detail": r.detail,
                        }
                        for r in rows
                    ],
                    indent=2,
                )
            )
        else:
            if not rows:
                typer.echo("No scheduled runs recorded yet.")
            for r in rows:
                typer.echo(
                    f"{r.finished_at.isoformat()}  {r.check_type.value:<22}  "
                    f"{r.result:<5}  exit={r.exit_code}  {r.duration_seconds:.1f}s"
                )
    finally:
        store.close()
    raise typer.Exit(code=EXIT_OK)


@schedule_app.command("run")
def schedule_run(
    compose_file: ComposeFileArg,
    check: str = typer.Argument(
        help="Check to run: coverage, integrity, deep_integrity, restore_verification."
    ),
    config: Path | None = typer.Option(None, "--config", "-c"),
) -> None:
    """Run one scheduled check type immediately (does not update next_run)."""
    from backuplint.schedule_config import ScheduleCheckType
    from backuplint.scheduler import run_scheduled_check

    config_path, _backup, schedule, store = _load_schedule_context(compose_file, config)
    try:
        try:
            check_type = ScheduleCheckType(check.strip().lower())
        except ValueError as exc:
            typer.echo(
                "Unknown check type. Use: coverage, integrity, deep_integrity, "
                "restore_verification.",
                err=True,
            )
            raise typer.Exit(code=EXIT_ERROR) from exc
        job = schedule.job_for(check_type)
        if job is None or not job.enabled:
            typer.echo(f"Check {check_type.value!r} is not enabled in schedule.", err=True)
            raise typer.Exit(code=EXIT_ERROR)
        result, exit_code, detail, outcome = run_scheduled_check(
            compose_file=compose_file,
            config_path=config_path,
            check_type=check_type,
        )
        if detail:
            typer.echo(detail, err=True)
        typer.echo(f"{check_type.value}: {result} (exit {exit_code})")
        if outcome is not None:
            try:
                backup_config = load_config(config_path)
                _maybe_submit_local_siem(backup_config, outcome)
            except ConfigError:
                pass
    finally:
        store.close()
    raise typer.Exit(code=exit_code if exit_code in {0, 1, 2} else EXIT_ERROR)


@app.command("daemon")
def daemon(
    compose_file: ComposeFileArg,
    config: Path | None = typer.Option(None, "--config", "-c"),
    poll_seconds: float = typer.Option(
        5.0,
        "--poll-seconds",
        help="Scheduler poll interval in seconds (tests may use smaller values).",
        min=0.1,
    ),
) -> None:
    """Run the local scheduler in the foreground until SIGTERM/SIGINT."""
    from backuplint.scheduler import Scheduler, SchedulerError, resolve_state_dir

    config_path = config if config is not None else default_config_path(compose_file)
    try:
        scheduler = Scheduler.from_paths(
            compose_file=compose_file,
            config_path=config_path,
            poll_seconds=poll_seconds,
        )
    except (ConfigError, SchedulerError) as exc:
        typer.echo(exc.message, err=True)
        raise typer.Exit(code=EXIT_ERROR) from exc
    typer.echo(
        f"BackupLint scheduler started (state: {resolve_state_dir(scheduler.schedule)})",
        err=True,
    )
    try:
        scheduler.run_forever()
    except SchedulerError as exc:
        typer.echo(exc.message, err=True)
        raise typer.Exit(code=EXIT_ERROR) from exc
    typer.echo("BackupLint scheduler stopped.", err=True)
    raise typer.Exit(code=EXIT_OK)


@controller_app.command("init")
def controller_init(
    data_dir: Path = typer.Option(
        Path("backuplint-controller"),
        "--data-dir",
        help="Controller data directory (CA, DB, agent certs).",
    ),
    hostname: str = typer.Option("localhost", "--hostname"),
) -> None:
    """Initialize controller CA/server material and SQLite store."""
    from backuplint.fleet.controller import ControllerError, FleetController
    from backuplint.fleet.controller_store import ControllerStoreError

    try:
        controller = FleetController(data_dir, hostname=hostname)
        controller.close()
    except (ControllerError, ControllerStoreError) as exc:
        typer.echo(exc.message, err=True)
        raise typer.Exit(code=EXIT_ERROR) from exc
    typer.echo(f"Controller initialized at {data_dir}")
    typer.echo(f"CA certificate: {data_dir / 'ca' / 'ca.crt'}")
    raise typer.Exit(code=EXIT_OK)


@controller_app.command("enroll-token")
def controller_enroll_token(
    data_dir: Path = typer.Option(Path("backuplint-controller"), "--data-dir"),
    label: str = typer.Option("agent", "--label"),
    ttl_hours: int = typer.Option(1, "--ttl-hours", min=1, max=168),
    agent_id: str | None = typer.Option(
        None,
        "--agent-id",
        help="Optional intended agent_id; generated when omitted.",
    ),
) -> None:
    """Create pending agent + one-time enrollment token (printed once).

    Output (two lines, sensitive)::

        agent_id=<id>
        token=<one-time-token>

    The controller stores only a hash of the token.
    """
    from backuplint.fleet.controller import FleetController

    controller = FleetController(data_dir)
    try:
        pending = controller.create_pending_agent(
            label=label, ttl_hours=ttl_hours, agent_id=agent_id
        )
    finally:
        controller.close()
    # Print once to stdout; do not write to logs.
    typer.echo(f"agent_id={pending['agent_id']}")
    typer.echo(f"token={pending['token']}")
    raise typer.Exit(code=EXIT_OK)


@controller_app.command("provision-batch")
def controller_provision_batch(
    data_dir: Path = typer.Option(Path("backuplint-controller"), "--data-dir"),
    count: int = typer.Option(..., "--count", min=1, max=10000),
    label_prefix: str = typer.Option("agent", "--label-prefix"),
    ttl_hours: int = typer.Option(24, "--ttl-hours", min=1, max=168),
    controller_url: str = typer.Option(..., "--controller-url"),
    output: Path = typer.Option(..., "--output", help="Sensitive JSON bundle path."),
    role: str = typer.Option("agent", "--role"),
    feature_profile: str = typer.Option("default", "--feature-profile"),
    deployment_form: str = typer.Option("native", "--deployment-form"),
    include_ca: bool = typer.Option(True, "--include-ca/--no-include-ca"),
) -> None:
    """Create many pending agents and export a generic provisioning bundle.

    The output file is mode 0600 and contains one-time tokens — treat as secret.
    """
    from backuplint.fleet.controller import ControllerError, FleetController
    from backuplint.fleet.provisioning import write_bundle_file

    controller = FleetController(data_dir)
    try:
        pending = controller.create_pending_agents_bulk(
            count=count, label_prefix=label_prefix, ttl_hours=ttl_hours
        )
        ca_pem = None
        if include_ca:
            ca_pem = (controller.ca_dir / "ca.crt").read_text(encoding="utf-8")
        records = controller.export_provisioning_records(
            controller_url=controller_url,
            pending=pending,
            ca_cert_pem=ca_pem,
            role=role,
            feature_profile=feature_profile,
            deployment_form=deployment_form,
        )
        write_bundle_file(output, records, include_tokens=True)  # type: ignore[arg-type]
    except (ControllerError, ValueError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=EXIT_ERROR) from exc
    finally:
        controller.close()
    typer.echo(f"Wrote {count} provisioning records to {output} (mode 0600)")
    raise typer.Exit(code=EXIT_OK)


@controller_app.command("pending")
def controller_pending_list(
    data_dir: Path = typer.Option(Path("backuplint-controller"), "--data-dir"),
    all_statuses: bool = typer.Option(False, "--all", help="Include non-pending rows."),
) -> None:
    """List pending enrollments (no raw tokens)."""
    from backuplint.fleet.controller import FleetController

    controller = FleetController(data_dir)
    try:
        rows = controller.store.list_pending_enrollments(
            status=None if all_statuses else "pending"
        )
        expired = controller.store.expire_due_pending_enrollments()
    finally:
        controller.close()
    if expired:
        typer.echo(f"# expired_now={expired}", err=True)
    for row in rows:
        typer.echo(
            f"{row['agent_id']}\t{row['label']}\t{row['status']}\t{row['expires_at']}"
        )
    raise typer.Exit(code=EXIT_OK)


@controller_app.command("pending-revoke")
def controller_pending_revoke(
    agent_id: str = typer.Argument(...),
    data_dir: Path = typer.Option(Path("backuplint-controller"), "--data-dir"),
) -> None:
    """Revoke a pending enrollment authorization."""
    from backuplint.fleet.controller import FleetController
    from backuplint.fleet.controller_store import ControllerStoreError

    controller = FleetController(data_dir)
    try:
        controller.store.revoke_pending_enrollment(agent_id)
    except ControllerStoreError as exc:
        typer.echo(exc.message, err=True)
        raise typer.Exit(code=EXIT_ERROR) from exc
    finally:
        controller.close()
    typer.echo(f"revoked pending {agent_id}")
    raise typer.Exit(code=EXIT_OK)


@controller_app.command("run")
def controller_run(
    data_dir: Path = typer.Option(Path("backuplint-controller"), "--data-dir"),
    listen: str = typer.Option("127.0.0.1:8443", "--listen"),
    hostname: str = typer.Option("localhost", "--hostname"),
    dashboard: bool = typer.Option(
        False,
        "--dashboard/--no-dashboard",
        help="Enable the optional read-only web dashboard on this controller.",
    ),
    dashboard_password_file: Path | None = typer.Option(
        None,
        "--dashboard-password-file",
        help="Optional path to scrypt password hash (default: DATA_DIR/dashboard/password.scrypt).",
    ),
    siem_config: Path | None = typer.Option(
        None,
        "--siem-config",
        help="Optional SIEM export YAML (or set BACKUPLINT_SIEM_CONFIG_FILE).",
    ),
) -> None:
    """Run the HTTPS fleet controller in the foreground.

    Stops cleanly on SIGTERM/SIGINT (container-friendly shutdown).
    """
    from backuplint.fleet.controller import ControllerError, FleetController
    from backuplint.fleet.dashboard.config import DashboardConfig
    from backuplint.siem.config import SiemConfig
    from backuplint.siem.runtime import resolve_controller_siem_config

    host, _, port_s = listen.partition(":")
    if not host or not port_s.isdigit():
        typer.echo("Invalid --listen; expected HOST:PORT", err=True)
        raise typer.Exit(code=EXIT_ERROR)
    dash_cfg = DashboardConfig(
        enabled=dashboard,
        password_hash_path=dashboard_password_file,
    )
    try:
        resolved_siem = resolve_controller_siem_config(siem_config_path=siem_config)
    except Exception as exc:  # noqa: BLE001
        message = getattr(exc, "message", str(exc))
        typer.echo(message, err=True)
        raise typer.Exit(code=EXIT_ERROR) from exc
    controller = FleetController(
        data_dir,
        hostname=hostname,
        dashboard=dash_cfg,
        siem=resolved_siem or SiemConfig(),
    )
    try:
        if dashboard and not controller.dashboard_auth.password_configured():
            typer.echo(
                "Dashboard enabled but password not set. "
                "Run: backuplint controller dashboard-password",
                err=True,
            )
            raise typer.Exit(code=EXIT_ERROR)
        bound_host, bound_port = controller.start(host=host, port=int(port_s))
        typer.echo(
            f"Controller listening on https://{bound_host}:{bound_port}",
            err=True,
        )
        if dashboard:
            typer.echo(
                f"Dashboard UI at https://{bound_host}:{bound_port}/dashboard/",
                err=True,
            )
        if controller.siem_config.enabled:
            typer.echo(
                f"SIEM export enabled (endpoint configured; telemetry under "
                f"{controller.siem_telemetry_path.parent}/)",
                err=True,
            )
        controller.wait_until_stopped()
    except ControllerError as exc:
        typer.echo(exc.message, err=True)
        raise typer.Exit(code=EXIT_ERROR) from exc
    finally:
        controller.close()
    raise typer.Exit(code=EXIT_OK)


@controller_app.command("dashboard-password")
def controller_dashboard_password(
    data_dir: Path = typer.Option(Path("backuplint-controller"), "--data-dir"),
    password: str | None = typer.Option(
        None,
        "--password",
        help="Dashboard password (min 12 chars). Prefer prompt/env in production.",
        hide_input=True,
    ),
) -> None:
    """Set or rotate the dashboard operator password hash (scrypt, mode 0600)."""
    import getpass
    import os

    from backuplint.fleet.dashboard.auth import DashboardAuth, DashboardAuthError
    from backuplint.fleet.dashboard.config import DashboardConfig

    value = password or os.environ.get("BACKUPLINT_DASHBOARD_PASSWORD")
    if not value:
        value = getpass.getpass("Dashboard password: ")
        confirm = getpass.getpass("Confirm password: ")
        if value != confirm:
            typer.echo("Passwords do not match", err=True)
            raise typer.Exit(code=EXIT_ERROR)
    auth = DashboardAuth(DashboardConfig(enabled=True), data_dir)
    try:
        auth.set_password(value)
    except DashboardAuthError as exc:
        typer.echo(exc.message, err=True)
        raise typer.Exit(code=EXIT_ERROR) from exc
    typer.echo(f"Dashboard password hash written to {auth.hash_path}", err=True)
    raise typer.Exit(code=EXIT_OK)


@siem_app.command("flush")
def siem_flush(
    state_dir: Path | None = typer.Option(
        None,
        "--state-dir",
        help="Local state directory (default: schedule state or XDG default).",
    ),
    config: Path | None = typer.Option(
        None,
        "--config",
        "-c",
        help="backuplint.yml containing an optional siem: block.",
    ),
    siem_config: Path | None = typer.Option(
        None,
        "--siem-config",
        help="Standalone SIEM YAML (overrides backuplint.yml siem block).",
    ),
) -> None:
    """Drain the local SIEM export queue once (standalone/all-in-one helper)."""
    from backuplint.siem.config import SiemConfig
    from backuplint.siem.runtime import LocalSiemFeed, load_siem_config_file

    siem_cfg: SiemConfig | None = None
    backup_config = None
    if siem_config is not None:
        try:
            siem_cfg = load_siem_config_file(siem_config)
        except Exception as exc:  # noqa: BLE001
            message = getattr(exc, "message", str(exc))
            typer.echo(message, err=True)
            raise typer.Exit(code=EXIT_ERROR) from exc
    elif config is not None:
        try:
            backup_config = load_config(config)
            siem_cfg = backup_config.siem
        except ConfigError as exc:
            typer.echo(exc.message, err=True)
            raise typer.Exit(code=EXIT_ERROR) from exc
    if siem_cfg is None or not siem_cfg.enabled:
        typer.echo("SIEM export is not enabled.", err=True)
        raise typer.Exit(code=EXIT_ERROR)
    if state_dir is None:
        if backup_config is None and config is not None:
            backup_config = load_config(config)
        if backup_config is not None:
            state_dir = _resolve_local_state_dir(backup_config)
        else:
            from backuplint.scheduler import default_state_dir

            state_dir = default_state_dir()
    feed = LocalSiemFeed.open(state_dir, siem_cfg)
    try:
        summary = feed.flush()
    finally:
        feed.close()
    if summary is None:
        typer.echo("No local SIEM exporter configured.")
    else:
        typer.echo(
            f"attempted={summary.attempted} delivered={summary.delivered} "
            f"failed={summary.failed} deferred={summary.deferred}"
        )
    raise typer.Exit(code=EXIT_OK)


@siem_app.command("status")
def siem_status_cmd(
    state_dir: Path | None = typer.Option(
        None,
        "--state-dir",
        help="Local state directory for standalone telemetry.jsonl.",
    ),
    config: Path | None = typer.Option(
        None,
        "--config",
        "-c",
        help="backuplint.yml containing an optional siem: block.",
    ),
    controller_url: str | None = typer.Option(
        None,
        "--controller",
        help="Query /v1/siem/status on a controller over mTLS.",
    ),
    identity_dir: Path = typer.Option(
        Path("backuplint-agent"),
        "--identity-dir",
        help="Agent identity for controller mTLS queries.",
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Print local SIEM telemetry or query controller /v1/siem/status."""
    import urllib.request

    if controller_url:
        from backuplint.fleet.agent import AgentIdentity, FleetAgent

        try:
            identity = AgentIdentity.load(identity_dir)
            agent = FleetAgent(
                controller_url=controller_url,
                identity=identity,
                queue=identity_dir / "queue.jsonl",
            )
            req = urllib.request.Request(  # noqa: S310
                controller_url.rstrip("/") + "/v1/siem/status",
                method="GET",
            )
            with urllib.request.urlopen(  # nosec B310  # noqa: S310
                req, context=agent._ssl_context(), timeout=10
            ) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=EXIT_ERROR) from exc
        if json_output:
            typer.echo(json.dumps(payload, indent=2), nl=False)
        else:
            siem = payload.get("siem", {})
            typer.echo(f"endpoint_health={siem.get('endpoint_health')}")
            depth = siem.get("queue_depth_by_status") or {}
            typer.echo(f"queue_pending={depth.get('pending', 0)}")
        raise typer.Exit(code=EXIT_OK)

    from backuplint.siem.runtime import LocalSiemFeed

    siem_cfg = None
    backup_config = None
    if config is not None:
        try:
            backup_config = load_config(config)
            siem_cfg = backup_config.siem
        except ConfigError as exc:
            typer.echo(exc.message, err=True)
            raise typer.Exit(code=EXIT_ERROR) from exc
    if state_dir is None and backup_config is not None:
        state_dir = _resolve_local_state_dir(backup_config)
    if state_dir is None:
        from backuplint.scheduler import default_state_dir

        state_dir = default_state_dir()
    feed = LocalSiemFeed.open(state_dir, siem_cfg)
    try:
        snap = feed.status()
    finally:
        feed.close()
    if snap is None:
        typer.echo("No SIEM telemetry available.", err=True)
        raise typer.Exit(code=EXIT_ERROR)
    if json_output:
        typer.echo(json.dumps(snap, indent=2), nl=False)
    else:
        typer.echo(f"endpoint_health={snap.get('endpoint_health')}")
        depth = snap.get("queue_depth_by_status") or {}
        typer.echo(f"queue_pending={depth.get('pending', 0)}")
    raise typer.Exit(code=EXIT_OK)


@controller_app.command("agents")
def controller_agents(
    data_dir: Path = typer.Option(Path("backuplint-controller"), "--data-dir"),
) -> None:
    """List enrolled agents."""
    from backuplint.fleet.controller import FleetController

    controller = FleetController(data_dir)
    try:
        for agent in controller.store.list_agents():
            hb = controller.store.last_heartbeat(agent.agent_id) or "never"
            typer.echo(
                f"{agent.agent_id}  {agent.status:<8}  {agent.label}  "
                f"host={agent.hostname}  last_seen={agent.last_seen}  hb={hb}"
            )
    finally:
        controller.close()


@controller_app.command("prune-history")
def controller_prune_history(
    data_dir: Path = typer.Option(Path("backuplint-controller"), "--data-dir"),
    events_keep_days: int = typer.Option(90, "--events-keep-days"),
    submissions_keep_days: int = typer.Option(90, "--submissions-keep-days"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Prune aged operational events/submissions (never deletes policy_audit)."""
    from backuplint.fleet.controller import FleetController
    from backuplint.fleet.controller_store import ControllerStoreError

    controller = FleetController(data_dir)
    try:
        stats = controller.store.prune_operational_history(
            events_keep_days=events_keep_days,
            submissions_keep_days=submissions_keep_days,
        )
    except ControllerStoreError as exc:
        typer.echo(exc.message, err=True)
        raise typer.Exit(code=EXIT_ERROR) from exc
    finally:
        controller.close()
    if json_output:
        typer.echo(json.dumps(stats, indent=2, sort_keys=True))
    else:
        typer.echo(
            f"deleted_events={stats['deleted_events']} "
            f"deleted_submissions={stats['deleted_submissions']} "
            f"events_keep_days={stats['events_keep_days']} "
            f"submissions_keep_days={stats['submissions_keep_days']}"
        )
    raise typer.Exit(code=EXIT_OK)


@controller_app.command("agent")
def controller_agent_show(
    agent_id: str,
    data_dir: Path = typer.Option(Path("backuplint-controller"), "--data-dir"),
) -> None:
    """Show one agent, current result by scan_time, and last-received if different."""
    from backuplint.fleet.controller import FleetController

    controller = FleetController(data_dir)
    try:
        agent = controller.store.get_agent(agent_id)
        if agent is None:
            typer.echo(f"unknown agent: {agent_id}", err=True)
            raise typer.Exit(code=EXIT_ERROR)
        hb = controller.store.last_heartbeat(agent_id) or "never"
        typer.echo(f"agent_id:   {agent.agent_id}")
        typer.echo(f"label:      {agent.label}")
        typer.echo(f"hostname:   {agent.hostname}")
        typer.echo(f"status:     {agent.status}")
        typer.echo(f"first_seen: {agent.first_seen}")
        typer.echo(f"last_seen:  {agent.last_seen}")
        typer.echo(f"heartbeat:  {hb}")
        current = controller.store.current_result_by_occurred_at(agent_id)
        latest = controller.store.latest_result(agent_id)
        if current is None:
            typer.echo("current:    none")
        else:
            result_body = current["result"] if isinstance(current["result"], dict) else {}
            summary = result_body.get("summary") if isinstance(result_body, dict) else None
            # Prefer top-level result (format_audit_json); fall back to summary.result.
            result_label = None
            if isinstance(result_body, dict):
                raw = result_body.get("result")
                if isinstance(raw, str) and raw.strip():
                    result_label = raw.strip()
            if result_label is None and isinstance(summary, dict):
                raw = summary.get("result")
                if isinstance(raw, str) and raw.strip():
                    result_label = raw.strip()
            if result_label is None:
                result_label = "unknown"
            typer.echo(
                f"current:    {result_label}  submission={current['submission_id']}"
            )
            typer.echo(f"scan_time:  {current['scan_time']}")
            typer.echo(f"received:   {current['received_time']}")
        if latest is not None and (
            current is None or latest["submission_id"] != current["submission_id"]
        ):
            result_body = latest["result"] if isinstance(latest["result"], dict) else {}
            summary = result_body.get("summary") if isinstance(result_body, dict) else None
            result_label = None
            if isinstance(result_body, dict):
                raw = result_body.get("result")
                if isinstance(raw, str) and raw.strip():
                    result_label = raw.strip()
            if result_label is None and isinstance(summary, dict):
                raw = summary.get("result")
                if isinstance(raw, str) and raw.strip():
                    result_label = raw.strip()
            if result_label is None:
                result_label = "unknown"
            typer.echo(
                f"last_recv:  {result_label}  submission={latest['submission_id']}"
            )
            typer.echo(f"recv_scan:  {latest['scan_time']}")
            typer.echo(f"recv_at:    {latest['received_time']}")
        typer.echo(
            "note: current is newest by scan_time; last_recv is newest by ingest time"
        )
        typer.echo(
            "note: current backup result is not proof the agent is currently online"
        )
    finally:
        controller.close()
    raise typer.Exit(code=EXIT_OK)


@controller_app.command("revoke")
def controller_revoke(
    agent_id: str,
    data_dir: Path = typer.Option(Path("backuplint-controller"), "--data-dir"),
) -> None:
    """Revoke an agent (submissions/heartbeats rejected)."""
    from backuplint.fleet.controller import FleetController
    from backuplint.fleet.controller_store import ControllerStoreError

    controller = FleetController(data_dir)
    try:
        controller.store.set_agent_status(agent_id, "revoked")
    except ControllerStoreError as exc:
        typer.echo(exc.message, err=True)
        raise typer.Exit(code=EXIT_ERROR) from exc
    finally:
        controller.close()
    typer.echo(f"revoked {agent_id}")
    raise typer.Exit(code=EXIT_OK)


@agent_app.command("enroll")
def agent_enroll(
    controller_url: str = typer.Option(..., "--controller"),
    token: str | None = typer.Option(
        None,
        "--token",
        help="One-time enrollment token (argv-visible; prefer --token-file).",
    ),
    token_file: Path | None = typer.Option(
        None,
        "--token-file",
        help="Read enrollment token from a protected file (mode 0600 recommended).",
    ),
    token_env: str | None = typer.Option(
        None,
        "--token-env",
        help="Environment variable name containing the enrollment token.",
    ),
    ca_cert: Path = typer.Option(..., "--ca-cert", help="Controller CA certificate."),
    identity_dir: Path = typer.Option(
        Path("backuplint-agent"),
        "--identity-dir",
    ),
    agent_id: str = typer.Option(
        ...,
        "--agent-id",
        help="Intended agent_id from controller enroll-token / provisioning bundle.",
    ),
) -> None:
    """Enroll with a controller using a locally generated private key + CSR."""
    from backuplint.fleet.agent import AgentError, FleetAgent
    from backuplint.secrets import (
        SecretError,
        SecretRef,
        SecretSource,
        resolve_secret,
        secret_ref_from_file_path,
    )

    provided = [token is not None, token_file is not None, token_env is not None]
    if sum(1 for flag in provided if flag) != 1:
        typer.echo(
            "Provide exactly one of --token-file, --token-env, or --token.",
            err=True,
        )
        raise typer.Exit(code=EXIT_ERROR)
    try:
        if token_file is not None:
            resolved = resolve_secret(secret_ref_from_file_path(token_file))
            token_value = resolved.get_secret_value()
        elif token_env is not None:
            resolved = resolve_secret(
                SecretRef(source=SecretSource.ENV, name=token_env.strip())
            )
            token_value = resolved.get_secret_value()
        elif token is not None:
            token_value = token
        else:
            typer.echo(
                "Provide exactly one of --token-file, --token-env, or --token.",
                err=True,
            )
            raise typer.Exit(code=EXIT_ERROR)
        identity = FleetAgent.enroll_with_ca(
            controller_url=controller_url,
            token=token_value,
            agent_id=agent_id,
            ca_cert=ca_cert,
            identity_dir=identity_dir,
        )
    except SecretError as exc:
        typer.echo(exc.message, err=True)
        raise typer.Exit(code=EXIT_ERROR) from exc
    except AgentError as exc:
        typer.echo(exc.message, err=True)
        raise typer.Exit(code=EXIT_ERROR) from exc
    typer.echo(f"Enrolled agent {identity.agent_id}")
    raise typer.Exit(code=EXIT_OK)


@agent_app.command("submit")
def agent_submit(
    compose_file: ComposeFileArg,
    controller_url: str = typer.Option(..., "--controller"),
    identity_dir: Path = typer.Option(Path("backuplint-agent"), "--identity-dir"),
    config: Path | None = typer.Option(None, "--config", "-c"),
) -> None:
    """Run a local scan and submit the result envelope to the controller."""
    from backuplint.fleet.agent import AgentError, AgentIdentity, AgentQueue, FleetAgent

    config_path = config if config is not None else default_config_path(compose_file)
    try:
        identity = AgentIdentity.load(identity_dir)
        agent = FleetAgent(
            controller_url=controller_url,
            identity=identity,
            queue=AgentQueue(identity_dir / "queue.jsonl"),
        )
        envelope = agent.run_check_and_submit(
            compose_file=compose_file,
            config_path=config_path,
        )
        typer.echo(f"submitted {envelope.submission_id} as {envelope.agent_id}")
    except (AgentError, ConfigError, OSError) as exc:
        message = getattr(exc, "message", str(exc))
        typer.echo(message, err=True)
        raise typer.Exit(code=EXIT_ERROR) from exc
    raise typer.Exit(code=EXIT_OK)


@agent_app.command("run")
def agent_run(
    controller_url: str = typer.Option(..., "--controller"),
    identity_dir: Path = typer.Option(
        Path("backuplint-agent"),
        "--identity-dir",
        help="Persistent agent identity + durable JSONL queue directory.",
    ),
    interval: float = typer.Option(
        30.0,
        "--interval",
        min=1.0,
        help="Seconds between heartbeat / policy / queue-flush cycles.",
    ),
    compose_file: Path | None = typer.Option(
        None,
        "--compose",
        help="Optional Compose file for periodic local scans (requires Docker).",
    ),
    config: Path | None = typer.Option(None, "--config", "-c"),
    submit_every: int = typer.Option(
        1,
        "--submit-every",
        min=1,
        help="When --compose is set, run a scan/submit every N cycles.",
    ),
    once: bool = typer.Option(
        False,
        "--once",
        help="Run a single cycle then exit (container/smoke friendly).",
    ),
) -> None:
    """Long-running agent loop: heartbeat, queue flush, policy apply, optional scan.

    Outbound-only. Does not require Docker socket unless --compose is provided.
    Private key material must already exist under --identity-dir (see enroll).
    """
    import signal
    import time

    from backuplint.fleet.agent import AgentError, AgentIdentity, AgentQueue, FleetAgent

    stop = False

    def _stop(_signum: int, _frame: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    try:
        identity = AgentIdentity.load(identity_dir)
        agent = FleetAgent(
            controller_url=controller_url,
            identity=identity,
            queue=AgentQueue(identity_dir / "queue.jsonl"),
            sleep=time.sleep,
        )
    except (AgentError, OSError) as exc:
        message = getattr(exc, "message", str(exc))
        typer.echo(message, err=True)
        raise typer.Exit(code=EXIT_ERROR) from exc

    if compose_file is not None and not compose_file.is_file():
        typer.echo(f"compose file not found: {compose_file}", err=True)
        raise typer.Exit(code=EXIT_ERROR)

    cycle = 0
    while not stop:
        cycle += 1
        try:
            agent.heartbeat(max_attempts=5)
            flushed = agent.flush(max_attempts_per_item=5)
            if flushed:
                typer.echo(f"flushed {flushed} queued submission(s)")
            applied = agent.poll_and_apply_policy(max_attempts=5)
            status = applied.get("apply_status") or applied.get("desired_status")
            typer.echo(f"cycle={cycle} heartbeat=ok policy={status}")
            if compose_file is not None and cycle % submit_every == 0:
                config_path = (
                    config if config is not None else default_config_path(compose_file)
                )
                envelope = agent.run_check_and_submit(
                    compose_file=compose_file,
                    config_path=config_path,
                )
                typer.echo(f"submitted {envelope.submission_id}")
        except AgentError as exc:
            typer.echo(f"cycle={cycle} error: {exc.message}", err=True)
            if once:
                raise typer.Exit(code=EXIT_ERROR) from exc
        except (ConfigError, OSError) as exc:
            message = getattr(exc, "message", str(exc))
            typer.echo(f"cycle={cycle} error: {message}", err=True)
            if once:
                raise typer.Exit(code=EXIT_ERROR) from exc
        if once or stop:
            break
        deadline = time.time() + float(interval)
        while not stop and time.time() < deadline:
            time.sleep(min(0.5, max(0.0, deadline - time.time())))

    raise typer.Exit(code=EXIT_OK)


@profile_app.command("validate")
def profile_validate(
    profile: Path = typer.Argument(..., help="Path to a YAML or JSON install profile."),
) -> None:
    """Validate and normalize an installation profile (no install)."""
    from backuplint.install.profile import ProfileError, load_profile_file

    try:
        parsed = load_profile_file(profile)
    except (OSError, ProfileError) as exc:
        message = getattr(exc, "message", str(exc))
        typer.echo(message, err=True)
        raise typer.Exit(code=EXIT_ERROR) from exc
    typer.echo(parsed.to_json(), nl=False)
    raise typer.Exit(code=EXIT_OK)


@profile_app.command("plan")
def profile_plan(
    profile: Path = typer.Argument(..., help="Path to a YAML or JSON install profile."),
    no_probe: bool = typer.Option(
        False,
        "--no-probe",
        help="Skip host dependency probes (plan structure only).",
    ),
) -> None:
    """Emit a machine-readable install plan JSON (never installs packages)."""
    from backuplint.install.plan import resolve_install_plan
    from backuplint.install.profile import ProfileError, load_profile_file

    try:
        parsed = load_profile_file(profile)
        plan = resolve_install_plan(parsed, probe_host=not no_probe)
    except (OSError, ProfileError) as exc:
        message = getattr(exc, "message", str(exc))
        typer.echo(message, err=True)
        raise typer.Exit(code=EXIT_ERROR) from exc
    typer.echo(plan.to_json(), nl=False)
    raise typer.Exit(code=EXIT_OK)


@install_app.callback(invoke_without_command=True)
def install_main(
    ctx: typer.Context,
    profile: Path | None = typer.Option(
        None,
        "--profile",
        help="YAML/JSON install profile for unattended native install.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show native mutation plan without changing the host.",
    ),
    non_interactive: bool = typer.Option(
        False,
        "--non-interactive",
        help="Never prompt; requires --profile and --yes for mutation.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Confirm package/service mutations (required with --non-interactive).",
    ),
    interactive: bool = typer.Option(
        False,
        "--interactive",
        help="Run the role/feature wizard (not allowed with --non-interactive).",
    ),
    prefix: Path | None = typer.Option(
        None,
        "--prefix",
        help="Filesystem prefix for lab installs (default: system paths).",
    ),
    start: bool = typer.Option(
        False,
        "--start",
        help="Start systemd services after enable (mutation only).",
    ),
) -> None:
    """Native install entry: interactive wizard or unattended profile execution."""
    if ctx.invoked_subcommand is not None:
        return
    from backuplint.install.executor import (
        InstallerError,
        build_native_install_plan,
        execute_native_install,
    )
    from backuplint.install.profile import ProfileError, load_profile_file
    from backuplint.install.wizard import WizardCancelled, run_interactive_wizard

    if non_interactive and interactive:
        typer.echo("Cannot combine --non-interactive with --interactive.", err=True)
        raise typer.Exit(code=EXIT_ERROR)
    if non_interactive and profile is None:
        typer.echo("--non-interactive requires --profile.", err=True)
        raise typer.Exit(code=EXIT_ERROR)
    if non_interactive and not dry_run and not yes:
        typer.echo(
            "--non-interactive mutation requires --yes (or use --dry-run).",
            err=True,
        )
        raise typer.Exit(code=EXIT_ERROR)

    try:
        if interactive or (profile is None and not non_interactive):
            if non_interactive:
                typer.echo(
                    "Interactive wizard disabled by --non-interactive.",
                    err=True,
                )
                raise typer.Exit(code=EXIT_ERROR)
            wizard = run_interactive_wizard()
            parsed = wizard.profile
        else:
            if profile is None:
                typer.echo("--profile is required.", err=True)
                raise typer.Exit(code=EXIT_ERROR)
            parsed = load_profile_file(profile)

        if dry_run:
            plan = build_native_install_plan(parsed, prefix=prefix)
            typer.echo(plan.to_json(), nl=False)
            raise typer.Exit(code=EXIT_OK)

        if not yes and not interactive:
            typer.echo(
                "Refusing to mutate without --yes "
                "(or use --interactive / --dry-run).",
                err=True,
            )
            raise typer.Exit(code=EXIT_NOT_CONFIRMED)

        result = execute_native_install(
            parsed,
            dry_run=False,
            prefix=prefix,
            assume_yes=True,
            start_services=start,
        )
        typer.echo(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        raise typer.Exit(code=EXIT_OK if result.ok else EXIT_ERROR)
    except WizardCancelled as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=EXIT_CANCEL) from exc
    except (OSError, ProfileError, InstallerError) as exc:
        message = getattr(exc, "message", str(exc))
        typer.echo(message, err=True)
        raise typer.Exit(code=EXIT_ERROR) from exc


@install_app.command("feature")
def install_feature_cmd(
    feature: str = typer.Argument(..., help="Feature id to install support for."),
    dry_run: bool = typer.Option(False, "--dry-run"),
    yes: bool = typer.Option(False, "--yes", "-y"),
    prefix: Path | None = typer.Option(None, "--prefix"),
) -> None:
    """Install support for one feature without enabling runtime behavior."""
    from backuplint.install.executor import InstallerError, add_feature
    from backuplint.install.features import parse_feature_id
    from backuplint.install.profile import ProfileError

    if not dry_run and not yes:
        typer.echo("Mutation requires --yes (or pass --dry-run).", err=True)
        raise typer.Exit(code=EXIT_NOT_CONFIRMED)
    try:
        fid = parse_feature_id(feature)
        result = add_feature(fid, dry_run=dry_run, prefix=prefix)
    except (ValueError, InstallerError, ProfileError, OSError) as exc:
        message = getattr(exc, "message", str(exc))
        typer.echo(message, err=True)
        raise typer.Exit(code=EXIT_ERROR) from exc
    typer.echo(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    raise typer.Exit(code=EXIT_OK if result.ok else EXIT_ERROR)


@install_app.command("uninstall")
def install_uninstall_cmd(
    dry_run: bool = typer.Option(True, "--dry-run/--execute"),
    yes: bool = typer.Option(False, "--yes", "-y"),
    prefix: Path | None = typer.Option(None, "--prefix"),
) -> None:
    """Plan or execute safe uninstall of installer-owned units/manifest only."""
    from backuplint.install.executor import (
        InstallerError,
        build_uninstall_plan,
        execute_uninstall,
    )

    try:
        if dry_run:
            plan = build_uninstall_plan(prefix=prefix)
            typer.echo(
                json.dumps({"ok": True, "dry_run": True, "plan": plan}, indent=2)
            )
            raise typer.Exit(code=EXIT_OK)
        if not yes:
            typer.echo("Uninstall execution requires --yes.", err=True)
            raise typer.Exit(code=EXIT_NOT_CONFIRMED)
        result = execute_uninstall(prefix=prefix, dry_run=False)
    except InstallerError as exc:
        typer.echo(exc.message, err=True)
        raise typer.Exit(code=EXIT_ERROR) from exc
    typer.echo(json.dumps(result, indent=2, sort_keys=True))
    raise typer.Exit(code=EXIT_OK)


def run() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":
    run()