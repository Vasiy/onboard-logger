"""Config-driven decoding of IAW 5AM live-data.

GuzziDiag / IAWDiag do NOT read one big block: each live value is its own
``ReadDataByLocalIdentifier`` request ``0x21 <rli>`` with its own record-local-id,
and the 16-bit value is taken from the response at ``data[1..2]`` (big-endian);
RPM is ``15000000 / period`` (see IAWDiag reverse in the repo notes). So the map
is a *list of parameters*, each with its own ``rli`` and scaling.

The exact rli/offset/scale of the 5AM set is not published; the values in
config/params.json are starting hypotheses to be calibrated from a live capture
(every raw frame is logged verbatim, so offsets can be dialed in by editing only
params.json). The legacy single-block schema (record_local_id + channels) is
still accepted and auto-converted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Param:
    key: str
    name: str
    rli: int                   # record-local-id: request is 0x21 <rli>
    unit: str = ""
    fmt: int = 2               # 2 = addressed frame [0x80|len][10][F1]; 0 = short [len]
    offset: int = 1            # byte offset into the response DATA field ([0]=0x61 SID)
    length: int = 2
    endian: str = "big"        # "big" | "little"
    signed: bool = False
    scale: float = 1.0
    bias: float = 0.0
    recip: float = 0.0         # if !=0: value = recip / raw (RPM = 15000000 / period)
    digits: int = 2
    default: bool = True       # selected for the decoded log by default (named params)
    # status channels: decode the raw number into text via config/status_maps.json.
    # ``map`` is a map name from that file, ``map_type`` overrides its type
    # ("enum" = exact value lookup, "bits" = every set bit listed).
    map: str = ""
    map_type: str = ""

    @property
    def with_addr(self) -> bool:
        return int(self.fmt) != 0

    def decode(self, data: bytes):
        """Decode from the full response DATA field (data[0] is the 0x61 SID echo)."""
        end = self.offset + self.length
        if not data or end > len(data):
            return None
        raw = int.from_bytes(data[self.offset:end], self.endian, signed=self.signed)
        if self.recip:
            return None if raw == 0 else round(self.recip / raw, self.digits)
        return round(raw * self.scale + self.bias, self.digits)


@dataclass
class ParamMap:
    poll_interval_ms: int
    poll_timeout_ms: int
    params: list[Param]

    @classmethod
    def load(cls, path: str | Path) -> "ParamMap":
        raw = json.loads(Path(path).read_text())
        if raw.get("params"):
            params = [Param(**p) for p in raw["params"]]
        else:  # legacy single-block schema -> one rli, per-channel offsets (+2 for SID+id)
            rli = int(raw.get("record_local_id", 1))
            params = [
                Param(
                    key=c["key"], name=c.get("name", c["key"]), rli=rli,
                    unit=c.get("unit", ""), offset=int(c.get("offset", 0)) + 2,
                    length=int(c.get("length", 1)), endian=c.get("endian", "big"),
                    signed=bool(c.get("signed", False)), scale=float(c.get("scale", 1.0)),
                    bias=float(c.get("bias", 0.0)), digits=int(c.get("digits", 2)),
                )
                for c in raw.get("channels", [])
            ]
        return cls(
            poll_interval_ms=int(raw.get("poll_interval_ms", 200)),
            poll_timeout_ms=int(raw.get("poll_timeout_ms", 150)),
            params=params,
        )

    def catalog(self) -> list[dict]:
        """Parameter metadata for the UI (key/name/unit/default/status map)."""
        return [
            {"key": p.key, "name": p.name, "unit": p.unit, "default": p.default,
             "rli": p.rli, "map": p.map, "map_type": p.map_type}
            for p in self.params
        ]
