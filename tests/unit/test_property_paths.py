"""Property-based tests for path coverage and restore destination safety."""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from backuplint.paths import is_path_covered, normalize_path
from backuplint.restore_dest import (
    RestoreDestinationError,
    assert_safe_restore_target,
    create_owned_restore_root,
    expected_materialized_path,
)

# Bound generation so CI stays fast; workshop campaigns can raise max_examples.
_settings = settings(
    max_examples=80,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)

_safe_seg = st.from_regex(r"[A-Za-z0-9_\-]{1,12}", fullmatch=True)
_unicode_seg = st.sampled_from(["café", "データ", "файл", "αβγ", "naïve"])
_edge_seg = st.sampled_from(
    [
        "name with spaces",
        ".dotfile-parent",
        "leading-dash-ok",
        "quote'here",
        'quote"here',
        "semi;colon",
        "amp&ersand",
        "dollar$sign",
        "back`tick",
        "paren(s)",
        "brack[et]",
        "brace{s}",
        "wild*card?",
        "emoji-🔐",
    ]
)
_path_seg = st.one_of(_safe_seg, _unicode_seg, _edge_seg)


@st.composite
def abs_path_under(draw: st.DrawFn, root: str = "/srv") -> str:
    depth = draw(st.integers(min_value=1, max_value=4))
    parts = [draw(_path_seg) for _ in range(depth)]
    # Avoid empty / traversal-only segments Hypothesis might still emit.
    cleaned = [p for p in parts if p not in {".", "..", ""}]
    if not cleaned:
        cleaned = ["data"]
    return root + "/" + "/".join(cleaned)


@_settings
@given(path=abs_path_under())
def test_normalize_idempotent_lexical(path: str) -> None:
    once = normalize_path(path)
    twice = normalize_path(once)
    assert once == twice


@_settings
@given(
    base=st.sampled_from(["/srv/app", "/data/store", "/var/lib/svc"]),
    sibling_suffix=st.from_regex(r"[A-Za-z0-9]{1,8}", fullmatch=True),
)
def test_common_prefix_sibling_never_covered(base: str, sibling_suffix: str) -> None:
    sibling = base + sibling_suffix
    assert not is_path_covered(sibling, base)
    assert not is_path_covered(base, sibling)


@_settings
@given(
    parent=abs_path_under("/backup"),
    child_extra=_safe_seg,
)
def test_parent_covers_nested_child(parent: str, child_extra: str) -> None:
    child = f"{parent}/{child_extra}"
    assert is_path_covered(child, parent)
    assert is_path_covered(parent, parent)


@_settings
@given(
    slashes=st.integers(min_value=2, max_value=5),
    name=_safe_seg,
)
def test_redundant_slashes_normalize_consistently(slashes: int, name: str) -> None:
    messy = "/" + ("/" * slashes) + "srv" + ("/" * slashes) + name
    assert normalize_path(messy) == normalize_path(f"/srv/{name}")


@_settings
@given(data=st.binary(min_size=0, max_size=64))
def test_expected_materialized_stays_under_root(tmp_path: Path, data: bytes) -> None:
    # Fixture + hypothesis: use a fresh subdir per example via token in path.
    root = tmp_path / "restore-root"
    root.mkdir(exist_ok=True)
    source = "/srv/app/payload.bin"
    out = expected_materialized_path(root, source)
    assert root in out.parents or out == root / "srv" / "app" / "payload.bin"
    assert str(out).startswith(str(root))
    # Writing oracle bytes must not escape.
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    assert out.resolve().is_relative_to(root.resolve())


@_settings
@given(name=_safe_seg)
def test_owned_restore_root_never_escapes_parent(tmp_path: Path, name: str) -> None:
    parent = tmp_path / name
    parent.mkdir(exist_ok=True)
    owned = create_owned_restore_root(parent=parent)
    try:
        assert owned.path.parent == parent.resolve()
        assert owned.path.name.startswith("backuplint-restore-")
        assert owned.path.resolve().is_relative_to(parent.resolve())
    finally:
        from backuplint.restore_dest import cleanup_owned_restore_root

        cleanup_owned_restore_root(owned)


def test_repo_and_live_paths_not_safe_destinations(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    live = tmp_path / "live"
    repo.mkdir()
    live.mkdir()
    with pytest.raises(RestoreDestinationError):
        assert_safe_restore_target(repo, repository=str(repo))
    with pytest.raises(RestoreDestinationError):
        assert_safe_restore_target(live, live_bind_paths=(live,))
    # Sibling of live remains allowed.
    assert_safe_restore_target(tmp_path / "sibling", live_bind_paths=(live,))
