"""Unit tests for restore operational network failure classification."""

from __future__ import annotations

from backuplint.restore_verify import _classify_restore_operational_failure


def test_connection_refused_is_operational_error() -> None:
    msg = _classify_restore_operational_failure(
        returncode=1,
        output="Fatal: unable to open repo: dial tcp 127.0.0.1:9: connection refused",
    )
    assert msg is not None
    assert "network" in msg.lower() or "unavailable" in msg.lower()


def test_corruption_like_message_not_forced_operational() -> None:
    msg = _classify_restore_operational_failure(
        returncode=1,
        output="pack id does not match, ciphertext verification failed",
    )
    assert msg is None
