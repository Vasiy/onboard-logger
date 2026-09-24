"""Offline tests for the /api/fan/probe endpoint's config persistence
(app/main.py) -- the calibration sweep itself (FanController.probe) is
covered directly in tests/test_fan.py; this covers only what the endpoint
does with the result: save min_duty_pct/max_rpm into config.json when the
sweep found the fan spinning, leave a prior calibration alone when it
didn't, and keep the live controller's max_rpm in sync immediately.

Run directly:  python tests/test_fan_api.py
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import main  # noqa: E402
from app.web.config_mgr import ConfigManager  # noqa: E402


class _FakeController:
    """Stands in for the real FanController: probe() returns a canned
    calibration result instead of touching any hardware, so the endpoint's
    own persistence logic can be exercised without a board."""

    def __init__(self, result: dict):
        self._result = result
        self._max_rpm = None

    def probe(self, cfg, gpio):
        return self._result


def _run_probe(fan_cfg: dict, probe_result: dict, gpio: int = 16):
    with tempfile.TemporaryDirectory() as d:
        cfg_path = Path(d) / "config.json"
        cfg_path.write_text(json.dumps({"fan": fan_cfg}))
        cm = ConfigManager(config_path=cfg_path)
        orig_cm, orig_ctl = main.cm, main.fan_controller
        main.cm, main.fan_controller = cm, _FakeController(probe_result)
        try:
            response = asyncio.run(main.probe_fan_gpio({"gpio": gpio}))
            saved = cm.load()["fan"]
            return response, saved, main.fan_controller._max_rpm
        finally:
            main.cm, main.fan_controller = orig_cm, orig_ctl


def test_probe_persists_min_duty_and_max_rpm_when_the_sweep_finds_the_fan_spinning():
    fan_cfg = {"pwm_chip": "/sys/class/pwm/pwmchip0"}
    result = {"gpio": 16, "active": True, "min_duty_pct": 30, "max_rpm": 4200}
    response, saved, ctl_max_rpm = _run_probe(fan_cfg, result)
    assert response == result
    assert saved["min_duty_pct"] == 30
    assert saved["max_rpm"] == 4200
    assert ctl_max_rpm == 4200, "the live controller's max_rpm must update immediately too"


def test_probe_saves_nothing_when_the_sweep_never_finds_the_fan_spinning():
    """A dead/disconnected sensor's active:False result must not overwrite
    a previously-good calibration with Nones."""
    fan_cfg = {"pwm_chip": "/sys/class/pwm/pwmchip0", "min_duty_pct": 30, "max_rpm": 4200}
    result = {"gpio": 16, "active": False, "min_duty_pct": None, "max_rpm": None}
    response, saved, ctl_max_rpm = _run_probe(fan_cfg, result)
    assert response == result
    assert saved["min_duty_pct"] == 30, "must not clobber the prior calibration"
    assert saved["max_rpm"] == 4200
    assert ctl_max_rpm is None, "the fake controller's max_rpm was never poked"


def test_probe_persists_min_duty_even_when_the_100pct_step_saw_no_pulses():
    """max_rpm can legitimately come back None on an otherwise-active sweep
    (the fan starts spinning below 100% but the 100%-step sample happened
    to miss every pulse) -- min_duty_pct must still be saved on its own."""
    fan_cfg = {"pwm_chip": "/sys/class/pwm/pwmchip0"}
    result = {"gpio": 16, "active": True, "min_duty_pct": 40, "max_rpm": None}
    response, saved, ctl_max_rpm = _run_probe(fan_cfg, result)
    assert response == result
    assert saved["min_duty_pct"] == 40
    assert saved.get("max_rpm") is None
    assert ctl_max_rpm is None


def _main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _main()
