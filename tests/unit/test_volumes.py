"""Unit tests for Docker volume mountpoint resolution."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from backuplint.compose import ComposeError
from backuplint.volumes import resolve_volume_mountpoint


def test_invalid_volume_name_rejected() -> None:
    with pytest.raises(ComposeError, match="invalid Docker volume name"):
        resolve_volume_mountpoint("../etc")


def test_missing_volume_returns_none() -> None:
    with patch("backuplint.volumes.shutil.which", return_value="/usr/bin/docker"):
        with patch("backuplint.volumes.run_argv") as run:
            run.return_value.returncode = 1
            run.return_value.stdout = ""
            run.return_value.stderr = "Error: No such volume"
            assert resolve_volume_mountpoint("missing_vol") is None


def test_mountpoint_returned() -> None:
    with patch("backuplint.volumes.shutil.which", return_value="/usr/bin/docker"):
        with patch("backuplint.volumes.run_argv") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "/var/lib/docker/volumes/appdata/_data\n"
            assert (
                resolve_volume_mountpoint("appdata")
                == "/var/lib/docker/volumes/appdata/_data"
            )
