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


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


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
        return cfg

    def save(self, cfg: dict) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))

    # -- validation --------------------------------------------------------
    def validate(self, cfg: dict) -> None:
        wifi = cfg["wifi"]
        mode = self.mode(cfg)
        if wifi.get("mode", "ap") not in ("ap", "client"):
            raise ValueError("Режим Wi-Fi: ap или client")
        if not (1 <= len(wifi["ssid"]) <= 32):
            raise ValueError("SSID должен быть 1..32 символа")
        pw = wifi.get("password", "")
        if pw and not (8 <= len(pw) <= 63):
            raise ValueError("Пароль WPA2 должен быть 8..63 символа (или пустой = открытая сеть)")
        if not (1 <= int(wifi["channel"]) <= 14):
            raise ValueError("Канал Wi-Fi должен быть 1..14")

        client = wifi.get("client", {})
        if mode == "client":
            if not (1 <= len(client.get("ssid", "")) <= 32):
                raise ValueError("Сеть для подключения: укажите SSID")
            cpw = client.get("password", "")
            if cpw and not (8 <= len(cpw) <= 63):
                raise ValueError("Пароль сети должен быть 8..63 символа (или пустой = открытая)")
            if str(client.get("ipv4", "dhcp")) not in ("dhcp", "static"):
                raise ValueError("Адрес клиента: dhcp или static")
            if str(client.get("ipv4", "dhcp")) == "static":
                cip = ipaddress.ip_address(client.get("ip", ""))
                cnet = ipaddress.ip_network(f"{cip}/{int(client.get('prefix', 24))}", strict=False)
                gw = client.get("gateway", "")
                if gw and ipaddress.ip_address(gw) not in cnet:
                    raise ValueError(f"Шлюз {gw} вне подсети {cnet}")
                for d in str(client.get("dns", "")).replace(",", " ").split():
                    ipaddress.ip_address(d)   # raises on junk

        net = cfg["network"]
        ap_ip = ipaddress.ip_address(net["ap_ip"])
        prefix = int(net["prefix"])
        subnet = ipaddress.ip_network(f"{ap_ip}/{prefix}", strict=False)
        ap_gw = net.get("gateway", "")
        if ap_gw and ipaddress.ip_address(ap_gw) not in subnet:
            raise ValueError(f"Адрес маршрутизатора {ap_gw} вне подсети {subnet}")

        dhcp = cfg["dhcp"]
        if dhcp["enabled"]:
            start = ipaddress.ip_address(dhcp["pool_start"])
            end = ipaddress.ip_address(dhcp["pool_end"])
            if start > end:
                raise ValueError("Начало пула DHCP больше конца")
            for ip in (start, end):
                if ip not in subnet:
                    raise ValueError(f"Адрес пула {ip} вне подсети {subnet}")
            if ap_ip in {start, end} or start <= ap_ip <= end:
                raise ValueError("IP точки доступа не должен входить в пул DHCP")

        host = cfg.get("hostname", "")
        if host and not all(c.isalnum() or c == "-" for c in host):
            raise ValueError("Hostname: только буквы, цифры и дефис")

        country = wifi.get("country", "")
        if not (len(country) == 2 and country.isalpha()):
            raise ValueError("Код страны: 2 буквы (например DE)")

        baud = cfg.get("kline", {}).get("baud", "auto")
        if str(baud).lower() != "auto":
            try:
                b = int(baud)
            except (TypeError, ValueError):
                raise ValueError("Скорость шины: Auto или число")
            if not (300 <= b <= 115200):
                raise ValueError("Скорость шины вне диапазона 300..115200")

        locale = cfg.get("locale", "en")
        if locale not in {"en", "de", "es", "fr", "it", "nl", "bg", "ru"}:
            raise ValueError(f"Неизвестная локаль: {locale}")

        # the diagnostics log shares the SD card with the ride logs: a limit of
        # 0 would rotate on every line, a huge one fills the card
        mb = cfg.get("diag", {}).get("max_mb", 2)
        try:
            mb = float(mb)
        except (TypeError, ValueError):
            raise ValueError("Размер diag-файла: число в МБ")
        if not (0.05 <= mb <= 64):
            raise ValueError("Размер diag-файла: 0.05..64 МБ")

        # the log destination is picked through /api/storage (which has to mount
        # something); this only stops a hand-edited config.json from arriving
        st = cfg.get("storage", {})
        if st.get("dest", "internal") not in ("internal", "usb"):
            raise ValueError("Место записи: internal или usb")
        mp = str(st.get("mount_point", "/media/usb0"))
        if not mp.startswith("/") or ".." in mp:
            raise ValueError("Точка монтирования: абсолютный путь")

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
            msgs.append(f"пропущено (нет {cmd[0]}): {' '.join(cmd)}")
            return False
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            if r.returncode != 0:
                msgs.append(f"ошибка `{' '.join(cmd)}`: {r.stderr.strip() or r.stdout.strip()}")
                return False
            return True
        except (OSError, subprocess.SubprocessError) as exc:
            msgs.append(f"исключение `{' '.join(cmd)}`: {exc}")
            return False

    def _write_conf(self, path: Path, content: str, msgs: list[str],
                    mode: int | None = None) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            if mode is not None:
                path.chmod(mode)
        except OSError as exc:
            msgs.append(f"не удалось записать {path}: {exc}")

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
        applied.append(f"Режим Wi-Fi → клиент ({ssid})")

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
            applied.append(f"Адрес вручную → {client.get('ip', '')}/{client.get('prefix', 24)}")
        else:
            self._run(["dhcpcd", "-b", self.iface], msgs)
            applied.append("Адрес по DHCP")

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
            msgs.append(f"клиентский режим не поднялся: {why} — возвращаю точку доступа")
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
            return False, "нет ассоциации с сетью (SSID, пароль или уровень сигнала)"
        return False, "ассоциация есть, но адреса нет (DHCP не ответил)"

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
                applied.append(f"Режим Wi-Fi → клиент ({client.get('ssid', '')})")
                applied.append("Адрес вручную" if client.get("ipv4") == "static" else "Адрес по DHCP")
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
            applied.append("Режим Wi-Fi → точка доступа" if mode_changed else "Wi-Fi (hostapd)")
        if d["ip"]:
            applied.append(f"IP точки доступа → {cfg['network']['ap_ip']}")
        if d["dhcp"] or d["ip"]:
            applied.append("DHCP включён" if cfg["dhcp"]["enabled"] else "DHCP выключен")
        if d["hostname"] and cfg.get("hostname"):
            applied.append(f"Hostname → {cfg['hostname']}")
        # non-network changes (applied live by main.post_config)
        pk = (prev or {}).get("kline", {})
        nk = cfg.get("kline", {})
        if prev is None or pk.get("baud") != nk.get("baud"):
            applied.append(f"Скорость шины → {nk.get('baud')}")
        if prev is None or pk.get("echo") != nk.get("echo"):
            applied.append("Эхо K-Line")
        if prev is None or pk.get("init") != nk.get("init"):
            applied.append(f"Init K-Line → {nk.get('init', 'fast')}")
        if prev is not None and prev.get("logging") != cfg.get("logging"):
            applied.append("Логирование по умолчанию")
        if prev is not None and prev.get("locale") != cfg.get("locale"):
            applied.append(f"Язык → {cfg.get('locale')}")
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
            msgs.append(f"интерфейс {self.iface} отсутствует — Wi-Fi не настраивается")

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
                applied.append(f"Hostname → {cfg['hostname']}")
                rep["reboot_recommended"] = True
            return rep

        # AP owns the radio: make sure the station side is not still holding it
        # (and that hostapd comes back at boot after a spell in client mode)
        if self.iface_present() and (prev is None or self.mode(prev) == "client"):
            self._stop_client(msgs)
            self._run(["systemctl", "enable", "hostapd"], msgs)
            if prev is not None:
                applied.append("Режим Wi-Fi → точка доступа")

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
                    m = f"авто-канал {chosen} (AP рядом: {info.get('aps_seen', '?')})"
                    applied.append(m)
                    print(f"[wifi] {m} {info.get('candidates', '')}", flush=True)
                else:
                    msgs.append(
                        f"авто-канал не выбран ({info.get('error', '?')}), "
                        f"канал {cfg['wifi']['channel']}"
                    )
            self._write_conf(self.hostapd_conf, self.render_hostapd(render_cfg), msgs)
            self._run(["systemctl", "restart", "hostapd"], msgs)
            applied.append("Wi-Fi (hostapd)")
            reconnect = True

        # 2. wlan0 IP
        if ip_changed:
            self._set_wlan_ip(net["ap_ip"], prefix, msgs)
            applied.append(f"IP точки доступа → {net['ap_ip']}")
            reconnect = True

        # 3. DHCP / dnsmasq
        if dhcp_changed or ip_changed:
            if cfg["dhcp"]["enabled"]:
                self._write_conf(self.dnsmasq_conf, self.render_dnsmasq(cfg), msgs)
                self._run(["systemctl", "restart", "dnsmasq"], msgs)
                applied.append("DHCP включён")
            else:
                self._run(["systemctl", "stop", "dnsmasq"], msgs)
                applied.append("DHCP выключен")

        # 4. hostname
        if host_changed and cfg.get("hostname"):
            self.set_hostname(cfg["hostname"], msgs)
            applied.append(f"Hostname → {cfg['hostname']}")
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
