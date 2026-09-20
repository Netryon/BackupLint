"""Explicit client delivery / ACK accounting semantics for fleet submit.

Product contract (FleetAgent.flush):
  Any successful HTTP response from POST /v1/results is a durable ACK.
  The agent deletes the queue item and does not inspect ``created``.
  ``created`` is advisory for observers (harnesses, probes) that need to
  distinguish first insert vs idempotent replay.

Wire response:
  HTTP 200 {"ok": true, "created": true}  → newly inserted and confirmed
  HTTP 200 {"ok": true, "created": false} → already present / idempotently confirmed
  Transport / 5xx / other AgentError      → retryable failure (item stays queued)
  Auth / revoke (401/403)                 → permanent reject (harness policy)
  Item still in local queue               → still queued

Lost-ACK confirmation:
  If an attempt fails after the controller may have committed, a later
  ``created=false`` for the same submission_id confirms durable delivery.
  That confirmation must count toward delivery conservation — not only as
  an interesting duplicate.

Honest conservation equations (client observation):

  delivery_confirmed = newly_confirmed + lost_ack_confirmed

  generated_valid == delivery_confirmed + remaining_queued
                   + permanently_rejected_valid
                   (+ in_flight_unconfirmed when mid-attempt)

  campaign_events_in_db == delivery_confirmed
    when remaining_queued == 0 and permanent rejects never inserted.

Soft bounds (always honest under lost ACK):

  newly_confirmed <= campaign_events_in_db
    <= newly_confirmed + idempotent_observations + in_flight_ceiling

Do NOT require campaign_events_in_db == newly_confirmed alone; that fails
when the client never observed the original created=true.

Example (off-by-one at 2000):
  generated_valid=164659, permanently_rejected_valid=21, rem=0
  campaign_events_in_db=164638
  Client saw created=true 164637 times and one lost-ACK created=false.
  delivery_confirmed = 164637 + 1 = 164638
  generated_valid == delivery_confirmed + rem + perm  → holds
  campaign_events_in_db == delivery_confirmed         → holds
  campaign_events_in_db == newly_confirmed            → fails by 1 (dishonest gate)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from threading import Lock


class DeliveryState(StrEnum):
    """Client-visible delivery outcome for one submit attempt or queue item."""

    NEWLY_CONFIRMED = "newly_confirmed"
    IDEMPOTENTLY_CONFIRMED = "idempotently_confirmed"
    LOST_ACK_CONFIRMED = "lost_ack_confirmed"
    RETRYABLE_FAILURE = "retryable_failure"
    PERMANENT_REJECT = "permanent_reject"
    STILL_QUEUED = "still_queued"


def is_auth_reject_message(message: str) -> bool:
    return "401" in message or "403" in message or "revoked" in message


def classify_http_result(
    *,
    created: bool | None = None,
    error_message: str | None = None,
    prior_attempt_failed: bool = False,
    already_delivery_confirmed: bool = False,
) -> DeliveryState:
    """Classify one attempt from wire outcome + prior attempt history.

    ``created`` is only meaningful when ``error_message`` is None (HTTP 200 body).
    """
    if error_message is not None:
        if is_auth_reject_message(error_message):
            return DeliveryState.PERMANENT_REJECT
        return DeliveryState.RETRYABLE_FAILURE
    if created is None:
        raise ValueError("created is required when error_message is None")
    if created:
        return DeliveryState.NEWLY_CONFIRMED
    if prior_attempt_failed and not already_delivery_confirmed:
        return DeliveryState.LOST_ACK_CONFIRMED
    return DeliveryState.IDEMPOTENTLY_CONFIRMED


def counts_as_delivery_confirmation(state: DeliveryState) -> bool:
    return state in {
        DeliveryState.NEWLY_CONFIRMED,
        DeliveryState.LOST_ACK_CONFIRMED,
    }


@dataclass
class DeliveryAccounting:
    """Thread-safe per-submission delivery accounting for scale/correction harnesses.

    Tracks which submission_ids have seen a retryable failure so a later
    ``created=false`` can be classified as lost-ACK confirmation.
    """

    generated_valid: int = 0
    generated_invalid: int = 0
    queued_created: int = 0
    queued_drained: int = 0
    attempted: int = 0
    # Wire observations
    newly_confirmed: int = 0  # created=true (legacy: acked_new)
    idempotently_confirmed: int = 0  # created=false without prior fail (legacy dup)
    lost_ack_confirmed: int = 0  # created=false after attempt_failed
    submit_errors: int = 0
    permanently_rejected_valid: int = 0
    rejected_auth: int = 0
    rejected_schema: int = 0
    rejected_other: int = 0
    # Per-submission sets
    _attempt_failed: set[str] = field(default_factory=set)
    _delivery_confirmed: set[str] = field(default_factory=set)
    _lock: Lock = field(default_factory=Lock)

    @property
    def acked_new(self) -> int:
        """Alias for harness compatibility."""
        return self.newly_confirmed

    @property
    def acked_duplicate(self) -> int:
        """All created=false observations (idempotent + lost-ACK)."""
        return self.idempotently_confirmed + self.lost_ack_confirmed

    @property
    def delivery_confirmed(self) -> int:
        return self.newly_confirmed + self.lost_ack_confirmed

    @property
    def retryable_failures(self) -> int:
        """Non-auth submit errors that leave the item queued."""
        return self.rejected_schema + self.rejected_other

    def record_generated(self, *, valid: bool = True) -> None:
        with self._lock:
            if valid:
                self.generated_valid += 1
            else:
                self.generated_invalid += 1
            self.queued_created += 1

    def note_drained(self) -> None:
        with self._lock:
            self.queued_drained += 1

    def note_permanent_reject_drain(self) -> None:
        """Queue item dropped as permanent reject (e.g. revoked agent drain)."""
        with self._lock:
            self.queued_drained += 1
            self.permanently_rejected_valid += 1

    def observe(
        self,
        submission_id: str,
        *,
        created: bool | None = None,
        error_message: str | None = None,
    ) -> DeliveryState:
        """Record one submit attempt outcome; return classified DeliveryState."""
        with self._lock:
            self.attempted += 1
            prior_failed = submission_id in self._attempt_failed
            already = submission_id in self._delivery_confirmed
            state = classify_http_result(
                created=created,
                error_message=error_message,
                prior_attempt_failed=prior_failed,
                already_delivery_confirmed=already,
            )
            if state is DeliveryState.NEWLY_CONFIRMED:
                self.newly_confirmed += 1
                self._delivery_confirmed.add(submission_id)
                self._attempt_failed.discard(submission_id)
            elif state is DeliveryState.LOST_ACK_CONFIRMED:
                self.lost_ack_confirmed += 1
                self._delivery_confirmed.add(submission_id)
                self._attempt_failed.discard(submission_id)
            elif state is DeliveryState.IDEMPOTENTLY_CONFIRMED:
                self.idempotently_confirmed += 1
                self._delivery_confirmed.add(submission_id)
                self._attempt_failed.discard(submission_id)
            elif state is DeliveryState.RETRYABLE_FAILURE:
                self.submit_errors += 1
                msg = error_message or ""
                if "schema" in msg or "protocol" in msg:
                    self.rejected_schema += 1
                else:
                    self.rejected_other += 1
                self._attempt_failed.add(submission_id)
            elif state is DeliveryState.PERMANENT_REJECT:
                self.submit_errors += 1
                self.permanently_rejected_valid += 1
                self.rejected_auth += 1
                self._attempt_failed.discard(submission_id)
            return state

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "generated_valid": self.generated_valid,
                "generated_invalid": self.generated_invalid,
                "queued_created": self.queued_created,
                "queued_drained": self.queued_drained,
                "attempted": self.attempted,
                "acked_new": self.newly_confirmed,
                "acked_duplicate": self.acked_duplicate,
                "newly_confirmed": self.newly_confirmed,
                "idempotently_confirmed": self.idempotently_confirmed,
                "lost_ack_confirmed": self.lost_ack_confirmed,
                "delivery_confirmed": self.delivery_confirmed,
                "retryable_failures": self.retryable_failures,
                "submit_errors": self.submit_errors,
                "permanently_rejected_valid": self.permanently_rejected_valid,
                "rejected_auth": self.rejected_auth,
                "rejected_schema": self.rejected_schema,
                "rejected_other": self.rejected_other,
            }


def conservation_equations(
    *,
    generated_valid: int,
    delivery_confirmed: int,
    remaining_queued: int,
    permanently_rejected_valid: int,
    campaign_events_in_db: int,
    newly_confirmed: int,
    queued_created: int,
    queued_drained: int,
) -> dict[str, bool]:
    """Evaluate honest lost-ACK-aware conservation equations."""
    eq_valid = generated_valid == (
        delivery_confirmed + remaining_queued + permanently_rejected_valid
    )
    eq_delivery_storage = campaign_events_in_db == delivery_confirmed
    eq_queue = (queued_created - queued_drained) == remaining_queued
    # Soft bound: never require newly_confirmed alone to equal DB events.
    eq_soft_new_le_db = newly_confirmed <= campaign_events_in_db
    return {
        "generated_valid == delivery_confirmed + rem + permanently_rejected_valid": eq_valid,
        "campaign_events_in_db == delivery_confirmed": eq_delivery_storage,
        "queued_created - queued_drained == remaining_queued": eq_queue,
        "newly_confirmed <= campaign_events_in_db": eq_soft_new_le_db,
    }
