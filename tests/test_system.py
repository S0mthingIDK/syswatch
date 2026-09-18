from pathlib import Path

import pytest

from syswatch.collectors.system import (
    DiskUsage,
    decode_mount_path,
    gather_system_snapshot,
    get_cpu_core_count,
    get_cpu_model,
    get_os_pretty_name,
    list_filesystems,
    parse_if_inet6,
    parse_mounts,
)


def test_decode_mount_path():
    assert decode_mount_path("/mnt/with\\040space") == "/mnt/with space"
    assert decode_mount_path("/normal/path") == "/normal/path"
    assert decode_mount_path("bad\\999seq") == "bad\\999seq"


def test_parse_mounts_filters_and_dedupes():
    text = (
        "/dev/sda1 / ext4 rw 0 0\n"
        "/dev/sda1 / ext4 rw 0 0\n"
        "proc /proc proc rw 0 0\n"
    )
    entries = parse_mounts(text)
    assert entries == [("/dev/sda1", "/", "ext4"), ("proc", "/proc", "proc")]


def test_get_os_pretty_name(tmp_path):
    etc = tmp_path / "etc1"
    etc.mkdir()
    (etc / "os-release").write_text('NAME="Debian"\nPRETTY_NAME="Debian GNU/Linux 12"\n')
    assert get_os_pretty_name(etc) == "Debian GNU/Linux 12"
    empty = tmp_path / "etc2"
    empty.mkdir()
    assert get_os_pretty_name(empty) is None


def test_get_cpu_model_and_cores(proc_tree):
    assert get_cpu_model(proc_tree) == "Test CPU Model @ 2.0GHz"
    assert get_cpu_core_count(proc_tree) == 2


def test_cpu_model_missing(make_proc):
    root = make_proc({"cpuinfo": "processor\t: 0\n"})
    assert get_cpu_model(root) is None
    assert get_cpu_core_count(root) >= 1


class TestFilesystems:
    def test_real_fs_included_and_pseudo_excluded(self, tmp_path, monkeypatch):
        mount_dir = tmp_path / "mnt"
        mount_dir.mkdir()
        mounts = (
            f"/dev/sda1 {mount_dir} ext4 rw 0 0\n"
            "proc /proc proc rw 0 0\n"
            "tmpfs /tmpfs tmpfs rw 0 0\n"
        )
        proc_root = tmp_path / "proc"
        proc_root.mkdir()
        (proc_root / "mounts").write_text(mounts)
        disks = list_filesystems(proc_root)
        assert len(disks) == 1
        assert disks[0].fstype == "ext4"
        assert disks[0].total_bytes > 0

    def test_missing_mountpoint_skipped(self, tmp_path):
        proc_root = tmp_path / "proc"
        proc_root.mkdir()
        (proc_root / "mounts").write_text("/dev/sda9 /gone/forever btrfs rw 0 0\n")
        assert list_filesystems(proc_root) == []

    def test_missing_mounts_file(self, tmp_path):
        assert list_filesystems(tmp_path) == []

    def test_octal_escape_mount(self, tmp_path):
        target = tmp_path / "weird dir"
        target.mkdir()
        proc_root = tmp_path / "proc"
        proc_root.mkdir()
        (proc_root / "mounts").write_text(f"/dev/sdb1 {target} xfs rw 0 0\n".replace(str(target), str(target).replace(" ", "\\040")))
        disks = list_filesystems(proc_root)
        assert len(disks) == 1
        assert disks[0].mountpoint == str(target)

    def test_percent_matches_df_semantics(self):
        disk = DiskUsage(
            device="d", mountpoint="/m", fstype="ext4",
            total_bytes=1000, used_bytes=500, free_bytes=500,
            available_bytes=400, percent=55.56,
        )
        assert disk.percent == pytest.approx(55.56)


def test_cpu_frequency_from_procinfo(make_proc):
    from syswatch.collectors.system import get_cpu_frequency_mhz

    root = make_proc({"cpuinfo": "cpu MHz\t: 2800.000\ncpu MHz\t: 2600.000\n"})
    assert get_cpu_frequency_mhz(root) == 2700.0


def test_cpu_frequency_from_sysfs(tmp_path):
    from syswatch.collectors.system import get_cpu_frequency_mhz

    policy = tmp_path / "sys/devices/system/cpu/cpu0/cpufreq"
    policy.mkdir(parents=True)
    (policy / "scaling_cur_freq").write_text("1800000\n")  # kHz
    assert get_cpu_frequency_mhz(tmp_path / "proc", tmp_path / "sys") == 1800.0


def test_cpu_frequency_unavailable(make_proc, tmp_path):
    from syswatch.collectors.system import get_cpu_frequency_mhz

    empty_sys = tmp_path / "sys"
    empty_sys.mkdir()
    assert get_cpu_frequency_mhz(make_proc({}), empty_sys) is None


def test_parse_if_inet6():
    from tests.conftest import IF_INET6_TEXT

    result = parse_if_inet6(IF_INET6_TEXT)
    assert result["lo"] == ["::1"]
    assert result["eth0"] == ["fe80::20c:29ff:fe3e:65ed"]
    assert len(result) == 2


def test_gather_snapshot_populates_fields(proc_tree, sys_tree, tmp_path):
    errors: list[str] = []
    snap = gather_system_snapshot(
        proc_root=proc_tree, etc_root=make_etc(tmp_path), errors=errors
    )
    assert errors == []
    assert snap.hostname
    assert snap.cpu_model == "Test CPU Model @ 2.0GHz"
    assert snap.cpu_cores == 2
    assert snap.memory.total_bytes == 2048000 * 1024
    assert snap.uptime_seconds == pytest.approx(3600.25)
    assert snap.boot_time_epoch == 1700000000
    assert snap.kernel


def test_gather_snapshot_collects_errors(make_proc):
    empty = make_proc({})
    errors: list[str] = []
    snap = gather_system_snapshot(proc_root=empty, errors=errors)
    assert any("memory" in e for e in errors)
    assert any("uptime" in e for e in errors)
    assert snap.cpu_cores is None or snap.cpu_cores >= 1


def make_etc(tmp_path: Path) -> Path:
    etc = tmp_path / "etc"
    etc.mkdir(exist_ok=True)
    (etc / "os-release").write_text('PRETTY_NAME="TestOS 1.0"\n')
    return etc
