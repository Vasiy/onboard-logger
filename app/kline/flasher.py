"""Firmware read/write for the Magneti Marelli IAW 5AM, over our own K-Line transport.

The message sequence, the header forms and the constants follow denandz/5am_util
(main.c, BSD 3-Clause, Copyright (c) 2018 DoI; see THIRD_PARTY.md) and were checked
against the byte traces of a write that completed on the real ECU. What differs from
the util: every reply is framed, checksummed and *checked* (the util only counts
bytes), "response pending" is handled by the protocol instead of a fixed sleep, and
anything that can fail before the erase is checked before the erase.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from . import fw_codec
from .kwp2000 import BAUD_SETTLE_S
from .transport import ECU_ADDR, TESTER_ADDR, KLineError, KLineTransport

# the util sends the speed switch, security access and block reads from "source 0x01"
SRC_SECURE = 0x01

WRITE_BAUD = 38400
READ_BAUD = 64200
CHUNK = 254                       # data bytes per 0x36 block on write
READ_CHUNK = 0x20                 # bytes per 0x36 0x21 request on read
READ_BLOCK = 0x4000
READ_BLOCKS = 20
READ_SKIP = range(2, 6)           # blocks the ECU never serves; the util fills them with 0xFF

PENDING_ERASE_S = 90.0            # the ECU streams "pending" frames for the whole erase
PENDING_PROGRAM_S = 120.0
SEED_TRIES = 10
SLOW_REPLY_S = 3.0                # one reply may take this long (seed, pending frames)

Trace = Callable[[str, str, bytes], None]          # (step, "tx"|"rx", raw frame)
Progress = Callable[[int, int], None]              # (done, total)


class FlashError(KLineError):
    """A step failed. ``erased`` is True once the erase has been requested: from then
    on the ECU may hold no valid program and the write has to be repeated, powered."""

    def __init__(self, step: str, detail: str, *, erased: bool = False, nrc: int | None = None):
        super().__init__("%s: %s" % (step, detail))
        self.step = step
        self.detail = detail
        self.erased = erased
        self.nrc = nrc


@dataclass
class WriteResult:
    ident: bytes
    confirmed: bool       # the ECU finished the programming routine on the wire


def make_frame(data: bytes, *, src: int = TESTER_ADDR, tgt: int = ECU_ADDR,
               long: bool = False) -> bytes:
    """``[0x80|len] tgt src data cs``, or ``[0x80] tgt src len data cs`` when ``long`` or
    the data does not fit 6 bits -- the util uses both forms and the ECU is verified on both."""
    if long or len(data) > 0x3F:
        head = bytes([0x80, tgt, src, len(data)]) + data
    else:
        head = bytes([0x80 | len(data), tgt, src]) + data
    return head + bytes([sum(head) & 0xFF])


class _Bus:
    def __init__(self, t: KLineTransport, trace: Trace | None, erased=lambda: False):
        self.t = t
        self._trace = trace
        self._erased = erased

    def trace(self, step: str, kind: str, raw: bytes) -> None:
        if self._trace is not None:
            self._trace(step, kind, raw)

    def xchg(self, step: str, data: bytes, *, src: int = TESTER_ADDR, long: bool = False,
             expect: bytes = b"", pending_s: float = 0.0, timeout: float | None = None) -> bytes:
        frame = make_frame(data, src=src, long=long)
        self.trace(step, "tx", frame)
        try:
            self.t.send_raw(frame)
        except KLineError as e:
            raise FlashError(step, "send failed: %s" % e, erased=self._erased()) from e
        deadline = time.monotonic() + pending_s
        while True:
            try:
                fr = self.t.recv_frame(timeout)
            except KLineError as e:
                raise FlashError(step, str(e), erased=self._erased()) from e
            self.trace(step, "rx", fr.raw)
            d = fr.data
            if len(d) >= 3 and d[0] == 0x7F:
                if d[2] == 0x78 and time.monotonic() < deadline:
                    continue
                raise FlashError(step, "negative response %02X %02X" % (d[1], d[2]),
                                 erased=self._erased(), nrc=d[2])
            break
        if not d.startswith(expect):
            raise FlashError(step, "unexpected reply %s" % d.hex(), erased=self._erased())
        return d


def _open_link(bus: _Bus, t: KLineTransport, expect_ident: bytes) -> bytes:
    t.fast_init(26, 25)
    bus.xchg("start_comm", b"\x81", expect=b"\xC1")
    bus.xchg("diag_session", b"\x10\x85", expect=b"\x50\x85")
    ident = bus.xchg("identification", b"\x1A\x80", expect=b"\x5A\x80")
    if expect_ident and expect_ident not in ident:
        raise FlashError("identification", "not an %s ECU: %r" % (
            expect_ident.decode("ascii", "replace"), ident[:40]))
    return ident


def _switch_baud(t: KLineTransport, baud: int) -> None:
    time.sleep(BAUD_SETTLE_S)      # the ECU is still clocking out its last stop bit at the old rate
    t.set_baud(baud)


def _login(bus: _Bus) -> None:
    seed = b""
    for attempt in range(SEED_TRIES):
        try:
            d = bus.xchg("seed", b"\x27\x01", src=SRC_SECURE, expect=b"\x67\x01", timeout=SLOW_REPLY_S)
        except FlashError as e:
            if e.nrc is None or attempt == SEED_TRIES - 1:
                raise
            time.sleep(1.0)        # "give the device a chance to come up with a seed"
            continue
        if len(d) == 6:
            seed = d[2:]
            break
        time.sleep(1.0)
    if not seed:
        raise FlashError("seed", "no usable seed after %d tries" % SEED_TRIES)
    key = fw_codec.calc_key(int.from_bytes(seed, "big"))
    bus.xchg("key", b"\x27\x02" + key.to_bytes(4, "big"), src=SRC_SECURE, expect=b"\x67\x02")


def write(t: KLineTransport, image: bytes, *, progress: Progress | None = None,
          trace: Trace | None = None, cancelled: Callable[[], bool] | None = None,
          expect_ident: bytes = b"IAW5AM") -> WriteResult:
    """Everything up to and including the writer/date records is reversible; the
    erase is requested only after the whole payload was built, the link, the
    identification, the speed switch and the security access all answered."""
    payload = fw_codec.build_payload(image)          # raises on a wrong size, before any traffic
    checksum = fw_codec.checksum16(image)
    state = {"erase": False}
    bus = _Bus(t, trace, erased=lambda: state["erase"])

    ident = _open_link(bus, t, expect_ident)
    bus.xchg("programming_session", b"\x10\x85\x03", expect=b"\x50\x85")
    _switch_baud(t, WRITE_BAUD)
    bus.xchg("timing", bytes([0x83, 0x03, 0x1E, 0x02, 0x0A, 0x14, 0x00]), expect=b"\xC3")
    _login(bus)
    bus.xchg("writer", b"\x3B\x98\x20", expect=b"\x7B\x98")
    bus.xchg("write_date", b"\x3B\x99\x20\x18\x01\x01", expect=b"\x7B\x99")
    if cancelled is not None and cancelled():
        raise FlashError("cancelled", "stopped before the erase")

    state["erase"] = True            # from here a failure may leave the ECU without a program
    bus.xchg("erase_routine", b"\x31\x02\x00\x40\x00\x04\xFF\xFF", expect=b"\x71\x02")
    bus.xchg("erase", b"\x33\x02", expect=b"\x73\x02", pending_s=PENDING_ERASE_S, timeout=SLOW_REPLY_S)
    bus.xchg("request_download", b"\x34\x00\x40\x00\x33\x04\xC0\x00", expect=b"\x74")

    total = len(payload)
    done = 0
    while done < total:
        chunk = payload[done:done + CHUNK]
        bus.xchg("transfer", b"\x36" + chunk, long=True, expect=b"\x76")
        done += len(chunk)
        if progress is not None:
            progress(done, total)
    bus.xchg("transfer_exit", b"\x37", long=True, expect=b"\x77")
    bus.xchg("program_routine", b"\x31\x01\x00\x40\x00\x04\xFF\xFF" + checksum.to_bytes(2, "big"),
             long=True, expect=b"\x71\x01")
    # The util stops here after a fixed 2 s. The ECU answers "pending" while it programs;
    # a positive reply is the only proof it finished, so wait for it -- but not fatally:
    # whether it ever sends one is unconfirmed, and the read-back is the real check.
    confirmed = True
    try:
        bus.xchg("program", b"\x33\x01", expect=b"\x73", pending_s=PENDING_PROGRAM_S, timeout=SLOW_REPLY_S)
    except FlashError as e:
        if e.nrc is not None and e.nrc != 0x78:
            raise
        confirmed = False
    return WriteResult(ident=ident, confirmed=confirmed)


def read(t: KLineTransport, *, progress: Progress | None = None, trace: Trace | None = None,
         cancelled: Callable[[], bool] | None = None, read_baud: int = READ_BAUD,
         expect_ident: bytes = b"IAW5AM") -> bytes:
    """Non-destructive: dump the image, 0xFF for the four blocks the ECU does not serve."""
    bus = _Bus(t, trace)
    _open_link(bus, t, expect_ident)
    bus.xchg("read_session", b"\x10\x0C\x0C\x09", src=SRC_SECURE, expect=b"\x50")
    _switch_baud(t, read_baud)
    _login(bus)

    out = bytearray()
    wire_blocks = [b for b in range(READ_BLOCKS) if b not in READ_SKIP]
    steps_total = len(wire_blocks) * (READ_BLOCK // READ_CHUNK)
    steps = 0
    for block in range(READ_BLOCKS):
        if block in READ_SKIP:
            out += b"\xFF" * READ_BLOCK
            continue
        bus.xchg("block", bytes([0x36, 0x11, 0x00, 0xFE, 0x02, 0x01, block]),
                 src=SRC_SECURE, expect=b"\x76\x11")
        for off in range(0x4000, 0x8000, READ_CHUNK):
            if cancelled is not None and cancelled():
                raise FlashError("cancelled", "read stopped")
            d = bus.xchg("read", bytes([0x36, 0x21, 0x00, off >> 8, off & 0xFF, READ_CHUNK]),
                         src=SRC_SECURE, expect=b"\x76\x21")
            if len(d) != 6 + READ_CHUNK:
                raise FlashError("read", "partial block: %d bytes at block %d offset %#x" % (
                    len(d), block, off - 0x4000))
            out += d[6:]
            steps += 1
            if progress is not None:
                progress(steps, steps_total)
    if len(out) != fw_codec.IMAGE_SIZE:
        raise FlashError("read", "image is %d bytes, expected %d" % (len(out), fw_codec.IMAGE_SIZE))
    return bytes(out)
