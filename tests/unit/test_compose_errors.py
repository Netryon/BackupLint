"""Failure-path unit tests that do not require a working Docker daemon."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from backuplint.compose import ComposeError, load_compose_config


def test_docker_missing(tmp_path: Path) -> None:
    compose = tmp_path / "compose.yml"
    compose.write_text("services: {}\n")
    with patch("backuplint.compose.shutil.which", return_value=None):
        with pytest.raises(ComposeError, match="Docker is not installed"):
            load_compose_config(compose)


def test_compose_path_is_directory(tmp_path: Path) -> None:
    with pytest.raises(ComposeError, match="not a file"):
        load_compose_config(tmp_path)
