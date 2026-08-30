"""KWP2000 (ISO 14230) service layer for the IAW 5AM ECU.

Only the services needed for live-data logging and the Testing tab are
implemented:
  * 0x81 StartCommunication
  * 0x83 AccessTimingParameter      (test mode: timing params, as the PC tools do)
  * 0x10 StartDiagnosticSession     (test mode: session 0x81)
  * 0x1A ReadEcuIdentification      (grabs the "IAW 5AM ..." identity string)
  * 0x3E TesterPresent              (keep-alive)
  * 0x21 ReadDataByLocalIdentifier  (the live-data poll)
  * 0x18 / 0x14                     (read / clear DTCs)
  * 0x31 / 0x30                     (adaptation resets, actuator tests)
  * 0x20 / 0x82                     (teardown)

Security access (0x27 seed/key) is intentionally omitted: it is only required
for flashing/EEPROM, not for reading measurements.
"""

from __future__ import annotations

import re
import time

from .transport import ChecksumError, KLineError, KLineTimeout, KLineTransport, Frame

# Service IDs
SID_START_COMM = 0x81
SID_START_SESSION = 0x10
SID_READ_ECU_ID = 0x1A
SID_TESTER_PRESENT = 0x3E
SID_READ_DATA_BY_LID = 0x21
SID_READ_DTC = 0x18       # ReadDiagnosticTroubleCodesByStatus
SID_CLEAR_DTC = 0x14      # ClearDiagnosticInformation
SID_START_ROUTINE = 0x31  # StartRoutineByLocalIdentifier
SID_IO_CONTROL = 0x30     # InputOutputControlByLocalIdentifier
SID_ACCESS_TIMING = 0x83  # AccessTimingParameter
SID_ROUTINE_RESULTS = 0x33  # RequestRoutineResultsByLocalIdentifier
SID_STOP_SESSION = 0x20   # StopDiagnosticSession
SID_STOP_COMM = 0x82      # StopCommunication

POSITIVE = 0x40  # positive response = request SID + 0x40
NEG_RESPONSE = 0x7F

DIAG_SESSION = 0x81     # standard diagnostic session — what GuzziDiag/IAWDiag start
PROG_SESSION = 0x85     # programming session (firmware path only; 5AM rejects it here)
DEFAULT_SESSION = DIAG_SESSION
DEFAULT_ECU_ID_OPTION = 0x80
# identification options worth asking for: 0x80 is what we have always used,
# 0x00 is what IAWDiag asks its IAW-5AM-family ECUs for
ECU_ID_OPTIONS = (0x80, 0x00)

# AccessTimingParameter payload both reference tools send right after 0x81:
# subfunction 0x03 (set to given values) + P2min P2max P3min P3max P4min.
TIMING_PARAMS = bytes([0x03, 0x00, 0xFF, 0x00, 0xFF, 0x00])

# ISO 14230 negative response codes worth naming
NRC = {
    0x10: "generalReject",
    0x11: "serviceNotSupported",
    0x12: "subFunctionNotSupported",
    0x21: "busyRepeatRequest",
    0x22: "conditionsNotCorrect",
    0x31: "requestOutOfRange",
    0x33: "securityAccessDenied",
    0x78: "responsePending",
}


def _short_error(exc: Exception) -> str:
    """One-word tag for a failed best-effort request (shown in the UI badge)."""
    if isinstance(exc, KLineTimeout):
        return "timeout"
    if isinstance(exc, ChecksumError):
        return "bad_frame"
    return type(exc).__name__


class NegativeResponse(KLineError):
    def __init__(self, request_sid: int, code: int):
        name = NRC.get(code, "unknown")
        super().__init__(
            f"negative response to SID {request_sid:#04x}: {code:#04x} ({name})"
        )
        self.request_sid = request_sid
        self.code = code


class KWP2000Session:
    def __init__(self, transport: KLineTransport, *, response_pending_retries: int = 5):
        self.t = transport
        self.response_pending_retries = response_pending_retries
        self.ecu_id: str | None = None
        self.ecu_hw: str = ""      # parsed hardware version, e.g. "IAW5AMHW610"
        self.ecu_id_raw: bytes = b""  # raw identification payload (for the .txt)
        self.ecu_id_blocks: dict[int, bytes] = {}  # every 0x1A option that answered
        self.last_tx_at = 0.0
        self.test_mode = False     # diagnostic session started (see enter_test_mode)
        self.test_mode_detail = ""

    # -- low level ---------------------------------------------------------
    def _service(self, data: bytes) -> tuple[bytes, Frame]:
        """Send a service request; return (tx_bytes, positive-response frame).

        Handles the 0x7F .. 0x78 "response pending" pattern by re-reading.
        """
        req_sid = data[0]
        tx, resp = self.t.request(data)
        self.last_tx_at = time.monotonic()
        tries = 0
        while (
            len(resp.data) >= 3
            and resp.data[0] == NEG_RESPONSE
            and resp.data[2] == 0x78
            and tries < self.response_pending_retries
        ):
            resp = self.t.recv_frame()  # ECU is still working; wait for the real one
            tries += 1
        if resp.data and resp.data[0] == NEG_RESPONSE:
            code = resp.data[2] if len(resp.data) >= 3 else 0
            raise NegativeResponse(req_sid, code)
        if resp.sid != (req_sid + POSITIVE) & 0xFF:
            raise KLineError(
                f"unexpected response SID {resp.sid:#04x} to request {req_sid:#04x}"
            )
        return tx, resp

    # -- services ----------------------------------------------------------
    def start_communication(self) -> Frame:
        return self._service(bytes([SID_START_COMM]))[1]

    def start_diagnostic_session(self, session: int = DEFAULT_SESSION) -> Frame:
        return self._service(bytes([SID_START_SESSION, session]))[1]

    def access_timing_parameters(self, params: bytes = TIMING_PARAMS) -> Frame:
        """0x83 03 00 FF 00 FF 00 — set P2/P3/P4 to their maximum windows."""
        return self._service(bytes([SID_ACCESS_TIMING]) + params)[1]

    def stop_diagnostic_session(self) -> Frame:
        return self._service(bytes([SID_STOP_SESSION]))[1]

    def stop_communication(self) -> Frame:
        return self._service(bytes([SID_STOP_COMM]))[1]

    # -- test mode ---------------------------------------------------------
    def enter_test_mode(self) -> dict:
        """Arm the diagnostic session used by actuator tests and adaptation resets.

        This is the bring-up both reference tools do right after StartCommunication
        (GuzziDiag ``sub_415D1B``, IAWDiag @0x4954E4): ``83 03 00 FF 00 FF 00``
        (AccessTimingParameter) then ``10 81`` (StartDiagnosticSession, session
        0x81 — *not* the 0x85 programming session, which the 5AM rejects).

        Live polling (0x21) works without it, so it is armed lazily on the first
        Testing command instead of at connect: a logging session stays byte-for-byte
        what it was before. Both tools ignore the answers to these two requests, so
        a negative response or a timeout is recorded but never raised — worst case
        the ECU stays in its default session and rejects the test that follows.
        Idempotent: only the first call talks to the ECU.
        """
        if self.test_mode:
            return {"ok": True, "cached": True, "detail": self.test_mode_detail}
        out: dict = {"ok": True, "cached": False, "timing": "", "session": "", "nrc": None}
        try:
            out["timing"] = self.access_timing_parameters().hex()
        except NegativeResponse as e:
            out["timing"] = f"nrc {e.code:#04x}"
        except KLineError as e:
            out["timing"] = _short_error(e)
        try:
            out["session"] = self.start_diagnostic_session(DIAG_SESSION).hex()
        except NegativeResponse as e:
            out["ok"] = False
            out["nrc"] = e.code
            out["session"] = f"nrc {e.code:#04x}"
        except KLineError as e:
            out["ok"] = False
            out["session"] = _short_error(e)
        self.test_mode = True   # do not retry every command; a re-link resets it
        self.test_mode_detail = f"83:{out['timing']} 10 81:{out['session']}"
        out["detail"] = self.test_mode_detail
        return out

    def read_ecu_identification(self, option: int = DEFAULT_ECU_ID_OPTION) -> str:
        _tx, resp = self._service(bytes([SID_READ_ECU_ID, option]))
        payload = resp.data[2:]  # skip SID echo + option byte
        self.ecu_id_raw = bytes(payload)
        self.ecu_id_blocks = {option: bytes(payload)}
        text = bytes(b for b in payload if 0x20 <= b < 0x7F).decode("ascii", "replace")
        self.ecu_id = text.strip()
        # autodetect hardware version token, e.g. "IAW5AMHW610"
        m = re.search(r"[A-Z0-9]*HW\d+", self.ecu_id)
        self.ecu_hw = m.group(0) if m else ""
        return self.ecu_id

    def read_ecu_identification_all(
        self, options: tuple[int, ...] = ECU_ID_OPTIONS
    ) -> str:
        """Read every identification block the ECU offers, not just one option.

        We used 0x80 alone; IAWDiag asks its IAW-5AM-family ECUs with option 0x00.
        Both are tried: the first block that answers becomes the identity string,
        the rest are kept in ``ecu_id_blocks`` for the identity panel/.txt dump.
        Never raises — some 5AM variants reject 0x1A entirely.
        """
        blocks: dict[int, bytes] = {}
        ident = ""
        for opt in options:
            try:
                text = self.read_ecu_identification(opt)
            except KLineError:
                continue
            blocks[opt] = self.ecu_id_raw
            if not ident and text:
                ident, primary = text, self.ecu_id_raw
        self.ecu_id_blocks = blocks
        if ident:
            self.ecu_id, self.ecu_id_raw = ident, primary
            m = re.search(r"[A-Z0-9]*HW\d+", ident)
            self.ecu_hw = m.group(0) if m else ""
        else:
            self.ecu_id, self.ecu_id_raw, self.ecu_hw = "", b"", ""
        return self.ecu_id

    def tester_present(self) -> None:
        self._service(bytes([SID_TESTER_PRESENT]))

    # -- diagnostic trouble codes (Testing tab) ---------------------------
    @staticmethod
    def _dtc_code(hi: int, lo: int) -> str:
        """Decode a 2-byte DTC into an SAE J2012 string, e.g. 0x01 0x23 -> 'P0123'."""
        return f"{'PCBU'[hi >> 6]}{(hi >> 4) & 3}{hi & 0x0F:X}{lo:02X}"

    # status byte layout as both PC tools decode it (GuzziDiag sub_40D..., IAWDiag
    # @26617/26655): low nibble = fault kind (1/2/4/8), bit 0x20 = stored (vs
    # current). GuzziDiag uses the coarser "0x60 != 0 -> stored"; IAWDiag shifts
    # (status & 0x60) >> 5 and files 1 and 3 as stored, i.e. bit 0x20 decides.
    # The wording behind the four kinds lives in the tools' external localization
    # file (keys Fault_3..Fault_6) and is NOT in either binary — hence numbers.
    DTC_KINDS = (1, 2, 4, 8)

    @classmethod
    def _dtc_status(cls, status: int) -> dict:
        kind = status & 0x0F
        return {
            "kind": kind if kind in cls.DTC_KINDS else 0,
            "stored": bool(status & 0x20),
            "warn": bool(status & 0x40),
            "bits": f"{status:08b}",
        }

    def read_dtc(self) -> dict:
        """ReadDTCByStatus (0x18 00 FF 00) — same request IAWDiag sends.

        Response ``58 <count> [hi lo status]*count``; each 3-byte record is a
        2-byte DTC + status byte (low nibble = fault kind). Read-only.
        """
        _tx, resp = self._service(bytes([SID_READ_DTC, 0x00, 0xFF, 0x00]))
        d = resp.data                       # [0x58, count, hi, lo, st, ...]
        count = d[1] if len(d) >= 2 else 0
        dtcs = []
        for i in range(count):
            off = 2 + i * 3
            if off + 2 >= len(d):
                break
            hi, lo, st = d[off], d[off + 1], d[off + 2]
            dtcs.append({"code": self._dtc_code(hi, lo), "status": st,
                         "raw": bytes([hi, lo, st]).hex(),
                         **self._dtc_status(st)})
        return {"count": count, "dtcs": dtcs, "raw": resp.hex()}

    def clear_dtc(self) -> dict:
        """ClearDiagnosticInformation (0x14 FF 00) — erase stored DTCs."""
        _tx, resp = self._service(bytes([SID_CLEAR_DTC, 0xFF, 0x00]))
        return {"raw": resp.hex()}

    # -- adaptation resets (verified for IAW 5AM, GuzziDiag v0.42 reverse) --
    def reset_tps(self) -> dict:
        """TPS reset / Drosselklappe zurücksetzen — StartRoutineByLocalId 0x31 0x21.

        Resets the throttle-position adaptation (closed-throttle reference). Engine
        must be off (kill-switch), ignition on. Expects positive response 0x71 0x21.
        """
        _tx, resp = self._service(bytes([SID_START_ROUTINE, 0x21]))
        # IAWDiag confirms the routine with 0x33 on the families that answer it;
        # the 5AM is expected to reject this, which is fine and reported as-is.
        return {"raw": resp.hex(), "results": self.routine_results(0x21)}

    def routine_results(self, localid: int) -> dict:
        """RequestRoutineResultsByLocalIdentifier — 0x33 <localid>.

        IAWDiag polls it right after StartRoutine on the ECU families that support
        it (``31 21`` then ``33 21``); the 5AM branch of both tools does not, so
        this is a best-effort probe: a negative response means "this ECU has no
        result for that routine", not a failure. Never raises.
        """
        lid = localid & 0xFF
        try:
            _tx, resp = self._service(bytes([SID_ROUTINE_RESULTS, lid]))
        except NegativeResponse as e:
            return {"ok": False, "nrc": e.code, "localid": lid}
        except (KLineError, OSError) as e:
            return {"ok": False, "error": _short_error(e), "localid": lid}
        return {"ok": True, "raw": resp.hex(), "data": resp.data.hex(), "localid": lid}

    def reset_adaptation(self) -> dict:
        """Self-adaptation reset / Selbstadaption zurücksetzen — IOControl 0x30 0x7E 0x04.

        Clears the ECU's self-learned fuel/idle adaptation. Expects 0x70 0x7E ...
        """
        _tx, resp = self._service(bytes([SID_IO_CONTROL, 0x7E, 0x04]))
        return {"raw": resp.hex()}

    def actuator_set(self, localid: int, on: bool) -> dict:
        """One actuator frame — InputOutputControlByLocalIdentifier.

        ``30 <localid> 07`` energizes the output, ``30 <localid> 00`` releases it.
        GuzziDiag holds an actuator on for as long as its checkbox is ticked and
        sends the 00 frame when it is unticked — there is no ECU-side timer, so the
        caller owns the timing (the worker does, via a deadline). Command byte
        07/00 from the GuzziDiag reverse, LocalID->actuator from JPDiag [Tests].
        Expects positive SID 0x70.
        """
        lid = localid & 0xFF
        _tx, resp = self._service(bytes([SID_IO_CONTROL, lid, 0x07 if on else 0x00]))
        return {"raw": resp.hex(), "localid": lid, "on": bool(on)}

    def actuator_pulse(self, localid: int, pulse_ms: int = 5000) -> dict:
        """Blocking momentary test: activate, wait, deactivate.

        Kept for tests and one-off scripts; the worker drives long pulses through
        ``actuator_set`` + a deadline so polling keeps running. The deactivate is
        always sent (finally) so the output can never be left energized.
        """
        lid = localid & 0xFF
        r = self.actuator_set(lid, True)
        try:
            time.sleep(max(0, min(int(pulse_ms), 30000)) / 1000.0)
        finally:
            try:
                self.actuator_set(lid, False)
            except KLineError:
                pass
        return r

    def poll(self, record_local_id: int) -> tuple[bytes, bytes, Frame]:
        """ReadDataByLocalIdentifier. Returns (tx_bytes, measurement bytes, frame).

        Measurement bytes are the response data past the SID echo (0x61) and the
        record-id echo, i.e. the raw block to feed into ``ParamMap.decode``.
        """
        tx, resp = self._service(bytes([SID_READ_DATA_BY_LID, record_local_id]))
        measurements = resp.data[2:] if len(resp.data) >= 2 else b""
        return tx, measurements, resp

    def read_local(
        self, rli: int, *, with_addr: bool = True, timeout: float | None = None
    ) -> dict:
        """Probe ReadDataByLocalIdentifier for one record id, never raising.

        Used by the rli-scan and the per-parameter poll. Returns a dict:
          status : "ok" | "nrc" | "timeout" | "bad_frame"
          tx/rx  : hex of the sent frame / received frame
          data   : response data field hex (positive answers only) -> [0x61][rli][val...]
          val    : big-endian value from data[2:] (after SID 0x61 + rli echo)
          vlen   : number of value bytes (1/2/3, varies per rli)
          nrc    : negative-response code (status == "nrc")

        ``with_addr=False`` uses the short header frame ``[len][data][cs]`` that
        IAWDiag emits for some record ids (dword_495594 == 0).
        """
        payload = bytes([SID_READ_DATA_BY_LID, rli & 0xFF])
        out: dict = {
            "rli": rli & 0xFF, "with_addr": with_addr,
            "tx": self.t.build_frame(payload, with_addr=with_addr).hex(),
            "rx": "", "status": "", "data": "", "nrc": None, "val": None, "vlen": 0,
        }
        try:
            self.t.send_frame(payload, with_addr=with_addr)
            self.last_tx_at = time.monotonic()
            resp = self.t.recv_frame(timeout=timeout)
        except KLineTimeout:
            out["status"] = "timeout"
            return out
        except ChecksumError:
            out["status"] = "bad_frame"
            return out
        out["rx"] = resp.hex()
        d = resp.data
        if d and d[0] == NEG_RESPONSE:
            out["status"] = "nrc"
            out["nrc"] = d[2] if len(d) >= 3 else 0
            return out
        out["status"] = "ok"
        out["data"] = d.hex()
        body = d[2:] if len(d) >= 3 else b""   # value after SID(0x61) + rli echo
        out["val"] = int.from_bytes(body, "big") if body else None
        out["vlen"] = len(body)
        return out

    # -- high level --------------------------------------------------------
    def connect(self, session: int | None = None, slow: bool = False) -> str:
        """Live-data bring-up: init + (StartCommunication) + ReadEcuId.

        ``slow`` selects 5-baud slow init (address 0x33 + keybyte handshake), which
        establishes the session on its own — no StartCommunication follows. The
        default fast-init sends the wake-up BREAK then StartCommunication (0x81).

        No StartDiagnosticSession is sent here: 0x21 polling works in the default
        session, and the session the PC tools start (0x10 0x81) is armed lazily by
        ``enter_test_mode`` when the Testing tab needs it. 0x85 is the *programming*
        session used before a baud switch for flashing — passing ``session``
        explicitly still sends whatever is asked for.
        """
        self.test_mode = False       # a fresh link is back in the default session
        self.test_mode_detail = ""
        if slow:
            self.t.slow_init()
        else:
            self.t.fast_init()
            self.start_communication()
        if session is not None:
            self.start_diagnostic_session(session)
        # never raises: some 5AM variants reject 0x1A, the link is still usable
        return self.read_ecu_identification_all()

    def keepalive_if_idle(self, idle_s: float = 1.0) -> None:
        if time.monotonic() - self.last_tx_at >= idle_s:
            self.tester_present()

    def end_link(self) -> None:
        """Best-effort teardown, mirroring both PC tools: 0x20 then 0x82.

        Only 0x20 is skipped when no diagnostic session was ever started. Every
        failure is swallowed — this runs while the link is already going away.
        """
        try:
            if self.test_mode:
                self.stop_diagnostic_session()
        except (KLineError, OSError):
            pass
        try:
            self.stop_communication()
        except (KLineError, OSError):
            pass
        self.test_mode = False
