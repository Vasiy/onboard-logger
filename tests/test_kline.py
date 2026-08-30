"""Offline tests for the K-Line stack — no hardware, no pyserial port.

Run directly:  python tests/test_kline.py   (or: pytest)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.kline.transport import KLineTransport, Frame, ChecksumError  # noqa: E402
from app.kline.kwp2000 import KWP2000Session, NegativeResponse  # noqa: E402
from app.kline.params import Param, ParamMap  # noqa: E402


class FakeSerial:
    """Minimal half-duplex loopback: writing enqueues echo + scripted response."""

    def __init__(self, responder):
        self.responder = responder
        self.rx = bytearray()
        self.tx = bytearray()
        self.timeout = 0.1
        self.write_timeout = 1.0
        self.break_condition = False

    def reset_input_buffer(self):
        self.rx.clear()

    def reset_output_buffer(self):
        pass

    def write(self, data):
        self.tx.extend(data)
        self.rx.extend(data)                 # single-wire echo
        self.rx.extend(self.responder(bytes(data)))  # ECU reply
        return len(data)

    def flush(self):
        pass

    def read(self, n):
        take = self.rx[:n]
        del self.rx[:n]
        return bytes(take)

    def close(self):
        pass


def _resp_frame(data: bytes) -> bytes:
    """Build an ECU->tester response frame (target=0xF1, source=0x10)."""
    fmt = 0x80 | len(data)
    head = bytes([fmt, 0xF1, 0x10]) + data
    return head + bytes([sum(head) & 0xFF])


def test_checksum_and_build():
    t = KLineTransport("/dev/null")
    assert t.checksum8(b"\x81\x10\xf1\x81") == 0x03
    assert t.build_frame(b"\x81") == b"\x81\x10\xf1\x81\x03"


def test_frame_roundtrip_and_echo():
    # ECU answers StartCommunication positively (0xC1 ...)
    def responder(tx):
        return _resp_frame(bytes([0xC1, 0xEA, 0x8F]))

    t = KLineTransport("/dev/null")
    t.ser = FakeSerial(responder)
    tx, resp = t.request(b"\x81")
    assert tx == b"\x81\x10\xf1\x81\x03"
    assert isinstance(resp, Frame)
    assert resp.sid == 0xC1
    assert resp.source == 0x10 and resp.target == 0xF1


def test_checksum_error():
    def responder(tx):
        good = bytearray(_resp_frame(bytes([0xC1])))
        good[-1] ^= 0xFF  # corrupt checksum
        return bytes(good)

    t = KLineTransport("/dev/null")
    t.ser = FakeSerial(responder)
    try:
        t.request(b"\x81")
    except ChecksumError:
        return
    raise AssertionError("expected ChecksumError")


def test_negative_response():
    def responder(tx):
        return _resp_frame(bytes([0x7F, 0x21, 0x22]))  # conditionsNotCorrect

    t = KLineTransport("/dev/null")
    t.ser = FakeSerial(responder)
    sess = KWP2000Session(t)
    try:
        sess.poll(0x01)
    except NegativeResponse as e:
        assert e.request_sid == 0x21 and e.code == 0x22
        return
    raise AssertionError("expected NegativeResponse")


def test_read_local_and_decode():
    # response DATA = [0x61, id_echo] + block; rpm=3000 (0x0BB8), coolant=0x64
    block = bytes([0x0B, 0xB8, 0x64, 0x64, 0x28, 0x50, 0xC0, 0x30, 0x01, 0xF4, 0x14, 0x00])

    def responder(tx):
        return _resp_frame(bytes([0x61, 0x01]) + block)

    t = KLineTransport("/dev/null")
    t.ser = FakeSerial(responder)
    sess = KWP2000Session(t)
    r = sess.read_local(0x01)
    assert r["status"] == "ok"
    assert r["vlen"] == len(block)            # value bytes = everything after SID + rli echo
    data = bytes.fromhex(r["data"])           # full response DATA field = [0x61,0x01]+block

    # Param.decode over the full response DATA field (offset counts from the SID echo)
    rpm = Param(key="rpm", name="RPM", rli=1, offset=2, length=2)         # 0x0BB8
    coolant = Param(key="coolant_t", name="Coolant", rli=1, offset=5, length=1, bias=-40.0)
    assert rpm.decode(data) == 3000
    assert coolant.decode(data) == 0x64 - 40  # 60 °C


def test_read_local_nrc_and_short_frame():
    # ECU rejects with negative response 0x7F 0x21 0x31 (requestOutOfRange)
    def responder(tx):
        return _resp_frame(bytes([0x7F, 0x21, 0x31]))

    t = KLineTransport("/dev/null")
    t.ser = FakeSerial(responder)
    sess = KWP2000Session(t)
    r = sess.read_local(0x99)
    assert r["status"] == "nrc" and r["nrc"] == 0x31

    # short-header frame build (fmt 0, no address bytes): [len][data][cs]
    assert t.build_frame(b"\x21\x47", with_addr=False) == b"\x02\x21\x47\x6a"


def test_read_dtc():
    # response 58 <count=1> [01 23 60] -> DTC P0123, status 0x60
    def responder(tx):
        return _resp_frame(bytes([0x58, 0x01, 0x01, 0x23, 0x60]))

    t = KLineTransport("/dev/null")
    t.ser = FakeSerial(responder)
    sess = KWP2000Session(t)
    r = sess.read_dtc()
    assert r["count"] == 1
    assert r["dtcs"][0]["code"] == "P0123"
    assert r["dtcs"][0]["status"] == 0x60


def test_clear_dtc_and_code():
    def responder(tx):
        return _resp_frame(bytes([0x54]))  # positive to ClearDiagnosticInformation

    t = KLineTransport("/dev/null")
    t.ser = FakeSerial(responder)
    sess = KWP2000Session(t)
    assert "raw" in sess.clear_dtc()
    assert KWP2000Session._dtc_code(0x81, 0x45) == "B0145"  # category bits -> 'B'


def test_adaptation_resets():
    # ECU answers each service positively (SID + 0x40)
    def responder(tx):
        sid = tx[3]  # [fmt, tgt, src, sid, ...]
        if sid == 0x31: return _resp_frame(bytes([0x71, 0x21]))      # TPS reset
        if sid == 0x30: return _resp_frame(bytes([0x70, 0x7E]))      # Selbstadaption
        return _resp_frame(bytes([0x7F, sid, 0x11]))

    t = KLineTransport("/dev/null")
    t.ser = FakeSerial(responder)
    sess = KWP2000Session(t)
    assert "raw" in sess.reset_tps()          # 31 21 -> 71 21
    assert "raw" in sess.reset_adaptation()   # 30 7E 04 -> 70 7E


def test_enter_test_mode():
    # bring-up both PC tools do: 83 03 00 FF 00 FF 00 then 10 81
    def responder(tx):
        sid = tx[3]
        if sid == 0x83: return _resp_frame(bytes([0xC3, 0x03]))
        if sid == 0x10: return _resp_frame(bytes([0x50, 0x81]))
        return _resp_frame(bytes([0x7F, sid, 0x11]))

    t = KLineTransport("/dev/null")
    t.ser = FakeSerial(responder)
    sess = KWP2000Session(t)
    r = sess.enter_test_mode()
    assert r["ok"] and not r["cached"] and sess.test_mode
    sent = bytes(t.ser.tx)
    assert b"\x83\x03\x00\xff\x00\xff\x00" in sent   # AccessTimingParameter
    assert b"\x10\x81" in sent                       # StartDiagnosticSession 0x81
    before = len(t.ser.tx)
    assert sess.enter_test_mode()["cached"]          # idempotent: no more frames
    assert len(t.ser.tx) == before


def test_enter_test_mode_survives_rejection():
    # the 5AM may reject either request; both tools ignore the answers, so must we
    def responder(tx):
        return _resp_frame(bytes([0x7F, tx[3], 0x12]))

    t = KLineTransport("/dev/null")
    t.ser = FakeSerial(responder)
    sess = KWP2000Session(t)
    r = sess.enter_test_mode()                       # must not raise
    assert r["ok"] is False and r["nrc"] == 0x12
    assert sess.test_mode                            # armed anyway; tests may proceed


def test_actuator_set_frames():
    def responder(tx):
        return _resp_frame(bytes([0x70, tx[4]]))

    t = KLineTransport("/dev/null")
    t.ser = FakeSerial(responder)
    sess = KWP2000Session(t)
    assert sess.actuator_set(6, True)["on"] is True
    assert bytes(t.ser.tx) == b"\x83\x10\xf1\x30\x06\x07\xc1"   # 30 06 07
    t.ser.tx.clear()
    sess.actuator_set(6, False)
    assert bytes(t.ser.tx) == b"\x83\x10\xf1\x30\x06\x00\xba"   # 30 06 00


def test_dtc_status_decode():
    # status semantics from both PC tools: low nibble = kind, bit 0x20 = stored
    st = KWP2000Session._dtc_status
    assert st(0x21) == {"kind": 1, "stored": True, "warn": False, "bits": "00100001"}
    assert st(0x04)["stored"] is False and st(0x04)["kind"] == 4
    assert st(0x60)["stored"] is True and st(0x60)["warn"] is True
    assert st(0x03)["kind"] == 0          # 3 is not one of the 1/2/4/8 kinds


def test_read_dtc_carries_status_fields():
    def responder(tx):
        return _resp_frame(bytes([0x58, 0x01, 0x01, 0x23, 0x28]))

    t = KLineTransport("/dev/null")
    t.ser = FakeSerial(responder)
    d = KWP2000Session(t).read_dtc()["dtcs"][0]
    assert d["code"] == "P0123" and d["stored"] is True and d["kind"] == 8


def test_routine_results_probe():
    # 0x33 answers positively on ECUs that support it ...
    def ok(tx):
        return _resp_frame(bytes([0x73, 0x21, 0x02]))
    t = KLineTransport("/dev/null"); t.ser = FakeSerial(ok)
    r = KWP2000Session(t).routine_results(0x21)
    assert r["ok"] and r["data"] == "732102"

    # ... and is expected to reject on the 5AM: probe must not raise
    def nrc(tx):
        return _resp_frame(bytes([0x7F, 0x33, 0x11]))
    t2 = KLineTransport("/dev/null"); t2.ser = FakeSerial(nrc)
    r2 = KWP2000Session(t2).routine_results(0x21)
    assert r2["ok"] is False and r2["nrc"] == 0x11


def test_reset_tps_attaches_results():
    def responder(tx):
        sid = tx[3]
        if sid == 0x31: return _resp_frame(bytes([0x71, 0x21]))
        return _resp_frame(bytes([0x7F, sid, 0x11]))   # 0x33 rejected, as on a 5AM

    t = KLineTransport("/dev/null")
    t.ser = FakeSerial(responder)
    r = KWP2000Session(t).reset_tps()
    assert r["raw"].startswith("83f110") or "7121" in r["raw"]
    assert r["results"]["ok"] is False        # reported, never fatal


def test_ecu_id_all_options():
    """0x1A is asked with 0x80 and 0x00; a rejected option is just skipped."""
    def responder(tx):
        if tx[4] == 0x80:
            return _resp_frame(bytes([0x5A, 0x80]) + b"IAW5AMHW610")
        return _resp_frame(bytes([0x7F, 0x1A, 0x12]))

    t = KLineTransport("/dev/null")
    t.ser = FakeSerial(responder)
    sess = KWP2000Session(t)
    assert sess.read_ecu_identification_all() == "IAW5AMHW610"
    assert sess.ecu_hw == "IAW5AMHW610"
    assert list(sess.ecu_id_blocks) == [0x80]


def test_param_status_map_fields():
    p = Param(key="st", name="State", rli=0x49, map="motorstate", map_type="bits")
    assert p.map == "motorstate" and p.map_type == "bits"
    m = ParamMap(poll_interval_ms=100, poll_timeout_ms=100, params=[p])
    assert m.catalog()[0]["map"] == "motorstate"


def test_fast_init_no_crash():
    t = KLineTransport("/dev/null")
    t.ser = FakeSerial(lambda tx: b"")
    t.fast_init(low_ms=1, high_ms=1)  # must not raise


def test_slow_init():
    # 5-baud init: ECU keybytes (0x55 0x8F 0xEF) appear after the address bit-bang;
    # inverted-keyword write is echoed and answered with the inverted address (0xCC).
    class F(FakeSerial):
        def __init__(s):
            super().__init__(lambda tx: b"")
            s.resets = 0
        def reset_input_buffer(s):
            s.rx.clear()
            s.resets += 1
            if s.resets == 2:                 # right after the 5-baud address
                s.rx.extend(b"\x55\x8f\xef")
        def write(s, data):                   # inverted KW2 -> echo + inverted address
            s.tx.extend(data); s.rx.extend(data); s.rx.extend(b"\xcc")
            return len(data)

    t = KLineTransport("/dev/null")
    t.ser = F()
    assert t.slow_init(bit_ms=1) == (0x8F, 0xEF)


def _main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _main()
