"""Offline tests for the two independent log streams (decoded CSV + raw NDJSON)."""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.kline.logger import KLineWorker  # noqa: E402
from app.web.led import Led  # noqa: E402
from app.web.storage import day_name  # noqa: E402
from app.web.state import State  # noqa: E402

PARAMS = str(Path(__file__).resolve().parent.parent / "config" / "params.json")


def _worker(tmp, dec=True, raw=False):
    return KLineWorker(
        port="/dev/null", params_path=PARAMS, log_dir=tmp,
        state=State(), led=Led(), log_decoded_default=dec, log_raw_default=raw,
    )


def test_defaults():
    with tempfile.TemporaryDirectory() as tmp:
        w = _worker(tmp)
        assert w._want_dec is True and w._want_raw is False


def test_decoded_csv():
    with tempfile.TemporaryDirectory() as tmp:
        w = _worker(tmp)
        w._open_dec()
        w._write_decoded({"rpm": 3000, "throttle": 1.8, "coolant_t": 60, "vbat": 13.9})
        w._write_decoded({"rpm": 3200})  # missing channels -> blank cells
        assert w.state.log_decoded_records == 2        # counted while writing
        path = w._dec_path
        w._close_dec()
        lines = Path(path).read_text().splitlines()
        assert lines[0].startswith("time,rpm,")
        cols = lines[0].split(",")
        assert "throttle" in cols and "coolant_t" in cols
        assert ",3000," in lines[1]
        assert lines[2].split(",")[1] == "3200"        # rpm column present
        assert lines[2].split(",")[cols.index("throttle")] == ""   # throttle blank
        assert w.state.log_decoded_file == ""          # writing stopped after close
        assert w.state.logging_decoded is True         # armed (default on) persists
        assert path.name.startswith("kline-dec-") and path.suffix == ".csv"
        assert path.parent.name == day_name(), "logs are grouped by the day they were written"
        assert path.parent.parent == Path(tmp)


def test_raw_ndjson():
    with tempfile.TemporaryDirectory() as tmp:
        w = _worker(tmp, dec=False, raw=True)
        w._open_raw()
        w._write_raw({"t": 1.0, "dir": "tx", "hex": "810f"})
        path = w._raw_path
        w._close_raw()
        rec = json.loads(Path(path).read_text().splitlines()[0])
        assert rec["dir"] == "tx" and rec["hex"] == "810f"
        assert path.name.startswith("kline-") and path.name.endswith(".raw.log")
        assert path.parent.name == day_name()


def test_decoded_columns_follow_selection():
    with tempfile.TemporaryDirectory() as tmp:
        w = _worker(tmp)
        w.set_selected(["rpm", "vbat"])
        w._open_dec()
        w._write_decoded({"rpm": 1000, "throttle": 5, "vbat": 13.0})
        path = w._dec_path
        w._close_dec()
        lines = Path(path).read_text().splitlines()
        assert lines[0] == "time,rpm,vbat"                 # header = selected only
        assert lines[1].split(",")[1:] == ["1000", "13.0"]  # throttle excluded


def test_selection_change_flags_restart():
    with tempfile.TemporaryDirectory() as tmp:
        w = _worker(tmp)
        w._open_dec()
        assert w._dec_restart is False
        w.set_selected(["rpm"])          # changing selection while open -> roll
        assert w._dec_restart is True
        w._close_dec()


def test_zip_after_close():
    with tempfile.TemporaryDirectory() as tmp:
        w = _worker(tmp)
        w.set_zip_after(True)
        w._open_dec()
        w._write_decoded({"rpm": 1})
        p = w._dec_path
        w._close_dec()
        assert not p.exists()                              # original removed
        assert (p.parent / (p.name + ".zip")).exists()     # archived


def test_reconcile_independent():
    with tempfile.TemporaryDirectory() as tmp:
        w = _worker(tmp, dec=False, raw=False)
        w._reconcile_logging()
        assert w._dec_fh is None and w._raw_fh is None
        w.set_logging_decoded(True)
        w.set_logging_raw(True)
        w._reconcile_logging()
        assert w._dec_fh is not None and w._raw_fh is not None
        assert w.state.logging_decoded and w.state.logging_raw
        w.set_logging_decoded(False)  # decoded off, raw stays on
        w._reconcile_logging()
        assert w._dec_fh is None and w._raw_fh is not None
        w._close_all_logs()


def test_root_may_be_a_callable_and_is_followed_live():
    """A stick pulled mid-ride moves the root; the open file must follow it."""
    with tempfile.TemporaryDirectory() as usb, tempfile.TemporaryDirectory() as sd:
        where = [usb]
        w = KLineWorker(port="/dev/null", params_path=PARAMS,
                        log_dir=lambda: where[0], state=State(), led=Led())
        w._open_dec()
        assert w._dec_path.parent.parent == Path(usb)
        where[0] = sd                       # the stick is gone; fall back to the card
        w._root_check_at = 0.0
        w._reconcile_logging()
        assert w._dec_path.parent.parent == Path(sd), "the file followed the root"
        assert w._dec_fh is not None, "and logging never stopped"
        w._close_all_logs()


def test_a_dead_disk_costs_the_file_not_the_link():
    with tempfile.TemporaryDirectory() as tmp:
        w = _worker(tmp)
        w._open_dec()

        class Dead:
            def write(self, _s):
                raise OSError(5, "Input/output error")

        w._dec_fh = Dead()
        w._write_decoded({"rpm": 1000})     # must not raise into the poll loop
        assert w._dec_fh is None and w.state.log_decoded_file == ""


def test_diagnostics_run_only_while_a_log_is_open():
    with tempfile.TemporaryDirectory() as tmp:
        calls = []

        class FakeDiag:
            def start(self):
                calls.append("start")

            def stop(self):
                calls.append("stop")

            def event(self, kind, **f):
                pass

        w = KLineWorker(port="/dev/null", params_path=PARAMS, log_dir=tmp,
                        state=State(), led=Led(), diag=FakeDiag())
        w._reconcile_logging()              # decoded is armed by default -> opens
        assert calls[-1] == "start", calls
        w.set_logging_decoded(False)
        w._reconcile_logging()
        assert calls[-1] == "stop", calls


def test_link_events_are_silent_while_nothing_is_recorded():
    """A bike parked with the ignition off must not produce a diagnostics file.

    The worker retries the link forever; each failure used to write an event,
    and an event opens the file on demand — so an idle board wrote logs about
    having nothing to log.
    """
    with tempfile.TemporaryDirectory() as tmp:
        seen = []

        class FakeDiag:
            def start(self):
                seen.append("start")

            def stop(self):
                seen.append("stop")

            def event(self, kind, **f):
                seen.append(kind)

        w = KLineWorker(port="/dev/null", params_path=PARAMS, log_dir=tmp,
                        state=State(), led=Led(), diag=FakeDiag())
        w._diag("link_fail", err="SerialException")
        assert seen == [], f"idle worker wrote {seen}"

        # once a ride log is open the same call goes through
        w._reconcile_logging()
        assert "start" in seen
        seen.clear()
        w._diag("link_fail", err="SerialException")
        assert seen == ["link_fail"], seen


def test_link_up_opens_the_diagnostics_file_it_belongs_to():
    """link_up happens before the first file exists; it must not be lost."""
    with tempfile.TemporaryDirectory() as tmp:
        seen = []

        class FakeDiag:
            def start(self):
                seen.append("start")

            def stop(self):
                seen.append("stop")

            def event(self, kind, **f):
                seen.append((kind, f.get("baud")))

        w = KLineWorker(port="/dev/null", params_path=PARAMS, log_dir=tmp,
                        state=State(), led=Led(), diag=FakeDiag())
        w._link_ctx = {"baud": 10400, "init": "fast", "ecu": "IAW5AM"}
        w._reconcile_logging()
        assert seen[0] == "start" and seen[1] == ("link_up", 10400), seen
        w._close_all_logs()
        assert "stop" in [x for x in seen if isinstance(x, str)]
        assert w._link_ctx == {}, "the context belongs to the link that just ended"


def _main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _main()
