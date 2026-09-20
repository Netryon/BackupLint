"""Policy persistence mixin for ControllerStore (schema v7)."""

from __future__ import annotations

import json
import secrets
import sqlite3
import uuid
from typing import TYPE_CHECKING, Any

from backuplint.fleet.protocol import utc_now_iso
from backuplint.policy.errors import PolicyError
from backuplint.policy.resolve import AssignmentInputs, resolve_effective_assignment
from backuplint.policy.rollout import (
    RolloutMemberStatus,
    RolloutStatus,
    compute_batch_number,
    transition_rollout,
    validate_rollout_config,
)
from backuplint.policy.schema import (
    PolicySnapshot,
    build_policy_snapshot,
    snapshot_to_dict,
)

if TYPE_CHECKING:
    pass


class PolicyStoreMixin:
    """Policy CRUD, assignment resolution, rollout, and audit."""

    _conn: sqlite3.Connection
    _lock: Any

    def _policy_meta_get(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM policy_meta WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None

    def _policy_meta_set(self, key: str, value: str) -> None:
        self._conn.execute(
            """
            INSERT INTO policy_meta (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (key, value),
        )

    def _next_assignment_generation(self) -> int:
        raw = self._policy_meta_get("assignment_generation")
        current = int(raw) if raw is not None else 0
        nxt = current + 1
        self._policy_meta_set("assignment_generation", str(nxt))
        return nxt

    def _audit_policy(
        self,
        *,
        actor: str,
        action: str,
        target: str,
        result: str,
        old_ref: str | None = None,
        new_ref: str | None = None,
        details: dict[str, object] | None = None,
    ) -> str:
        audit_id = f"aud-{uuid.uuid4().hex[:16]}"
        self._conn.execute(
            """
            INSERT INTO policy_audit (
              audit_id, occurred_at, actor, action, target,
              old_ref, new_ref, result, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                audit_id,
                utc_now_iso(),
                actor,
                action,
                target,
                old_ref,
                new_ref,
                result,
                json.dumps(details or {}, separators=(",", ":")),
            ),
        )
        return audit_id

    def _fetch_revision(self, revision_id: str) -> PolicySnapshot | None:
        row = self._conn.execute(
            """
            SELECT schema_version, policy_id, revision_id, created_at, created_by,
                   content_sha256, display_name, description, settings_json
            FROM policy_revisions WHERE revision_id = ?
            """,
            (revision_id,),
        ).fetchone()
        if row is None:
            return None
        settings = json.loads(str(row[8]))
        return PolicySnapshot(
            schema_version=int(row[0]),
            policy_id=str(row[1]),
            revision_id=str(row[2]),
            created_at=str(row[3]),
            created_by=str(row[4]),
            content_sha256=str(row[5]),
            display_name=str(row[6]),
            description=str(row[7]),
            settings=settings,
        )

    def policy_create(
        self,
        *,
        display_name: str,
        description: str,
        settings: dict[str, object],
        created_by: str,
        policy_id: str | None = None,
    ) -> PolicySnapshot:
        from backuplint.policy.schema import new_policy_id

        resolved_id = policy_id or new_policy_id()
        snapshot = build_policy_snapshot(
            policy_id=resolved_id,
            created_by=created_by,
            display_name=display_name,
            description=description,
            settings=settings,
        )
        with self._lock:
            with self._immediate_txn():
                existing = self._conn.execute(
                    "SELECT 1 FROM policies WHERE policy_id = ?", (resolved_id,)
                ).fetchone()
                if existing is not None:
                    raise self._store_error(f"policy already exists: {resolved_id}")
                now = utc_now_iso()
                self._conn.execute(
                    """
                    INSERT INTO policies (
                      policy_id, display_name, description, created_at, created_by
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot.policy_id,
                        snapshot.display_name,
                        snapshot.description,
                        now,
                        created_by,
                    ),
                )
                self._insert_revision(snapshot)
                self._audit_policy(
                    actor=created_by,
                    action="policy.create",
                    target=snapshot.policy_id,
                    result="ok",
                    new_ref=snapshot.revision_id,
                )
        return snapshot

    def policy_create_revision(
        self,
        policy_id: str,
        *,
        display_name: str,
        description: str,
        settings: dict[str, object],
        created_by: str,
    ) -> PolicySnapshot:
        snapshot = build_policy_snapshot(
            policy_id=policy_id,
            created_by=created_by,
            display_name=display_name,
            description=description,
            settings=settings,
        )
        with self._lock:
            with self._immediate_txn():
                row = self._conn.execute(
                    "SELECT 1 FROM policies WHERE policy_id = ?", (policy_id,)
                ).fetchone()
                if row is None:
                    raise self._store_error(f"unknown policy: {policy_id}")
                self._insert_revision(snapshot)
                self._audit_policy(
                    actor=created_by,
                    action="policy.revision.create",
                    target=policy_id,
                    result="ok",
                    new_ref=snapshot.revision_id,
                )
        return snapshot

    def _insert_revision(self, snapshot: PolicySnapshot) -> None:
        self._conn.execute(
            """
            INSERT INTO policy_revisions (
              revision_id, policy_id, schema_version, created_at, created_by,
              content_sha256, display_name, description, settings_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot.revision_id,
                snapshot.policy_id,
                snapshot.schema_version,
                snapshot.created_at,
                snapshot.created_by,
                snapshot.content_sha256,
                snapshot.display_name,
                snapshot.description,
                json.dumps(snapshot.settings, separators=(",", ":")),
            ),
        )

    def policy_get_revision(self, revision_id: str) -> PolicySnapshot | None:
        with self._lock:
            return self._fetch_revision(revision_id)

    def policy_list(self) -> list[dict[str, object]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT p.policy_id, p.display_name, p.description, p.created_at, p.created_by,
                       (
                         SELECT revision_id FROM policy_revisions r
                         WHERE r.policy_id = p.policy_id
                         ORDER BY created_at DESC LIMIT 1
                       ) AS latest_revision_id
                FROM policies p ORDER BY p.created_at DESC
                """
            ).fetchall()
        return [
            {
                "policy_id": row[0],
                "display_name": row[1],
                "description": row[2],
                "created_at": row[3],
                "created_by": row[4],
                "latest_revision_id": row[5],
            }
            for row in rows
        ]

    def policy_list_revisions(self, policy_id: str) -> list[dict[str, object]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT revision_id, created_at, created_by, content_sha256, display_name
                FROM policy_revisions WHERE policy_id = ?
                ORDER BY created_at DESC
                """,
                (policy_id,),
            ).fetchall()
        return [
            {
                "revision_id": row[0],
                "created_at": row[1],
                "created_by": row[2],
                "content_sha256": row[3],
                "display_name": row[4],
            }
            for row in rows
        ]

    def policy_group_create(
        self, *, display_name: str, description: str = "", group_id: str | None = None
    ) -> str:
        resolved = group_id or f"grp-{secrets.token_hex(8)}"
        now = utc_now_iso()
        with self._lock:
            with self._immediate_txn():
                self._conn.execute(
                    """
                    INSERT INTO policy_groups (
                      group_id, display_name, description, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (resolved, display_name, description, now, now),
                )
                self._audit_policy(
                    actor="operator",
                    action="policy.group.create",
                    target=resolved,
                    result="ok",
                )
        return resolved

    def policy_group_add_member(self, group_id: str, agent_id: str) -> None:
        with self._lock:
            with self._immediate_txn():
                self._ensure_group(group_id)
                agent = self._fetch_agent(agent_id)
                if agent is None:
                    raise self._store_error(f"unknown agent: {agent_id}")
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO policy_group_members (group_id, agent_id, added_at)
                    VALUES (?, ?, ?)
                    """,
                    (group_id, agent_id, utc_now_iso()),
                )
                self._recompute_desired_for_agent(agent_id)
                self._audit_policy(
                    actor="operator",
                    action="policy.group.member.add",
                    target=f"{group_id}:{agent_id}",
                    result="ok",
                )

    def policy_group_remove_member(self, group_id: str, agent_id: str) -> None:
        with self._lock:
            with self._immediate_txn():
                self._conn.execute(
                    "DELETE FROM policy_group_members WHERE group_id = ? AND agent_id = ?",
                    (group_id, agent_id),
                )
                self._recompute_desired_for_agent(agent_id)
                self._audit_policy(
                    actor="operator",
                    action="policy.group.member.remove",
                    target=f"{group_id}:{agent_id}",
                    result="ok",
                )

    def policy_assign_agent(
        self, agent_id: str, revision_id: str, *, actor: str = "operator"
    ) -> int:
        with self._lock:
            with self._immediate_txn():
                return self._assign_agent_locked(
                    agent_id, revision_id, actor=actor
                )

    def _assign_agent_locked(
        self, agent_id: str, revision_id: str, *, actor: str = "operator"
    ) -> int:
        """Assign policy to agent. Caller must hold lock + open transaction."""
        snapshot = self._require_revision_unlocked(revision_id)
        agent = self._fetch_agent(agent_id)
        if agent is None:
            raise self._store_error(f"unknown agent: {agent_id}")
        now = utc_now_iso()
        self._conn.execute(
            """
            INSERT INTO policy_assignments (
              assignment_id, kind, target_id, policy_id, revision_id, created_at, updated_at
            ) VALUES (?, 'agent', ?, ?, ?, ?, ?)
            ON CONFLICT(kind, target_id) DO UPDATE SET
              policy_id=excluded.policy_id,
              revision_id=excluded.revision_id,
              updated_at=excluded.updated_at
            """,
            (
                f"asg-{agent_id}",
                agent_id,
                snapshot.policy_id,
                revision_id,
                now,
                now,
            ),
        )
        generation = self._recompute_desired_for_agent(agent_id)
        self._audit_policy(
            actor=actor,
            action="policy.assign.agent",
            target=agent_id,
            result="ok",
            new_ref=revision_id,
            details={"assignment_generation": generation},
        )
        return int(generation or 0)

    def policy_assign_group(
        self, group_id: str, revision_id: str, *, actor: str = "operator"
    ) -> None:
        with self._lock:
            with self._immediate_txn():
                snapshot = self._require_revision_unlocked(revision_id)
                self._ensure_group(group_id)
                now = utc_now_iso()
                self._conn.execute(
                    """
                    INSERT INTO policy_assignments (
                      assignment_id, kind, target_id, policy_id, revision_id, created_at, updated_at
                    ) VALUES (?, 'group', ?, ?, ?, ?, ?)
                    ON CONFLICT(kind, target_id) DO UPDATE SET
                      policy_id=excluded.policy_id,
                      revision_id=excluded.revision_id,
                      updated_at=excluded.updated_at
                    """,
                    (
                        f"asg-group-{group_id}",
                        group_id,
                        snapshot.policy_id,
                        revision_id,
                        now,
                        now,
                    ),
                )
                members = self._conn.execute(
                    "SELECT agent_id FROM policy_group_members WHERE group_id = ?",
                    (group_id,),
                ).fetchall()
                for (member_id,) in members:
                    self._recompute_desired_for_agent(str(member_id))
                self._audit_policy(
                    actor=actor,
                    action="policy.assign.group",
                    target=group_id,
                    result="ok",
                    new_ref=revision_id,
                )

    def policy_set_default(self, revision_id: str, *, actor: str = "operator") -> None:
        with self._lock:
            with self._immediate_txn():
                snapshot = self._require_revision_unlocked(revision_id)
                now = utc_now_iso()
                self._conn.execute(
                    """
                    INSERT INTO policy_assignments (
                      assignment_id, kind, target_id, policy_id, revision_id, created_at, updated_at
                    ) VALUES ('default', 'default', '', ?, ?, ?, ?)
                    ON CONFLICT(kind, target_id) DO UPDATE SET
                      policy_id=excluded.policy_id,
                      revision_id=excluded.revision_id,
                      updated_at=excluded.updated_at
                    """,
                    (
                        snapshot.policy_id,
                        revision_id,
                        now,
                        now,
                    ),
                )
                agents = self._conn.execute("SELECT agent_id FROM agents").fetchall()
                for (agent_id,) in agents:
                    self._recompute_desired_for_agent(str(agent_id))
                self._audit_policy(
                    actor=actor,
                    action="policy.assign.default",
                    target="default",
                    result="ok",
                    new_ref=revision_id,
                )

    def policy_rollback_agent(
        self, agent_id: str, revision_id: str, *, actor: str = "operator"
    ) -> int:
        """Assign older revision with new assignment_generation."""
        return self.policy_assign_agent(agent_id, revision_id, actor=actor)

    def _require_revision_unlocked(self, revision_id: str) -> PolicySnapshot:
        snap = self._fetch_revision(revision_id)
        if snap is None:
            raise self._store_error(f"unknown revision: {revision_id}")
        return snap

    def _require_revision(self, revision_id: str) -> PolicySnapshot:
        with self._lock:
            return self._require_revision_unlocked(revision_id)

    def _ensure_group(self, group_id: str) -> None:
        row = self._conn.execute(
            "SELECT 1 FROM policy_groups WHERE group_id = ?", (group_id,)
        ).fetchone()
        if row is None:
            raise self._store_error(f"unknown group: {group_id}")

    def _assignment_tuple(
        self, row: tuple[object, ...] | None
    ) -> tuple[str, str, str] | None:
        if row is None:
            return None
        revision_id = str(row[1])
        snap = self._fetch_revision(revision_id)
        if snap is None:
            return None
        return (snap.policy_id, revision_id, snap.content_sha256)

    def _resolve_inputs(self, agent_id: str) -> AssignmentInputs:
        explicit_row = self._conn.execute(
            """
            SELECT policy_id, revision_id FROM policy_assignments
            WHERE kind = 'agent' AND target_id = ?
            """,
            (agent_id,),
        ).fetchone()
        explicit = None
        if explicit_row is not None:
            explicit = self._assignment_tuple(explicit_row)

        group_rows = self._conn.execute(
            """
            SELECT g.group_id, a.policy_id, a.revision_id
            FROM policy_group_members m
            JOIN policy_groups g ON g.group_id = m.group_id
            JOIN policy_assignments a ON a.kind = 'group' AND a.target_id = g.group_id
            WHERE m.agent_id = ?
            """,
            (agent_id,),
        ).fetchall()
        group_assignments: list[tuple[str, str, str, str]] = []
        for group_id, _policy_id, revision_id in group_rows:
            snap = self._fetch_revision(str(revision_id))
            if snap is not None:
                group_assignments.append(
                    (str(group_id), snap.policy_id, snap.revision_id, snap.content_sha256)
                )

        default_row = self._conn.execute(
            """
            SELECT policy_id, revision_id FROM policy_assignments
            WHERE kind = 'default'
            """
        ).fetchone()
        default_assignment = self._assignment_tuple(default_row)

        return AssignmentInputs(
            agent_id=agent_id,
            explicit=explicit,
            group_assignments=tuple(group_assignments),
            default_assignment=default_assignment,
        )

    def _recompute_desired_for_agent(self, agent_id: str) -> int | None:
        try:
            resolved = resolve_effective_assignment(self._resolve_inputs(agent_id))
        except PolicyError as exc:
            if "conflicting" in exc.message:
                raise self._store_error(exc.message) from exc
            self._conn.execute(
                "DELETE FROM policy_desired_state WHERE agent_id = ?", (agent_id,)
            )
            return None
        generation = self._next_assignment_generation()
        now = utc_now_iso()
        self._conn.execute(
            """
            INSERT INTO policy_desired_state (
              agent_id, assignment_generation, policy_id, revision_id, content_sha256,
              resolved_at, source_kind, source_ref
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(agent_id) DO UPDATE SET
              assignment_generation=excluded.assignment_generation,
              policy_id=excluded.policy_id,
              revision_id=excluded.revision_id,
              content_sha256=excluded.content_sha256,
              resolved_at=excluded.resolved_at,
              source_kind=excluded.source_kind,
              source_ref=excluded.source_ref
            """,
            (
                agent_id,
                generation,
                resolved.policy_id,
                resolved.revision_id,
                resolved.content_sha256,
                now,
                resolved.source.value,
                resolved.source_ref,
            ),
        )
        self._update_drift_pending(
            agent_id, generation, resolved.revision_id, resolved.content_sha256
        )
        return generation

    def _update_drift_pending(
        self, agent_id: str, generation: int, revision_id: str, sha256: str
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO policy_drift (
              agent_id, drift_status, desired_generation, desired_revision_id,
              desired_sha256, updated_at
            ) VALUES (?, 'PENDING', ?, ?, ?, ?)
            ON CONFLICT(agent_id) DO UPDATE SET
              drift_status='PENDING',
              desired_generation=excluded.desired_generation,
              desired_revision_id=excluded.desired_revision_id,
              desired_sha256=excluded.desired_sha256,
              updated_at=excluded.updated_at
            """,
            (agent_id, generation, revision_id, sha256, utc_now_iso()),
        )

    def policy_get_desired(
        self,
        agent_id: str,
        *,
        current_assignment_generation: int | None = None,
        current_revision_id: str | None = None,
        current_content_sha256: str | None = None,
    ) -> dict[str, object]:
        with self._lock:
            agent = self._fetch_agent(agent_id)
            if agent is None:
                raise self._store_error("unknown agent")
            if agent.status != "active":
                raise self._store_error(f"agent is {agent.status}")
            row = self._conn.execute(
                """
                SELECT assignment_generation, policy_id, revision_id, content_sha256,
                       resolved_at, source_kind, source_ref
                FROM policy_desired_state WHERE agent_id = ?
                """,
                (agent_id,),
            ).fetchone()
            if row is None:
                return {"status": "NO_ASSIGNMENT"}
            generation = int(row[0])
            if (
                current_assignment_generation is not None
                and current_revision_id is not None
                and current_content_sha256 is not None
                and generation == current_assignment_generation
                and str(row[2]) == current_revision_id
                and str(row[3]) == current_content_sha256
            ):
                return {"status": "NO_CHANGE", "assignment_generation": generation}
            snapshot = self._fetch_revision(str(row[2]))
            if snapshot is None:
                raise self._store_error("desired revision missing")
            return {
                "status": "ASSIGNED",
                "assignment_generation": generation,
                "policy_id": row[1],
                "revision_id": row[2],
                "content_sha256": row[3],
                "resolved_at": row[4],
                "source_kind": row[5],
                "source_ref": row[6],
                "policy": snapshot_to_dict(snapshot),
            }

    def policy_report_applied(
        self,
        agent_id: str,
        *,
        assignment_generation: int,
        revision_id: str,
        content_sha256: str,
        apply_status: str,
        drift_status: str,
        reason: str = "",
        applied_at: str | None = None,
        local_config_sha256: str | None = None,
    ) -> None:
        with self._lock:
            with self._immediate_txn():
                agent = self._fetch_agent(agent_id)
                if agent is None:
                    raise self._store_error("unknown agent")
                if agent.status != "active":
                    raise self._store_error(f"agent is {agent.status}")
                desired = self._conn.execute(
                    """
                    SELECT assignment_generation, revision_id, content_sha256
                    FROM policy_desired_state WHERE agent_id = ?
                    """,
                    (agent_id,),
                ).fetchone()
                now = utc_now_iso()
                self._conn.execute(
                    """
                    INSERT INTO policy_applied_state (
                      agent_id, assignment_generation, revision_id, content_sha256,
                      apply_status, drift_status, reason, applied_at, reported_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(agent_id) DO UPDATE SET
                      assignment_generation=excluded.assignment_generation,
                      revision_id=excluded.revision_id,
                      content_sha256=excluded.content_sha256,
                      apply_status=excluded.apply_status,
                      drift_status=excluded.drift_status,
                      reason=excluded.reason,
                      applied_at=excluded.applied_at,
                      reported_at=excluded.reported_at
                    """,
                    (
                        agent_id,
                        assignment_generation,
                        revision_id,
                        content_sha256,
                        apply_status,
                        drift_status,
                        reason,
                        applied_at or now,
                        now,
                    ),
                )
                resolved_drift = drift_status
                if desired is not None:
                    if int(desired[0]) != assignment_generation:
                        resolved_drift = "DRIFTED"
                    elif str(desired[1]) != revision_id or str(desired[2]) != content_sha256:
                        resolved_drift = "DRIFTED"
                    elif drift_status == "IN_SYNC":
                        resolved_drift = "IN_SYNC"
                self._conn.execute(
                    """
                    INSERT INTO policy_drift (
                      agent_id, drift_status, desired_generation, desired_revision_id,
                      desired_sha256, applied_revision_id, applied_sha256,
                      local_config_sha256, reason, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(agent_id) DO UPDATE SET
                      drift_status=excluded.drift_status,
                      desired_generation=excluded.desired_generation,
                      desired_revision_id=excluded.desired_revision_id,
                      desired_sha256=excluded.desired_sha256,
                      applied_revision_id=excluded.applied_revision_id,
                      applied_sha256=excluded.applied_sha256,
                      local_config_sha256=excluded.local_config_sha256,
                      reason=excluded.reason,
                      updated_at=excluded.updated_at
                    """,
                    (
                        agent_id,
                        resolved_drift,
                        desired[0] if desired else None,
                        desired[1] if desired else None,
                        desired[2] if desired else None,
                        revision_id,
                        content_sha256,
                        local_config_sha256,
                        reason,
                        now,
                    ),
                )
                self._audit_policy(
                    actor=agent_id,
                    action="policy.apply.report",
                    target=agent_id,
                    result=apply_status,
                    new_ref=revision_id,
                    details={
                        "assignment_generation": assignment_generation,
                        "drift_status": resolved_drift,
                    },
                )

    def policy_list_drift(self, limit: int = 200) -> list[dict[str, object]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT agent_id, drift_status, desired_generation, desired_revision_id,
                       desired_sha256, applied_revision_id, applied_sha256,
                       local_config_sha256, reason, updated_at
                FROM policy_drift
                ORDER BY updated_at DESC LIMIT ?
                """,
                (max(1, min(limit, 1000)),),
            ).fetchall()
        return [
            {
                "agent_id": row[0],
                "drift_status": row[1],
                "desired_generation": row[2],
                "desired_revision_id": row[3],
                "desired_sha256": row[4],
                "applied_revision_id": row[5],
                "applied_sha256": row[6],
                "local_config_sha256": row[7],
                "reason": row[8],
                "updated_at": row[9],
            }
            for row in rows
        ]

    def policy_list_audit(self, limit: int = 200) -> list[dict[str, object]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT audit_id, occurred_at, actor, action, target,
                       old_ref, new_ref, result, details_json
                FROM policy_audit ORDER BY occurred_at DESC LIMIT ?
                """,
                (max(1, min(limit, 1000)),),
            ).fetchall()
        return [
            {
                "audit_id": row[0],
                "occurred_at": row[1],
                "actor": row[2],
                "action": row[3],
                "target": row[4],
                "old_ref": row[5],
                "new_ref": row[6],
                "result": row[7],
                "details": json.loads(str(row[8])),
            }
            for row in rows
        ]

    def policy_get_effective(self, agent_id: str) -> dict[str, object] | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT assignment_generation, policy_id, revision_id, content_sha256,
                       resolved_at, source_kind, source_ref
                FROM policy_desired_state WHERE agent_id = ?
                """,
                (agent_id,),
            ).fetchone()
            applied = self._conn.execute(
                """
                SELECT assignment_generation, revision_id, content_sha256,
                       apply_status, drift_status, reason, applied_at, reported_at
                FROM policy_applied_state WHERE agent_id = ?
                """,
                (agent_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "desired": {
                "assignment_generation": row[0],
                "policy_id": row[1],
                "revision_id": row[2],
                "content_sha256": row[3],
                "resolved_at": row[4],
                "source_kind": row[5],
                "source_ref": row[6],
            },
            "applied": (
                {
                    "assignment_generation": applied[0],
                    "revision_id": applied[1],
                    "content_sha256": applied[2],
                    "apply_status": applied[3],
                    "drift_status": applied[4],
                    "reason": applied[5],
                    "applied_at": applied[6],
                    "reported_at": applied[7],
                }
                if applied
                else None
            ),
        }

    # Rollout operations

    def policy_rollout_create(
        self,
        *,
        revision_id: str,
        target_group_id: str | None,
        batch_size: int,
        max_concurrent: int,
        pause_between_batches_seconds: int,
        failure_threshold: int,
        created_by: str,
        agent_ids: list[str] | None = None,
    ) -> str:
        validate_rollout_config(
            batch_size=batch_size,
            max_concurrent=max_concurrent,
            pause_between_batches_seconds=pause_between_batches_seconds,
            failure_threshold=failure_threshold,
        )
        rollout_id = f"rol-{secrets.token_hex(8)}"
        now = utc_now_iso()
        with self._lock:
            with self._immediate_txn():
                snap = self._require_revision_unlocked(revision_id)
                if target_group_id:
                    self._ensure_group(target_group_id)
                    member_rows = self._conn.execute(
                        "SELECT agent_id FROM policy_group_members WHERE group_id = ?",
                        (target_group_id,),
                    ).fetchall()
                    members = [str(r[0]) for r in member_rows]
                else:
                    members = list(agent_ids or [])
                if not members:
                    raise self._store_error("rollout requires target agents")
                self._conn.execute(
                    """
                    INSERT INTO policy_rollouts (
                      rollout_id, policy_id, revision_id, target_group_id,
                      batch_size, max_concurrent, pause_between_batches_seconds,
                      failure_threshold, status, created_at, created_by
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        rollout_id,
                        snap.policy_id,
                        revision_id,
                        target_group_id,
                        batch_size,
                        max_concurrent,
                        pause_between_batches_seconds,
                        failure_threshold,
                        RolloutStatus.PENDING.value,
                        now,
                        created_by,
                    ),
                )
                for idx, member in enumerate(members):
                    self._conn.execute(
                        """
                        INSERT INTO policy_rollout_members (
                          rollout_id, agent_id, status, batch_number, updated_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            rollout_id,
                            member,
                            RolloutMemberStatus.PENDING.value,
                            compute_batch_number(idx, batch_size),
                            now,
                        ),
                    )
                self._audit_policy(
                    actor=created_by,
                    action="policy.rollout.create",
                    target=rollout_id,
                    result="ok",
                    new_ref=revision_id,
                )
        return rollout_id

    def policy_rollout_start(self, rollout_id: str, *, actor: str = "operator") -> None:
        with self._lock:
            with self._immediate_txn():
                row = self._fetch_rollout(rollout_id)
                current = RolloutStatus(str(row["status"]))
                new_status = transition_rollout(current, RolloutStatus.RUNNING)
                now = utc_now_iso()
                self._conn.execute(
                    """
                    UPDATE policy_rollouts SET status = ?, started_at = COALESCE(started_at, ?)
                    WHERE rollout_id = ?
                    """,
                    (new_status.value, now, rollout_id),
                )
                self._advance_rollout_batch(rollout_id)
                self._audit_policy(
                    actor=actor,
                    action="policy.rollout.start",
                    target=rollout_id,
                    result="ok",
                )

    def policy_rollout_pause(self, rollout_id: str, *, actor: str = "operator") -> None:
        with self._lock:
            with self._immediate_txn():
                row = self._fetch_rollout(rollout_id)
                new_status = transition_rollout(
                    RolloutStatus(str(row["status"])), RolloutStatus.PAUSED
                )
                self._conn.execute(
                    """
                    UPDATE policy_rollouts SET status = ?, paused_at = ?
                    WHERE rollout_id = ?
                    """,
                    (new_status.value, utc_now_iso(), rollout_id),
                )
                self._audit_policy(
                    actor=actor,
                    action="policy.rollout.pause",
                    target=rollout_id,
                    result="ok",
                )

    def policy_rollout_resume(self, rollout_id: str, *, actor: str = "operator") -> None:
        with self._lock:
            with self._immediate_txn():
                row = self._fetch_rollout(rollout_id)
                new_status = transition_rollout(
                    RolloutStatus(str(row["status"])), RolloutStatus.RUNNING
                )
                self._conn.execute(
                    "UPDATE policy_rollouts SET status = ?, paused_at = NULL WHERE rollout_id = ?",
                    (new_status.value, rollout_id),
                )
                self._advance_rollout_batch(rollout_id)
                self._audit_policy(
                    actor=actor,
                    action="policy.rollout.resume",
                    target=rollout_id,
                    result="ok",
                )

    def policy_rollout_abort(self, rollout_id: str, *, actor: str = "operator") -> None:
        with self._lock:
            with self._immediate_txn():
                row = self._fetch_rollout(rollout_id)
                new_status = transition_rollout(
                    RolloutStatus(str(row["status"])), RolloutStatus.ABORTED
                )
                now = utc_now_iso()
                self._conn.execute(
                    """
                    UPDATE policy_rollouts SET status = ?, completed_at = ?
                    WHERE rollout_id = ?
                    """,
                    (new_status.value, now, rollout_id),
                )
                self._audit_policy(
                    actor=actor,
                    action="policy.rollout.abort",
                    target=rollout_id,
                    result="ok",
                )

    def policy_rollout_get(self, rollout_id: str) -> dict[str, object]:
        with self._lock:
            rollout = self._fetch_rollout(rollout_id)
            members = self._conn.execute(
                """
                SELECT agent_id, status, batch_number, updated_at
                FROM policy_rollout_members WHERE rollout_id = ?
                ORDER BY batch_number, agent_id
                """,
                (rollout_id,),
            ).fetchall()
        rollout["members"] = [
            {
                "agent_id": m[0],
                "status": m[1],
                "batch_number": m[2],
                "updated_at": m[3],
            }
            for m in members
        ]
        return rollout

    def policy_rollout_list(self, limit: int = 100) -> list[dict[str, object]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT rollout_id, policy_id, revision_id, target_group_id, status,
                       batch_size, max_concurrent, failure_threshold,
                       created_at, started_at, completed_at
                FROM policy_rollouts ORDER BY created_at DESC LIMIT ?
                """,
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [
            {
                "rollout_id": row[0],
                "policy_id": row[1],
                "revision_id": row[2],
                "target_group_id": row[3],
                "status": row[4],
                "batch_size": row[5],
                "max_concurrent": row[6],
                "failure_threshold": row[7],
                "created_at": row[8],
                "started_at": row[9],
                "completed_at": row[10],
            }
            for row in rows
        ]

    def _fetch_rollout(self, rollout_id: str) -> dict[str, object]:
        row = self._conn.execute(
            """
            SELECT rollout_id, policy_id, revision_id, target_group_id,
                   batch_size, max_concurrent, pause_between_batches_seconds,
                   failure_threshold, status, created_at, started_at, completed_at,
                   paused_at, created_by
            FROM policy_rollouts WHERE rollout_id = ?
            """,
            (rollout_id,),
        ).fetchone()
        if row is None:
            raise self._store_error(f"unknown rollout: {rollout_id}")
        return {
            "rollout_id": row[0],
            "policy_id": row[1],
            "revision_id": row[2],
            "target_group_id": row[3],
            "batch_size": row[4],
            "max_concurrent": row[5],
            "pause_between_batches_seconds": row[6],
            "failure_threshold": row[7],
            "status": row[8],
            "created_at": row[9],
            "started_at": row[10],
            "completed_at": row[11],
            "paused_at": row[12],
            "created_by": row[13],
        }

    def _advance_rollout_batch(self, rollout_id: str) -> None:
        rollout = self._fetch_rollout(rollout_id)
        revision_id = str(rollout["revision_id"])
        max_concurrent = int(rollout["max_concurrent"])
        failure_threshold = int(rollout["failure_threshold"])
        assigned = self._conn.execute(
            """
            SELECT COUNT(*) FROM policy_rollout_members
            WHERE rollout_id = ? AND status = ?
            """,
            (rollout_id, RolloutMemberStatus.ASSIGNED.value),
        ).fetchone()
        active_assigned = int(assigned[0]) if assigned else 0
        slots = max(0, max_concurrent - active_assigned)
        if slots <= 0:
            return
        pending = self._conn.execute(
            """
            SELECT agent_id FROM policy_rollout_members
            WHERE rollout_id = ? AND status = ?
            ORDER BY batch_number, agent_id LIMIT ?
            """,
            (rollout_id, RolloutMemberStatus.PENDING.value, slots),
        ).fetchall()
        now = utc_now_iso()
        for (agent_id,) in pending:
            self._assign_agent_locked(str(agent_id), revision_id, actor="rollout")
            self._conn.execute(
                """
                UPDATE policy_rollout_members SET status = ?, updated_at = ?
                WHERE rollout_id = ? AND agent_id = ?
                """,
                (RolloutMemberStatus.ASSIGNED.value, now, rollout_id, agent_id),
            )
        failed = self._conn.execute(
            """
            SELECT COUNT(*) FROM policy_rollout_members
            WHERE rollout_id = ? AND status = ?
            """,
            (rollout_id, RolloutMemberStatus.FAILED.value),
        ).fetchone()
        if failed and int(failed[0]) >= failure_threshold:
            self._conn.execute(
                """
                UPDATE policy_rollouts SET status = ?, completed_at = ?
                WHERE rollout_id = ?
                """,
                (RolloutStatus.FAILED.value, now, rollout_id),
            )
            return
        remaining = self._conn.execute(
            """
            SELECT COUNT(*) FROM policy_rollout_members
            WHERE rollout_id = ? AND status IN (?, ?)
            """,
            (
                rollout_id,
                RolloutMemberStatus.PENDING.value,
                RolloutMemberStatus.ASSIGNED.value,
            ),
        ).fetchone()
        if remaining and int(remaining[0]) == 0:
            self._conn.execute(
                """
                UPDATE policy_rollouts SET status = ?, completed_at = ?
                WHERE rollout_id = ?
                """,
                (RolloutStatus.COMPLETED.value, now, rollout_id),
            )

    def policy_rollout_tick_member(
        self, rollout_id: str, agent_id: str, apply_status: str
    ) -> None:
        """Update rollout member based on agent apply result."""
        with self._lock:
            with self._immediate_txn():
                if apply_status == "success":
                    status = RolloutMemberStatus.APPLIED.value
                else:
                    status = RolloutMemberStatus.FAILED.value
                self._conn.execute(
                    """
                    UPDATE policy_rollout_members SET status = ?, updated_at = ?
                    WHERE rollout_id = ? AND agent_id = ?
                    """,
                    (status, utc_now_iso(), rollout_id, agent_id),
                )
                self._advance_rollout_batch(rollout_id)

    def _store_error(self, message: str) -> Exception:
        # Prefer the exception type bound on the concrete ControllerStore class so
        # temporary sys.modules removals (modularity tests) cannot fork identity.
        err_type = getattr(type(self), "_store_error_type", None)
        if isinstance(err_type, type) and issubclass(err_type, Exception):
            return err_type(message)
        from backuplint.fleet.controller_store import ControllerStoreError

        return ControllerStoreError(message)

    def _immediate_txn(self) -> Any:
        from backuplint.sqlite_util import immediate_transaction

        return immediate_transaction(self._conn)
