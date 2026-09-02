"""Offline tests for the log-destination manager (app/web/storage.py).

No block devices and no root: lsblk is replaced by a canned JSON document,
/proc/mounts and /sys/block are temp trees, and every shell-out lands in a
recorder instead of touching a disk. The destructive path (format) is therefore
exercised for its *guards* and its command order, which is the part that must
never regress.

Run directly:  python tests/test_storage.py
"""

import json
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.web import storage  # noqa: E402
from app.web.storage import StorageManager, day_name, parse_day, resolve_root  # noqa: E402

# One 32 GB stick with an exFAT partition, plus the board's own SD card. lsblk
# on the NEO3 reports the card as mmcblk0 with the root filesystem on p9.
LSBLK = {
    "blockdevices": [
        {"name": "sda", "path": "/dev/sda", "size": 31000000000, "type": "disk",
         "rm": True, "model": "Ultra Fit", "vendor": "SanDisk",
         "children": [
             {"name": "sda1", "path": "/dev/sda1", "size": 30990000000,
              "type": "part", "fstype": "exfat", "label": "LOGS",
              "uuid": "AABB-CCDD", "mountpoint": None},
         ]},
        {"name": "mmcblk0", "path": "/dev/mmcblk0", "size": 62000000000,
         "type": "disk", "rm": False, "model": "SD32G", "vendor": "",
         "children": [
             {"name": "mmcblk0p9", "path": "/dev/mmcblk0p9", "size": 61000000000,
              "type": "part", "fstype": "ext4", "label": "rootfs",
              "uuid": "1111-2222", "mountpoint": "/"},
         ]},
    ]
}


def _fake(tmp: Path, mounts: str = "", lsblk: dict | None = None) -> tuple[str, str]:
    """Temp /sys and /proc; sda hangs off USB, mmcblk0 does not."""
    sysd, procd = tmp / "sys", tmp / "proc"
    (sysd / "devices" / "platform" / "usb1" / "sda").mkdir(parents=True)
    (sysd / "devices" / "platform" / "mmc0" / "mmcblk0").mkdir(parents=True)
    (sysd / "block").mkdir(parents=True)
    (sysd / "block" / "sda").symlink_to(sysd / "devices" / "platform" / "usb1" / "sda")
    (sysd / "block" / "mmcblk0").symlink_to(
        sysd / "devices" / "platform" / "mmc0" / "mmcblk0")
    procd.mkdir(parents=True)
    (procd / "mounts").write_text(mounts or "/dev/mmcblk0p9 / ext4 rw 0 0\n")
    storage._out = lambda cmd: json.dumps(LSBLK if lsblk is None else lsblk)
    return str(sysd), str(procd)


def _rec():
    """Replace the shell-out with a recorder; returns the list it fills.

    Also tells the preflight that every tool is present — the recorder stands in
    for a board that can run them, and the dev host has none of them.
    """
    ran = []

    def run(cmd, stdin_text="", timeout=20):
        ran.append(" ".join(cmd))
        return True, ""

    storage._run = run
    storage.shutil.which = lambda name: "/sbin/" + name
    return ran


class _no_tool:
    """Hide one binary from the preflight, and put it back afterwards."""

    def __init__(self, name):
        self.name = name

    def __enter__(self):
        self.real = storage.shutil.which
        storage.shutil.which = lambda n: None if n == self.name else "/sbin/" + n
        return self

    def __exit__(self, *exc):
        storage.shutil.which = self.real
        return False


def _sm(tmp, internal, mounts="", cfg=None, lsblk=None):
    sysd, procd = _fake(Path(tmp), mounts, lsblk)
    return StorageManager(internal, cfg or {}, sys_root=sysd, proc_root=procd)


# -- day folders ------------------------------------------------------------
def test_day_name_and_parse():
    assert day_name(date(2026, 8, 31)) == "31-08-2026"
    assert parse_day("31-08-2026") == date(2026, 8, 31)
    assert parse_day("2026-08-31") is None          # the ISO form is not ours
    assert parse_day("31-13-2026") is None          # no thirteenth month
    assert parse_day("..") is None and parse_day("") is None


def test_day_folders_sort_by_date_not_by_name():
    names = ["01-09-2026", "31-08-2026"]
    assert sorted(names) == ["01-09-2026", "31-08-2026"], "lexicographic order is wrong"
    assert sorted(names, key=parse_day) == ["31-08-2026", "01-09-2026"]


def test_resolve_root_takes_a_callable():
    assert resolve_root("/root/k-line") == Path("/root/k-line")
    box = ["/a"]
    root = lambda: box[0]                                          # noqa: E731
    assert resolve_root(root) == Path("/a")
    box[0] = "/b"
    assert resolve_root(root) == Path("/b"), "must re-ask, not cache"


# -- enumeration ------------------------------------------------------------
def test_devices_lists_the_stick_and_hides_the_boot_card():
    with tempfile.TemporaryDirectory() as tmp:
        sm = _sm(tmp, tmp + "/k-line")
        devs = sm.devices()
        assert [d["dev"] for d in devs] == ["/dev/sda"], "only the USB disk"
        assert devs[0]["model"] == "Ultra Fit"
        p = devs[0]["parts"][0]
        assert p["uuid"] == "AABB-CCDD" and p["usable"] and p["reason"] == ""


def test_unformatted_partition_is_offered_for_formatting():
    blank = json.loads(json.dumps(LSBLK))
    blank["blockdevices"][0]["children"][0]["fstype"] = None
    with tempfile.TemporaryDirectory() as tmp:
        sm = _sm(tmp, tmp + "/k-line", lsblk=blank)
        p = sm.devices()[0]["parts"][0]
        assert not p["usable"] and p["reason"] == "no_fs"


def test_no_lsblk_is_not_an_error():
    with tempfile.TemporaryDirectory() as tmp:
        sm = _sm(tmp, tmp + "/k-line")
        storage._out = lambda cmd: ""          # dev host: no lsblk at all
        assert sm.devices() == []
        assert sm.active_root() == Path(tmp + "/k-line")


# -- active_root, all four branches -----------------------------------------
def test_internal_is_the_default():
    with tempfile.TemporaryDirectory() as tmp:
        sm = _sm(tmp, tmp + "/k-line")
        assert sm.active_root() == Path(tmp + "/k-line")
        assert sm.fallback() == ""


def test_usb_selected_and_mounted():
    with tempfile.TemporaryDirectory() as tmp:
        mnt = tmp + "/media/usb0"
        Path(mnt).mkdir(parents=True)
        sm = _sm(tmp, tmp + "/k-line",
                 mounts=f"/dev/mmcblk0p9 / ext4 rw 0 0\n/dev/sda1 {mnt} exfat rw 0 0\n",
                 cfg={"dest": "usb", "mount_point": mnt, "usb_uuid": "AABB-CCDD"})
        assert sm.active_root() == Path(mnt + "/k-line"), "logs go to the stick"
        assert sm.fallback() == ""


def test_usb_selected_but_absent_falls_back_with_a_warning():
    with tempfile.TemporaryDirectory() as tmp:
        sm = _sm(tmp, tmp + "/k-line",
                 cfg={"dest": "usb", "mount_point": tmp + "/media/usb0",
                      "usb_uuid": "AABB-CCDD"})
        assert sm.active_root() == Path(tmp + "/k-line"), "the ride is never lost"
        assert sm.fallback() == "usb_missing", "and the UI is told why"


def test_usb_root_uses_the_internal_folder_name():
    with tempfile.TemporaryDirectory() as tmp:
        sm = _sm(tmp, "/root/k-line", cfg={"mount_point": "/media/usb0"})
        assert sm.usb_root() == Path("/media/usb0/k-line")


# -- mounting ---------------------------------------------------------------
def test_mount_by_uuid_issues_one_mount():
    with tempfile.TemporaryDirectory() as tmp:
        sm = _sm(tmp, tmp + "/k-line", cfg={"mount_point": tmp + "/media/usb0"})
        ran = _rec()
        res = sm.mount(uuid="AABB-CCDD")
        assert res["ok"], res
        assert len(ran) == 1 and ran[0].startswith("mount -o rw,noatime ")
        assert "/dev/sda1" in ran[0], "the partition, not the disk"


def test_exfat_is_never_mounted_with_flush():
    """The kernel answers "exfat: Unknown parameter 'flush'" and refuses the
    whole mount, so the option that protects a yanked vfat stick must not be
    sent to an exFAT one."""
    with tempfile.TemporaryDirectory() as tmp:
        sm = _sm(tmp, tmp + "/k-line", cfg={"mount_point": tmp + "/media/usb0"})
        ran = _rec()
        assert sm.mount(uuid="AABB-CCDD")["ok"]          # the canned stick is exfat
        assert "flush" not in ran[0], ran[0]


def test_vfat_still_gets_flush():
    fat = json.loads(json.dumps(LSBLK))
    fat["blockdevices"][0]["children"][0]["fstype"] = "vfat"
    with tempfile.TemporaryDirectory() as tmp:
        sm = _sm(tmp, tmp + "/k-line", cfg={"mount_point": tmp + "/media/usb0"},
                 lsblk=fat)
        ran = _rec()
        assert sm.mount(uuid="AABB-CCDD")["ok"]
        assert ran[0].startswith("mount -o rw,noatime,flush "), ran[0]


def test_mount_refuses_a_device_that_is_not_in_the_list():
    with tempfile.TemporaryDirectory() as tmp:
        sm = _sm(tmp, tmp + "/k-line")
        ran = _rec()
        res = sm.mount(dev="/dev/mmcblk0p9")     # the boot card
        assert res == {"ok": False, "error": "err.storage_unknown_dev"}
        assert ran == [], "nothing may be run for a device we did not enumerate"


def test_unmount_syncs_before_letting_go():
    with tempfile.TemporaryDirectory() as tmp:
        mnt = tmp + "/media/usb0"
        sm = _sm(tmp, tmp + "/k-line",
                 mounts=f"/dev/sda1 {mnt} exfat rw 0 0\n",
                 cfg={"mount_point": mnt})
        ran = _rec()
        assert sm.unmount()["ok"]
        assert ran == ["sync", f"umount {mnt}"], "flush first, then umount"


# -- destination ------------------------------------------------------------
def test_select_usb_mounts_and_remembers_the_uuid():
    with tempfile.TemporaryDirectory() as tmp:
        sm = _sm(tmp, tmp + "/k-line", cfg={"mount_point": tmp + "/media/usb0"})
        _rec()
        res = sm.select("usb", uuid="AABB-CCDD")
        assert res["ok"] and sm.dest == "usb"
        assert sm.usb_uuid == "AABB-CCDD", "so tick() can find it again"


def test_select_rejects_a_bogus_destination():
    with tempfile.TemporaryDirectory() as tmp:
        sm = _sm(tmp, tmp + "/k-line")
        assert sm.select("floppy") == {"ok": False, "error": "err.storage_bad_dest"}


# -- formatting: the guards matter more than the command --------------------
def test_format_refuses_the_boot_device():
    with tempfile.TemporaryDirectory() as tmp:
        sm = _sm(tmp, tmp + "/k-line")
        ran = _rec()
        res = sm.format("/dev/mmcblk0", "exfat")
        assert res["error"] == "err.storage_unknown_dev"
        assert ran == [], "the board's own card never reaches wipefs"


def test_format_refuses_an_unknown_device():
    with tempfile.TemporaryDirectory() as tmp:
        sm = _sm(tmp, tmp + "/k-line")
        ran = _rec()
        assert sm.format("/dev/sdz", "exfat")["error"] == "err.storage_unknown_dev"
        assert ran == []


def test_format_refuses_an_unsupported_filesystem():
    with tempfile.TemporaryDirectory() as tmp:
        sm = _sm(tmp, tmp + "/k-line")
        ran = _rec()
        assert sm.format("/dev/sda", "btrfs")["error"] == "err.storage_bad_fs"
        assert ran == []


def test_format_order_is_wipe_partition_mkfs():
    with tempfile.TemporaryDirectory() as tmp:
        sm = _sm(tmp, tmp + "/k-line", cfg={"mount_point": tmp + "/media/usb0"})
        ran = _rec()
        res = sm.format("/dev/sda", "exfat", label="my logs!")
        assert res["ok"], res
        assert ran[0] == "wipefs -a /dev/sda"
        assert ran[1].startswith("sfdisk --label dos /dev/sda")
        assert "mkfs.exfat -n MYLOGS /dev/sda1" in ran, f"label sanitised: {ran}"
        assert ran.index("wipefs -a /dev/sda") < ran.index("mkfs.exfat -n MYLOGS /dev/sda1")


def test_format_fat32_uses_mkfs_vfat_and_an_11_char_label():
    with tempfile.TemporaryDirectory() as tmp:
        sm = _sm(tmp, tmp + "/k-line", cfg={"mount_point": tmp + "/media/usb0"})
        ran = _rec()
        assert sm.format("/dev/sda", "vfat", label="abcdefghijklmnop")["ok"]
        assert "mkfs.vfat -F 32 -n ABCDEFGHIJK /dev/sda1" in ran, ran


def test_a_missing_tool_is_found_before_anything_is_destroyed():
    """The real 2026-08-31 failure: the board had wipefs but no sfdisk, so the
    partition table was erased and the run then died — leaving a stick with no
    table and no filesystem. Feasibility has to be settled before the wipe."""
    with tempfile.TemporaryDirectory() as tmp:
        sm = _sm(tmp, tmp + "/k-line", cfg={"mount_point": tmp + "/media/usb0"})
        ran = _rec()
        with _no_tool("sfdisk"):
            res = sm.format("/dev/sda", "exfat")
        assert res["error"] == "err.storage_no_tools", res
        assert "sfdisk" in res.get("missing", []), res
        assert not any(c.startswith("wipefs") for c in ran), \
            f"the device was touched before the run could finish: {ran}"


def test_only_the_mkfs_missing_still_offers_the_other_filesystem():
    with tempfile.TemporaryDirectory() as tmp:
        sm = _sm(tmp, tmp + "/k-line", cfg={"mount_point": tmp + "/media/usb0"})
        ran = _rec()
        with _no_tool("mkfs.exfat"):
            res = sm.format("/dev/sda", "exfat")
        assert res["error"] == "err.storage_no_mkfs", res
        assert ran == [], "nothing may run when the filesystem cannot be made"


def test_format_waits_for_the_kernel_to_see_the_new_filesystem():
    """Straight after mkfs the stick still reads as "no filesystem", so a format
    that returned immediately rendered as a drive that had not worked."""
    with tempfile.TemporaryDirectory() as tmp:
        blank = json.loads(json.dumps(LSBLK))
        blank["blockdevices"][0]["children"][0]["fstype"] = None
        sm = _sm(tmp, tmp + "/k-line", cfg={"mount_point": tmp + "/media/usb0"},
                 lsblk=blank)
        ran = _rec()
        calls = {"n": 0}

        def out(cmd):
            # the third look is the one where blkid can finally name it
            calls["n"] += 1
            return json.dumps(LSBLK if calls["n"] >= 3 else blank)

        storage._out = out
        res = sm.format("/dev/sda", "exfat")
        assert res["ok"], res
        assert "udevadm settle" in ran, ran
        assert calls["n"] >= 3, "format returned before the filesystem was visible"


def test_a_failing_command_reports_which_step_and_why():
    """A tool that exists but fails must hand its own words back to the UI —
    "formatting failed" with nothing else took an ssh session to explain once."""
    with tempfile.TemporaryDirectory() as tmp:
        sm = _sm(tmp, tmp + "/k-line", cfg={"mount_point": tmp + "/media/usb0"})
        _rec()

        def run(cmd, stdin_text="", timeout=20):
            if cmd[0].startswith("mkfs."):
                return False, "mkfs.exfat: Device is busy"
            return True, ""

        storage._run = run
        res = sm.format("/dev/sda", "exfat")
        assert res["error"] == "err.storage_format"
        assert res["step"] == "mkfs.exfat" and "busy" in res["detail"], res


# -- automatic re-attach ----------------------------------------------------
def test_tick_remounts_the_remembered_stick():
    with tempfile.TemporaryDirectory() as tmp:
        sm = _sm(tmp, tmp + "/k-line",
                 cfg={"dest": "usb", "mount_point": tmp + "/media/usb0",
                      "usb_uuid": "AABB-CCDD"})
        ran = _rec()
        sm.tick()
        assert any(c.startswith("mount ") for c in ran)


def test_tick_ignores_a_stranger_stick():
    with tempfile.TemporaryDirectory() as tmp:
        sm = _sm(tmp, tmp + "/k-line",
                 cfg={"dest": "usb", "mount_point": tmp + "/media/usb0",
                      "usb_uuid": "0000-0000"})       # a different stick was chosen
        ran = _rec()
        sm.tick()
        assert ran == [], "only the stick the rider picked is picked up"


def test_tick_does_nothing_when_the_destination_is_internal():
    with tempfile.TemporaryDirectory() as tmp:
        sm = _sm(tmp, tmp + "/k-line",
                 cfg={"dest": "internal", "mount_point": tmp + "/media/usb0",
                      "usb_uuid": "AABB-CCDD"})
        ran = _rec()
        sm.tick()
        assert ran == [], "a plugged stick must not hijack the destination"


def test_status_carries_what_the_ui_needs():
    with tempfile.TemporaryDirectory() as tmp:
        sm = _sm(tmp, tmp + "/k-line",
                 cfg={"dest": "usb", "mount_point": tmp + "/media/usb0"})
        st = sm.status()
        for k in ("dest", "root", "is_usb", "fallback", "mounted", "devices"):
            assert k in st, k
        assert st["fallback"] == "usb_missing" and st["is_usb"] is False


def _main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _main()
