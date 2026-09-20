# Scheduling (v0.4)

BackupLint can run local verification automatically.

## Product statement

> BackupLint keeps checking my backups automatically.

v0.4 is **local-only** for scheduling. Optional v0.5 `fleet:` config can submit
scheduled results to a controller after each check.

## Commands

```bash
backuplint daemon compose.yml -c backuplint.yml
backuplint schedule status compose.yml -c backuplint.yml
backuplint schedule next compose.yml -c backuplint.yml
backuplint schedule history compose.yml -c backuplint.yml
backuplint schedule run compose.yml coverage -c backuplint.yml
```

`backuplint scan` is unchanged.

## Config

```yaml
backup_paths:
  - /srv/docker

schedule:
  # Optional; default ~/.local/share/backuplint/schedule
  # state_dir: /var/lib/backuplint/schedule
  # Optional additive jitter (never fires earlier than the interval)
  # jitter: 5m
  history_limit: 500
  coverage:
    every: 30m
  integrity:
    every: 6h
  restore_verification:
    every: 24h
  deep_integrity:
    every: 7d
```

Conservative defaults match the intervals above when `every` is omitted for a listed job.

Disable a job with `enabled: false`.

Unknown keys are rejected. Schedule config never accepts shell commands.

## Behavior notes

- **Missed runs:** if the machine was off, BackupLint runs each overdue check **once** after resume (no catch-up storm). Multiple missed intervals collapse to a single catch-up; the next run is scheduled from resume time + interval (plus optional jitter).
- **Overlap:** checks are serialized (at most one check per tick). Additional due checks wait with an explicit deferred note and remain due until they run — overlap does not enqueue duplicate work.
- **Run order after outage:** when several checks are due together, coverage runs before integrity, then restore verification, then deep integrity.
- **Scheduled overrides:** each check type forces its integrity/restore mode (`coverage` → both off; `integrity` → standard; `deep_integrity` → deep; `restore_verification` → selected restore). Job-type overrides win over config defaults for those modes.
- **Crash / restart:** `next_run` advances only after a check finishes. A crash mid-check leaves the job due, so restart catch-up is one run — a stale in-memory lock cannot permanently block future work. Daemon flock is released on process exit.
- **Locking:** a POSIX flock on `daemon.lock` prevents two daemons on the same state dir.
- **History:** SQLite under the state dir; trimmed to `history_limit`; mode `0600`.
- **Results vs schedule truth:** PASS/FAIL/ERROR are audit outcomes. Schedule launch/deferral state is separate. An old PASS does not mean the check is current (status may show `stale schedule` when overdue).
- **Config reload:** unsupported; restart the daemon after config changes.

## systemd example

See [systemd/backuplint-scheduler.service](systemd/backuplint-scheduler.service).

Restart is required after editing `backuplint.yml`.
