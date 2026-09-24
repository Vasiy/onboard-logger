"""Runtime configuration + network (AP/DHCP/hostname) management.

Owns config.json and renders/applies the AP state:
  * /etc/hostapd/hostapd.conf        (SSID / passphrase / channel)
  * /etc/dnsmasq.d/onboard-logger.conf (DHCP pool on wlan0 only)
  * wlan0 static IP via ``ip addr``

``apply_network`` diffs old vs new config and restarts only what changed,
reporting whether a reconnect (client drop / new AP IP) or a reboot is needed.
All privileged actions degrade to captured messages on a dev host so the API
never crashes when sysfs / systemctl are absent.
"""

from __future__ import annotations

import copy
import ipaddress
import re
import json
import shutil
import subprocess
import time
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from .wifi_scan import DEFAULT_CANDIDATES, pick_best_channel

APP_DIR = Path(__file__).resolve().parent.parent          # .../app
REPO_DIR = APP_DIR.parent
TEMPLATES_DIR = APP_DIR / "templates"

DEFAULT_CONFIG_PATH = REPO_DIR / "config" / "config.default.json"
CONFIG_PATH = Path("/etc/onboard-logger/config.json")

HOSTAPD_CONF = Path("/etc/hostapd/hostapd.conf")
WPA_CONF_DIR = Path("/etc/wpa_supplicant")
DNSMASQ_CONF = Path("/etc/dnsmasq.d/onboard-logger.conf")
WLAN_IFACE = "wlan0"

# NanoPi NEO3 physical pins 8/10 (GPIO3_A4/A6, UART1_TX/RX) -- a fixed
# platform tty, not a udev-enumerated device like the USB/FTDI /dev/kline
# symlink, so there is nothing for the rider to override here.
UART1_PORT = "/dev/ttyS1"


def resolve_kline_port(cfg: dict) -> str:
    """Which device node K-Line actually talks over, given kline.iface."""
    if cfg.get("kline", {}).get("iface") == "uart1":
        return UART1_PORT
    return cfg["kline"]["port"]


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


# Hardware-safe PWM frequency range for the fan's 2-pin-connector MOSFET
# chop, same bound as pwm-fan-addon's period_ns clamp and the PWM frequency
# slider (app.js): below 50 Hz the chop turns audible/mechanically rough,
# above 500 Hz is past what this has been run with. Clamped rather than
# rejected -- a stray period_ns (hand-edited config.json, or a slider value
# saved before this bound existed) should not need a repair trip to fix.
FAN_MIN_FREQ_HZ = 50
FAN_MAX_FREQ_HZ = 500
FAN_MAX_PERIOD_NS = 1_000_000_000 // FAN_MIN_FREQ_HZ
FAN_MIN_PERIOD_NS = 1_000_000_000 // FAN_MAX_FREQ_HZ


def _clamp_fan_period_ns(cfg: dict) -> None:
    fan = cfg.get("fan")
    if isinstance(fan, dict) and "period_ns" in fan:
        fan["period_ns"] = max(FAN_MIN_PERIOD_NS, min(FAN_MAX_PERIOD_NS, fan["period_ns"]))


def _act(key: str, value: str = "") -> dict:
    """One line of the applied report: an i18n key, plus the value that makes it
    concrete when there is one. The UI translates the key and appends the value,
    so a hostname or an SSID does not have to be baked into a sentence."""
    return {"k": key, "v": str(value)}


class ConfigError(ValueError):
    """A validation failure the UI can translate.

    The message is an i18n key; the values that make it concrete -- which
    gateway, which subnet -- ride in `detail`, which the API already carries and
    the banner already appends. Baking them into the sentence is what made these
    untranslatable in the first place.
    """

    def __init__(self, key: str, detail: str = ""):
        super().__init__(key)
        self.detail = detail


class ConfigManager:
    def __init__(
        self,
        config_path: Path = CONFIG_PATH,
        default_path: Path = DEFAULT_CONFIG_PATH,
        hostapd_conf: Path = HOSTAPD_CONF,
        dnsmasq_conf: Path = DNSMASQ_CONF,
        iface: str = WLAN_IFACE,
        wpa_conf_dir: Path = WPA_CONF_DIR,
    ):
        self.config_path = Path(config_path)
        self.default_path = Path(default_path)
        self.hostapd_conf = Path(hostapd_conf)
        self.dnsmasq_conf = Path(dnsmasq_conf)
        self.iface = iface
        # wpa_supplicant@<iface>.service reads exactly this path
        self.wpa_conf = Path(wpa_conf_dir) / f"wpa_supplicant-{iface}.conf"
        self.chosen_channel: int | None = None
        self.client_timeout = 45.0   # seconds to wait for a join before restoring the AP
        # last startup scan, exposed to the UI channel-occupancy chart
        self.last_survey: list[dict] | None = None
        self.last_scan_channel: int | None = None
        self.last_scan_ts: float = 0.0
        self.jinja = Environment(
            loader=FileSystemLoader(str(TEMPLATES_DIR)),
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
        )

    # -- persistence -------------------------------------------------------
    def defaults(self) -> dict:
        return json.loads(self.default_path.read_text())

    def load(self) -> dict:
        cfg = self.defaults()
        if self.config_path.exists():
            cfg = _deep_merge(cfg, json.loads(self.config_path.read_text()))
        _clamp_fan_period_ns(cfg)
        return cfg

    def save(self, cfg: dict) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))

    # -- validation --------------------------------------------------------
    def validate(self, cfg: dict) -> None:
        wifi = cfg["wifi"]
        mode = self.mode(cfg)
        if wifi.get("mode", "ap") not in ("ap", "client"):
            raise ConfigError("cfgerr.wifi_mode")
        if not (1 <= len(wifi["ssid"]) <= 32):
            raise ConfigError("cfgerr.ssid_len")
        pw = wifi.get("password", "")
        if pw and not (8 <= len(pw) <= 63):
            raise ConfigError("cfgerr.ap_password")
        if not (1 <= int(wifi["channel"]) <= 14):
            raise ConfigError("cfgerr.channel")

        client = wifi.get("client", {})
        if mode == "client":
            if not (1 <= len(client.get("ssid", "")) <= 32):
                raise ConfigError("cfgerr.client_ssid")
            cpw = client.get("password", "")
            if cpw and not (8 <= len(cpw) <= 63):
                raise ConfigError("cfgerr.client_password")
            if str(client.get("ipv4", "dhcp")) not in ("dhcp", "static"):
                raise ConfigError("cfgerr.client_ipv4")
            if str(client.get("ipv4", "dhcp")) == "static":
                cip = ipaddress.ip_address(client.get("ip", ""))
                cnet = ipaddress.ip_network(f"{cip}/{int(client.get('prefix', 24))}", strict=False)
                gw = client.get("gateway", "")
                if gw and ipaddress.ip_address(gw) not in cnet:
                    raise ConfigError("cfgerr.gw_outside", f"{gw} / {cnet}")
                for d in str(client.get("dns", "")).replace(",", " ").split():
                    ipaddress.ip_address(d)   # raises on junk

        net = cfg["network"]
        ap_ip = ipaddress.ip_address(net["ap_ip"])
        prefix = int(net["prefix"])
        subnet = ipaddress.ip_network(f"{ap_ip}/{prefix}", strict=False)
        ap_gw = net.get("gateway", "")
        if ap_gw and ipaddress.ip_address(ap_gw) not in subnet:
            raise ConfigError("cfgerr.ap_gw_outside", f"{ap_gw} / {subnet}")

        dhcp = cfg["dhcp"]
        if dhcp["enabled"]:
            start = ipaddress.ip_address(dhcp["pool_start"])
            end = ipaddress.ip_address(dhcp["pool_end"])
            if start > end:
                raise ConfigError("cfgerr.pool_order")
            for ip in (start, end):
                if ip not in subnet:
                    raise ConfigError("cfgerr.pool_outside", f"{ip} / {subnet}")
            if ap_ip in {start, end} or start <= ap_ip <= end:
                raise ConfigError("cfgerr.ap_in_pool")

        host = cfg.get("hostname", "")
        if host and not all(c.isalnum() or c == "-" for c in host):
            raise ConfigError("cfgerr.hostname")

        country = wifi.get("country", "")
        if not (len(country) == 2 and country.isalpha()):
            raise ConfigError("cfgerr.country")

        baud = cfg.get("kline", {}).get("baud", "auto")
        if str(baud).lower() != "auto":
            try:
                b = int(baud)
            except (TypeError, ValueError):
                raise ConfigError("cfgerr.baud_kind")
            if not (300 <= b <= 115200):
                raise ConfigError("cfgerr.baud_range")

        # which physical path K-Line rides on: the USB/FTDI adapter (port,
        # below) or the board's own UART1 header with a directly-wired L9637D
        iface = str(cfg.get("kline", {}).get("iface", "usb"))
        if iface not in ("usb", "uart1"):
            raise ConfigError("cfgerr.kline_iface")

        locale = cfg.get("locale", "en")
        if locale not in {"en", "de", "es", "fr", "it", "nl", "bg", "ru"}:
            raise ConfigError("cfgerr.locale", str(locale))

        # the diagnostics log shares the SD card with the ride logs: a limit of
        # 0 would rotate on every line, a huge one fills the card
        mb = cfg.get("diag", {}).get("max_mb", 2)
        try:
            mb = float(mb)
        except (TypeError, ValueError):
            raise ConfigError("cfgerr.diag_kind")
        if not (0.05 <= mb <= 64):
            raise ConfigError("cfgerr.diag_range")

        # the log destination is picked through /api/storage (which has to mount
        # something); this only stops a hand-edited config.json from arriving
        st = cfg.get("storage", {})
        if st.get("dest", "internal") not in ("internal", "usb"):
            raise ConfigError("cfgerr.storage_dest")
        mp = str(st.get("mount_point", "/media/usb0"))
        if not mp.startswith("/") or ".." in mp:
            raise ConfigError("cfgerr.mount_point")

        # Power save powers the board off, so a hand-edited config.json must not
        # be able to say "in one second". Nothing else under system. is validated
        # -- system.cpu got away without it because cpu_apply() refuses a value
        # the kernel does not offer, and there is no such backstop here.
        # 0 is in bounds on purpose: it is how a rider disables one step of the
        # ladder without a second config key (power_save_step() in system.py).
        ps = cfg.get("system", {}).get("power_save", {}) or {}
        for key in ("idle_min", "off_min"):
            try:
                minutes = float(ps.get(key, 15))
            except (TypeError, ValueError):
                raise ConfigError("cfgerr.power_kind")
            if not (0 <= minutes <= 99):
                raise ConfigError("cfgerr.power_range")

        # Where the clock comes from, and which module if not the first trusted
        # one. The device name is checked for shape only -- whether it exists is
        # the board's business and changes with the hardware.
        rt = cfg.get("system", {}).get("rtc", {}) or {}
        if str(rt.get("source", "web")) not in ("web", "rtc"):
            raise ConfigError("cfgerr.rtc_source")
        dev = str(rt.get("device", "") or "")
        if dev and not re.fullmatch(r"rtc\d+", dev):
            raise ConfigError("cfgerr.rtc_device", dev)

        # how far back a tile's sparkline looks; the slider offers 3..30 s and a
        # buffer outside that is either too short to read or pointlessly long
        try:
            spark = int(cfg.get("ui", {}).get("spark_s", 3))
        except (TypeError, ValueError):
            raise ConfigError("cfgerr.spark_span")
        if not (3 <= spark <= 30):
            raise ConfigError("cfgerr.spark_span")

    # -- rendering ---------------------------------------------------------
    def render_hostapd(self, cfg: dict) -> str:
        wifi = dict(cfg["wifi"])
        wifi["country"] = str(wifi.get("country", "DE")).upper()  # hostapd wants uppercase
        return self.jinja.get_template("hostapd.conf.j2").render(
            iface=self.iface, wifi=wifi
        )

    def render_wpa_supplicant(self, cfg: dict) -> str:
        """Client-mode supplicant config (holds the network's PSK in clear)."""
        wifi = cfg["wifi"]
        return self.jinja.get_template("wpa_supplicant.conf.j2").render(
            iface=self.iface,
            country=str(wifi.get("country", "DE")).upper(),
            client=wifi.get("client", {}),
        )

    def render_dnsmasq(self, cfg: dict) -> str:
        return self.jinja.get_template("dnsmasq.conf.j2").render(
            iface=self.iface, network=cfg["network"], dhcp=cfg["dhcp"]
        )

    # -- system helpers ----------------------------------------------------
    @staticmethod
    def _run(cmd: list[str], msgs: list[str]) -> bool:
        exe = shutil.which(cmd[0])
        if exe is None:
            msgs.append(f"skipped (no {cmd[0]}): {' '.join(cmd)}")
            return False
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            if r.returncode != 0:
                msgs.append(f"failed `{' '.join(cmd)}`: {r.stderr.strip() or r.stdout.strip()}")
                return False
            return True
        except (OSError, subprocess.SubprocessError) as exc:
            msgs.append(f"raised `{' '.join(cmd)}`: {exc}")
            return False

    def _write_conf(self, path: Path, content: str, msgs: list[str],
                    mode: int | None = None) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            if mode is not None:
                path.chmod(mode)
        except OSError as exc:
            msgs.append(f"could not write {path}: {exc}")

    def _set_wlan_ip(self, ap_ip: str, prefix: int, msgs: list[str]) -> None:
        self._run(["ip", "addr", "flush", "dev", self.iface], msgs)
        self._run(["ip", "link", "set", self.iface, "up"], msgs)
        self._run(["ip", "addr", "add", f"{ap_ip}/{prefix}", "dev", self.iface], msgs)

    @staticmethod
    def _cmd_out(cmd: list[str], timeout: int = 8) -> str:
        """Run a read-only command and return stdout ("" on any failure)."""
        if shutil.which(cmd[0]) is None:
            return ""
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return r.stdout if r.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            return ""

    def iface_ipv4(self) -> str:
        """Current IPv4 (with prefix) on the Wi-Fi interface, or ""."""
        out = self._cmd_out(["ip", "-4", "-o", "addr", "show", "dev", self.iface])
        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+/\d+)", out)
        return m.group(1) if m else ""

    def client_status(self) -> dict:
        """What the station side is doing right now — for the UI status line."""
        if not self.iface_present():
            return {"associated": False, "ssid": "", "signal": None, "ip": "",
                    "iface_missing": True}
        link = self._cmd_out(["iw", "dev", self.iface, "link"])
        ssid = re.search(r"^[^\S\n]*SSID:[^\S\n]*(.+)$", link, re.M)
        sig = re.search(r"^\s*signal:\s*(-?\d+)", link, re.M)
        return {
            "associated": "Connected to" in link,
            "ssid": ssid.group(1).strip() if ssid else "",
            "signal": int(sig.group(1)) if sig else None,
            "ip": self.iface_ipv4(),
            "iface_missing": False,
        }

    def _stop_ap(self, msgs: list[str]) -> None:
        """Release the radio from the AP side (also for a scan)."""
        self._run(["systemctl", "stop", "hostapd"], msgs)
        self._run(["systemctl", "stop", "dnsmasq"], msgs)

    def _stop_client(self, msgs: list[str]) -> None:
        self._run(["systemctl", "stop", f"wpa_supplicant@{self.iface}"], msgs)
        self._run(["dhcpcd", "-k", self.iface], msgs)


    # -- client (station) mode ---------------------------------------------
    def _apply_client(self, cfg: dict, msgs: list[str], applied: list[str]) -> dict:
        """Join an existing network instead of serving one.

        No DHCP server is started — the network already has one. The address is
        either leased (dhcpcd) or set by hand. If the join does not come up in
        ``client_timeout`` seconds the AP is restored, so a wrong password can
        never leave the board off the air (there is no eth0 on the bike).
        """
        client = cfg["wifi"].get("client", {})
        ssid = client.get("ssid", "")

        self._stop_ap(msgs)
        # keep hostapd from grabbing the radio on the next boot while we are a station
        self._run(["systemctl", "disable", "hostapd"], msgs)

        self._write_conf(self.wpa_conf, self.render_wpa_supplicant(cfg), msgs, mode=0o600)
        self._run(["ip", "addr", "flush", "dev", self.iface], msgs)
        self._run(["ip", "link", "set", self.iface, "up"], msgs)
        self._run(["systemctl", "restart", f"wpa_supplicant@{self.iface}"], msgs)
        applied.append(_act("apply.wifiClient", ssid))

        static = str(client.get("ipv4", "dhcp")) == "static"
        if static:
            self._set_wlan_ip(client.get("ip", ""), int(client.get("prefix", 24)), msgs)
            gw = client.get("gateway", "")
            if gw:
                self._run(["ip", "route", "replace", "default", "via", gw,
                           "dev", self.iface], msgs)
            dns = str(client.get("dns", "")).replace(",", " ").split()
            if dns:
                self._run(["resolvectl", "dns", self.iface, *dns], msgs)
            applied.append(_act("apply.staticAddr",
                                f"{client.get('ip', '')}/{client.get('prefix', 24)}"))
        else:
            self._run(["dhcpcd", "-b", self.iface], msgs)
            applied.append(_act("apply.dhcpAddr"))

        ok, why = self.wait_client_up()
        report = {
            "applied": applied,
            "messages": msgs,
            "iface_missing": False,
            "reconnect_required": True,
            "reboot_recommended": False,
            "ap_ip": cfg["network"]["ap_ip"],
            "mode": "client",
            "client_ok": ok,
            "client": self.client_status(),
            "fallback_to_ap": False,
        }
        if not ok:
            msgs.append(f"client mode did not come up: {why} -- putting the access point back")
            ap_cfg = copy.deepcopy(cfg)
            ap_cfg["wifi"]["mode"] = "ap"
            back = self.apply_network(ap_cfg, prev=None)   # prev=None -> full re-apply
            msgs.extend(back.get("messages", []))
            applied.extend(back.get("applied", []))
            report.update({"fallback_to_ap": True, "client_error": why, "mode": "ap"})
        return report

    def wait_client_up(self, timeout: float | None = None) -> tuple[bool, str]:
        """Block until the station is associated *and* has an IPv4, or give up."""
        limit = self.client_timeout if timeout is None else timeout
        deadline = time.monotonic() + limit
        st = self.client_status()
        while time.monotonic() < deadline:
            st = self.client_status()
            if st["associated"] and st["ip"]:
                return True, ""
            time.sleep(1.0)
        if not st["associated"]:
            return False, "not associated with the network (SSID, password or signal)"
        return False, "associated but with no address (DHCP did not answer)"

    def set_hostname(self, name: str, msgs: list[str]) -> None:
        self._run(["hostnamectl", "set-hostname", name], msgs)

    def iface_present(self) -> bool:
        """Is the AP interface actually there?

        Without a Wi-Fi dongle every AP step is not just useless but slow: the
        hostapd unit waits up to 30 s for wlan0 in its ExecStartPre, so a plain
        ``systemctl restart hostapd`` blocks half a minute and, when it runs
        during startup, keeps port 80 closed that whole time (the browser then
        fails any request with a bare network error).
        """
        return Path(f"/sys/class/net/{self.iface}").exists()

    @staticmethod
    def _diff_flags(cfg: dict, prev: dict | None) -> dict:
        return {
            "wifi": prev is None or prev.get("wifi") != cfg["wifi"],
            "ip": prev is None or prev.get("network") != cfg["network"],
            "dhcp": prev is None or prev.get("dhcp") != cfg["dhcp"],
            "hostname": prev is None or prev.get("hostname") != cfg.get("hostname"),
        }

    def plan(self, prev: dict | None, cfg: dict) -> dict:
        """Predict what apply_network would do, with no side effects.

        Used to answer the HTTP request before the AP is actually reconfigured
        (which may drop the client that sent the request)."""
        d = self._diff_flags(cfg, prev)
        applied: list[str] = []
        mode = self.mode(cfg)
        mode_changed = prev is None or self.mode(prev) != mode
        if not self.iface_present():   # no radio -> nothing Wi-Fi related will run
            d = dict(d, wifi=False, ip=False, dhcp=False)
        elif mode == "client":
            client = cfg["wifi"].get("client", {})
            if d["wifi"]:
                applied.append(_act("apply.wifiClient", client.get("ssid", "")))
                applied.append(_act("apply.staticAddr" if client.get("ipv4") == "static"
                                    else "apply.dhcpAddr"))
            return {
                "applied": applied,
                "messages": [],
                "iface_missing": False,
                "mode": mode,
                "reconnect_required": d["wifi"],
                "reboot_recommended": d["hostname"] and bool(cfg.get("hostname")),
                "ap_ip": cfg["network"]["ap_ip"],
            }
        if d["wifi"]:
            applied.append(_act("apply.wifiAp" if mode_changed else "apply.hostapd"))
        if d["ip"]:
            applied.append(_act("apply.apIp", cfg["network"]["ap_ip"]))
        if d["dhcp"] or d["ip"]:
            applied.append(_act("apply.dhcpOn" if cfg["dhcp"]["enabled"] else "apply.dhcpOff"))
        if d["hostname"] and cfg.get("hostname"):
            applied.append(_act("apply.hostname", cfg["hostname"]))
        # non-network changes (applied live by main.post_config)
        pk = (prev or {}).get("kline", {})
        nk = cfg.get("kline", {})
        if prev is None or pk.get("baud") != nk.get("baud"):
            applied.append(_act("apply.baud", nk.get("baud")))
        if prev is None or pk.get("echo") != nk.get("echo"):
            applied.append(_act("apply.echo"))
        if prev is None or pk.get("init") != nk.get("init"):
            applied.append(_act("apply.klineInit", nk.get("init", "fast")))
        if prev is None or pk.get("iface") != nk.get("iface"):
            applied.append(_act("apply.klineIface", nk.get("iface", "usb")))
        if prev is not None and prev.get("logging") != cfg.get("logging"):
            applied.append(_act("apply.logging"))
        if prev is not None and prev.get("locale") != cfg.get("locale"):
            applied.append(_act("apply.locale", cfg.get("locale")))
        return {
            "applied": applied,
            "messages": [],
            "iface_missing": not self.iface_present(),
            "mode": mode,
            "reconnect_required": d["wifi"] or d["ip"],
            "reboot_recommended": d["hostname"] and bool(cfg.get("hostname")),
            "ap_ip": cfg["network"]["ap_ip"],
        }

    # -- apply -------------------------------------------------------------
    @staticmethod
    def mode(cfg: dict) -> str:
        return "client" if cfg.get("wifi", {}).get("mode") == "client" else "ap"

    def apply_network(self, cfg: dict, prev: dict | None = None) -> dict:
        """Render + apply the Wi-Fi side (AP or client) + hostname.

        The two modes are exclusive and each one shuts the other down: leaving
        hostapd running while wpa_supplicant associates (or the other way round)
        just fights over the single radio.
        """
        msgs: list[str] = []
        applied: list[str] = []
        reconnect = False
        reboot = False

        net = cfg["network"]
        prefix = int(net["prefix"])

        d = self._diff_flags(cfg, prev)
        wifi_changed, ip_changed, dhcp_changed, host_changed = (
            d["wifi"], d["ip"], d["dhcp"], d["hostname"],
        )
        # No Wi-Fi adapter: skip every radio step. hostapd would otherwise sit in
        # its 30 s wait-for-wlan0 pre-start and stall whoever called us.
        if not self.iface_present():
            wifi_changed = ip_changed = dhcp_changed = False
            msgs.append(f"interface {self.iface} is absent -- Wi-Fi is not configured")

        if self.mode(cfg) == "client" and self.iface_present():
            if not wifi_changed:
                # nothing about the station side changed (locale, K-Line, logging…):
                # never tear a working link down just to re-join the same network
                return {
                    "applied": applied, "messages": msgs, "iface_missing": False,
                    "mode": "client", "client_ok": None, "fallback_to_ap": False,
                    "client": self.client_status(), "reconnect_required": False,
                    "reboot_recommended": False, "ap_ip": net["ap_ip"],
                }
            rep = self._apply_client(cfg, msgs, applied)
            if host_changed and cfg.get("hostname"):
                self.set_hostname(cfg["hostname"], msgs)
                applied.append(_act("apply.hostname", cfg["hostname"]))
                rep["reboot_recommended"] = True
            return rep

        # AP owns the radio: make sure the station side is not still holding it
        # (and that hostapd comes back at boot after a spell in client mode).
        # Enabling the unit is safe even though the dongle may be gone by the next
        # boot: its drop-in carries ConditionPathExists=/sys/class/net/wlan0, so
        # with no radio systemd skips it rather than sitting in the 30 s pre-start
        # and then restarting forever.
        if self.iface_present() and (prev is None or self.mode(prev) == "client"):
            self._stop_client(msgs)
            self._run(["systemctl", "enable", "hostapd"], msgs)
            if prev is not None:
                applied.append(_act("apply.wifiAp"))

        # 1. hostapd (SSID / passphrase / channel) — auto-pick the quietest
        #    channel first (scan needs the radio free, so stop hostapd)
        render_cfg = cfg
        if wifi_changed:
            if cfg["wifi"].get("auto_channel", True):
                self._run(["systemctl", "stop", "hostapd"], msgs)  # no-op if down
                candidates = tuple(cfg["wifi"].get("channel_candidates") or DEFAULT_CANDIDATES)
                chosen, info = pick_best_channel(self.iface, candidates)
                if chosen:
                    self.chosen_channel = chosen
                    self.last_scan_channel = chosen
                    self.last_survey = info.get("survey")
                    self.last_scan_ts = time.time()
                    render_cfg = copy.deepcopy(cfg)
                    render_cfg["wifi"]["channel"] = chosen
                    m = f"{chosen} (APs nearby: {info.get('aps_seen', '?')})"
                    applied.append(_act("apply.autoChannel", m))
                    print(f"[wifi] {m} {info.get('candidates', '')}", flush=True)
                else:
                    msgs.append(
                        f"auto channel not picked ({info.get('error', '?')}), "
                        f"channel {cfg['wifi']['channel']}"
                    )
            self._write_conf(self.hostapd_conf, self.render_hostapd(render_cfg), msgs)
            self._run(["systemctl", "restart", "hostapd"], msgs)
            applied.append(_act("apply.hostapd"))
            reconnect = True

        # 2. wlan0 IP
        if ip_changed:
            self._set_wlan_ip(net["ap_ip"], prefix, msgs)
            applied.append(_act("apply.apIp", net["ap_ip"]))
            reconnect = True

        # 3. DHCP / dnsmasq
        if dhcp_changed or ip_changed:
            if cfg["dhcp"]["enabled"]:
                self._write_conf(self.dnsmasq_conf, self.render_dnsmasq(cfg), msgs)
                self._run(["systemctl", "restart", "dnsmasq"], msgs)
                applied.append(_act("apply.dhcpOn"))
            else:
                self._run(["systemctl", "stop", "dnsmasq"], msgs)
                applied.append(_act("apply.dhcpOff"))

        # 4. hostname
        if host_changed and cfg.get("hostname"):
            self.set_hostname(cfg["hostname"], msgs)
            applied.append(_act("apply.hostname", cfg["hostname"]))
            reboot = True

        return {
            "applied": applied,
            "messages": msgs,
            "iface_missing": not self.iface_present(),
            "mode": self.mode(cfg),
            "client_ok": None,
            "fallback_to_ap": False,
            "reconnect_required": reconnect,
            "reboot_recommended": reboot,
            "ap_ip": net["ap_ip"],
            "channel": render_cfg["wifi"]["channel"],
        }
