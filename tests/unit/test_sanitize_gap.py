"""Unit: sanitize should not leave credentials in restic error text paths we control."""

from __future__ import annotations

from backuplint.restic import _sanitize


def test_sanitize_strips_secret_substrings() -> None:
    raw = "Fatal: wrong password for key with secret-value-xyz"
    out = _sanitize(raw, ("secret-value-xyz",))
    assert "secret-value-xyz" not in out
    assert "authentication failed" in out.lower() or "***" in out
