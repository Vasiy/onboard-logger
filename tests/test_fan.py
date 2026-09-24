"""Offline tests for fan control (app/web/fan.py): PWM/tach sysfs adapters,
the temperature -> duty algorithm, and FanController -- the in-process
replacement for the separate nanopi-pwm-fan-addon service. Folded in here
because onboard-logger's Add-ons mechanism never executes code (by design,
see app/web/addons.py's own docstring), so a real hardware-control backend
cannot ship through it; this ships through the ordinary Update mechanism
instead, like gear/diag/power_save.

Run directly:  python tests/test_fan.py
"""

import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.web import fan  # noqa: E402

DEFAULT_CFG = {
    "fan_type": 2,
    "pwm_channel": 0,
    "period_ns": 8000000,
    "polarity": "normal",
    "target_temps_c": [65000, 60000, 55000, 50000],
    "duty_cycles_ns": [8000000, 6000000, 4000000, 2000000],
    "default_duty_ns": 0,
    "tach_gpio": None,
    "tach_pulses_per_rev": 2,
}


# -- duty selection -----------------------------------------------------

def test_hottest_temp_picks_the_first_step():
    duty = fan.select_duty_ns(70000, DEFAULT_CFG["target_temps_c"], DEFAULT_CFG["duty_cycles_ns"], 0)
    assert duty == 8000000, duty


def test_temp_exactly_on_a_threshold_does_not_count_as_exceeding_it():
    duty = fan.select_duty_ns(60000, DEFAULT_CFG["target_temps_c"], DEFAULT_CFG["duty_cycles_ns"], 0)
    assert duty == 4000000, duty


def test_temp_below_every_threshold_is_the_default():
    duty = fan.select_duty_ns(40000, DEFAULT_CFG["target_temps_c"], DEFAULT_CFG["duty_cycles_ns"], 0)
    assert duty == 0, duty


def test_full_speed_duty_respects_polarity():
    assert fan.full_speed_duty_ns({"period_ns": 8000000, "polarity": "normal"}) == 8000000
    assert fan.full_speed_duty_ns({"period_ns": 8000000, "polarity": "inversed"}) == 0


def test_duty_ns_for_speed_pct_respects_polarity():
    """full_speed_duty_ns is this at pct=100 -- the calibration sweep (see
    FanController.probe) needs every intermediate percentage too, and the
    same inversion rule the single-point version already got right."""
    assert fan.duty_ns_for_speed_pct(8000000, "normal", 100) == 8000000
    assert fan.duty_ns_for_speed_pct(8000000, "normal", 0) == 0
    assert fan.duty_ns_for_speed_pct(8000000, "normal", 50) == 4000000
    assert fan.duty_ns_for_speed_pct(8000000, "inversed", 100) == 0
    assert fan.duty_ns_for_speed_pct(8000000, "inversed", 0) == 8000000
    assert fan.duty_ns_for_speed_pct(8000000, "inversed", 50) == 4000000


# -- PWM sysfs ------------------------------------------------------------

def _chip(tmp: Path) -> Path:
    chip = tmp / "pwmchip0"
    chip.mkdir(parents=True)
    (chip / "export").write_text("")
    return chip


def _pwm_channel(chip: Path, *, enable="0", duty_cycle="0", period="0", polarity="normal") -> Path:
    pwm0 = chip / "pwm0"
    pwm0.mkdir()
    (pwm0 / "enable").write_text(enable)
    (pwm0 / "duty_cycle").write_text(duty_cycle)
    (pwm0 / "period").write_text(period)
    (pwm0 / "polarity").write_text(polarity)
    return pwm0


def test_pwm_is_exported_false_then_true():
    with tempfile.TemporaryDirectory() as d:
        chip = _chip(Path(d))
        assert fan.pwm_is_exported(chip, 0) is False
        _pwm_channel(chip)
        assert fan.pwm_is_exported(chip, 0) is True


def test_pwm_configure_disables_zeroes_duty_and_sets_period_and_polarity():
    with tempfile.TemporaryDirectory() as d:
        chip = _chip(Path(d))
        pwm0 = _pwm_channel(chip, enable="1", duty_cycle="500", period="1000", polarity="normal")
        fan.pwm_configure(pwm0, period_ns=8000000, polarity="inversed")
        assert (pwm0 / "enable").read_text() == "0"
        assert (pwm0 / "duty_cycle").read_text() == "0"
        assert (pwm0 / "period").read_text() == "8000000"
        assert (pwm0 / "polarity").read_text() == "inversed"


def test_pwm_set_duty_and_read_duty_round_trip():
    with tempfile.TemporaryDirectory() as d:
        chip = _chip(Path(d))
        pwm0 = _pwm_channel(chip)
        fan.pwm_set_duty_ns(pwm0, 4000000)
        assert fan.pwm_read_duty_ns(pwm0) == 4000000



# -- tach GPIO sysfs + math -------------------------------------------------

def _gpio_root(tmp: Path) -> Path:
    root = tmp / "gpio"
    root.mkdir(parents=True)
    (root / "export").write_text("")
    return root


def _gpio_line(root: Path, line: int, *, direction="in", edge="none", value="0") -> Path:
    d = root / f"gpio{line}"
    d.mkdir()
    (d / "direction").write_text(direction)
    (d / "edge").write_text(edge)
    (d / "value").write_text(value)
    return d


def test_gpio_configure_input_sets_direction_and_edge():
    with tempfile.TemporaryDirectory() as d:
        root = _gpio_root(Path(d))
        gpio_dir = _gpio_line(root, 16)
        fan.gpio_configure_input(gpio_dir, edge="both")
        assert (gpio_dir / "direction").read_text() == "in"
        assert (gpio_dir / "edge").read_text() == "both"


def test_gpio_ensure_exported_waits_for_kernel():
    with tempfile.TemporaryDirectory() as d:
        root = _gpio_root(Path(d))

        def _kernel_creates_it_soon():
            time.sleep(0.05)
            _gpio_line(root, 16)

        threading.Thread(target=_kernel_creates_it_soon).start()
        gpio_dir = fan.gpio_ensure_exported(root, 16, timeout_s=2.0, poll_interval_s=0.01)
        assert gpio_dir == root / "gpio16"


def test_rpm_from_edges():
    assert fan.rpm_from_edges(edge_count=20, window_s=1.0, pulses_per_rev=2) == 600.0


def test_is_rpm_valid_only_at_full_duty_for_3_pin():
    assert fan.is_rpm_valid(duty_ns=8000000, period_ns=8000000, fan_type=3) is True
    assert fan.is_rpm_valid(duty_ns=4000000, period_ns=8000000, fan_type=3) is False


def test_is_rpm_valid_none_for_2_pin():
    assert fan.is_rpm_valid(duty_ns=8000000, period_ns=8000000, fan_type=2) is None


def test_edge_window_counter_drops_edges_older_than_the_window():
    c = fan.EdgeWindowCounter(window_s=1.0)
    c.add_edge(0.0)
    c.add_edge(1.6)
    assert c.count_at(1.6) == 1


# -- pinmux availability (the physical-pin picker) --------------------------
# The board's own pinmux-pins debugfs table is what showed pin 16/44 were
# internally wired to Ethernet (ff540000.ethernet, function gmac-1) -- these
# tests exercise the same parsing against a fake tree instead of the real
# board, and match the exact line format captured there:
#   pin 16 (gpio0-16): ff540000.ethernet (GPIO UNCLAIMED) function gmac-1 group rgmiim1-pins
#   pin 101 (gpio3-5): (MUX UNCLAIMED) (GPIO UNCLAIMED)

def _pinctrl_root(tmp: Path, lines: list[str]) -> Path:
    root = tmp / "pinctrl"
    d = root / "pinctrl-rockchip-pinctrl"
    d.mkdir(parents=True, exist_ok=True)  # exist_ok: a test may build more than one tree under the same tmp
    (d / "pinmux-pins").write_text("Pinmux settings per pin\n" + "\n".join(lines) + "\n")
    return root


def _free_pinctrl_root(tmp: Path, *gpio_numbers: int) -> Path:
    """A fake pinctrl tree marking each given gpio number MUX UNCLAIMED --
    for tests that need apply_config()/probe()'s is_gpio_available() gate to
    pass on a fake gpio number (real ones like 16/44 are genuinely claimed by
    Ethernet on the actual board, see TACH_HEADER_PINS's comment)."""
    lines = [f"pin {g} (gpio{g // 32}-{g % 32}): (MUX UNCLAIMED) (GPIO UNCLAIMED)"
             for g in gpio_numbers]
    return _pinctrl_root(tmp, lines)


def test_pinmux_claims_parses_claimed_and_unclaimed_lines():
    with tempfile.TemporaryDirectory() as d:
        root = _pinctrl_root(Path(d), [
            "pin 16 (gpio0-16): ff540000.ethernet (GPIO UNCLAIMED) function gmac-1 group rgmiim1-pins",
            "pin 101 (gpio3-5): (MUX UNCLAIMED) (GPIO UNCLAIMED)",
        ])
        claims = fan._pinmux_claims(root)
        assert claims[16] is False   # gpio0-16 = 0*32+16 = 16, claimed by ethernet
        assert claims[101] is True   # gpio3-5 = 3*32+5 = 101, free


def test_pinmux_claims_empty_when_debugfs_missing():
    assert fan._pinmux_claims("/no/such/pinctrl") == {}


def test_is_gpio_available():
    with tempfile.TemporaryDirectory() as d:
        root = _pinctrl_root(Path(d), [
            "pin 16 (gpio0-16): ff540000.ethernet (GPIO UNCLAIMED) function gmac-1 group rgmiim1-pins",
            "pin 101 (gpio3-5): (MUX UNCLAIMED) (GPIO UNCLAIMED)",
        ])
        assert fan.is_gpio_available(101, root) is True
        assert fan.is_gpio_available(16, root) is False


def test_is_gpio_available_unknown_pin_is_unavailable():
    """Unknown means unavailable, never the reverse -- a pin the parser never
    saw must not be treated as safe to touch."""
    with tempfile.TemporaryDirectory() as d:
        root = _pinctrl_root(Path(d), [
            "pin 101 (gpio3-5): (MUX UNCLAIMED) (GPIO UNCLAIMED)",
        ])
        assert fan.is_gpio_available(999, root) is False


def test_list_available_tach_pins_only_lists_free_header_pins():
    with tempfile.TemporaryDirectory() as d:
        # gpio 66 = pin 7 (free), gpio 100 = pin 8 (claimed by K-Line's UART1)
        root = _pinctrl_root(Path(d), [
            "pin 66 (gpio2-2): (MUX UNCLAIMED) (GPIO UNCLAIMED)",
            "pin 100 (gpio3-4): ff120000.serial (GPIO UNCLAIMED) function uart1 group uart1-xfer",
            "pin 101 (gpio3-5): (MUX UNCLAIMED) (GPIO UNCLAIMED)",
        ])
        pins = fan.list_available_tach_pins(root)
        assert {"pin": 7, "gpio": 66} in pins
        assert {"pin": 16, "gpio": 101} in pins
        assert not any(p["pin"] == 8 for p in pins), "pin 8/gpio100 is claimed by K-Line's UART1"


def test_list_available_tach_pins_empty_when_debugfs_missing():
    assert fan.list_available_tach_pins("/no/such/pinctrl") == []


# -- FanController.tick() --------------------------------------------------

def _board(tmp: Path):
    chip = _chip(tmp)
    pwm0 = _pwm_channel(chip, enable="1", duty_cycle="0", period="8000000", polarity="normal")
    temp_path = tmp / "temp"
    temp_path.write_text("70000")
    return chip, pwm0, temp_path


def test_apply_config_spins_full_speed_briefly_before_settling():
    """Ported from ckwun's original script: run full speed for a few seconds
    whenever the channel is (re)configured, proving the fan actually spins
    before the thermal curve takes over -- not just once at process start,
    here, but on every reconfiguration."""
    with tempfile.TemporaryDirectory() as d:
        chip, pwm0, temp_path = _board(Path(d))
        cfg = dict(DEFAULT_CFG, fan_type=2, pwm_chip=str(chip), temp_path=str(temp_path))
        slept = []
        ctl = fan.FanController(sleep=slept.append)
        ctl.apply_config(cfg)
        assert slept == [5.0]
        assert fan.pwm_read_duty_ns(pwm0) == 8000000
        assert fan.pwm_read_enabled(pwm0) is True


def test_tach_disabled_kill_switch_skips_gpio_regardless_of_config():
    """The manual override for whoever wires a pin that turns out claimed the
    same way pin 16/44 were (see TACH_DISABLED_DEFAULT's comment): with
    tach_disabled=True, FanController must not call any gpio_* function at
    all, regardless of what fan_type/tach_gpio config says. tach_gpio_root
    points at a path that doesn't exist: if the switch were not honoured,
    gpio_ensure_exported would raise TimeoutError trying to reach it."""
    with tempfile.TemporaryDirectory() as d:
        chip, pwm0, temp_path = _board(Path(d))
        cfg = dict(DEFAULT_CFG, fan_type=3, pwm_chip=str(chip), temp_path=str(temp_path),
                   tach_gpio=16, tach_gpio_root="/no/such/gpio/root")
        ctl = fan.FanController(sleep=lambda s: None, tach_disabled=True)
        status = ctl.tick(cfg, now_monotonic=100.0, now_wall=1_700_000_000.0)
        assert ctl._edge_counter is None
        assert ctl._reader is None
        assert status["rpm"] is None


def test_tach_disabled_kill_switch_makes_probe_refuse_without_touching_gpio():
    with tempfile.TemporaryDirectory() as d:
        chip, pwm0, temp_path = _board(Path(d))
        cfg = dict(DEFAULT_CFG, fan_type=3, pwm_chip=str(chip), temp_path=str(temp_path),
                   tach_gpio_root="/no/such/gpio/root")
        ctl = fan.FanController(sleep=lambda s: None, tach_disabled=True)
        try:
            ctl.probe(cfg, 16)
            assert False, "expected TachUnavailable"
        except fan.TachUnavailable:
            pass


def test_apply_config_retries_after_a_failed_attempt_with_the_same_config():
    """Found live: apply_config used to record _cfg_key *before* doing the
    PWM/GPIO work it guards. A transient failure partway through (real
    example: tick()'s own duty write raising because a stale
    duty_cycles_ns tier exceeded period_ns -- see clamp_duty_ns) then left
    the channel half-configured, and the next call with the identical
    config short-circuited on "nothing changed" and never retried --
    stalling fan control forever with no log line, since the caller
    swallows the exception by design (app/main.py's _fan_control). A
    failed attempt must not be recorded as applied."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        chip = tmp / "pwmchip0"  # does not exist yet -- export write fails
        cfg = dict(DEFAULT_CFG, pwm_chip=str(chip), temp_path=str(tmp / "temp"))
        ctl = fan.FanController(sleep=lambda s: None)
        try:
            ctl.apply_config(cfg)
            assert False, "expected a failure writing export to a chip dir that does not exist"
        except OSError:
            pass
        assert ctl._cfg_key is None, "a failed attempt must not be recorded as applied"

        # the chip "appears" -- e.g. a transient startup race healing
        _pwm_channel(_chip(tmp))
        ctl.apply_config(cfg)  # same cfg -- must retry, not no-op
        assert ctl._pwm_dir is not None


def test_apply_config_skips_gpio_when_configured_pin_is_claimed():
    """Config-drift guard: a tach_gpio saved earlier while free (e.g. K-Line
    hardware flow control was off) must not still be trusted once the board's
    own pinmux reports it claimed -- apply_config must not export it, same
    as the tach_disabled path, just gated per-pin instead of globally."""
    with tempfile.TemporaryDirectory() as d:
        chip, pwm0, temp_path = _board(Path(d))
        # no gpio100 line pre-created: if the guard is missing, exporting it
        # would hang waiting for a kernel that never creates the directory
        gpio_root = _gpio_root(Path(d))
        claimed_root = _pinctrl_root(Path(d), [
            "pin 100 (gpio3-4): ff120000.serial (GPIO UNCLAIMED) function uart1 group uart1-xfer",
        ])
        cfg = dict(DEFAULT_CFG, fan_type=3, pwm_chip=str(chip), temp_path=str(temp_path),
                   tach_gpio=100, tach_gpio_root=str(gpio_root), pinctrl_root=str(claimed_root))
        ctl = fan.FanController(start_reader=lambda gpio_dir, counter: None, sleep=lambda s: None)
        ctl.apply_config(cfg)
        assert ctl._edge_counter is None
        assert ctl._reader is None
        assert not fan.gpio_is_exported(gpio_root, 100), "a claimed pin must never be exported"


def test_probe_refuses_a_claimed_pin_without_touching_gpio():
    with tempfile.TemporaryDirectory() as d:
        chip, pwm0, temp_path = _board(Path(d))
        gpio_root = _gpio_root(Path(d))
        claimed_root = _pinctrl_root(Path(d), [
            "pin 100 (gpio3-4): ff120000.serial (GPIO UNCLAIMED) function uart1 group uart1-xfer",
        ])
        cfg = dict(DEFAULT_CFG, fan_type=3, pwm_chip=str(chip), temp_path=str(temp_path),
                   tach_gpio_root=str(gpio_root), pinctrl_root=str(claimed_root))
        ctl = fan.FanController(sleep=lambda s: None)
        try:
            ctl.probe(cfg, 100)
            assert False, "expected TachUnavailable"
        except fan.TachUnavailable as exc:
            assert "100" in str(exc)
        assert not fan.gpio_is_exported(gpio_root, 100), "a claimed pin must never be exported"


def test_tach_enabled_by_default_now_root_cause_is_known():
    """Root cause resolved 2026-09-20: pin 16/44 were internally wired to
    Ethernet, not a kernel bug -- so a fresh FanController must default to
    touching GPIO normally (tach_disabled=False) again."""
    assert fan.TACH_DISABLED_DEFAULT is False
    with tempfile.TemporaryDirectory() as d:
        chip, pwm0, temp_path = _board(Path(d))
        gpio_root = _gpio_root(Path(d))
        _gpio_line(gpio_root, 101)
        cfg = dict(DEFAULT_CFG, fan_type=3, pwm_chip=str(chip), temp_path=str(temp_path),
                   tach_gpio=101, tach_gpio_root=str(gpio_root),
                   pinctrl_root=str(_free_pinctrl_root(Path(d), 101)))
        ctl = fan.FanController(start_reader=lambda gpio_dir, counter: None, sleep=lambda s: None)
        ctl.apply_config(cfg)
        assert ctl._edge_counter is not None


def test_controller_tick_2_pin_has_no_rpm():
    with tempfile.TemporaryDirectory() as d:
        chip, pwm0, temp_path = _board(Path(d))
        cfg = dict(DEFAULT_CFG, fan_type=2, pwm_chip=str(chip), temp_path=str(temp_path))
        ctl = fan.FanController(sleep=lambda s: None)
        status = ctl.tick(cfg, now_monotonic=100.0, now_wall=1_700_000_000.0)
        assert status["duty_ns"] == 8000000, status
        assert status["rpm"] is None and status["rpm_valid"] is None
        assert status["ts"] == 1_700_000_000.0


def test_controller_tick_3_pin_reports_rpm_and_validity():
    """Regression: the edge window must be sampled on the same (monotonic)
    clock the reader thread would use, never the wall clock passed for ts --
    mixing them silently zeroes every reading (caught in the sibling
    nanopi-pwm-fan-addon project's service.py the same way)."""
    with tempfile.TemporaryDirectory() as d:
        chip, pwm0, temp_path = _board(Path(d))
        gpio_root = _gpio_root(Path(d))
        _gpio_line(gpio_root, 16)
        cfg = dict(DEFAULT_CFG, fan_type=3, pwm_chip=str(chip), temp_path=str(temp_path),
                   tach_gpio=16, tach_gpio_root=str(gpio_root),
                   pinctrl_root=str(_free_pinctrl_root(Path(d), 16)))
        ctl = fan.FanController(start_reader=lambda gpio_dir, counter: None, sleep=lambda s: None, tach_disabled=False)
        ctl.apply_config(cfg)
        for t in (99.05, 99.25, 99.45, 99.65, 99.85):
            ctl._edge_counter.add_edge(t)
        status = ctl.tick(cfg, now_monotonic=100.0, now_wall=1_700_000_000.0)
        assert status["duty_ns"] == status["period_ns"] == 8000000
        assert status["rpm"] == 150.0, status
        assert status["rpm_valid"] is True


def test_controller_tick_3_pin_flags_invalid_below_full_duty():
    with tempfile.TemporaryDirectory() as d:
        chip, pwm0, temp_path = _board(Path(d))
        temp_path.write_text("40000")
        gpio_root = _gpio_root(Path(d))
        _gpio_line(gpio_root, 16)
        cfg = dict(DEFAULT_CFG, fan_type=3, pwm_chip=str(chip), temp_path=str(temp_path),
                   tach_gpio=16, tach_gpio_root=str(gpio_root),
                   pinctrl_root=str(_free_pinctrl_root(Path(d), 16)))
        ctl = fan.FanController(start_reader=lambda gpio_dir, counter: None, sleep=lambda s: None, tach_disabled=False)
        status = ctl.tick(cfg, now_monotonic=100.0, now_wall=1_700_000_000.0)
        assert status["duty_ns"] == 0
        assert status["rpm_valid"] is False


def test_apply_config_learns_max_rpm_from_the_startup_spin():
    """The bug this guards: on first boot self._reader is None going into
    apply_config, so if the tach reader were readied *after* the full-speed
    spin (as it originally was), nothing would be listening during the one
    window the spin exists to prove -- max_rpm would then stay None forever
    on a board that never independently gets hot enough to hit 100% duty.
    The fake sleep() stands in for the real reader thread accumulating edges
    over the (real) 5s window."""
    with tempfile.TemporaryDirectory() as d:
        chip, pwm0, temp_path = _board(Path(d))
        gpio_root = _gpio_root(Path(d))
        _gpio_line(gpio_root, 16)
        cfg = dict(DEFAULT_CFG, fan_type=4, pwm_chip=str(chip), temp_path=str(temp_path),
                   tach_gpio=16, tach_gpio_root=str(gpio_root),
                   pinctrl_root=str(_free_pinctrl_root(Path(d), 16)))

        captured = {}

        def fake_start_reader(gpio_dir, counter):
            captured["counter"] = counter
            return None

        def fake_sleep(seconds):
            for t in (99.05, 99.25, 99.45, 99.65, 99.85):  # -> 150 rpm at 2 ppr/1s
                captured["counter"].add_edge(t)

        ctl = fan.FanController(start_reader=fake_start_reader, sleep=fake_sleep,
                                 monotonic=lambda: 100.0, tach_disabled=False)
        ctl.apply_config(cfg)
        assert ctl._max_rpm == 150.0


def test_tick_reports_a_confident_zero_below_the_known_min_duty():
    """min_duty_pct (from Probe's calibration sweep, see FanController.probe)
    means the fan is known not to spin below it -- a raw tach reading down
    there is a chop/coast artifact, not real rpm, so tick() overrides it to a
    *confident* 0 (rpm_valid True, not the "unreliable" flag 3-pin's own
    100%-only rule uses) rather than passing the artifact through."""
    with tempfile.TemporaryDirectory() as d:
        chip, pwm0, temp_path = _board(Path(d))
        temp_path.write_text("62000")  # -> duty_cycles_ns[1] = 6000000 = 75% of period
        gpio_root = _gpio_root(Path(d))
        _gpio_line(gpio_root, 16)
        cfg = dict(DEFAULT_CFG, fan_type=4, pwm_chip=str(chip), temp_path=str(temp_path),
                   tach_gpio=16, tach_gpio_root=str(gpio_root), min_duty_pct=80,
                   pinctrl_root=str(_free_pinctrl_root(Path(d), 16)))
        ctl = fan.FanController(start_reader=lambda gpio_dir, counter: None, sleep=lambda s: None, tach_disabled=False)
        ctl.apply_config(cfg)
        for t in (99.05, 99.25, 99.45, 99.65, 99.85):  # would read as 150 rpm if not overridden
            ctl._edge_counter.add_edge(t)
        status = ctl.tick(cfg, now_monotonic=100.0, now_wall=1_700_000_000.0)
        assert status["duty_pct"] == 75.0
        assert status["rpm"] == 0.0
        assert status["rpm_valid"] is True


def test_tick_ignores_a_leftover_min_duty_pct_for_a_2_pin_fan():
    """min_duty_pct is measured by Probe's tach sweep on a 3/4-pin fan --
    switching Wiring back to 2-pin (no tach line at all) must not leave the
    override active: a 2-pin fan has no rpm to report, confident or not, and
    ui_fan.js pins that fan_type 2 must never claim one."""
    with tempfile.TemporaryDirectory() as d:
        chip, pwm0, temp_path = _board(Path(d))
        temp_path.write_text("62000")  # -> 75% duty, same as the fan_type 4 case above
        cfg = dict(DEFAULT_CFG, fan_type=2, pwm_chip=str(chip), temp_path=str(temp_path),
                   min_duty_pct=80)
        ctl = fan.FanController(start_reader=lambda gpio_dir, counter: None, sleep=lambda s: None, tach_disabled=False)
        status = ctl.tick(cfg, now_monotonic=100.0, now_wall=1_700_000_000.0)
        assert status["duty_pct"] == 75.0
        assert status["rpm"] is None
        assert status["rpm_valid"] is None


def test_tick_trusts_a_real_reading_at_or_above_the_known_min_duty():
    with tempfile.TemporaryDirectory() as d:
        chip, pwm0, temp_path = _board(Path(d))  # temp=70000 -> hottest step -> 100% duty
        gpio_root = _gpio_root(Path(d))
        _gpio_line(gpio_root, 16)
        cfg = dict(DEFAULT_CFG, fan_type=4, pwm_chip=str(chip), temp_path=str(temp_path),
                   tach_gpio=16, tach_gpio_root=str(gpio_root), min_duty_pct=80,
                   pinctrl_root=str(_free_pinctrl_root(Path(d), 16)))
        ctl = fan.FanController(start_reader=lambda gpio_dir, counter: None, sleep=lambda s: None, tach_disabled=False)
        ctl.apply_config(cfg)
        for t in (99.05, 99.25, 99.45, 99.65, 99.85):
            ctl._edge_counter.add_edge(t)
        status = ctl.tick(cfg, now_monotonic=100.0, now_wall=1_700_000_000.0)
        assert status["duty_pct"] == 100.0
        assert status["rpm"] == 150.0


def test_tick_clamps_a_stale_duty_tier_to_the_current_period():
    """Regression, found live: the PWM frequency slider changes period_ns
    but does not rescale duty_cycles_ns (a config.json-only field with no
    UI control) -- after lowering the frequency, every configured tier can
    end up above the new, smaller period. A real board's kernel rejects a
    duty_cycle sysfs write above period outright (EINVAL), and
    _fan_control's own bare `except Exception: pass` (app/main.py) then
    silently stalls fan control forever with no log line -- exactly what a
    freshly-deployed board was found doing. tick() must clamp instead of
    handing the write a value the kernel will refuse."""
    with tempfile.TemporaryDirectory() as d:
        chip, pwm0, temp_path = _board(Path(d))
        temp_path.write_text("70000")  # -> duty_cycles_ns[0] = 8,000,000
        cfg = dict(DEFAULT_CFG, pwm_chip=str(chip), temp_path=str(temp_path),
                   period_ns=3322259)  # smaller than every configured tier
        ctl = fan.FanController(sleep=lambda s: None)
        status = ctl.tick(cfg, now_monotonic=1.0, now_wall=1.0)
        assert status["duty_ns"] == 3322259, status
        assert status["duty_pct"] == 100.0
        assert fan.pwm_read_duty_ns(pwm0) == 3322259


def test_controller_probe_restores_a_clamped_duty_when_the_configured_tier_exceeds_period():
    with tempfile.TemporaryDirectory() as d:
        chip, pwm0, temp_path = _board(Path(d))
        temp_path.write_text("70000")
        gpio_root = _gpio_root(Path(d))
        _gpio_line(gpio_root, 16)
        cfg = dict(DEFAULT_CFG, fan_type=4, pwm_chip=str(chip), temp_path=str(temp_path),
                   period_ns=3322259, tach_gpio_root=str(gpio_root),
                   pinctrl_root=str(_free_pinctrl_root(Path(d), 16)))
        ctl = fan.FanController(start_reader=lambda gpio_dir, counter: None, sleep=lambda s: None, tach_disabled=False)
        ctl.probe(cfg, 16, sample_s=0.01, count_edges=lambda g, d: 0)
        assert fan.pwm_read_duty_ns(pwm0) == 3322259


def test_tick_learns_max_rpm_only_at_full_duty():
    """'Max rpm' is defined as whatever the sensor reports the one time duty
    is genuinely 100% -- not any other reading, even a valid one (fan_type 4
    is tach-valid at every duty, so a partial-duty reading must not be
    mistaken for the maximum)."""
    with tempfile.TemporaryDirectory() as d:
        chip, pwm0, temp_path = _board(Path(d))  # temp=70000 -> hottest step -> 100% duty
        gpio_root = _gpio_root(Path(d))
        _gpio_line(gpio_root, 16)
        cfg = dict(DEFAULT_CFG, fan_type=4, pwm_chip=str(chip), temp_path=str(temp_path),
                   tach_gpio=16, tach_gpio_root=str(gpio_root),
                   pinctrl_root=str(_free_pinctrl_root(Path(d), 16)))
        ctl = fan.FanController(start_reader=lambda gpio_dir, counter: None, sleep=lambda s: None, tach_disabled=False)
        ctl.apply_config(cfg)
        for t in (99.05, 99.25, 99.45, 99.65, 99.85):  # 5 edges/1s/2ppr -> 150 rpm
            ctl._edge_counter.add_edge(t)
        status = ctl.tick(cfg, now_monotonic=100.0, now_wall=1_700_000_000.0)
        assert status["duty_ns"] == status["period_ns"]
        assert status["max_rpm"] == 150.0, status

        # now a partial-duty tick: fan_type 4 keeps reporting a (still valid)
        # rpm, but it must not overwrite the learned maximum
        temp_path.write_text("40000")  # -> default (0) duty
        ctl._edge_counter.add_edge(100.5)
        status = ctl.tick(cfg, now_monotonic=101.0, now_wall=1_700_000_001.0)
        assert status["duty_ns"] == 0
        assert status["rpm_valid"] is True   # fan_type 4: valid at any duty
        assert status["max_rpm"] == 150.0, "a partial-duty reading must not replace the max"


def test_apply_config_seeds_max_rpm_from_config():
    """A board that has never independently hit 100% duty this boot (fan_type
    2, say -- most boards, most of the time) would otherwise never learn a
    max_rpm live. Probe's deliberate calibration (persisted to fan.max_rpm)
    seeds the live value on reconfigure; tick()'s own observation still wins
    if it later sees something different (see test_tick_learns_max_rpm...)."""
    with tempfile.TemporaryDirectory() as d:
        chip, pwm0, temp_path = _board(Path(d))
        cfg = dict(DEFAULT_CFG, fan_type=2, pwm_chip=str(chip), temp_path=str(temp_path),
                   max_rpm=1180)
        ctl = fan.FanController(sleep=lambda s: None)
        ctl.apply_config(cfg)
        assert ctl._max_rpm == 1180


def test_apply_config_resets_max_rpm_on_reconfigure():
    """A different pin/fan_type may be a different fan entirely -- a stale
    maximum from the old setup must not linger and skew the new one's tiers."""
    with tempfile.TemporaryDirectory() as d:
        chip, pwm0, temp_path = _board(Path(d))
        gpio_root = _gpio_root(Path(d))
        _gpio_line(gpio_root, 16)
        cfg = dict(DEFAULT_CFG, fan_type=4, pwm_chip=str(chip), temp_path=str(temp_path),
                   tach_gpio=16, tach_gpio_root=str(gpio_root),
                   pinctrl_root=str(_free_pinctrl_root(Path(d), 16)))
        ctl = fan.FanController(start_reader=lambda gpio_dir, counter: None, sleep=lambda s: None, tach_disabled=False)
        ctl.apply_config(cfg)
        for t in (99.05, 99.25, 99.45, 99.65, 99.85):
            ctl._edge_counter.add_edge(t)
        status = ctl.tick(cfg, now_monotonic=100.0, now_wall=1_700_000_000.0)
        assert status["max_rpm"] == 150.0

        # apply_config() alone, not tick(): a tick would immediately re-learn
        # from the freshly-reset (empty) counter, which is a separate, real
        # behaviour this assertion is not about -- this checks the reset itself
        cfg2 = dict(cfg, polarity="inversed")  # a _CFG_KEYS field -> forces reconfigure
        ctl.apply_config(cfg2)
        assert ctl._max_rpm is None


def test_controller_reconfigures_when_pwm_chip_changes():
    """Config edits (e.g. a different pwm_chip) must take effect on the next
    tick without a process restart -- the same live-apply every other
    feature flag in this app gets."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        chip_a, _, temp_path = _board(tmp / "a")
        chip_b = _chip(tmp / "b")
        _pwm_channel(chip_b, period="8000000")
        cfg = dict(DEFAULT_CFG, fan_type=2, pwm_chip=str(chip_a), temp_path=str(temp_path))
        ctl = fan.FanController(sleep=lambda s: None)
        ctl.tick(cfg, now_monotonic=1.0, now_wall=1.0)
        cfg2 = dict(cfg, pwm_chip=str(chip_b))
        ctl.tick(cfg2, now_monotonic=2.0, now_wall=2.0)
        assert (chip_b / "pwm0" / "period").read_text() == "8000000"


def test_probe_and_tick_do_not_interleave():
    """Regression: probe() sweeps duty across CALIBRATION_STEPS_PCT while
    holding the controller lock for the whole sweep, and tick() writes duty
    every second -- without one lock covering both, a tick landing mid-sweep
    would interleave its own duty write with the sweep's, corrupting the
    calibration, and the sweep's restore-on-exit could then clobber that
    tick's own fresh value."""
    with tempfile.TemporaryDirectory() as d:
        chip, pwm0, temp_path = _board(Path(d))
        gpio_root = _gpio_root(Path(d))
        _gpio_line(gpio_root, 16)
        cfg = dict(DEFAULT_CFG, fan_type=3, pwm_chip=str(chip), temp_path=str(temp_path),
                   tach_gpio=16, tach_gpio_root=str(gpio_root),
                   pinctrl_root=str(_free_pinctrl_root(Path(d), 16)))
        ctl = fan.FanController(start_reader=lambda gpio_dir, counter: None, sleep=lambda s: None, tach_disabled=False)
        ctl.apply_config(cfg)

        calls = []
        probe_started = threading.Event()
        release_probe = threading.Event()

        def slow_count_edges(gpio_dir, sample_s):
            calls.append(fan.pwm_read_duty_ns(pwm0))
            if len(calls) == 1:
                probe_started.set()
                release_probe.wait(timeout=2.0)
            return 4

        probe_thread = threading.Thread(
            target=lambda: ctl.probe(cfg, 16, sample_s=0.01, count_edges=slow_count_edges))
        probe_thread.start()
        assert probe_started.wait(timeout=2.0), "probe did not start"

        # a tick that would normally drop the duty -- must block on the lock
        temp_path.write_text("40000")
        tick_done = threading.Event()

        def run_tick():
            ctl.tick(cfg, now_monotonic=1.0, now_wall=1.0)
            tick_done.set()

        tick_thread = threading.Thread(target=run_tick)
        tick_thread.start()
        time.sleep(0.2)
        assert not tick_done.is_set(), "tick must be blocked while the probe holds the lock"

        release_probe.set()
        probe_thread.join(timeout=2.0)
        tick_thread.join(timeout=2.0)
        assert tick_done.is_set()

        assert len(calls) == len(fan.CALIBRATION_STEPS_PCT), \
            "the whole sweep must run under one lock acquisition, not one per step"
        assert fan.pwm_read_duty_ns(pwm0) == 0, \
            "the tick that ran after the probe released the lock must win, not be undone by the probe's restore"


# -- probe_gpio -------------------------------------------------------------

def test_probe_gpio_drives_full_speed_then_restores_prior_duty():
    with tempfile.TemporaryDirectory() as d:
        chip, pwm0, temp_path = _board(Path(d))
        fan.pwm_set_duty_ns(pwm0, 2000000)
        gpio_root = _gpio_root(Path(d))
        gpio_dir = _gpio_line(gpio_root, 16)
        cfg = dict(DEFAULT_CFG, pwm_chip=str(chip))
        seen_duty_during_probe = {}

        def fake_count_edges(gdir, duration_s):
            seen_duty_during_probe["duty"] = fan.pwm_read_duty_ns(pwm0)
            return 7

        result = fan.probe_gpio(pwm0, cfg, gpio_dir, duration_s=2.0, count_edges=fake_count_edges)
        assert seen_duty_during_probe["duty"] == 8000000, "must be driven to full speed for the sample"
        assert fan.pwm_read_duty_ns(pwm0) == 2000000, "prior duty restored after the probe"
        assert result == {"gpio": 16, "pulses": 7, "active": True, "duration_s": 2.0}


def test_probe_gpio_zero_pulses_is_not_active():
    with tempfile.TemporaryDirectory() as d:
        chip, pwm0, temp_path = _board(Path(d))
        gpio_root = _gpio_root(Path(d))
        gpio_dir = _gpio_line(gpio_root, 5)
        cfg = dict(DEFAULT_CFG, pwm_chip=str(chip))
        result = fan.probe_gpio(pwm0, cfg, gpio_dir, duration_s=2.0, count_edges=lambda g, d: 0)
        assert result["active"] is False and result["pulses"] == 0


def test_controller_probe_ensures_pwm_exported_and_gpio_configured_first():
    """The probe button can be pressed before fan control has ever ticked
    (e.g. right after picking fan_type in a fresh install) -- the PWM channel
    and the candidate GPIO must both be readied on demand, not assumed
    already set up, before the calibration sweep starts."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        chip = _chip(tmp)
        _pwm_channel(chip, period="8000000")
        temp_path = tmp / "temp"
        temp_path.write_text("40000")
        gpio_root = _gpio_root(tmp)
        _gpio_line(gpio_root, 16, edge="none")   # already exported by the kernel, not yet configured
        cfg = dict(DEFAULT_CFG, fan_type=3, pwm_chip=str(chip), temp_path=str(temp_path),
                   tach_gpio_root=str(gpio_root),
                   pinctrl_root=str(_free_pinctrl_root(tmp, 16)))
        ctl = fan.FanController(start_reader=lambda gpio_dir, counter: None, sleep=lambda s: None, tach_disabled=False)
        result = ctl.probe(cfg, 16, sample_s=0.01, count_edges=lambda g, d: 3)
        assert result == {
            "gpio": 16, "active": True,
            "min_duty_pct": fan.CALIBRATION_STEPS_PCT[0],
            "max_rpm": fan.rpm_from_edges(3, 0.01, cfg["tach_pulses_per_rev"]),
        }
        assert (gpio_root / "gpio16" / "direction").read_text() == "in"


def test_controller_probe_sweep_finds_the_min_duty_where_the_fan_starts_spinning():
    """The floor case this whole rewrite exists for: a fan that only starts
    producing tach pulses partway up the duty range. Below that step, tick()
    trusts a confident 0 rpm (see min_duty_pct in tick()) instead of reading
    a coast/chop artifact off the tach line."""
    with tempfile.TemporaryDirectory() as d:
        chip, pwm0, temp_path = _board(Path(d))
        gpio_root = _gpio_root(Path(d))
        gpio_dir = _gpio_line(gpio_root, 16)
        cfg = dict(DEFAULT_CFG, fan_type=4, pwm_chip=str(chip), temp_path=str(temp_path),
                   tach_gpio_root=str(gpio_root),
                   pinctrl_root=str(_free_pinctrl_root(Path(d), 16)))
        ctl = fan.FanController(start_reader=lambda gpio_dir, counter: None, sleep=lambda s: None, tach_disabled=False)

        def count_edges(gdir, sample_s):
            pct = round(fan.pwm_read_duty_ns(pwm0) / cfg["period_ns"] * 100)
            return 5 if pct >= 40 else 0

        result = ctl.probe(cfg, 16, sample_s=0.01, count_edges=count_edges)
        assert result["active"] is True
        assert result["min_duty_pct"] == 40
        assert result["max_rpm"] == fan.rpm_from_edges(5, 0.01, cfg["tach_pulses_per_rev"])


def test_controller_probe_sweep_reports_inactive_when_the_fan_never_spins():
    with tempfile.TemporaryDirectory() as d:
        chip, pwm0, temp_path = _board(Path(d))
        gpio_root = _gpio_root(Path(d))
        _gpio_line(gpio_root, 16)
        cfg = dict(DEFAULT_CFG, fan_type=4, pwm_chip=str(chip), temp_path=str(temp_path),
                   tach_gpio_root=str(gpio_root),
                   pinctrl_root=str(_free_pinctrl_root(Path(d), 16)))
        ctl = fan.FanController(start_reader=lambda gpio_dir, counter: None, sleep=lambda s: None, tach_disabled=False)
        result = ctl.probe(cfg, 16, sample_s=0.01, count_edges=lambda g, d: 0)
        assert result == {"gpio": 16, "active": False, "min_duty_pct": None, "max_rpm": None}


def test_controller_probe_restores_the_temperature_appropriate_duty_not_whatever_ran_before():
    """The sweep's own finally-restore must reflect the current temperature,
    not just replay whatever duty was set before the probe started -- the
    board keeps cooling with fresh numbers, not stale ones from the moment
    Probe was pressed."""
    with tempfile.TemporaryDirectory() as d:
        chip, pwm0, temp_path = _board(Path(d))
        fan.pwm_set_duty_ns(pwm0, 123456)   # whatever duty happened to be running before
        temp_path.write_text("70000")       # hottest step -> full duty once the sweep finishes
        gpio_root = _gpio_root(Path(d))
        _gpio_line(gpio_root, 16)
        cfg = dict(DEFAULT_CFG, fan_type=4, pwm_chip=str(chip), temp_path=str(temp_path),
                   tach_gpio_root=str(gpio_root),
                   pinctrl_root=str(_free_pinctrl_root(Path(d), 16)))
        ctl = fan.FanController(start_reader=lambda gpio_dir, counter: None, sleep=lambda s: None, tach_disabled=False)
        ctl.probe(cfg, 16, sample_s=0.01, count_edges=lambda g, d: 0)
        assert fan.pwm_read_duty_ns(pwm0) == 8000000, "must restore to what 70C calls for now, not 123456"


def test_controller_probe_records_min_duty_in_register_space_not_speed_space():
    """min_duty_pct must mean what tick()'s own duty_pct means -- the raw
    duty_cycle/period_ns fraction, no polarity translation -- or the two
    disagree at inversed polarity: full speed there is duty_ns=0 (register
    0%), so a min_duty_pct recorded in *speed* percent would make tick()
    read a fan at its very fastest as below threshold and report a
    confident 0 rpm instead."""
    with tempfile.TemporaryDirectory() as d:
        chip, pwm0, temp_path = _board(Path(d))
        gpio_root = _gpio_root(Path(d))
        _gpio_line(gpio_root, 16)
        cfg = dict(DEFAULT_CFG, fan_type=4, polarity="inversed", pwm_chip=str(chip),
                   temp_path=str(temp_path), tach_gpio_root=str(gpio_root),
                   pinctrl_root=str(_free_pinctrl_root(Path(d), 16)))
        ctl = fan.FanController(start_reader=lambda gpio_dir, counter: None, sleep=lambda s: None, tach_disabled=False)
        # fan starts pulsing once speed >= 30% -- inversed polarity means
        # that is duty_ns = period_ns - period_ns*0.3 = 5,600,000 (register 70%)
        result = ctl.probe(cfg, 16, sample_s=0.01,
                            count_edges=lambda g, d: 5 if fan.pwm_read_duty_ns(pwm0) <= 5600000 else 0)
        assert result["min_duty_pct"] == 70, result


def test_probe_gpio_respects_inversed_polarity_for_full_speed():
    with tempfile.TemporaryDirectory() as d:
        chip, pwm0, temp_path = _board(Path(d))
        cfg = dict(DEFAULT_CFG, pwm_chip=str(chip), polarity="inversed")
        gpio_root = _gpio_root(Path(d))
        gpio_dir = _gpio_line(gpio_root, 16)
        seen = {}

        def fake_count_edges(gdir, duration_s):
            seen["duty"] = fan.pwm_read_duty_ns(pwm0)
            return 1

        fan.probe_gpio(pwm0, cfg, gpio_dir, duration_s=1.0, count_edges=fake_count_edges)
        assert seen["duty"] == 0, "inversed polarity: duty 0 is full speed, not the period"


# -- diag_fields (unchanged behaviour, still reads State.snapshot()) -------

def test_diag_fields_empty_when_fan_log_is_off():
    snap = {"fan_type": 3, "fan_rpm": 1200, "status": "connected"}
    assert fan.diag_fields(snap, {"log": False}) == {}
    assert fan.diag_fields(snap, {}) == {}
    assert fan.diag_fields(snap, None) == {}


def test_diag_fields_carries_only_the_fan_prefixed_keys_when_logging():
    snap = {"fan_type": 3, "fan_rpm": 1200, "fan_rpm_valid": True, "status": "connected"}
    assert fan.diag_fields(snap, {"log": True}) == {
        "fan_type": 3, "fan_rpm": 1200, "fan_rpm_valid": True,
    }


def _main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _main()
