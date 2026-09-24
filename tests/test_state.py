"""Offline tests for the fan_* fields on State (app/web/state.py) --
mirrors the existing cpu_* fields: set_fan() takes what FanController.tick()
just computed, and with fan.enabled off (or before the first tick) it just
carries zeros/None through snapshot() so the UI hides the row.

Run directly:  python tests/test_state.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.web.state import State  # noqa: E402


def test_fan_fields_default_to_absent():
    s = State()
    snap = s.snapshot()
    assert snap["fan_type"] == 0
    assert snap["fan_temp_c"] == 0.0
    assert snap["fan_duty_pct"] == 0.0
    assert snap["fan_rpm"] is None
    assert snap["fan_rpm_valid"] is None
    assert snap["fan_max_rpm"] is None
    assert snap["fan_min_duty_pct"] is None


def test_set_fan_populates_the_snapshot():
    s = State()
    s.set_fan({"fan_type": 3, "temp_c": 46.3, "duty_pct": 100.0,
               "rpm": 1200, "rpm_valid": True, "max_rpm": 1200, "min_duty_pct": 30})
    snap = s.snapshot()
    assert snap["fan_type"] == 3
    assert snap["fan_temp_c"] == 46.3
    assert snap["fan_duty_pct"] == 100.0
    assert snap["fan_rpm"] == 1200
    assert snap["fan_rpm_valid"] is True
    assert snap["fan_max_rpm"] == 1200
    assert snap["fan_min_duty_pct"] == 30


def test_set_fan_with_an_empty_status_resets_to_absent():
    """An empty dict (fan.enabled off, or before the first tick) -- a board
    that had it running and then loses it must not keep stale numbers."""
    s = State()
    s.set_fan({"fan_type": 3, "temp_c": 46.3, "rpm": 1200, "rpm_valid": True,
               "max_rpm": 1200, "min_duty_pct": 30})
    s.set_fan({})
    snap = s.snapshot()
    assert snap["fan_type"] == 0
    assert snap["fan_rpm"] is None
    assert snap["fan_rpm_valid"] is None
    assert snap["fan_max_rpm"] is None
    assert snap["fan_min_duty_pct"] is None


def _main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _main()
