"""Shared fixtures: synthetic /proc and /sys trees for hermetic testing."""

from __future__ import annotations

from pathlib import Path

import pytest

MEMINFO_TEXT = """MemTotal:       2048000 kB
MemFree:         512000 kB
MemAvailable:    1024000 kB
Buffers:         128000 kB
Cached:          384000 kB
SwapTotal:       512000 kB
SwapFree:        256000 kB
Slab:             64000 kB
"""

PROC_STAT_TEXT = """cpu  100 20 50 800 40 10 5 5 0 0
cpu0 60 10 30 400 20 5 2 2 0 0
cpu1 40 10 20 400 20 5 3 3 0 0
intr 123456
btime 1700000000
"""

NET_DEV_TEXT = """Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
  eth0: 10000000   20000    0    0    0     0          0         0  5000000   15000    1    0    0     0       0          0
  wlan0: 2000   100    0    0    0     0          0         0   3000    120    0    0    0     0       0          0
broken-line-without-colon
  badif: not numbers here
"""

# Gateway/destination hex matches what /proc/net/route actually prints on
# little-endian machines: the host representation of the big-endian address.
#   192.168.100.1 -> 0164A8C0 ; 172.16.0.1 -> 010010AC
ROUTE_TEXT = """Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT
eth0\t00000000\t0164A8C0\t0003\t0\t0\t0\t00000000\t0\t0\t0
eth0\t0064A8C0\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0
wlan0\t00000000\t010010AC\t0003\t0\t0\t100\t00000000\t0\t0\t0
"""

IF_INET6_TEXT = """fe80000000000000020c29fffe3e65ed 02 40 20 80 eth0
00000000000000000000000000000001 01 80 10 80 lo
shortline 01 80 10
"""


def make_stat_line(
    pid: int = 1,
    comm: str = "init",
    state: str = "S",
    ppid: int = 0,
    utime: float = 10,
    stime: float = 5,
    rss_pages: int = 200,
    num_threads: int = 1,
    starttime_ticks: float = 500.0,
) -> str:
    rest = ["0"] * 40
    rest[0] = state
    rest[1] = str(ppid)
    rest[11] = str(utime)
    rest[12] = str(stime)
    rest[17] = str(num_threads)
    rest[19] = str(starttime_ticks)
    rest[21] = str(rss_pages)
    return f"{pid} ({comm}) {' '.join(rest)}"


@pytest.fixture
def make_proc(tmp_path: Path):
    """Factory writing a dict of {relative_path: text_content} under tmp."""

    def _make(files: dict[str, str]) -> Path:
        root = tmp_path / "proc"
        root.mkdir(exist_ok=True)
        for rel, content in files.items():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        return root

    return _make


@pytest.fixture
def proc_tree(make_proc) -> Path:
    """A representative fake /proc covering all collectors."""
    files = {
        "meminfo": MEMINFO_TEXT,
        "stat": PROC_STAT_TEXT,
        "uptime": "3600.25 7200.50\n",
        "cpuinfo": (
            "processor\t: 0\nmodel name\t: Test CPU Model @ 2.0GHz\n"
            "processor\t: 1\nmodel name\t: Test CPU Model @ 2.0GHz\n"
        ),
        "mounts": "/dev/sda1 / ext4 rw 0 0\nproc /proc proc rw 0 0\ntmpfs /tmp tmpfs rw 0 0\n",
        "net/dev": NET_DEV_TEXT,
        "net/route": ROUTE_TEXT,
        "net/if_inet6": IF_INET6_TEXT,
        "1/stat": make_stat_line(pid=1, comm="systemd", state="S", rss_pages=250),
        "1/cmdline": "/sbin/init\x00--user\x00",
        "42/stat": make_stat_line(pid=42, comm="worker", state="R", ppid=1, utime=100, stime=50, rss_pages=1024),
        "42/cmdline": "/usr/bin/worker\x00--flag\x00",
        "99/stat": make_stat_line(pid=99, comm="zomb", state="Z", ppid=1, rss_pages=0),
    }
    return make_proc(files)


@pytest.fixture
def sys_tree(tmp_path: Path) -> Path:
    root = tmp_path / "sys"
    for iface, mac, state in (
        ("eth0", "aa:bb:cc:dd:ee:01", "up"),
        ("wlan0", "aa:bb:cc:dd:ee:02", "down"),
    ):
        d = root / "class" / "net" / iface
        d.mkdir(parents=True)
        (d / "address").write_text(mac + "\n")
        (d / "operstate").write_text(state + "\n")
    return root
