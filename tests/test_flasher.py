"""Flasher against a scripted ECU that replays the byte traces of a write that completed
on the real bike (2026-10-18) and fails on demand. No hardware, no sleeping."""
import os
import sys
import time
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.kline import flasher as fl, fw_codec as fc  # noqa: E402
from app.kline.flasher import FlashError, make_frame  # noqa: E402
from app.kline.transport import KLineTransport  # noqa: E402

IDENT = b"23ECCLGPSMEIAW5AMHW610&".ljust(61, b" ")


def synthetic_image() -> bytes:
    return bytes(((i * 31) + (i >> 8) * 7 + 5) & 0xFF for i in range(fc.IMAGE_SIZE))


@contextmanager
def fast_clock():
    real = time.sleep
    old = (fl.SLOW_REPLY_S, fl.PENDING_ERASE_S, fl.PENDING_PROGRAM_S)
    time.sleep = lambda s: None
    fl.SLOW_REPLY_S, fl.PENDING_ERASE_S, fl.PENDING_PROGRAM_S = 0.05, 0.3, 0.3
    try:
        yield
    finally:
        time.sleep = real
        fl.SLOW_REPLY_S, fl.PENDING_ERASE_S, fl.PENDING_PROGRAM_S = old


class FakeSerial:
    def __init__(self):
        self.rx = bytearray()
        self.timeout = 0.1
        self.write_timeout = 1.0
        self.break_condition = False
        self.baudrate = 10400
        self.responder = None

    def reset_input_buffer(self):
        self.rx.clear()

    def reset_output_buffer(self):
        pass

    def write(self, data):
        self.rx.extend(data)                         # single-wire echo
        self.rx.extend(self.responder(bytes(data)))
        return len(data)

    def flush(self):
        pass

    def read(self, n):
        take = self.rx[:n]
        del self.rx[:n]
        return bytes(take)

    def close(self):
        pass


class FakeEcu:
    def __init__(self, image=None, ident=IDENT):
        self.ser = None
        self.baud = 10400
        self.image = image
        self.ident = ident
        self.requests: list[bytes] = []
        self.frames: list[bytes] = []
        self.erase_requested = False
        self.downloading = False
        self.payload = bytearray()
        self.checksum = None
        self.seed = 0x718E718F
        self.seed_nrcs = 0
        self.erase_pending = 300
        self.final_ok = True               # a positive 73 01 after the programming "pending"
        self.fault = None                  # (prefix, nth, mode)
        self.block = 0
        self._seen: dict[bytes, int] = {}

    # -- plumbing ---------------------------------------------------------
    @staticmethod
    def parse(tx):
        ln = tx[0] & 0x3F
        if ln == 0:
            ln, data = tx[3], tx[4:-1]
        else:
            data = tx[3:-1]
        assert sum(tx[:-1]) & 0xFF == tx[-1], "tester sent a bad checksum"
        assert len(data) == ln
        return tx[1], tx[2], data

    @staticmethod
    def frame(data, to):
        return make_frame(data, src=0x10, tgt=to, long=(to == fl.SRC_SECURE))

    def connect(self) -> KLineTransport:
        t = KLineTransport("/dev/null", response_timeout=0.02, inter_byte_timeout=0.005)
        t.ser = FakeSerial()
        t.ser.responder = self.respond
        self.ser = t.ser
        return t

    # -- the ECU ----------------------------------------------------------
    def respond(self, tx):
        tgt, src, data = self.parse(tx)
        self.requests.append(bytes(data))
        self.frames.append(bytes(tx))
        if self.ser.baudrate != self.baud:
            return b""                                    # the tester is on the wrong speed
        if self.fault:
            prefix, nth, mode = self.fault
            if data.startswith(prefix):
                k = self._seen.get(prefix, 0)
                self._seen[prefix] = k + 1
                if k == nth:
                    if mode == "silent":
                        return b""
                    if mode == "nrc":
                        return self.frame(bytes([0x7F, data[0], 0x22]), src)
                    if mode == "badcs":
                        f = bytearray(self.reply(data, src))
                        f[-1] ^= 0xFF
                        return bytes(f)
        return self.reply(data, src)

    def reply(self, d, src):
        sid = d[0]
        f = lambda body: self.frame(bytes(body), src)       # noqa: E731
        if sid == 0x81:
            return f(b"\xC1\xEF\x8F")
        if sid == 0x10:
            if d[1:] == b"\x85\x03":
                self.baud = 38400
            elif d[1:] == b"\x0C\x0C\x09":
                self.baud = fl.READ_BAUD
                return f(b"\x50")
            return f(b"\x50\x85")
        if sid == 0x1A:
            return f(b"\x5A\x80" + self.ident)
        if sid == 0x83:
            return f(b"\xC3\x03")
        if sid == 0x27:
            if d[1] == 1:
                if self.seed_nrcs:
                    self.seed_nrcs -= 1
                    return f(b"\x7F\x27\x21")
                return f(b"\x67\x01" + self.seed.to_bytes(4, "big"))
            if int.from_bytes(d[2:6], "big") != fc.calc_key(self.seed):
                return f(b"\x7F\x27\x35")
            return f(b"\x67\x02\x34")
        if sid == 0x3B:
            return f(bytes([0x7B, d[1]]))
        if sid == 0x31:
            if d[1] == 2:
                self.erase_requested = True
                return f(b"\x71\x02")
            self.checksum = int.from_bytes(d[-2:], "big")
            return f(b"\x71\x01")
        if sid == 0x33:
            pending = f(b"\x7F\x33\x78")
            if d[1] == 2:
                return pending * self.erase_pending + f(b"\x73\x02")
            return pending * 3 + (f(b"\x73\x01") if self.final_ok else b"")
        if sid == 0x34:
            self.downloading = True
            return f(b"\x74\xFF")
        if sid == 0x36:
            if self.downloading:
                self.payload += d[1:]
                return f(b"\x76")
            if d[1] == 0x11:
                self.block = d[-1]
                return f(b"\x76\x11\x02")
            off = (d[3] << 8 | d[4]) - 0x4000
            base = self.block * 0x4000 + off
            return f(b"\x76\x21\x40\x00\x00\x20" + self.image[base:base + 0x20])
        if sid == 0x37:
            return f(b"\x77")
        raise AssertionError("tester sent something the ECU does not know: %s" % d.hex())


def run_write(ecu, image=None, **kw):
    image = synthetic_image() if image is None else image
    t = ecu.connect()
    with fast_clock():
        return fl.write(t, image, **kw)


def run_read(ecu, **kw):
    t = ecu.connect()
    with fast_clock():
        return fl.read(t, **kw)


def expect_error(fn):
    try:
        fn()
    except FlashError as e:
        return e
    raise AssertionError("expected a FlashError")


# --------------------------------------------------------------------------
def test_frame_forms_match_the_bytes_the_util_put_on_the_bike():
    h = bytes.fromhex
    assert make_frame(b"\x81") == h("81 10 F1 81 03")
    assert make_frame(b"\x10\x85") == h("82 10 F1 10 85 18")
    assert make_frame(b"\x1A\x80") == h("82 10 F1 1A 80 1D")
    assert make_frame(b"\x10\x85\x03") == h("83 10 F1 10 85 03 1C")
    assert make_frame(bytes.fromhex("83 03 1E 02 0A 14 00")) == h("87 10 F1 83 03 1E 02 0A 14 00 4C")
    assert make_frame(b"\x27\x01", src=1) == h("82 10 01 27 01 BB")
    assert make_frame(bytes.fromhex("27 02 E4 46 69 27"), src=1) == h("86 10 01 27 02 E4 46 69 27 7A")
    assert make_frame(b"\x3B\x98\x20") == h("83 10 F1 3B 98 20 77")
    assert make_frame(bytes.fromhex("3B 99 20 18 01 01")) == h("86 10 F1 3B 99 20 18 01 01 95")
    assert make_frame(bytes.fromhex("31 02 00 40 00 04 FF FF")) == h("88 10 F1 31 02 00 40 00 04 FF FF FE")
    assert make_frame(b"\x33\x02") == h("82 10 F1 33 02 B8")
    assert make_frame(bytes.fromhex("34 00 40 00 33 04 C0 00")) == h("88 10 F1 34 00 40 00 33 04 C0 00 F4")
    assert make_frame(b"\x37", long=True) == h("80 10 F1 01 37 B9")
    assert make_frame(bytes.fromhex("31 01 00 40 00 04 FF FF ED 02"), long=True) == \
        h("80 10 F1 0A 31 01 00 40 00 04 FF FF ED 02 EE")
    assert make_frame(b"\x33\x01") == h("82 10 F1 33 01 B7")
    # a full write block: length byte 0xFF = 254 bytes + the 0x36
    blk = make_frame(b"\x36" + bytes(254), long=True)
    assert blk[:5] == h("80 10 F1 FF 36") and len(blk) == 4 + 255 + 1
    # ECU replies use the same forms
    assert FakeEcu.frame(b"\x50\x85", 0xF1) == h("82 F1 10 50 85 58")
    assert FakeEcu.frame(b"\x67\x02\x34", 1) == h("80 01 10 03 67 02 34 31")


def test_write_walks_the_verified_sequence_and_delivers_the_payload():
    img = synthetic_image()
    ecu = FakeEcu()
    progress = []
    res = run_write(ecu, img, progress=lambda d, t: progress.append((d, t)))
    assert res.confirmed and res.ident.startswith(b"\x5A\x80")
    heads = [r[:1] + (r[1:2] if r[0] in (0x10, 0x27, 0x3B, 0x31, 0x33) else b"")
             for r in ecu.requests if not (r[0] == 0x36)]
    assert heads == [b"\x81", b"\x10\x85", b"\x1A", b"\x10\x85", b"\x83", b"\x27\x01", b"\x27\x02",
                     b"\x3B\x98", b"\x3B\x99", b"\x31\x02", b"\x33\x02", b"\x34", b"\x37",
                     b"\x31\x01", b"\x33\x01"]
    assert bytes(ecu.payload) == fc.build_payload(img)
    assert ecu.checksum == fc.checksum16(img)
    assert progress[-1] == (fc.ENC_LEN, fc.ENC_LEN)
    chunks = [r for r in ecu.requests if r[0] == 0x36]
    assert len(chunks) == -(-fc.ENC_LEN // fl.CHUNK) and all(len(c) <= 255 for c in chunks)
    # the erase is requested only after the security access and the writer records
    order = [r[:2] for r in ecu.requests]
    assert order.index(b"\x31\x02") > order.index(b"\x27\x02") > order.index(b"\x1A\x80")


def test_unconfirmed_programming_is_reported_not_failed():
    ecu = FakeEcu()
    ecu.final_ok = False                 # pending frames, then silence: what the util saw
    res = run_write(ecu)
    assert res.confirmed is False and bytes(ecu.payload) == fc.build_payload(synthetic_image())


def test_every_failure_before_the_erase_leaves_the_ecu_untouched():
    steps = [(b"\x81", "start_comm"), (b"\x10\x85", "diag_session"), (b"\x1A", "identification"),
             (b"\x83", "timing"), (b"\x27\x01", "seed"), (b"\x27\x02", "key"),
             (b"\x3B\x98", "writer"), (b"\x3B\x99", "write_date")]
    for prefix, name in steps:
        for mode in ("silent", "nrc", "badcs"):
            if name == "seed" and mode == "nrc":
                continue                      # a busy ECU is retried on purpose, see the seed test
            ecu = FakeEcu()
            ecu.fault = (prefix, 0, mode)
            err = expect_error(lambda: run_write(ecu))
            assert err.step == name, (prefix, mode, err.step)
            assert not err.erased and not ecu.erase_requested, (prefix, mode)
            assert not ecu.payload


def test_programming_session_failure_is_a_pre_erase_failure_too():
    ecu = FakeEcu()
    ecu.fault = (b"\x10\x85\x03", 0, "silent")
    err = expect_error(lambda: run_write(ecu))
    assert err.step == "programming_session" and not ecu.erase_requested


def test_a_missed_speed_switch_stops_before_the_erase():
    ecu = FakeEcu()
    orig = fl._switch_baud
    fl._switch_baud = lambda t, baud: None          # the tester stays on 10400
    try:
        err = expect_error(lambda: run_write(ecu))
    finally:
        fl._switch_baud = orig
    assert err.step == "timing" and not ecu.erase_requested


def test_wrong_ecu_is_refused_before_any_security_traffic():
    ecu = FakeEcu(ident=b"96518407B  XYZ".ljust(61))
    err = expect_error(lambda: run_write(ecu))
    assert err.step == "identification" and not err.erased
    assert not any(r[0] in (0x27, 0x31) for r in ecu.requests)


def test_wrong_image_size_sends_nothing():
    ecu = FakeEcu()
    t = ecu.connect()
    try:
        fl.write(t, b"\0" * 100)
    except ValueError:
        pass
    else:
        raise AssertionError("accepted a short image")
    assert ecu.requests == []


def test_cancel_is_honoured_up_to_the_erase_and_no_further():
    ecu = FakeEcu()
    err = expect_error(lambda: run_write(ecu, cancelled=lambda: True))
    assert err.step == "cancelled" and not ecu.erase_requested
    ecu = FakeEcu()
    asked = []
    run_write(ecu, cancelled=lambda: asked.append(1) or False)
    assert len(asked) == 1               # checked once, before the point of no return


def test_failures_after_the_erase_are_flagged_as_dangerous():
    n_chunks = -(-fc.ENC_LEN // fl.CHUNK)
    for fault, step in (((b"\x31\x02", 0, "nrc"), "erase_routine"),
                        ((b"\x34", 0, "silent"), "request_download"),
                        ((b"\x36", 100, "silent"), "transfer"),
                        ((b"\x36", n_chunks - 1, "badcs"), "transfer"),
                        ((b"\x37", 0, "nrc"), "transfer_exit"),
                        ((b"\x31\x01", 0, "silent"), "program_routine")):
        ecu = FakeEcu()
        ecu.fault = fault
        err = expect_error(lambda: run_write(ecu))
        assert err.step == step and err.erased, (fault, err.step, err.erased)


def test_erase_pending_flood_is_waited_out_and_a_stuck_erase_times_out():
    ecu = FakeEcu()
    ecu.erase_pending = 2000
    assert run_write(ecu).confirmed
    ecu = FakeEcu()
    ecu.fault = (b"\x33\x02", 0, "silent")
    err = expect_error(lambda: run_write(ecu))
    assert err.step == "erase" and err.erased


def test_seed_is_retried_on_a_busy_ecu_and_gives_up_after_ten():
    ecu = FakeEcu()
    ecu.seed_nrcs = 3
    run_write(ecu)
    assert sum(1 for r in ecu.requests if r == b"\x27\x01") == 4
    ecu = FakeEcu()
    ecu.seed_nrcs = 99
    err = expect_error(lambda: run_write(ecu))
    assert err.step == "seed" and sum(1 for r in ecu.requests if r == b"\x27\x01") == fl.SEED_TRIES


def test_read_returns_the_image_with_ff_in_the_four_unserved_blocks():
    img = bytearray(synthetic_image())
    ecu = FakeEcu(image=bytes(img))
    progress = []
    got = run_read(ecu, progress=lambda d, t: progress.append((d, t)))
    for b in fl.READ_SKIP:
        img[b * 0x4000:(b + 1) * 0x4000] = b"\xFF" * 0x4000
    assert got == bytes(img)
    assert progress[-1][0] == progress[-1][1] == 16 * 512
    assert ecu.baud == fl.READ_BAUD
    assert not ecu.erase_requested and not ecu.payload


def test_read_can_be_cancelled_and_reports_a_partial_block():
    ecu = FakeEcu(image=synthetic_image())
    n = []
    err = expect_error(lambda: run_read(ecu, cancelled=lambda: n.append(1) or len(n) > 40))
    assert err.step == "cancelled"
    ecu = FakeEcu(image=synthetic_image())
    ecu.fault = (b"\x36\x21", 7, "silent")
    err = expect_error(lambda: run_read(ecu))
    assert err.step == "read"


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok ", name)
            n += 1
    print("%d passed" % n)
