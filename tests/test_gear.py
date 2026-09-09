"""Offline tests for the derived gear channel.

The ECU reports no gear number, but 0x53 turned out to be road speed and rpm
over speed is a constant per gear, so the number falls out of two channels we
already poll. The calibration is physical -- gearbox teeth, primary, final drive
and the rolling circumference -- so this file also pins the arithmetic that
turns those into one ratio per gear.

The overrides matter more than the arithmetic. On neutral, and on a pulled
clutch, the engine is not coupled to the wheel and rpm/speed is fiction; without
the overrides a blip while rolling in neutral prints a confident, wrong gear.

Run directly:  python tests/test_gear.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.kline.gear import NEUTRAL, GearMap, GearReader  # noqa: E402

# this Granpasso: 1.836 primary, 17/40 final, 150/70 R17 on the driven wheel
CFG = {
    "enabled": True,
    "primary": 1.836,
    "final": [17, 40],
    "teeth": [[13, 36], [17, 32], [20, 30], [22, 28], [23, 26], [24, 25]],
    "wheel_circumference_m": 2.0163,
}


def _reader():
    return GearReader(GearMap(CFG))


def test_the_teeth_become_one_ratio_per_gear():
    m = GearMap(CFG)
    assert m.ready
    want = {1: 98.9, 2: 67.2, 3: 53.6, 4: 45.4, 5: 40.4, 6: 37.2}
    for g, k in want.items():
        assert abs(m.k[g] - k) < 0.15, (g, m.k[g], k)
    # sixth at 7500 rpm is the sanity check that picked this calibration over the
    # alternatives: 202 km/h is what the bike actually does
    assert 200 <= 7500 / m.k[6] <= 205, 7500 / m.k[6]


def test_a_missing_or_broken_calibration_simply_says_nothing():
    for bad in ({}, {"enabled": False}, {"primary": 0, "final": [0, 0], "teeth": []},
                {"primary": 1.8, "final": [17, 40], "teeth": [[13, 36]],
                 "wheel_circumference_m": 0}):
        r = GearReader(GearMap(bad))
        assert r.update(4000, 60) is None, bad


def test_neutral_beats_any_ratio():
    r = _reader()
    # a pair that would otherwise read as a perfectly good second
    for _ in range(3):
        r.update(3239, 48)
    assert r.gear == 2, r.gear
    assert r.update(3239, 48, neutral=0) == NEUTRAL
    assert r.gear == NEUTRAL
    # and it keeps saying so while the flag says so, however the numbers look
    assert r.update(8000, 85, neutral=0) == NEUTRAL


def test_a_pulled_clutch_holds_the_gear_instead_of_recomputing():
    r = _reader()
    for _ in range(3):
        r.update(3239, 48)
    assert r.gear == 2
    # clutch in (0 = pulled), revs flare, speed unchanged: the ratio is fiction
    assert r.update(7000, 48, neutral=1, clutch=0) == 2
    assert r.update(1500, 48, neutral=1, clutch=0) == 2
    # released again and the numbers are meaningful once more
    for _ in range(3):
        r.update(3239, 48, neutral=1, clutch=1)
    assert r.gear == 2


def test_without_those_channels_only_the_thresholds_apply():
    r = _reader()
    for _ in range(3):
        r.update(3239, 48)
    assert r.gear == 2, "no neutral or clutch selected -> still a gear"
    assert r.update(1400, 0) is None, "standing still divides by nothing"
    assert r.update(700, 30) is None, "below idle the ratio is noise"


def test_a_change_needs_two_samples():
    r = _reader()
    for _ in range(3):
        r.update(3239, 48)
    assert r.gear == 2
    # one skewed sample -- the three channels are three separate requests
    assert r.update(8324, 87) == 2, "a single reading must not flick the display"
    assert r.update(8324, 87) == 1, "two in a row is a real change"


def test_the_recorded_ride_reads_as_two_gears():
    """The 2026-09-09 Unk-w ride, straight through the reader.

    That ride used two gears; their measured ratio matched G1/G2 to 0.6 %, which
    is what identified 0x53 as speed in the first place. If the calibration ever
    drifts, this is the test that says so.

    It also pins the one blemish. That preset polled no clutch, so an upshift --
    revs falling while speed still rises -- drags the ratio through a gear that
    is not there: at 11:39:14 k reads 59.4, which is nearest to third, for two
    samples in the middle of a 1->2 change. With the clutch channel selected
    override 2 covers exactly that; without it a shift can flash an intermediate
    gear for about a second, and the assertion below says how much of that is
    tolerable rather than pretending it does not happen.
    """
    import csv
    from collections import Counter

    log = Path(__file__).resolve().parent / "data" / "ride-20260909-unkw.csv"
    if not log.exists():
        print("skip test_the_recorded_ride_reads_as_two_gears (no sample log)")
        return
    r = _reader()
    seen = Counter()
    for row in csv.DictReader(open(log)):
        num = lambda k: (float(row[k]) if row.get(k) not in ("", None) else None)
        g = r.update(num("rpm"), num("r53"))
        seen[g] += 1
    moving = sum(v for g, v in seen.items() if isinstance(g, int))
    assert seen[1] and seen[2], "the ride used first and second: %s" % dict(seen)
    assert seen[1] + seen[2] > 0.99 * moving, \
        "the two real gears must be all but everything: %s" % dict(seen)
    # anything else is a shift transient, not a gear: a couple of samples, and
    # only while the clutch is out of the picture
    stray = moving - seen[1] - seen[2]
    assert stray <= 4, "too much flicker for shift transients: %s" % dict(seen)
    # first is the one it spent its moving time in -- second is the town cruise
    assert seen[1] > seen[2], dict(seen)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok " + name)
