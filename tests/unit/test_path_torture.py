"""Production-readiness path torture tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from backuplint.paths import PathResolutionError, is_path_covered


@pytest.mark.parametrize(
    ("data", "backup", "expected"),
    [
        ("/srv/app", "/srv/app", True),
        ("/srv/app2", "/srv/app", False),
        ("/srv/app-old", "/srv/app", False),
        ("/srv/app/child", "/srv/app", True),
        ("/srv/app", "/srv/app/child", False),
        ("/srv/app/", "/srv/app", True),
        ("/srv//app", "/srv/app", True),
        ("/srv/app/./x", "/srv/app", True),
        ("/srv/apps/../app", "/srv/app", True),
        ("/srv/app with spaces/data", "/srv/app with spaces", True),
        ("/srv/app(prod)/data", "/srv/app(prod)", True),
        ("/srv/app[1]/data", "/srv/app[1]", True),
        ("/srv/app-name_v2/data", "/srv/app-name_v2", True),
        ("/srv/应用/数据", "/srv/应用", True),
        ("/srv/" + ("a" * 200), "/srv/" + ("a" * 200), True),
        ("/srv/nonexistent-xyz/child", "/srv/nonexistent-xyz", True),
    ],
)
def test_path_matrix(data: str, backup: str, expected: bool) -> None:
    assert is_path_covered(data, backup) is expected


def test_leading_dash_path_is_data_not_flag(tmp_path: Path) -> None:
    path = tmp_path / "-weird-name"
    path.mkdir()
    assert is_path_covered(path, tmp_path) is True


def test_metacharacter_paths_are_literal(tmp_path: Path) -> None:
    for name in ('a;b', 'a&b', 'a$b', "a`b", "a'b", 'a"b'):
        path = tmp_path / name
        path.mkdir()
        assert is_path_covered(path, tmp_path) is True
        assert is_path_covered(path, tmp_path / "other") is False


def test_symlink_to_backup_root(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "child").mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    assert is_path_covered(link / "child", real) is True


def test_symlink_loop_raises_or_lexical(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.symlink_to(b)
    b.symlink_to(a)
    # Either a resolution error or lexical fallback is acceptable; never silent PASS
    # against an unrelated backup root.
    try:
        result = is_path_covered(a, tmp_path / "unrelated")
        assert result is False
    except (PathResolutionError, OSError, RuntimeError):
        pass


def test_permission_denied_does_not_false_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    backup = tmp_path / "other"

    def boom(self: Path) -> bool:  # noqa: ARG001
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "is_symlink", boom)
    # Lexical comparison still works; unrelated roots stay uncovered.
    assert is_path_covered(data, backup) is False
