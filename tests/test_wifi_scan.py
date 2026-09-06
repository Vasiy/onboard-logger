"""Offline tests for the Wi-Fi channel picker (pure scoring, no radio)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.web.wifi_scan import (
    parse_iw_networks,  # noqa: E402
    best_channel_from_scan,
    channel_survey,
    freq_to_channel,
    parse_iw_scan,
)

SAMPLE = """BSS 11:22:33:44:55:66(on wlan0)
\tfreq: 2437
\tsignal: -42.00 dBm
\tSSID: NeighbourA
BSS aa:bb:cc:dd:ee:ff(on wlan0)
\tfreq: 2437
\tsignal: -55.00 dBm
\tSSID: NeighbourB
BSS 00:00:00:00:00:01(on wlan0)
\tfreq: 2412
\tsignal: -80.00 dBm
\tSSID: FarAway
BSS 00:00:00:00:00:02(on wlan0)
\tfreq: 5180
\tsignal: -50.00 dBm
\tSSID: FiveGig
"""


def test_parse_iw_networks():
    nets = parse_iw_networks(SAMPLE)                     # strongest first
    assert [n["ssid"] for n in nets] == ["NeighbourA", "FiveGig", "NeighbourB", "FarAway"]
    assert nets[0]["channel"] == 6 and nets[0]["signal"] == -42.0
    assert nets[1]["channel"] == 0 and nets[1]["freq"] == 5180   # 5 GHz has no 2.4 channel
    assert all(n["secured"] is False for n in nets)      # sample has no Privacy bit

    # a hidden SSID must be dropped, not absorb the next line
    hidden = "BSS 00:00:00:00:00:03(on wlan0)\n\tfreq: 2412\n\tsignal: -60.00 dBm\n\tSSID: \n"
    assert parse_iw_networks(hidden) == []

    # same SSID on two BSSIDs collapses to the stronger one
    dup = SAMPLE + "BSS 00:00:00:00:00:04(on wlan0)\n\tfreq: 2412\n\tsignal: -30.00 dBm\n\tSSID: FarAway\n"
    far = [n for n in parse_iw_networks(dup) if n["ssid"] == "FarAway"]
    assert len(far) == 1 and far[0]["signal"] == -30.0


def test_freq_to_channel():
    assert freq_to_channel(2412) == 1
    assert freq_to_channel(2437) == 6
    assert freq_to_channel(2462) == 11
    assert freq_to_channel(2484) == 14
    assert freq_to_channel(5180) is None


def test_parse_iw_scan():
    aps = parse_iw_scan(SAMPLE)
    # 5 GHz BSS is dropped; three 2.4 GHz APs remain
    assert aps == [(6, -42.0), (6, -55.0), (1, -80.0)]


def test_best_channel_avoids_congestion():
    aps = parse_iw_scan(SAMPLE)
    ch, info = best_channel_from_scan(aps, (1, 6, 11))
    # ch6 has two strong APs, ch1 a weak one, ch11 is clear -> pick 11
    assert ch == 11
    assert info["candidates"][6]["cochannel"] == 2
    assert info["candidates"][11]["score"] == 0.0


def test_tie_prefers_lower_channel():
    ch, _ = best_channel_from_scan([], (1, 6, 11))  # nothing on air
    assert ch == 1


def test_channel_survey():
    aps = parse_iw_scan(SAMPLE)  # [(6,-42),(6,-55),(1,-80)]
    survey = channel_survey(aps)
    assert [s["channel"] for s in survey] == list(range(1, 14))
    by_ch = {s["channel"]: s for s in survey}
    assert by_ch[6]["count"] == 2      # two co-channel APs on 6
    assert by_ch[1]["count"] == 1
    assert by_ch[13]["count"] == 0
    assert by_ch[6]["load"] > by_ch[1]["load"]  # ch6 far busier


def _main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _main()
