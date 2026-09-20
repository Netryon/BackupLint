"""Small SQLite helpers for deterministic WAL / transaction behavior.

Used by ControllerStore and ScheduleStore. Keep helpers minimal so store
callers own locking and semantic policy.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def immediate_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a bounded critical section under BEGIN IMMEDIATE.

    Requires ``isolation_level=None`` (autocommit) connections so BEGIN/COMMIT
    are explicit. Nested transactions are not supported — callers must not
    enter this helper while a transaction is already open on ``conn``.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def configure_store_connection(
    conn: sqlite3.Connection,
    *,
    busy_timeout_ms: int = 5_000,
) -> None:
    """Apply shared durability pragmas used by BackupLint SQLite stores."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
    conn.execute("PRAGMA foreign_keys=ON")


def journal_mode(conn: sqlite3.Connection) -> str:
    row = conn.execute("PRAGMA journal_mode").fetchone()
    return str(row[0]).lower() if row else ""


def integrity_check_ok(conn: sqlite3.Connection) -> bool:
    row = conn.execute("PRAGMA integrity_check").fetchone()
    return row is not None and row[0] == "ok"
