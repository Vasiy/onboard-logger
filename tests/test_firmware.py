"""Firmware op forces both K-Line log streams off, then restores them."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.kline.ecu_id import DEFAULT_FIELDS, describe, parse_desc  # noqa: E402
from app.web.firmware import FirmwareBlocked, FirmwareManager  # noqa: E402
from app.web.state import State  # noqa: E402


class FakeWorker:
    def __init__(self, dec=True, raw=True):
        self._dec, self._raw = dec, raw
        self.calls = []

    def logging_state(self):
        return (self._dec, self._raw)

    def set_logging_decoded(self, on):
        self.calls.append(("dec", on)); self._dec = on

    def set_logging_raw(self, on):
        self.calls.append(("raw", on)); self._raw = on

    def request_pause(self, timeout=10.0):
        self.calls.append(("pause",)); return True

    def resume(self):
        self.calls.append(("resume",))


def test_write_desc_from_ecu_id():
    with tempfile.TemporaryDirectory() as tmp:
        st = State()
        st.set_ecu_id_raw(bytes(range(60)).hex())  # mostly non-printable -> blanks
        st.set_ecu_hw("IAW5AMHW610")
        fm = FirmwareManager(lambda: None, "/usr/bin/true", tmp, "/dev/kline", st)
        fm._write_desc("x.bin")
        p = Path(tmp) / "x.bin.txt"
        assert p.exists()
        raw = p.read_bytes()
        assert raw.startswith(b"Drawing:")
        assert b"Hardware: IAW5AMHW610" in raw   # hw falls back to the regex-parsed value
        assert raw.endswith(b"\r\n")             # GuzziDiag-style CRLF preserved on disk


def test_write_desc_skips_without_id():
    with tempfile.TemporaryDirectory() as tmp:
        fm = FirmwareManager(lambda: None, "/usr/bin/true", tmp, "/dev/kline", State())
        fm._write_desc("y.bin")
        assert not (Path(tmp) / "y.bin.txt").exists()  # no ECU id -> no file


def test_desc_without_extra_is_unchanged():
    raw = bytes(range(60))
    text = describe(raw, "IAW5AMHW610", DEFAULT_FIELDS)
    assert text == describe(raw, "IAW5AMHW610", DEFAULT_FIELDS, extra=None)
    assert text == describe(raw, "IAW5AMHW610", DEFAULT_FIELDS, extra=[])
    assert "Brand" not in text          # nothing new leaks into an old-style dump


def test_desc_extra_lines_keep_the_guzzidiag_shape():
    text = describe(bytes(range(60)), "IAW5AMHW610", DEFAULT_FIELDS,
                    extra=[("Brand", "Moto Morini"), ("Model", ""), ("Image", "23ECCLGPSMD")])
    assert text.startswith("Drawing:") and text.endswith("\r\n")
    assert "Brand: Moto Morini" in text
    assert "Model:" not in text          # empty values are dropped, as in parse_fields
    assert text.index("Drawing:") < text.index("Brand:")   # appended, never prepended


def test_parse_desc_round_trips():
    text = describe(bytes(range(60)), "IAW5AMHW610", DEFAULT_FIELDS,
                    extra=[("Brand", "Moto Morini"), ("Model", "Granpasso 1200")])
    got = parse_desc(text)
    assert got["Hardware"] == "IAW5AMHW610"
    assert got["Brand"] == "Moto Morini" and got["Model"] == "Granpasso 1200"


def test_write_desc_records_the_bike():
    with tempfile.TemporaryDirectory() as tmp:
        st = State()
        st.set_ecu_id_raw(bytes(range(60)).hex())
        st.set_ecu_hw("IAW5AMHW610")
        fm = FirmwareManager(lambda: None, "/usr/bin/true", tmp, "/dev/kline", st,
                             describe_image=lambda n: [("Brand", "Moto Morini"),
                                                       ("Model", "Granpasso 1200"),
                                                       ("Image", "23ECCLGPSMD (5AM X0000)"),
                                                       ("Catalog", "verified")])
        fm._write_desc("x.bin")
        raw = (Path(tmp) / "x.bin.txt").read_bytes()
        assert raw.startswith(b"Drawing:") and raw.endswith(b"\r\n")
        assert b"Brand: Moto Morini" in raw and b"Image: 23ECCLGPSMD (5AM X0000)" in raw


def test_write_desc_falls_back_to_the_image_when_the_ecu_is_gone():
    with tempfile.TemporaryDirectory() as tmp:
        fm = FirmwareManager(lambda: None, "/usr/bin/true", tmp, "/dev/kline", State(),
                             describe_image=lambda n: [("Image", "23ECCLGPSMC")])
        fm._write_desc("z.bin")
        raw = (Path(tmp) / "z.bin.txt").read_bytes()
        assert b"Image: 23ECCLGPSMC" in raw   # a dump is still worth a passport


def test_guard_blocks_below_the_http_layer():
    """A direct start_write() must not be able to skip the check."""
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "other.bin").write_bytes(b"\x00" * 16)
        verdict = {"level": "block", "reason": "model_mismatch"}
        fm = FirmwareManager(lambda: None, "/usr/bin/true", tmp, "/dev/kline", State(),
                             guard=lambda name: verdict)
        try:
            fm.start_write("other.bin")
            raise AssertionError("start_write should have refused")
        except FirmwareBlocked as e:
            assert e.verdict["reason"] == "model_mismatch"
        assert fm.status()["op"] == "idle"    # nothing was started


def test_overridden_write_leaves_a_trace_in_the_operation_log():
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "other.bin").write_bytes(b"\x00" * 16)
        fm = FirmwareManager(lambda: None, "/usr/bin/true", tmp, "/dev/kline", State(),
                             guard=lambda name: {"level": "warn", "reason": "model_mismatch",
                                                 "overridden": True})
        fm.start_write("other.bin")
        fm._thread.join(timeout=5)
        assert any("guard overridden: model_mismatch" in ln for ln in fm.status()["log"])


def _run_true(worker, tmp):
    fm = FirmwareManager(
        worker_getter=lambda: worker, util_path="/usr/bin/true",
        fw_dir=tmp, port="/dev/kline", state=State(),
    )
    fm._run(["/usr/bin/true"], confirm=False)  # synchronous
    return fm


def test_logs_forced_off_then_restored():
    with tempfile.TemporaryDirectory() as tmp:
        w = FakeWorker(dec=True, raw=True)
        fm = _run_true(w, tmp)
        c = w.calls
        # both streams disabled BEFORE the port is taken
        assert ("dec", False) in c and ("raw", False) in c
        assert c.index(("dec", False)) < c.index(("pause",))
        assert c.index(("raw", False)) < c.index(("pause",))
        # restored after, and resumed
        assert w._dec is True and w._raw is True
        assert ("resume",) in c
        assert c.index(("resume",)) > c.index(("pause",))
        assert fm.status()["result"] == "ok"


def test_restore_respects_prior_state():
    with tempfile.TemporaryDirectory() as tmp:
        w = FakeWorker(dec=True, raw=False)   # raw was off before
        _run_true(w, tmp)
        assert w._dec is True and w._raw is False  # exact prior intent restored


def _main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn(); print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _main()
