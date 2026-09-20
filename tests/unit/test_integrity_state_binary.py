"""Regression: integrity state binary/garbage must not crash as UnicodeDecodeError."""

from __future__ import annotations

from pathlib import Path

import pytest

from backuplint.integrity_state import IntegrityStateError, load_integrity_state


def test_binary_state_file_raises_integrity_state_error(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_bytes(b"\x80\xff\xfe binary-not-utf8")
    with pytest.raises(IntegrityStateError, match="Unable to read integrity state"):
        load_integrity_state(path)
