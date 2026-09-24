"""Offline tests for the fan PWM-frequency clamp (app/web/config_mgr.py).

50-500 Hz is the same hardware-safe range as pwm-fan-addon's period_ns clamp
and the PWM frequency slider (app.js) -- see _clamp_fan_period_ns's docstring
for why. This covers the clamp helper itself and ConfigManager.load()
applying it to whatever is sitting in config.json.

Run directly:  python tests/test_fan_freq_clamp.py
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.web.config_mgr import (  # noqa: E402
    ConfigManager,
    FAN_MAX_PERIOD_NS,
    FAN_MIN_PERIOD_NS,
    _clamp_fan_period_ns,
)


def test_clamp_caps_period_ns_above_500hz():
    cfg = {"fan": {"period_ns": 1555210}}  # ~643 Hz
    _clamp_fan_period_ns(cfg)
    assert cfg["fan"]["period_ns"] == FAN_MIN_PERIOD_NS  # 500 Hz ceiling


def test_clamp_floors_period_ns_below_50hz():
    cfg = {"fan": {"period_ns": 100000000}}  # 10 Hz
    _clamp_fan_period_ns(cfg)
    assert cfg["fan"]["period_ns"] == FAN_MAX_PERIOD_NS  # 50 Hz floor


def test_clamp_leaves_in_range_period_ns_untouched():
    cfg = {"fan": {"period_ns": 5000000}}  # 200 Hz
    _clamp_fan_period_ns(cfg)
    assert cfg["fan"]["period_ns"] == 5000000


def test_clamp_tolerates_missing_fan_section():
    cfg = {"wifi": {"mode": "ap"}}
    _clamp_fan_period_ns(cfg)  # must not raise
    assert "fan" not in cfg


def test_load_clamps_an_out_of_range_period_ns_already_on_disk():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "config.json"
        path.write_text(json.dumps({"fan": {"period_ns": 1555210}}))  # ~643 Hz
        cm = ConfigManager(config_path=path)
        cfg = cm.load()
        assert cfg["fan"]["period_ns"] == FAN_MIN_PERIOD_NS


def _main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _main()
