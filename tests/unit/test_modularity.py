"""Optional module boundary tests (standalone / scheduler / fleet ladder)."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

from backuplint.config import parse_config_data


def test_standalone_config_without_schedule_or_fleet() -> None:
    cfg = parse_config_data({"backup_paths": ["/data"]})
    assert cfg.schedule is None
    assert cfg.fleet is None


def test_scheduler_config_without_fleet() -> None:
    cfg = parse_config_data(
        {
            "backup_paths": ["/data"],
            "schedule": {
                "coverage": {"every": "1h"},
                "integrity": {"enabled": False, "every": "1d"},
                "restore_verification": {"enabled": False, "every": "1d"},
                "deep_integrity": {"enabled": False, "every": "1d"},
            },
        }
    )
    assert cfg.schedule is not None
    assert cfg.fleet is None


def test_core_audit_import_does_not_need_controller() -> None:
    # Ensure audit can load even if fleet.controller was never imported.
    saved_controller = sys.modules.pop("backuplint.fleet.controller", None)
    saved_store = sys.modules.pop("backuplint.fleet.controller_store", None)
    try:
        audit = importlib.import_module("backuplint.audit")
        assert hasattr(audit, "run_audit")
        assert "backuplint.fleet.controller" not in sys.modules
    finally:
        # Restore prior modules so later tests keep stable exception identities.
        if saved_controller is not None:
            sys.modules["backuplint.fleet.controller"] = saved_controller
        else:
            sys.modules.pop("backuplint.fleet.controller", None)
        if saved_store is not None:
            sys.modules["backuplint.fleet.controller_store"] = saved_store
        else:
            sys.modules.pop("backuplint.fleet.controller_store", None)


def test_events_module_independent_of_ui() -> None:
    events = importlib.import_module("backuplint.events")
    assert hasattr(events, "CanonicalEvent")
    assert not Path(events.__file__).as_posix().endswith("dashboard.py")
