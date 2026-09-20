"""Unit tests for duration parsing and age formatting."""

from __future__ import annotations

from datetime import timedelta

import pytest

from backuplint.timeutil import format_age, parse_duration


def test_parse_simple_units() -> None:
    assert parse_duration("24h") == timedelta(hours=24)
    assert parse_duration("7d") == timedelta(days=7)
    assert parse_duration("90m") == timedelta(minutes=90)
    assert parse_duration("30s") == timedelta(seconds=30)


def test_parse_combined_units() -> None:
    assert parse_duration("1d12h") == timedelta(days=1, hours=12)
    assert parse_duration("1h30m") == timedelta(hours=1, minutes=30)


def test_parse_invalid_duration() -> None:
    with pytest.raises(ValueError):
        parse_duration("")
    with pytest.raises(ValueError):
        parse_duration("yesterday")
    with pytest.raises(ValueError):
        parse_duration("0h")


def test_format_age() -> None:
    assert format_age(timedelta(hours=4)) == "4h"
    assert format_age(timedelta(days=3, hours=2)) == "3d 2h"
    assert format_age(timedelta(seconds=12)) == "12s"
