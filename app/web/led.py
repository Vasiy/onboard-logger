"""Control of the NanoPi NEO3 green ``status_led`` via sysfs.

    /sys/class/leds/status_led/{trigger,brightness}

Steady-on  -> trigger=none, brightness=1   (K-Line connected, data flowing)
Blinking   -> trigger=heartbeat            (searching / connecting)
Off        -> trigger=none, brightness=0

All writes are best-effort: on a dev host without the sysfs node the class
degrades to a no-op so the rest of the app still runs.
"""

from __future__ import annotations

from pathlib import Path


class Led:
    def __init__(self, name: str = "status_led"):
        self.base = Path(f"/sys/class/leds/{name}")
        self.available = self.base.is_dir()
        self._state = "unknown"

    def _write(self, node: str, value: str) -> None:
        if not self.available:
            return
        try:
            (self.base / node).write_text(value)
        except OSError:
            pass

    def connected(self) -> None:
        if self._state == "connected":
            return
        self._write("trigger", "none")
        self._write("brightness", "1")
        self._state = "connected"

    def searching(self) -> None:
        if self._state == "searching":
            return
        self._write("trigger", "heartbeat")
        self._state = "searching"

    def off(self) -> None:
        if self._state == "off":
            return
        self._write("trigger", "none")
        self._write("brightness", "0")
        self._state = "off"
