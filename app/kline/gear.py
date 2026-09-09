"""Which gear the bike is in, worked out from rpm and road speed.

The ECU does not report a gear number -- 0x61 is a neutral flag and nothing in
the 79 identifiers carries the number. But 0x53 turned out to be road speed on
2026-09-09, and rpm over speed is a constant per gear, so the number falls out
of two channels we already have.

The calibration is physical rather than a magic constant: gearbox teeth, primary
drive, final drive and the rolling circumference of the wheel the sensor is on.
Change a sprocket or a tyre and it is a number in config, not code.

    k(g) = primary * (driven/driving) * final * 1000 / (circumference_m * 60)

For this Granpasso -- 1.836 primary, 17/40 final, 150/70 R17 -- that gives
k = 98.9, 67.2, 53.6, 45.4, 40.4, 37.2 for gears one to six. The 2026-09-09 ride
used two of them and their measured ratio, 1.4627, matched G1/G2 = 1.4712 to
0.6 %: the best of all fifteen pairs, and the only one whose implied primary
(1.836) and top speed (202 km/h at 7500 rpm in sixth) are physically sane.

Poll the clutch with it where you can. During an upshift the revs fall while the
speed still climbs, so the ratio sweeps through gears that are not there -- on
that same ride it read third for two samples in the middle of a 1->2 change.
The clutch flag covers exactly that; without it a shift can flash an
intermediate gear for about a second.
"""

from __future__ import annotations

import math

NEUTRAL = "N"

# Below these the ratio is noise: at a standstill it is a division by nothing,
# and just off idle a single sample of speed swings it by a whole gear.
MIN_SPEED = 5.0     # km/h
MIN_RPM = 900.0

# How much better the new gear has to look before the display moves. Gear steps
# are 1.08 (5-6) to 1.47 (1-2) apart, so in logs 0.08 to 0.39; half the tightest
# step is wide enough to swallow a reading that lands on a boundary and narrow
# enough never to block a real shift. Without it the 2026-09-09 ride flicked
# through third for two samples while crossing the 2|3 line at k = 59.3.
MARGIN = 0.04


class GearMap:
    """Turns the physical numbers into one k per gear, once."""

    def __init__(self, cfg: dict | None = None):
        c = cfg or {}
        self.enabled = bool(c.get("enabled", True))
        teeth = c.get("teeth") or []
        primary = float(c.get("primary", 0) or 0)
        final = c.get("final") or [0, 0]
        circ = float(c.get("wheel_circumference_m", 0) or 0)
        self.k: dict[int, float] = {}
        try:
            fin = float(final[1]) / float(final[0])
            base = primary * fin * 1000.0 / (circ * 60.0)
            for i, (driving, driven) in enumerate(teeth, start=1):
                self.k[i] = base * (float(driven) / float(driving))
        except (TypeError, ValueError, ZeroDivisionError, IndexError):
            self.k = {}
        self.ready = self.enabled and len(self.k) >= 2

    def nearest(self, k: float) -> int | None:
        """The gear whose ratio is closest in log space -- gear steps are
        multiplicative, so a plain difference would favour the tall gears."""
        if not self.k or k <= 0:
            return None
        lk = math.log(k)
        return min(self.k, key=lambda g: abs(math.log(self.k[g]) - lk))


class GearReader:
    """Holds the one bit of memory the answer needs: the gear last shown.

    Two things want it. A change is only accepted after two samples agree, so a
    single skewed reading -- the channels are three separate requests -- cannot
    flick the display; and a pulled clutch holds the last gear rather than
    recomputing, because the ratio is meaningless then but the gear has not
    actually changed.
    """

    def __init__(self, gmap: GearMap):
        self.map = gmap
        self.gear: int | str | None = None
        self._pending: int | None = None
        self._count = 0

    def _beats(self, k: float, cand: int, cur: int) -> bool:
        d = lambda g: abs(math.log(self.map.k[g]) - math.log(k))
        return d(cand) + MARGIN < d(cur)

    def update(self, rpm, speed, neutral=None, clutch=None):
        """values: rpm, speed km/h, and the two switches when they are polled.

        `neutral` is 0 in neutral and 1 with a gear engaged (status_maps
        'neutral'); `clutch` is 0 pulled and 1 released ('clutch', and the
        polarity really is that way round -- JPDiag had it inverted).
        """
        if not self.map.ready:
            return None

        # 1. the neutral flag is the authority. On neutral the engine is not
        #    coupled to the wheel at all, so any rpm/speed there is fiction --
        #    rolling in neutral with the engine blipped would otherwise print a
        #    confident, entirely wrong gear.
        if neutral is not None and float(neutral) == 0:
            self.gear = NEUTRAL
            self._pending, self._count = None, 0
            return NEUTRAL

        # 2. clutch pulled: same fiction, but the gear itself did not change, so
        #    hold what was last shown instead of blinking for the whole coast.
        if clutch is not None and float(clutch) == 0:
            return self.gear

        if rpm is None or speed is None:
            return self.gear
        rpm, speed = float(rpm), float(speed)
        if speed < MIN_SPEED or rpm < MIN_RPM:
            self.gear = None
            self._pending, self._count = None, 0
            return None

        k = rpm / speed
        g = self.map.nearest(k)
        if g == self.gear:
            self._pending, self._count = None, 0
            return self.gear
        # 3. a boundary crossing is not a shift: the candidate has to beat the
        #    gear on screen by a margin, not merely tie with it
        if isinstance(self.gear, int) and g is not None and not self._beats(k, g, self.gear):
            self._pending, self._count = None, 0
            return self.gear
        # 4. and two in a row before it moves
        if g == self._pending:
            self._count += 1
        else:
            self._pending, self._count = g, 1
        if self._count >= 2:
            self.gear = g
            self._pending, self._count = None, 0
        return self.gear
