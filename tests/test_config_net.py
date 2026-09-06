"""Offline tests for the Wi-Fi side of ConfigManager: AP vs client mode.

No radio and no system calls: ``_run`` is replaced by a recorder, so the tests
assert on *which commands would run* and on the rendered config files.
"""

import copy
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.web.config_mgr import ConfigManager  # noqa: E402

DEFAULTS = Path(__file__).resolve().parent.parent / "config" / "config.default.json"


def _cm(tmp, iface_present=True):
    cm = ConfigManager(
        config_path=Path(tmp) / "config.json",
        hostapd_conf=Path(tmp) / "hostapd.conf",
        dnsmasq_conf=Path(tmp) / "dnsmasq.conf",
        wpa_conf_dir=Path(tmp),
    )
    cm.ran = []
    cm._run = lambda cmd, msgs: (cm.ran.append(" ".join(cmd)), True)[1]
    cm.iface_present = lambda: iface_present
    cm.client_status = lambda: {"associated": True, "ssid": "home", "signal": -55,
                                "ip": "192.168.1.42/24", "iface_missing": False}
    cm.client_timeout = 0.1
    return cm


def _cfg(**over):
    cfg = json.loads(DEFAULTS.read_text())
    for path, val in over.items():
        node, *rest = path.split("__")
        d = cfg[node]
        for k in rest[:-1]:
            d = d[k]
        d[rest[-1]] = val
    return cfg


def test_defaults_carry_new_fields():
    cfg = _cfg()
    assert cfg["wifi"]["mode"] == "ap"
    assert cfg["network"]["gateway"] == ""          # AP does not route by default
    assert cfg["wifi"]["client"]["ipv4"] == "dhcp"


def test_dnsmasq_router_option():
    with tempfile.TemporaryDirectory() as tmp:
        cm = _cm(tmp)
        # empty gateway -> bare "dhcp-option=3" tells clients there is no router
        conf = cm.render_dnsmasq(_cfg())
        assert "dhcp-option=3\n" in conf and "dhcp-option=3," not in conf
        conf = cm.render_dnsmasq(_cfg(network__gateway="192.168.5.254"))
        assert "dhcp-option=3,192.168.5.254" in conf
        assert "dhcp-option=6,192.168.5.1" in conf   # DNS stays on the board


def test_wpa_conf_render():
    with tempfile.TemporaryDirectory() as tmp:
        cm = _cm(tmp)
        conf = cm.render_wpa_supplicant(
            _cfg(wifi__client__ssid="home", wifi__client__password="secret123"))
        assert 'ssid="home"' in conf and 'psk="secret123"' in conf
        assert "key_mgmt=WPA-PSK" in conf and "country=DE" in conf
        open_net = cm.render_wpa_supplicant(_cfg(wifi__client__ssid="open"))
        assert "key_mgmt=NONE" in open_net and "psk=" not in open_net


def test_ap_mode_does_not_touch_client_services():
    with tempfile.TemporaryDirectory() as tmp:
        cm = _cm(tmp)
        rep = cm.apply_network(_cfg(), prev=None)
        cmds = " ".join(cm.ran)
        assert "restart hostapd" in cmds and "restart dnsmasq" in cmds
        assert "wpa_supplicant@" not in cmds.replace("stop wpa_supplicant@wlan0", "")
        assert "dhcpcd -b" not in cmds
        assert rep["mode"] == "ap" and rep["fallback_to_ap"] is False
        assert (Path(tmp) / "hostapd.conf").exists()


def test_client_mode_stops_ap_and_leases():
    with tempfile.TemporaryDirectory() as tmp:
        cm = _cm(tmp)
        rep = cm.apply_network(_cfg(wifi__mode="client", wifi__client__ssid="home",
                                    wifi__client__password="secret123"), prev=None)
        cmds = cm.ran
        assert "systemctl stop hostapd" in cmds and "systemctl stop dnsmasq" in cmds
        assert "systemctl disable hostapd" in cmds       # and stays down after a reboot
        assert "systemctl restart wpa_supplicant@wlan0" in cmds
        assert "dhcpcd -b wlan0" in cmds
        assert not any("restart hostapd" in c for c in cmds)
        assert not any("restart dnsmasq" in c for c in cmds)
        assert rep["mode"] == "client" and rep["client_ok"] is True
        wpa = (Path(tmp) / "wpa_supplicant-wlan0.conf")
        assert wpa.exists() and oct(wpa.stat().st_mode)[-3:] == "600"   # holds the PSK


def test_client_static_address():
    with tempfile.TemporaryDirectory() as tmp:
        cm = _cm(tmp)
        cm.apply_network(_cfg(wifi__mode="client", wifi__client__ssid="home",
                              wifi__client__ipv4="static", wifi__client__ip="192.168.1.42",
                              wifi__client__prefix=24, wifi__client__gateway="192.168.1.1",
                              wifi__client__dns="192.168.1.1 1.1.1.1"), prev=None)
        cmds = cm.ran
        assert "ip addr add 192.168.1.42/24 dev wlan0" in cmds
        assert "ip route replace default via 192.168.1.1 dev wlan0" in cmds
        assert "resolvectl dns wlan0 192.168.1.1 1.1.1.1" in cmds
        assert "dhcpcd -b wlan0" not in cmds        # no lease wanted with a fixed address


def test_client_untouched_when_only_other_settings_change():
    with tempfile.TemporaryDirectory() as tmp:
        cm = _cm(tmp)
        client = _cfg(wifi__mode="client", wifi__client__ssid="home")
        other = copy.deepcopy(client)
        other["locale"] = "ru"                       # nothing Wi-Fi about it
        rep = cm.apply_network(other, prev=client)
        assert cm.ran == []                          # the live link is left alone
        assert rep["mode"] == "client" and rep["reconnect_required"] is False


def test_client_failure_falls_back_to_ap():
    with tempfile.TemporaryDirectory() as tmp:
        cm = _cm(tmp)
        cm.client_status = lambda: {"associated": False, "ssid": "", "signal": None,
                                    "ip": "", "iface_missing": False}
        rep = cm.apply_network(_cfg(wifi__mode="client", wifi__client__ssid="typo"), prev=None)
        assert rep["fallback_to_ap"] is True and rep["client_ok"] is False
        assert "ассоциаци" in rep["client_error"]
        cmds = " ".join(cm.ran)
        assert "restart hostapd" in cmds            # the AP is back on the air
        assert "systemctl enable hostapd" in cmds   # and survives a reboot
        assert rep["mode"] == "ap"


def test_no_iface_skips_everything():
    with tempfile.TemporaryDirectory() as tmp:
        cm = _cm(tmp, iface_present=False)
        rep = cm.apply_network(_cfg(wifi__mode="client", wifi__client__ssid="home"), prev=None)
        assert rep["iface_missing"] is True
        assert not any("hostapd" in c or "wpa_supplicant" in c for c in cm.ran)


def test_plan_reports_mode_switch():
    with tempfile.TemporaryDirectory() as tmp:
        cm = _cm(tmp)
        ap = _cfg()
        client = _cfg(wifi__mode="client", wifi__client__ssid="home")
        rep = cm.plan(ap, client)
        assert rep["mode"] == "client" and rep["reconnect_required"] is True
        assert any("клиент" in a for a in rep["applied"])
        back = cm.plan(client, ap)
        assert back["mode"] == "ap" and any("точка доступа" in a for a in back["applied"])


def test_validate_rules():
    with tempfile.TemporaryDirectory() as tmp:
        cm = _cm(tmp)
        cm.validate(_cfg())                                   # AP defaults are valid
        cm.validate(_cfg(wifi__mode="client", wifi__client__ssid="home"))
        bad = [
            _cfg(wifi__mode="client"),                                    # no SSID
            _cfg(wifi__mode="client", wifi__client__ssid="h",
                 wifi__client__ipv4="static", wifi__client__ip=""),       # static, no IP
            _cfg(wifi__mode="client", wifi__client__ssid="h", wifi__client__ipv4="static",
                 wifi__client__ip="192.168.1.42", wifi__client__gateway="10.0.0.1"),
            _cfg(network__gateway="10.9.9.9"),                            # gw outside AP subnet
            _cfg(wifi__mode="bridge"),                                    # unknown mode
        ]
        for cfg in bad:
            try:
                cm.validate(cfg)
            except ValueError:
                continue
            raise AssertionError(f"expected ValueError for {cfg['wifi']}")


def _main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok ", fn.__name__)
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _main()
