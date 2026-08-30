"""Thread-safe shared state between the K-Line worker thread and the web layer."""

from __future__ import annotations

import threading
import time


class State:
    def __init__(self):
        self._lock = threading.Lock()
        self.status = "init"          # init | searching | connected | error
        self.status_msg = ""
        self.ecu_id = ""
        self.ecu_hw = ""              # parsed HW version, e.g. IAW5AMHW610
        self.ecu_id_raw = ""          # hex of the raw ECU-id payload (for the .txt)
        self.ecu_fields: dict = {}    # parsed id fields {Drawing/Hardware/Software/...}
        self.ecu_id_blocks: dict = {}  # every 0x1A option that answered {"80": hex, ...}
        self.ecu_desc = ""            # GuzziDiag-style labelled description (Date=today)
        self.ap_channel = 0            # effective Wi-Fi channel (auto-picked)
        self.wifi_mode = "ap"          # ap | client — which side owns the radio
        self.wifi_link: dict = {}      # client mode: {associated, ssid, signal, ip}
        self.bus_baud = 0             # K-Line baud currently in use / being tried
        self.values: dict[str, float | None] = {}
        self.values_ts = 0.0          # wall-clock of last measurement
        self.poll_hz = 0.0
        # two independent log streams
        self.logging_decoded = False
        self.log_decoded_file = ""
        self.log_decoded_records = 0
        self.logging_raw = False
        self.log_raw_file = ""
        self.log_raw_records = 0
        self.selected: list[str] = []  # channel keys the UI shows live
        self.catalog: list[dict] = []  # [{key,name,unit}, ...]
        # rli-scan (bus sweep) status
        self.scan_on = False
        self.scan_sweeps = 0
        self.scan_alive = 0
        self.scan_file = ""
        self.scan_remaining = -1       # seconds until auto-stop (-1 = no timer)
        self.scan_pos = 0              # rli index reached in the current sweep
        self.scan_total = 0            # rli count in the sweep range (progress = pos/total)
        # Testing tab: diagnostic session armed (0x83 + 0x10 81) and the actuator
        # currently energized, if any
        self.test_mode = False
        self.test_mode_detail = ""
        self.act_lid = None            # LocalID of the running actuator test
        self.act_key = ""
        self.act_until = 0.0           # wall-clock end of the pulse (0 = idle)
        self._last_poll_mono = 0.0

    # -- writers (worker thread) ------------------------------------------
    def set_status(self, status: str, msg: str = "") -> None:
        with self._lock:
            self.status = status
            self.status_msg = msg

    def set_ecu_id(self, ecu_id: str) -> None:
        with self._lock:
            self.ecu_id = ecu_id

    def set_ecu_hw(self, ecu_hw: str) -> None:
        with self._lock:
            self.ecu_hw = ecu_hw

    def set_ecu_id_raw(self, hex_str: str) -> None:
        with self._lock:
            self.ecu_id_raw = hex_str

    def set_ecu_id_blocks(self, blocks: dict) -> None:
        with self._lock:
            self.ecu_id_blocks = dict(blocks)

    def set_ecu_fields(self, fields: dict) -> None:
        with self._lock:
            self.ecu_fields = dict(fields)

    def set_ecu_desc(self, desc: str) -> None:
        with self._lock:
            self.ecu_desc = desc

    def set_ap_channel(self, channel: int) -> None:
        with self._lock:
            self.ap_channel = int(channel or 0)

    def set_wifi(self, mode: str, link: dict | None = None) -> None:
        with self._lock:
            self.wifi_mode = mode
            self.wifi_link = dict(link or {})

    def set_bus_baud(self, baud: int) -> None:
        with self._lock:
            self.bus_baud = int(baud or 0)

    def set_catalog(self, catalog: list[dict], default_selected: list[str] | None = None) -> None:
        with self._lock:
            self.catalog = catalog
            if default_selected is not None:
                self.selected = list(default_selected)
            elif not self.selected:
                self.selected = [c["key"] for c in catalog]

    def update_values(self, values: dict) -> None:
        now_mono = time.monotonic()
        with self._lock:
            self.values = values
            self.values_ts = time.time()
            if self._last_poll_mono:
                dt = now_mono - self._last_poll_mono
                if dt > 0:
                    # light smoothing of the rate estimate
                    self.poll_hz = round(0.7 * self.poll_hz + 0.3 * (1.0 / dt), 1)
            self._last_poll_mono = now_mono

    # "armed" = toggle intent (survives disconnect); "file" = actually writing now
    def set_decoded_armed(self, on: bool) -> None:
        with self._lock:
            self.logging_decoded = on

    def set_decoded_file(self, log_file: str, records: int = 0) -> None:
        with self._lock:
            self.log_decoded_file = log_file
            self.log_decoded_records = records

    def inc_decoded_records(self, n: int = 1) -> None:
        with self._lock:
            self.log_decoded_records += n

    def set_raw_armed(self, on: bool) -> None:
        with self._lock:
            self.logging_raw = on

    def set_raw_file(self, log_file: str, records: int = 0) -> None:
        with self._lock:
            self.log_raw_file = log_file
            self.log_raw_records = records

    def inc_raw_records(self, n: int = 1) -> None:
        with self._lock:
            self.log_raw_records += n

    def set_scan(self, on: bool, sweeps: int = 0, alive: int = 0, log_file: str = "",
                 remaining: int = -1) -> None:
        with self._lock:
            self.scan_on = bool(on)
            self.scan_sweeps = int(sweeps)
            self.scan_alive = int(alive)
            self.scan_file = log_file
            self.scan_remaining = int(remaining)

    def set_test_mode(self, on: bool, detail: str = "") -> None:
        with self._lock:
            self.test_mode = bool(on)
            self.test_mode_detail = detail

    def set_actuator(self, lid: int | None, key: str = "", until: float = 0.0) -> None:
        with self._lock:
            self.act_lid = lid
            self.act_key = key
            self.act_until = float(until)

    def set_scan_progress(self, pos: int, total: int) -> None:
        with self._lock:
            self.scan_pos = int(pos)
            self.scan_total = int(total)

    # -- writers (web thread) ---------------------------------------------
    def set_selected(self, keys: list[str]) -> None:
        with self._lock:
            self.selected = list(keys)

    # -- reader ------------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            return {
                "status": self.status,
                "status_msg": self.status_msg,
                "ecu_id": self.ecu_id,
                "ecu_fields": dict(self.ecu_fields),
                "ecu_desc": self.ecu_desc,
                "ecu_hw": self.ecu_hw,
                "ecu_id_raw": self.ecu_id_raw,
                "ecu_id_blocks": dict(self.ecu_id_blocks),
                "ap_channel": self.ap_channel,
                "wifi_mode": self.wifi_mode,
                "wifi_link": dict(self.wifi_link),
                "bus_baud": self.bus_baud,
                "values": dict(self.values),
                "values_ts": self.values_ts,
                "poll_hz": self.poll_hz,
                "logging_decoded": self.logging_decoded,
                "log_decoded_file": self.log_decoded_file,
                "log_decoded_records": self.log_decoded_records,
                "logging_raw": self.logging_raw,
                "log_raw_file": self.log_raw_file,
                "log_raw_records": self.log_raw_records,
                "selected": list(self.selected),
                "catalog": list(self.catalog),
                "scan_on": self.scan_on,
                "scan_sweeps": self.scan_sweeps,
                "scan_alive": self.scan_alive,
                "scan_file": self.scan_file,
                "scan_remaining": self.scan_remaining,
                "scan_pos": self.scan_pos,
                "scan_total": self.scan_total,
                "test_mode": self.test_mode,
                "test_mode_detail": self.test_mode_detail,
                "act_lid": self.act_lid,
                "act_key": self.act_key,
                "act_until": self.act_until,
            }
