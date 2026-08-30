"""K-Line (ISO 14230 / KWP2000) serial transport for Magneti Marelli IAW 5AM.

Physical layer (confirmed against denandz/5am_util):
  * 10400 baud, 8N1, no flow control
  * single wire, half-duplex -> everything we transmit is echoed back on RX
    before the ECU replies; the echo must be read and discarded.
  * fast-init: pull the K line low ~25 ms (serial BREAK), release ~25 ms.

Frame format (both directions), KWP2000 with address information::

    [FMT] [TGT] [SRC] [DATA ...] [CS]

    FMT = 0x80 | len   (len = number of DATA bytes; if the 6 low bits are 0 a
                        separate length byte follows the address bytes)
    TGT = 0x10 (ECU)   SRC = 0xF1 (tester)
    CS  = (sum of all preceding bytes) & 0xFF   -- additive checksum, not CRC
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import serial

ECU_ADDR = 0x10
TESTER_ADDR = 0xF1

BAUD = 10400
BYTE_TIME = 10.0 / BAUD  # ~0.96 ms per byte at 8N1 (start+8+stop)


class KLineError(Exception):
    """Base class for transport errors."""


class KLineTimeout(KLineError):
    """Expected bytes did not arrive within the deadline."""


class ChecksumError(KLineError):
    def __init__(self, expected: int, got: int, raw: bytes):
        super().__init__(f"checksum mismatch: expected {expected:#04x}, got {got:#04x}")
        self.expected = expected
        self.got = got
        self.raw = raw


@dataclass
class Frame:
    fmt: int
    target: int
    source: int
    data: bytes
    checksum: int
    raw: bytes = field(repr=False, default=b"")

    @property
    def sid(self) -> int:
        return self.data[0] if self.data else -1

    def hex(self) -> str:
        return self.raw.hex()


class KLineTransport:
    """Blocking half-duplex K-Line transport built on a pyserial port."""

    def __init__(
        self,
        port: str,
        *,
        baud: int = BAUD,
        echo: bool = True,
        response_timeout: float = 1.2,
        inter_byte_timeout: float = 0.3,
    ):
        self.port_name = port
        self.baud = baud
        self.echo = echo
        self.response_timeout = response_timeout
        self.inter_byte_timeout = inter_byte_timeout
        self.ser: serial.Serial | None = None

    # -- lifecycle ---------------------------------------------------------
    def open(self) -> None:
        # pyserial sets a non-standard baud via TCSETS2/BOTHER on Linux.
        self.ser = serial.Serial(
            port=self.port_name,
            baudrate=self.baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=self.inter_byte_timeout,
            write_timeout=1.0,
            rtscts=False,
            dsrdtr=False,
            xonxoff=False,
        )
        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()

    def close(self) -> None:
        if self.ser is not None:
            try:
                self.ser.close()
            finally:
                self.ser = None

    def __enter__(self) -> "KLineTransport":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- fast init ---------------------------------------------------------
    def fast_init(self, low_ms: int = 25, high_ms: int = 25) -> None:
        """ISO 14230 fast init: BREAK (K low) then release (K high)."""
        assert self.ser is not None, "port not open"
        self.ser.reset_input_buffer()
        self.ser.break_condition = True
        time.sleep(low_ms / 1000.0)
        self.ser.break_condition = False
        time.sleep(high_ms / 1000.0)
        self.ser.reset_input_buffer()

    # -- slow (5-baud) init ------------------------------------------------
    def slow_init(self, address: int = 0x33, bit_ms: int = 200) -> tuple[int, int]:
        """ISO 14230 / ISO 9141-2 five-baud slow init.

        Bit-bangs ``address`` (0x33 for IAW/OBD) on the K line at 5 baud
        (200 ms/bit) via BREAK — idle high, start bit low, 8 data bits LSB-first,
        stop bit high — then reads the ECU keybytes (0x55 sync, KW1, KW2), echoes
        the inverted KW2 back and reads the inverted address. Returns (KW1, KW2).

        ~2 s to send the address (vs ~50 ms fast-init) but some ECUs only accept
        this wake-up. No StartCommunication follows — the handshake establishes the
        session.
        """
        assert self.ser is not None, "port not open"
        bit = bit_ms / 1000.0
        self.ser.reset_input_buffer()
        self.ser.break_condition = True             # start bit (line low)
        time.sleep(bit)
        for i in range(8):                          # 8 data bits, LSB first
            self.ser.break_condition = ((address >> i) & 1) == 0  # 1->high, 0->low
            time.sleep(bit)
        self.ser.break_condition = False            # stop bit (line high / idle)
        time.sleep(bit)
        self.ser.reset_input_buffer()               # drop the break-toggling RX noise
        # ECU replies at the normal baud after W1 (~60..300 ms): 0x55, KW1, KW2
        deadline = time.monotonic() + 0.6
        sync = self._read_exact(1, deadline)[0]
        if sync != 0x55:
            raise KLineError(f"slow init: expected 0x55 sync, got {sync:#04x}")
        kw = self._read_exact(2, deadline)
        kw1, kw2 = kw[0], kw[1]
        time.sleep(0.030)                           # W4
        self.ser.reset_input_buffer()
        self.ser.write(bytes([(~kw2) & 0xFF]))      # tester -> inverted KW2
        self.ser.flush()
        d2 = time.monotonic() + 0.5
        if self.echo:
            self._read_exact(1, d2)                 # discard our own echo
        self._read_exact(1, d2)                      # ECU -> inverted address (~0xCC)
        return kw1, kw2

    # -- raw io ------------------------------------------------------------
    def _read_exact(self, n: int, deadline: float) -> bytes:
        assert self.ser is not None, "port not open"
        buf = bytearray()
        while len(buf) < n:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise KLineTimeout(
                    f"read {len(buf)}/{n} bytes before timeout: {bytes(buf).hex()}"
                )
            self.ser.timeout = min(self.inter_byte_timeout, max(0.01, remaining))
            chunk = self.ser.read(n - len(buf))
            if chunk:
                buf.extend(chunk)
        return bytes(buf)

    @staticmethod
    def checksum8(payload: bytes) -> int:
        return sum(payload) & 0xFF

    def build_frame(
        self, data: bytes, target: int = ECU_ADDR, source: int = TESTER_ADDR,
        with_addr: bool = True,
    ) -> bytes:
        if not 1 <= len(data) <= 0x3F:
            raise ValueError("data length must be 1..63 for single-byte format")
        if with_addr:  # [0x80|len][tgt][src][data][cs]
            head = bytes([0x80 | len(data), target, source]) + data
        else:          # short header, no address: [len][data][cs]
            head = bytes([len(data)]) + data
        return head + bytes([self.checksum8(head)])

    # -- framed exchange ---------------------------------------------------
    def send_frame(self, data: bytes, target: int = ECU_ADDR, source: int = TESTER_ADDR,
                   with_addr: bool = True) -> bytes:
        assert self.ser is not None, "port not open"
        frame = self.build_frame(data, target, source, with_addr)
        self.ser.reset_input_buffer()
        self.ser.write(frame)
        self.ser.flush()
        if self.echo:
            # discard our own transmission looped back on the single wire
            self._read_exact(len(frame), time.monotonic() + self.response_timeout)
        return frame

    def recv_frame(self, timeout: float | None = None) -> Frame:
        deadline = time.monotonic() + (self.response_timeout if timeout is None else timeout)
        fmt = self._read_exact(1, deadline)[0]
        raw = bytearray([fmt])
        mode = fmt & 0xC0
        ln = fmt & 0x3F
        target = source = 0
        if mode & 0x80:  # address information present
            tgt_src = self._read_exact(2, deadline)
            target, source = tgt_src[0], tgt_src[1]
            raw.extend(tgt_src)
        if ln == 0:  # length carried in a separate byte
            lb = self._read_exact(1, deadline)[0]
            raw.append(lb)
            ln = lb
        data = self._read_exact(ln, deadline)
        raw.extend(data)
        cs = self._read_exact(1, deadline)[0]
        expected = self.checksum8(bytes(raw))
        raw.append(cs)
        if cs != expected:
            raise ChecksumError(expected, cs, bytes(raw))
        return Frame(fmt, target, source, bytes(data), cs, bytes(raw))

    def request(self, data: bytes, target: int = ECU_ADDR, source: int = TESTER_ADDR) -> tuple[bytes, Frame]:
        """Send a request, discard echo, return (tx_bytes, response Frame)."""
        tx = self.send_frame(data, target, source)
        return tx, self.recv_frame()
