"""Offline tests for the CPU governor / frequency ceiling.

The board sits in a closed case with no fan and idles ~5 C under its first
passive thermal trip, so the System panel can hold the clock down. Everything
the panel offers is read from the board rather than assumed -- this NanoPi NEO3
answers six governors and six frequencies, the next board need not -- and a
value the kernel does not offer is refused here instead of being written and
silently ignored, which is what sysfs would do.

Run directly:  python tests/test_system_cpu.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.web import system  # noqa: E402

# what the real board answered on 2026-09-09
GOVERNORS = "conservative ondemand userspace powersave performance schedutil"
FREQS = "408000 600000 816000 1008000 1200000 1296000"


def _board(tmp: Path, **over) -> tuple[Path, Path]:
    """A sysfs stand-in with the files the reader looks for."""
    cpu, th = tmp / "cpufreq", tmp / "thermal"
    cpu.mkdir(parents=True, exist_ok=True)
    th.mkdir(parents=True, exist_ok=True)
    files = {
        "scaling_available_governors": GOVERNORS,
        "scaling_available_frequencies": FREQS,
        "scaling_governor": "ondemand",
        "scaling_max_freq": "1296000",
        "scaling_cur_freq": "816000",
        "cpuinfo_min_freq": "408000",
        "cpuinfo_max_freq": "1296000",
    }
    files.update(over)
    for name, val in files.items():
        (cpu / name).write_text(val + "\n")
    (th / "temp").write_text("64583\n")
    (th / "trip_point_0_temp").write_text("70000\n")
    return cpu, th


def _use(cpu: Path, th: Path) -> None:
    system.CPUFREQ, system.THERMAL = cpu, th


def test_status_reads_what_the_board_offers():
    with tempfile.TemporaryDirectory() as d:
        _use(*_board(Path(d)))
        st = system.cpu_status()
        assert st["available"] is True, st
        assert st["governors"] == GOVERNORS.split(), st["governors"]
        assert st["freqs_khz"] == [408000, 600000, 816000, 1008000, 1200000, 1296000]
        assert st["governor"] == "ondemand" and st["cur_khz"] == 816000
        # the factory ceiling comes along so a caller can tell "nobody capped
        # this" from "something did" -- the thermal governor drives
        # scaling_max_freq too and can hold it down on its own
        assert st["hw_max_khz"] == 1296000 and st["max_khz"] == 1296000
        # the headroom is the number that matters: past the trip the kernel cuts
        # the clock itself and the panel has no say
        assert st["temp_c"] == 64.6 and st["trip_c"] == 70.0
        assert st["headroom_c"] == 5.4, st


def test_a_policy_without_a_table_still_names_its_two_ends():
    with tempfile.TemporaryDirectory() as d:
        cpu, th = _board(Path(d))
        (cpu / "scaling_available_frequencies").unlink()
        _use(cpu, th)
        st = system.cpu_status()
        assert st["available"] is True
        assert st["freqs_khz"] == [408000, 1296000], st["freqs_khz"]


def test_apply_writes_both_and_persists_nothing_else():
    with tempfile.TemporaryDirectory() as d:
        cpu, th = _board(Path(d))
        _use(cpu, th)
        res = system.cpu_apply("powersave", 816000)
        assert res["ok"] is True, res
        assert (cpu / "scaling_governor").read_text() == "powersave"
        assert (cpu / "scaling_max_freq").read_text() == "816000"
        assert set(res["applied"]) == {"governor powersave", "max 816000 kHz"}


def test_empty_and_zero_leave_the_kernel_alone():
    with tempfile.TemporaryDirectory() as d:
        cpu, th = _board(Path(d))
        _use(cpu, th)
        res = system.cpu_apply("", 0)
        assert res["ok"] is True and res["applied"] == []
        assert (cpu / "scaling_governor").read_text().strip() == "ondemand"
        assert (cpu / "scaling_max_freq").read_text().strip() == "1296000"


def test_a_value_the_kernel_does_not_offer_is_refused_not_written():
    with tempfile.TemporaryDirectory() as d:
        cpu, th = _board(Path(d))
        _use(cpu, th)
        res = system.cpu_apply("turbo", 999000)
        assert res["ok"] is False, res
        assert "governor turbo is not offered" in res["message"], res["message"]
        assert "frequency 999000 is not offered" in res["message"], res["message"]
        # nothing was written on the way to refusing
        assert (cpu / "scaling_governor").read_text().strip() == "ondemand"
        assert (cpu / "scaling_max_freq").read_text().strip() == "1296000"


def test_a_ceiling_held_down_by_something_else_is_visible():
    """The kernel's thermal governor writes scaling_max_freq as well.

    On 2026-09-09 the board sat at 81 C, eleven degrees past its trip, with the
    ceiling stuck at 600 MHz while config asked for nothing -- so the status has
    to carry both numbers or the panel cannot tell a capped board from a slow
    one.
    """
    with tempfile.TemporaryDirectory() as d:
        cpu, th = _board(Path(d), scaling_max_freq="600000")
        _use(cpu, th)
        st = system.cpu_status()
        assert st["max_khz"] == 600000 and st["hw_max_khz"] == 1296000, st
        assert st["max_khz"] < st["hw_max_khz"], "this is what the UI keys off"


def test_a_dev_host_with_no_cpufreq_degrades_instead_of_raising():
    with tempfile.TemporaryDirectory() as d:
        _use(Path(d) / "nope", Path(d) / "nope")
        st = system.cpu_status()
        assert st["available"] is False and st["governors"] == []
        assert "cpufreq" in st["message"]
        res = system.cpu_apply("powersave", 816000)
        assert res["ok"] is False and "cpufreq" in res["message"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok " + name)
