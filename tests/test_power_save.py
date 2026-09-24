"""Power save: slow the board down with no ECU, then power it off.

The ladder is a pure function so it can be driven here without a clock or an
event loop; the two privileged halves (``cpu_apply``, ``shutdown``) are module
attributes and are replaced by recorders, the way ``tests/test_system_time.py``
replaces ``_run``.

Run directly:  python tests/test_power_save.py
"""

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.web import system  # noqa: E402
from app.web.config_mgr import ConfigError, ConfigManager  # noqa: E402
from app.web.state import State  # noqa: E402

DEFAULTS = json.loads((ROOT / "config" / "config.default.json").read_text())
MIN = 60.0


def _cfg(enabled=True, idle=15, off=15):
    return {"system": {"power_save": {"enabled": enabled,
                                      "idle_min": idle, "off_min": off}}}


def test_the_ladder_slows_down_first_and_powers_off_second():
    cfg = _cfg()
    step = system.power_save_step
    assert step(0, cfg, 0, False) is None                  # ECU only just gone
    assert step(14 * MIN, cfg, 0, False) is None           # not yet
    assert step(15 * MIN, cfg, 0, False) == "slow"
    assert step(20 * MIN, cfg, 1, False) is None           # slowed, waiting
    assert step(29 * MIN, cfg, 1, False) is None
    assert step(30 * MIN, cfg, 1, False) == "off"
    assert step(99 * MIN, cfg, 2, False) is None           # already off, no repeat


def test_the_ecu_coming_back_undoes_each_step():
    cfg = _cfg()
    # the idle clock is what resets, so "ECU is back" simply reads as a small idle
    assert system.power_save_step(3, cfg, 1, False) == "restore"
    assert system.power_save_step(3, cfg, 2, False) == "restore"
    assert system.power_save_step(3, cfg, 0, False) is None   # nothing to undo
    # and turning the feature off puts the board back too
    assert system.power_save_step(99 * MIN, _cfg(enabled=False), 1, False) == "restore"
    assert system.power_save_step(99 * MIN, _cfg(enabled=False), 0, False) is None


def test_a_busy_board_refuses_the_poweroff_and_never_the_slowdown():
    """The ignition being off is the same fact in a field and in the garage.

    A browser with the page open, a firmware operation, a scan or an open ride
    log all say a person is standing there. None of them is a reason to keep the
    processor at full speed -- the UI works just as well slowed down.
    """
    cfg = _cfg()
    assert system.power_save_step(15 * MIN, cfg, 0, True) == "slow"
    assert system.power_save_step(30 * MIN, cfg, 1, True) is None
    assert system.power_save_step(30 * MIN, cfg, 1, False) == "off"


def test_nonsense_timers_wind_nothing_down():
    for bad in ({"enabled": True, "idle_min": "soon", "off_min": 15},
                {"enabled": True, "idle_min": None, "off_min": 15}):
        cfg = {"system": {"power_save": bad}}
        assert system.power_save_step(99 * MIN, cfg, 0, False) is None
        assert system.power_save_step(99 * MIN, cfg, 1, False) == "restore"
    # and a config with no power_save block at all (a board from before this)
    assert system.power_save_step(99 * MIN, {}, 0, False) is None


def test_zero_in_either_timer_disables_the_step_it_times():
    """0 is not nonsense any more -- it is how a rider turns off one step
    without a second config key. idle_min=0 means the ladder never starts, so
    "off" (which requires having slowed down first) never fires either.
    off_min=0 means it slows down and simply stays there."""
    never_slows = _cfg(idle=0, off=15)
    assert system.power_save_step(0, never_slows, 0, False) is None
    assert system.power_save_step(999 * MIN, never_slows, 0, False) is None
    assert system.power_save_step(999 * MIN, never_slows, 1, False) == "restore"

    never_offs = _cfg(idle=15, off=0)
    assert system.power_save_step(15 * MIN, never_offs, 0, False) == "slow"
    assert system.power_save_step(999 * MIN, never_offs, 1, False) is None
    assert system.power_save_step(999 * MIN, never_offs, 1, True) is None


def test_the_target_is_what_the_kernel_actually_offers():
    """cpu_apply refuses a governor or a frequency the kernel does not list, so
    asking for a hardcoded "powersave" would silently do nothing on a board that
    has no such governor -- and the frequency ceiling would be lost with it."""
    gov, khz = system.power_save_target(
        {"governors": ["powersave", "ondemand"], "freqs_khz": [408000, 816000, 1296000]})
    assert (gov, khz) == ("powersave", 408000)
    gov, khz = system.power_save_target({"governors": ["schedutil"], "freqs_khz": [600000]})
    assert (gov, khz) == ("", 600000), (gov, khz)
    assert system.power_save_target({}) == ("", 0)        # dev host, no cpufreq


def test_the_idle_clock_counts_answers_and_not_the_status():
    """With no adapter the worker alternates searching -> error every two seconds.
    Anything keyed off a status transition sees an edge each cycle and never
    times out, so the clock counts the last *answer* instead."""
    st = State()
    for _ in range(5):
        st.set_status("searching", "")
        st.set_status("error", "err.no_adapter")
    assert st.ecu_idle_s() > 0, "flapping status must not reset the idle clock"
    st.set_link_seen()
    assert st.ecu_idle_s() < 0.5
    time.sleep(0.05)
    assert st.snapshot()["ecu_idle_s"] >= 0.0


def test_the_timers_are_validated_like_every_other_bound():
    """Nothing else under system. is checked -- system.cpu got away with it
    because cpu_apply refuses what the kernel does not offer. There is no such
    backstop for a timer that powers the board off."""
    cm = ConfigManager.__new__(ConfigManager)
    for minutes in (-1, -0.5, 100, "quarter of an hour"):
        cfg = json.loads(json.dumps(DEFAULTS))
        cfg["system"]["power_save"]["idle_min"] = minutes
        try:
            cm.validate(cfg)
        except ConfigError as exc:
            assert str(exc.args[0]).startswith("cfgerr.power_"), exc.args
        else:
            raise AssertionError(f"{minutes!r} passed validation")
    cm.validate(json.loads(json.dumps(DEFAULTS)))          # the shipped default is fine
    zero = json.loads(json.dumps(DEFAULTS))
    zero["system"]["power_save"]["idle_min"] = 0            # 0 disables a step, not an error
    cm.validate(zero)


def test_the_shipped_default_is_off():
    """It powers the board off. Nobody gets that without asking for it."""
    ps = DEFAULTS["system"]["power_save"]
    assert ps["enabled"] is False
    assert ps["idle_min"] == 15 and ps["off_min"] == 15


def test_the_form_asks_for_what_the_board_will_accept():
    """A field the browser lets through and the board refuses saves to a red
    banner with no way to tell why, so the inputs carry validate()'s own bounds
    and the names the config actually has."""
    html = (ROOT / "app" / "static" / "index.html").read_text()
    for key in ("idle_min", "off_min"):
        field = f'name="system.power_save.{key}"'
        assert field in html, f"{field} is not in the config form"
        line = next(ln for ln in html.splitlines() if field in ln)
        assert 'min="0"' in line and 'max="99"' in line, \
            f"{key} bounds drifted from cfgerr.power_range: {line.strip()}"
    assert 'name="system.power_save.enabled"' in html
    assert set(DEFAULTS["system"]["power_save"]) == {"enabled", "idle_min", "off_min"}


def test_the_countdown_is_repainted_by_the_live_push():
    """Config -> System is a tab the rider sits on while the board winds down;
    a hint painted once on tab entry would freeze there."""
    app_js = (ROOT / "app" / "static" / "app.js").read_text()
    body = app_js[app_js.index("function applySnapshot"):]
    body = body[:body.index("\nfunction ")]
    assert "renderPowerSave(" in body, "applySnapshot no longer repaints the idle clock"


def _main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _main()
