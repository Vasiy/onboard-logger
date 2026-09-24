"""Fan control: PWM sysfs writer, tach GPIO reader, and the control loop that
ties them to CPU temperature. Runs as a background asyncio task in this
process (app/main.py's ``_fan_control``), not a separate service.

Why in-process: this started as a separate systemd service
(nanopi-pwm-fan-addon) specifically so a fan-control crash or an
onboard-logger restart could never stop cooling. The natural next step was to
ship its settings UI through onboard-logger's Add-ons tab -- except Add-ons
never executes code, by design (see addons.py's own docstring: an uploaded
archive is static content and a nameless blob store, nothing else, on
purpose). A real hardware-control backend cannot ship through that door. It
ships through the ordinary Update mechanism instead, like gear/diag/power_save
-- which means it has to live in this process after all. The tradeoff this
buys back: a few seconds of no active cooling during an update's swap, on a
board that idles well under its passive thermal trip and self-throttles if it
ever gets close.

The PWM/tach sysfs functions and the duty-selection algorithm started life in
the sibling nanopi-pwm-fan-addon project (control loop there ported from
ckwun/nanopi-pwm-fan, MIT) and are carried over here unchanged.

nanopi-pwm-fan-addon itself was never installed on the board (fan.enabled
here is what actually runs the fan) and has since fallen behind this copy --
no libgpiod migration, no Probe/sweep calibration, no PWM-frequency clamp.
Retired as of 2026-09-21; this module is the only maintained copy.
"""

from __future__ import annotations

import re
import select
import threading
import time
from collections import deque
from collections.abc import Sequence
from pathlib import Path
from typing import Callable

# 2026-09-20 root cause (resolved): the GPIO numbers first tried on the real
# board (16 on gpiochip0, 44 = gpio1-12) hung the kernel hard enough that even
# `timeout 5` could not recover it -- via legacy /sys/class/gpio/export AND
# via libgpiod's gpioget, so it looked like a library-independent kernel bug.
# It was not: /sys/kernel/debug/pinctrl/*/pinmux-pins showed both pins were
# internally wired to this board's Ethernet PHY (`ff540000.ethernet`, function
# gmac-1, group rgmiim1-pins) -- always electrically live while the board is
# reachable over its own Ethernet link, which every test happened to be run
# over. Requesting them as plain GPIO fights the running interface for the
# pin. "16" was the physical header pin number, not a Linux GPIO number; the
# header's pin 16 is actually Linux GPIO 101 (GPIO3_A5/UART1_RTSN), confirmed
# free ("MUX UNCLAIMED") and tested clean (export/read/unexport, no hang).
# tach_disabled stays available as a manual kill switch (FanController(...,
# tach_disabled=True) / TachUnavailable) for whoever wires a *different* pin
# that turns out to be claimed the same way -- see cfg.fanTypeHint's warning
# to check pinmux-pins before wiring a new one.
TACH_DISABLED_DEFAULT = False


class TachUnavailable(RuntimeError):
    """Raised instead of touching any GPIO line while TACH_DISABLED_DEFAULT
    (or FanController(tach_disabled=...)) is True."""


# Duty steps FanController.probe() sweeps through to find min_duty_pct --
# the lowest step that produces any tach pulses at all. Ten steps is enough
# resolution to be useful without dragging a Probe click out too long
# (steps * sample_s).
CALIBRATION_STEPS_PCT = (10, 20, 30, 40, 50, 60, 70, 80, 90, 100)


# -- duty selection -----------------------------------------------------

def select_duty_ns(
    temp_milli_c: int,
    target_temps: Sequence[int],
    duty_cycles: Sequence[int],
    default_duty_ns: int,
) -> int:
    for index, threshold in enumerate(target_temps):
        if temp_milli_c > threshold:
            if index < len(duty_cycles):
                return duty_cycles[index]
            return default_duty_ns
    return default_duty_ns


def clamp_duty_ns(duty_ns: int, period_ns: int) -> int:
    """A period change (the PWM frequency slider) does not rescale
    duty_cycles_ns -- a config.json-only field with no UI control -- so a
    tier sized for the old period can end up above the new, smaller one.
    A real board's kernel rejects a duty_cycle sysfs write above period
    outright (EINVAL), and the caller that swallows it (_fan_control in
    app/main.py, by design -- one bad tick must not kill the loop) then
    stalls fan control silently forever, with no log line. Clamping errs
    toward more cooling, never less."""
    return max(0, min(duty_ns, period_ns))


def duty_ns_for_speed_pct(period_ns: int, polarity: str, pct: float) -> int:
    """duty_cycle register value for `pct` percent of fan speed (0=off,
    100=full), accounting for polarity: more on-time is faster at normal
    polarity, slower at inversed. Used by the calibration sweep in
    FanController.probe(); full_speed_duty_ns is this at pct=100."""
    on_ns = round(period_ns * pct / 100)
    return on_ns if polarity == "normal" else period_ns - on_ns


def full_speed_duty_ns(cfg: dict) -> int:
    """duty_cycle that spins the fan fastest -- period_ns at normal polarity,
    0 at inversed (where duty_cycle == period is *off*)."""
    return duty_ns_for_speed_pct(cfg["period_ns"], cfg["polarity"], 100)


# -- PWM sysfs ------------------------------------------------------------

def _pwm_channel_dir(chip_dir: Path, channel: int) -> Path:
    return chip_dir / f"pwm{channel}"


def pwm_is_exported(chip_dir: Path, channel: int) -> bool:
    return _pwm_channel_dir(chip_dir, channel).is_dir()


def pwm_write_export(chip_dir: Path, channel: int) -> None:
    (chip_dir / "export").write_text(str(channel))


def pwm_wait_for_export(
    chip_dir: Path, channel: int, *, timeout_s: float, poll_interval_s: float
) -> Path:
    deadline = time.monotonic() + timeout_s
    channel_dir = _pwm_channel_dir(chip_dir, channel)
    while not channel_dir.is_dir():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"{channel_dir} did not appear within {timeout_s}s")
        time.sleep(poll_interval_s)
    return channel_dir


def pwm_ensure_exported(
    chip_dir: Path, channel: int, *, timeout_s: float = 5.0, poll_interval_s: float = 0.1
) -> Path:
    if pwm_is_exported(chip_dir, channel):
        return _pwm_channel_dir(chip_dir, channel)
    pwm_write_export(chip_dir, channel)
    return pwm_wait_for_export(chip_dir, channel, timeout_s=timeout_s, poll_interval_s=poll_interval_s)


def pwm_configure(pwm_dir: Path, *, period_ns: int, polarity: str) -> None:
    """Disable, zero the duty cycle, then set period and polarity -- polarity
    can only change while disabled, and a nonzero duty_cycle can reject an
    incoming period value."""
    if pwm_read_enabled(pwm_dir):
        pwm_set_enabled(pwm_dir, False)
    if pwm_read_duty_ns(pwm_dir) != 0:
        pwm_set_duty_ns(pwm_dir, 0)
    (pwm_dir / "period").write_text(str(period_ns))
    (pwm_dir / "polarity").write_text(polarity)


def pwm_set_duty_ns(pwm_dir: Path, duty_ns: int) -> None:
    (pwm_dir / "duty_cycle").write_text(str(duty_ns))


def pwm_read_duty_ns(pwm_dir: Path) -> int:
    return int((pwm_dir / "duty_cycle").read_text().strip())


def pwm_set_enabled(pwm_dir: Path, enabled: bool) -> None:
    (pwm_dir / "enable").write_text("1" if enabled else "0")


def pwm_read_enabled(pwm_dir: Path) -> bool:
    return (pwm_dir / "enable").read_text().strip() != "0"


# -- physical-pin picker for the tach line ----------------------------------
# 2026-09-20: the first two GPIO numbers tried on the real board (16, then 44)
# both hung the kernel -- turned out both were internally wired to this
# board's Ethernet PHY (confirmed via /sys/kernel/debug/pinctrl/*/pinmux-pins:
# "ff540000.ethernet ... function gmac-1"), always electrically live over the
# same Ethernet link every test was run over. "16" was the physical header
# pin number, not a Linux GPIO number -- the settings page now only offers
# physical pin numbers, mapped here, and only the ones the board's own
# pinmux currently reports free.
#
# (physical_header_pin, linux_gpio_number) for every NEO3 header pin whose
# function also has a plain GPIO number, per FriendlyElec's published pinout,
# each row arithmetic-checked against gpio = bank*32 + letter_index*8 + pin
# (e.g. GPIO3_A5 = 3*32 + 0*8 + 5 = 101). Only pin 16 (101) has actually been
# exported/read/unexported on real hardware so far; the others are believed
# correct but unverified. Power/GND/dedicated-bus-only rows have no plain
# GPIO and are not listed (header pins 1-6, 9, 14, 17, 20, 25, 26).
TACH_HEADER_PINS = [
    (7, 66), (8, 100), (10, 102), (11, 79), (12, 83), (13, 81), (15, 82),
    (16, 101), (18, 103), (19, 97), (21, 98), (22, 87), (23, 96), (24, 104),
]

_PINMUX_LINE_RE = re.compile(r"pin \d+ \(gpio(\d+)-(\d+)\): (.*)")


def _pinmux_claims(root: str | Path = "/sys/kernel/debug/pinctrl") -> dict[int, bool]:
    """{linux gpio number: is it free} parsed from the live pinmux-pins
    debugfs table. Empty on a dev host or a board with debugfs unmounted --
    callers then correctly see every pin as unavailable, never guess it's
    free."""
    claims: dict[int, bool] = {}
    try:
        paths = list(Path(root).glob("*/pinmux-pins"))
    except OSError:
        return claims
    for path in paths:
        try:
            text = path.read_text()
        except OSError:
            continue
        for line in text.splitlines():
            m = _PINMUX_LINE_RE.match(line)
            if not m:
                continue
            gpio_number = int(m.group(1)) * 32 + int(m.group(2))
            claims[gpio_number] = "(MUX UNCLAIMED)" in m.group(3)
    return claims


def is_gpio_available(gpio_number: int, root: str | Path = "/sys/kernel/debug/pinctrl") -> bool:
    """Unknown means unavailable, never the reverse -- a pin the parser never
    saw is not safe to assume free."""
    return _pinmux_claims(root).get(gpio_number, False)


def list_available_tach_pins(root: str | Path = "/sys/kernel/debug/pinctrl") -> list[dict]:
    """Header pins, by physical number, currently free to wire a tach sensor
    to -- what the settings page's dropdown offers."""
    claims = _pinmux_claims(root)
    return [{"pin": pin, "gpio": gpio} for pin, gpio in TACH_HEADER_PINS
            if claims.get(gpio, False)]


# -- tach GPIO sysfs --------------------------------------------------------
# Legacy /sys/class/gpio, not libgpiod: it is already present on this board
# and interrupt-driven (edge + poll(), no busy loop), with no extra package to
# install on a board that cannot apt-get mid-ride.

def _gpio_line_dir(gpio_root: Path, line: int) -> Path:
    return gpio_root / f"gpio{line}"


def gpio_is_exported(gpio_root: Path, line: int) -> bool:
    return _gpio_line_dir(gpio_root, line).is_dir()


def gpio_write_export(gpio_root: Path, line: int) -> None:
    (gpio_root / "export").write_text(str(line))


def gpio_wait_for_export(
    gpio_root: Path, line: int, *, timeout_s: float, poll_interval_s: float
) -> Path:
    deadline = time.monotonic() + timeout_s
    line_dir = _gpio_line_dir(gpio_root, line)
    while not line_dir.is_dir():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"{line_dir} did not appear within {timeout_s}s")
        time.sleep(poll_interval_s)
    return line_dir


def gpio_ensure_exported(
    gpio_root: Path, line: int, *, timeout_s: float = 5.0, poll_interval_s: float = 0.1
) -> Path:
    if gpio_is_exported(gpio_root, line):
        return _gpio_line_dir(gpio_root, line)
    gpio_write_export(gpio_root, line)
    return gpio_wait_for_export(gpio_root, line, timeout_s=timeout_s, poll_interval_s=poll_interval_s)


def gpio_configure_input(gpio_dir: Path, *, edge: str = "both") -> None:
    (gpio_dir / "direction").write_text("in")
    (gpio_dir / "edge").write_text(edge)


def gpio_read_value(gpio_dir: Path) -> int:
    return int((gpio_dir / "value").read_text().strip())


# -- tach math ---------------------------------------------------------

def rpm_from_edges(edge_count: int, window_s: float, pulses_per_rev: int) -> float:
    revolutions = edge_count / pulses_per_rev
    return revolutions / window_s * 60.0


def is_rpm_valid(duty_ns: int, period_ns: int, fan_type: int) -> bool | None:
    """fan_type 2 has no tach (not applicable). fan_type 3 shares the fan's
    power with the 2-pin connector's own PWM chop, so only a 100%-duty
    reading means anything. fan_type 4 has a dedicated 5V rail, trustworthy
    at any duty."""
    if fan_type == 2:
        return None
    if fan_type == 3:
        return duty_ns == period_ns
    return True


class EdgeWindowCounter:
    """Counts edge timestamps that fall within a trailing time window."""

    def __init__(self, window_s: float) -> None:
        self.window_s = window_s
        self._edges: deque[float] = deque()

    def add_edge(self, timestamp: float) -> None:
        self._edges.append(timestamp)

    def count_at(self, now: float) -> int:
        cutoff = now - self.window_s
        while self._edges and self._edges[0] <= cutoff:
            self._edges.popleft()
        return len(self._edges)


class GpioEdgeReader:
    """Blocks in select.poll() for POLLPRI on a sysfs gpio value fd, feeding
    each edge into an EdgeWindowCounter. Only a real, interrupt-capable GPIO
    line raises POLLPRI on this file, so this class has no offline test --
    exercised on the board itself, the same way DiagLog's KmsgReader (a
    comparable real-fd poll loop) has none either."""

    def __init__(self, gpio_dir: Path, counter: EdgeWindowCounter, *,
                 now: Callable[[], float] = time.monotonic):
        self._value_path = gpio_dir / "value"
        self._counter = counter
        self._now = now
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        fd = self._value_path.open("r")
        try:
            poller = select.poll()
            poller.register(fd, select.POLLPRI | select.POLLERR)
            fd.read()  # arm: an initial read is required before POLLPRI fires
            while not self._stop:
                events = poller.poll(500)  # ms; lets self._stop be checked
                if not events:
                    continue
                fd.seek(0)
                fd.read()
                self._counter.add_edge(self._now())
        finally:
            fd.close()


def _count_edges_blocking(gpio_dir: Path, duration_s: float) -> int:
    """Production edge counter for probe_gpio: a short-lived GpioEdgeReader
    thread, given no chance to test offline for the same reason
    GpioEdgeReader itself has none."""
    counter = EdgeWindowCounter(window_s=duration_s + 1.0)
    reader = GpioEdgeReader(gpio_dir, counter)
    t = threading.Thread(target=reader.run, daemon=True)
    t.start()
    time.sleep(duration_s)
    reader.stop()
    t.join(timeout=1.0)
    return counter.count_at(time.monotonic())


def probe_gpio(
    pwm_dir: Path,
    cfg: dict,
    gpio_dir: Path,
    *,
    duration_s: float = 2.0,
    count_edges: Callable[[Path, float], int] = _count_edges_blocking,
) -> dict:
    """Drive the fan to full speed for `duration_s` and count edges on
    `gpio_dir` -- a 3-wire fan's tach only pulses meaningfully at 100% duty
    (see is_rpm_valid), so probing at whatever duty happens to be running
    would read a live sensor as dead. The prior duty is restored afterwards;
    the caller's own tick() reasserts the correct value on its next pass
    regardless.
    """
    prior_duty = pwm_read_duty_ns(pwm_dir)
    pwm_set_duty_ns(pwm_dir, full_speed_duty_ns(cfg))
    try:
        pulses = count_edges(gpio_dir, duration_s)
    finally:
        pwm_set_duty_ns(pwm_dir, prior_duty)
    gpio_num = int(gpio_dir.name.removeprefix("gpio"))
    return {"gpio": gpio_num, "pulses": pulses, "active": pulses >= 1, "duration_s": duration_s}


# -- controller --------------------------------------------------------

def _start_reader_thread(gpio_dir: Path, counter: EdgeWindowCounter) -> GpioEdgeReader:
    reader = GpioEdgeReader(gpio_dir, counter)
    threading.Thread(target=reader.run, daemon=True).start()
    return reader


class FanController:
    """Owns the live PWM channel and tach reader thread, reconfigured live
    whenever the relevant config keys change -- no process restart, the same
    as every other feature flag in this app.

    `start_reader` is a seam for tests: the production default spins up a
    real GpioEdgeReader thread blocking on a real sysfs fd, which -- like
    GpioEdgeReader itself -- cannot run against a fake file in an offline
    test without depending on platform-specific poll() behaviour for regular
    files. Tests inject a stub that returns None and seed the counter by hand.
    """

    _CFG_KEYS = ("pwm_chip", "pwm_channel", "period_ns", "polarity",
                 "fan_type", "tach_gpio", "tach_gpio_root")

    def __init__(
        self,
        *,
        start_reader: Callable[[Path, "EdgeWindowCounter"], object | None] = _start_reader_thread,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        startup_spin_s: float = 5.0,
        tach_disabled: bool = TACH_DISABLED_DEFAULT,
    ) -> None:
        # RLock, not Lock: tick() and probe() hold it for their whole duration
        # (see class docstring) and both call apply_config(), which takes it
        # again on the same thread.
        self._lock = threading.RLock()
        self._start_reader = start_reader
        self._sleep = sleep
        self._monotonic = monotonic
        self._startup_spin_s = startup_spin_s
        self._tach_disabled = tach_disabled
        self._cfg_key: tuple | None = None
        self._pwm_dir: Path | None = None
        self._edge_counter: EdgeWindowCounter | None = None
        self._reader = None
        # "Max rpm" is whatever the sensor reports the one time duty is
        # genuinely 100% (see tick()) -- reset on reconfigure since a
        # different pin/fan_type may be a different fan entirely, and a
        # stale maximum would skew the header icon's speed tiers.
        self._max_rpm: float | None = None

    def _key(self, cfg: dict) -> tuple:
        return tuple(cfg.get(k) for k in self._CFG_KEYS)

    def apply_config(self, cfg: dict) -> None:
        with self._lock:
            key = self._key(cfg)
            if key == self._cfg_key:
                return
            # config wins on reconfigure (Probe's deliberate calibration,
            # persisted by the /api/fan/probe handler); live observation in
            # tick() below still refines it afterward if it sees something
            # different -- this is what lets a board that never
            # independently reaches 100% duty on its own (most of them, most
            # of the time) have a max_rpm at all
            self._max_rpm = cfg.get("max_rpm")

            chip_dir = Path(cfg["pwm_chip"])
            pwm_dir = pwm_ensure_exported(chip_dir, cfg.get("pwm_channel", 0))
            pwm_configure(pwm_dir, period_ns=cfg["period_ns"], polarity=cfg["polarity"])
            self._pwm_dir = pwm_dir

            if self._reader is not None:
                self._reader.stop()
                self._reader = None
                self._edge_counter = None

            # the tach reader is readied *before* the full-speed spin below,
            # not after: it has to already be running for the whole spin, or
            # the one reliable moment to learn max_rpm passes with nothing
            # listening (first boot's _reader is None going in -- readying it
            # after the spin, as this used to, meant the very edges the spin
            # exists to prove were never counted)
            if (not self._tach_disabled and cfg.get("fan_type") in (3, 4)
                    and cfg.get("tach_gpio") is not None
                    and is_gpio_available(cfg["tach_gpio"],
                                          cfg.get("pinctrl_root", "/sys/kernel/debug/pinctrl"))):
                gpio_root = Path(cfg.get("tach_gpio_root", "/sys/class/gpio"))
                gpio_dir = gpio_ensure_exported(gpio_root, cfg["tach_gpio"])
                gpio_configure_input(gpio_dir, edge="both")
                self._edge_counter = EdgeWindowCounter(window_s=1.0)
                self._reader = self._start_reader(gpio_dir, self._edge_counter)

            # run at full speed briefly whenever the channel is (re)configured,
            # same as ckwun's original script did once at startup -- proves the
            # fan actually spins before the thermal curve is trusted, and is
            # also the one moment tick()'s own max_rpm learning (gated on
            # duty_ns == period_ns) is guaranteed to see, so sample it here too
            pwm_set_duty_ns(pwm_dir, full_speed_duty_ns(cfg))
            pwm_set_enabled(pwm_dir, True)
            self._sleep(self._startup_spin_s)

            if self._edge_counter is not None and cfg["fan_type"] in (3, 4):
                edges = self._edge_counter.count_at(self._monotonic())
                spin_rpm = rpm_from_edges(edges, self._edge_counter.window_s, cfg["tach_pulses_per_rev"])
                if spin_rpm and is_rpm_valid(full_speed_duty_ns(cfg), cfg["period_ns"], cfg["fan_type"]):
                    self._max_rpm = spin_rpm

            # recorded only once every step above has actually succeeded --
            # a partial failure must not be mistaken for "nothing changed"
            # on the next call, or a half-configured channel never retries
            self._cfg_key = key

    def tick(self, cfg: dict, *, now_monotonic: float, now_wall: float) -> dict:
        """now_monotonic must be on the same clock GpioEdgeReader timestamps
        edges with (time.monotonic() in production) -- mixing it with wall
        clock silently zeroes every reading, since a Unix timestamp is always
        "older" than a monotonic one. now_wall is only for the ts field.

        Holds the lock for the whole tick: probe() also writes duty_cycle
        (to drive full speed for its sample window) and the two must not
        interleave, or a tick can undo the probe's full-speed duty mid-sample,
        or the probe's restore can undo a tick's fresh reading.
        """
        with self._lock:
            self.apply_config(cfg)
            temp_milli_c = int(Path(cfg["temp_path"]).read_text().strip())
            period_ns = cfg["period_ns"]
            duty_ns = clamp_duty_ns(select_duty_ns(
                temp_milli_c, cfg["target_temps_c"], cfg["duty_cycles_ns"], cfg["default_duty_ns"]
            ), period_ns)
            if pwm_read_duty_ns(self._pwm_dir) != duty_ns:
                pwm_set_duty_ns(self._pwm_dir, duty_ns)
            duty_pct = round(duty_ns / period_ns * 100.0, 1) if period_ns else 0.0
            min_duty_pct = cfg.get("min_duty_pct")

            if (cfg["fan_type"] in (3, 4) and min_duty_pct is not None
                    and duty_pct < min_duty_pct):
                # below the sweep-measured threshold the fan is known not to
                # spin at all (see FanController.probe) -- a raw tach reading
                # down there is a coast/chop artifact, not rotation, so this
                # is a *confident* zero, not the "reading exists but may be
                # wrong" case rpm_valid=False means for 3-pin below 100%
                rpm, rpm_valid = 0.0, True
            else:
                rpm = None
                if cfg["fan_type"] in (3, 4) and self._edge_counter is not None:
                    edges = self._edge_counter.count_at(now_monotonic)
                    rpm = rpm_from_edges(edges, self._edge_counter.window_s, cfg["tach_pulses_per_rev"])
                rpm_valid = is_rpm_valid(duty_ns, period_ns, cfg["fan_type"])

                # "max rpm" only ever comes from a genuinely-100%-duty reading
                # -- fan_type 4 is tach-valid at every duty, so without the
                # duty_ns == period_ns check a partial-speed reading would get
                # mistaken for the maximum and skew every percentage derived
                # from it (the header icon's rotation tier, for one)
                if duty_ns == period_ns and rpm is not None and rpm_valid:
                    self._max_rpm = rpm

            return {
                "fan_type": cfg["fan_type"],
                "temp_c": temp_milli_c / 1000.0,
                "period_ns": period_ns,
                "duty_ns": duty_ns,
                "duty_pct": duty_pct,
                "rpm": rpm,
                "rpm_valid": rpm_valid,
                "max_rpm": self._max_rpm,
                "min_duty_pct": min_duty_pct,
                "ts": now_wall,
            }

    def probe(
        self,
        cfg: dict,
        gpio: int,
        *,
        sample_s: float = 0.6,
        count_edges: Callable[[Path, float], int] = _count_edges_blocking,
    ) -> dict:
        """Try a candidate tach GPIO before committing it to config, and in
        the same pass calibrate it: sweep duty from 10% to 100%
        (CALIBRATION_STEPS_PCT) looking for the first step that produces any
        tach pulses at all -- that step is min_duty_pct, the point below
        which the fan is known not to be spinning (tick() trusts this as a
        confident 0 rpm rather than reading a coast/chop artifact off the
        tach line). max_rpm is read at the 100% step, the same thing
        apply_config()'s startup spin learns, just measured on demand
        instead of waited for.

        Readies the PWM channel and the given GPIO independently of whatever
        tach_gpio is currently configured, so probing a fresh pin works even
        if fan control has never ticked yet.

        Holds the lock for the whole sweep (readiness + every sample step),
        so a concurrent tick() cannot interleave a duty write with the
        sweep's own, and the sweep's restore-on-exit cannot clobber a tick
        that ran after it -- see tick()'s docstring.

        Raises TachUnavailable immediately, before touching any GPIO
        function, while tach_disabled is set (see TACH_DISABLED_DEFAULT) or
        while the board's own pinmux reports this specific pin claimed by
        something else (e.g. Ethernet, UART, I2C -- see TACH_HEADER_PINS).
        """
        if self._tach_disabled:
            raise TachUnavailable(
                "tach reading is disabled -- GPIO line requests hang this board's kernel")
        pinctrl_root = cfg.get("pinctrl_root", "/sys/kernel/debug/pinctrl")
        if not is_gpio_available(gpio, pinctrl_root):
            raise TachUnavailable(f"GPIO {gpio} is currently claimed by another peripheral")
        with self._lock:
            self.apply_config(cfg)
            gpio_root = Path(cfg.get("tach_gpio_root", "/sys/class/gpio"))
            gpio_dir = gpio_ensure_exported(gpio_root, gpio)
            gpio_configure_input(gpio_dir, edge="both")
            period_ns = cfg["period_ns"]
            polarity = cfg["polarity"]
            min_duty_pct = None
            max_rpm = None
            try:
                for pct in CALIBRATION_STEPS_PCT:
                    duty_ns = duty_ns_for_speed_pct(period_ns, polarity, pct)
                    pwm_set_duty_ns(self._pwm_dir, duty_ns)
                    pulses = count_edges(gpio_dir, sample_s)
                    if pulses >= 1:
                        if min_duty_pct is None:
                            # register-space (duty_ns/period_ns), matching
                            # what tick()'s own duty_pct means -- not the
                            # speed-space pct these steps are keyed by,
                            # which is only the same number at normal
                            # polarity
                            min_duty_pct = round(duty_ns / period_ns * 100) if period_ns else 0
                        if pct == 100:
                            max_rpm = rpm_from_edges(pulses, sample_s, cfg["tach_pulses_per_rev"])
            finally:
                # restore whatever the current temperature calls for right
                # now, not just whatever duty happened to be running before
                # Probe was pressed -- that number may be stale by the time
                # a multi-step sweep finishes
                temp_milli_c = int(Path(cfg["temp_path"]).read_text().strip())
                restore_duty_ns = clamp_duty_ns(select_duty_ns(
                    temp_milli_c, cfg["target_temps_c"], cfg["duty_cycles_ns"], cfg["default_duty_ns"]
                ), period_ns)
                pwm_set_duty_ns(self._pwm_dir, restore_duty_ns)
            return {
                "gpio": gpio,
                "active": min_duty_pct is not None,
                "min_duty_pct": min_duty_pct,
                "max_rpm": max_rpm,
            }


def diag_fields(snapshot: dict, fan_cfg: dict | None) -> dict:
    """Fan fields to merge into DiagLog's health probe (app/web/diag.py),
    gated on fan.log -- RPM is not motorcycle telemetry, so it must not
    appear in the ride's decoded CSV, only (optionally) in the board's own
    diagnostics log."""
    if not (fan_cfg or {}).get("log"):
        return {}
    return {k: v for k, v in snapshot.items() if k.startswith("fan_")}
