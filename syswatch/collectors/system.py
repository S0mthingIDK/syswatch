"""Static system facts: host/OS/CPU identity, filesystems and NICs."""

from __future__ import annotations

import fcntl
import logging
import os
import platform
import socket
import struct
from dataclasses import dataclass, field
from pathlib import Path

from syswatch.collectors.memory import MemoryInfo, get_memory
from syswatch.paths import ETC_ROOT, PROC_ROOT, SYSFS_ROOT
from syswatch.util import read_text

log = logging.getLogger("syswatch.collectors.system")

REAL_FILESYSTEMS = frozenset(
    {
        "ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "f2fs", "jfs",
        "reiserfs", "vfat", "exfat", "ntfs", "ntfs3", "fuseblk",
        "overlay", "bcachefs",
    }
)

_SIOCGIFADDR = 0x8915


@dataclass(frozen=True)
class DiskUsage:
    device: str
    mountpoint: str
    fstype: str
    total_bytes: int = 0
    used_bytes: int = 0
    free_bytes: int = 0
    available_bytes: int = 0
    percent: float | None = None


@dataclass(frozen=True)
class NetworkInterface:
    name: str
    state: str = "unknown"
    is_up: bool = False
    mac: str | None = None
    ipv4_addresses: list[str] = field(default_factory=list)
    ipv6_addresses: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SystemSnapshot:
    hostname: str | None = None
    os_pretty_name: str | None = None
    kernel: str | None = None
    arch: str | None = None
    cpu_model: str | None = None
    cpu_cores: int | None = None
    memory: MemoryInfo | None = None
    uptime_seconds: float | None = None
    boot_time_epoch: int | None = None


def get_hostname() -> str | None:
    try:
        name = socket.gethostname()
        if name:
            return name
    except OSError:
        pass
    text = read_text(PROC_ROOT / "sys" / "kernel" / "hostname")
    return text.strip() if text else None


def parse_os_release(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if not sep or not key.strip():
            continue
        value = value.strip().strip('"').strip("'")
        values[key.strip()] = value
    return values


def get_os_pretty_name(etc_root: Path = ETC_ROOT) -> str | None:
    text = read_text(etc_root / "os-release") or read_text(
        etc_root / "usr-lib-os-release"
    )
    if text is None:
        return None
    return parse_os_release(text).get("PRETTY_NAME")


def get_cpu_model(proc_root: Path = PROC_ROOT) -> str | None:
    text = read_text(proc_root / "cpuinfo")
    if text is None:
        return None
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() == "model name":
            return value.strip()
    return None


def get_cpu_core_count(proc_root: Path = PROC_ROOT) -> int | None:
    text = read_text(proc_root / "cpuinfo")
    if text is not None:
        count = sum(1 for line in text.splitlines() if line.startswith("processor"))
        if count > 0:
            return count
    return os.cpu_count()


def get_cpu_frequency_mhz(
    proc_root: Path = PROC_ROOT, sysfs_root: Path = SYSFS_ROOT
) -> float | None:
    """Current average CPU frequency in MHz, when the kernel exposes it."""
    text = read_text(proc_root / "cpuinfo")
    if text is not None:
        values = []
        for line in text.splitlines():
            key, sep, value = line.partition(":")
            if sep and key.strip().lower() in ("cpu mhz", "clock"):
                try:
                    values.append(float(value.strip().split()[0]))
                except (ValueError, IndexError):
                    continue
        if values:
            return round(sum(values) / len(values), 1)

    # cpufreq exposes kHz on most ARM/acpi systems
    total = 0.0
    count = 0
    try:
        for policy in sorted(sysfs_root.joinpath("devices/system/cpu").glob("cpu*/cpufreq/scaling_cur_freq")):
            raw = (policy.read_text() or "").strip()
            try:
                total += float(raw) / 1000.0
                count += 1
            except ValueError:
                continue
    except OSError:
        return None
    if count:
        return round(total / count, 1)
    return None


def get_uptime(proc_root: Path = PROC_ROOT) -> float | None:
    from syswatch.util import parse_float

    text = read_text(proc_root / "uptime")
    if text is None:
        return None
    first = text.split()[0] if text.split() else ""
    return parse_float(first)


def get_boot_time(proc_root: Path = PROC_ROOT) -> int | None:
    from syswatch.util import parse_int

    text = read_text(proc_root / "stat")
    if text is None:
        return None
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "btime":
            return parse_int(parts[1])
    return None


def decode_mount_path(raw: str) -> str:
    """Decode octal escapes such as \\040 used by /proc/mounts."""
    out: list[str] = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        # Need at least 3 chars after the backslash for a full octal escape.
        if ch == "\\" and i + 3 < len(raw):
            try:
                out.append(chr(int(raw[i + 1 : i + 4], 8)))
                i += 4
                continue
            except ValueError:
                pass
        out.append(ch)
        i += 1
    return "".join(out)


def parse_mounts(text: str) -> list[tuple[str, str, str]]:
    entries: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        device, mountpoint, fstype = (
            decode_mount_path(fields[0]),
            decode_mount_path(fields[1]),
            fields[2],
        )
        key = (device, mountpoint)
        if key not in seen:
            seen.add(key)
            entries.append((device, mountpoint, fstype))
    return entries


def list_filesystems(
    proc_root: Path = PROC_ROOT,
    real_filesystems: frozenset[str] = REAL_FILESYSTEMS,
) -> list[DiskUsage]:
    """Disk usage for real mounted filesystems, matching df semantics."""
    mounts_file = proc_root / "mounts"
    text = read_text(mounts_file)
    if text is None:
        log.warning("cannot read %s", mounts_file)
        return []
    results: list[DiskUsage] = []
    for device, mountpoint, fstype in parse_mounts(text):
        if fstype not in real_filesystems:
            continue
        if not os.path.isdir(mountpoint):
            log.debug("skipping %s: mountpoint vanished", mountpoint)
            continue
        try:
            st = os.statvfs(mountpoint)
        except OSError as exc:
            log.debug("statvfs failed for %s: %s", mountpoint, exc)
            continue
        frsize = st.f_frsize or st.f_bsize or 0
        if frsize <= 0:
            continue
        total = st.f_blocks * frsize
        free_total = st.f_bfree * frsize
        available = st.f_bavail * frsize
        used = max(0, total - free_total)
        denom = used + available
        percent = round(used / denom * 100, 2) if denom > 0 else None
        results.append(
            DiskUsage(
                device=device,
                mountpoint=mountpoint,
                fstype=fstype,
                total_bytes=total,
                used_bytes=used,
                free_bytes=free_total,
                available_bytes=available,
                percent=percent,
            )
        )
    results.sort(key=lambda d: d.mountpoint)
    return results


def _ipv4_for_interface(name: str) -> str | None:
    """Primary IPv4 address of an interface via SIOCGIFADDR ioctl."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return None
    try:
        request = struct.pack("256s", name.encode()[:255])
        response = fcntl.ioctl(sock.fileno(), _SIOCGIFADDR, request)
        return socket.inet_ntoa(response[20:24])
    except OSError:
        return None
    finally:
        try:
            sock.close()
        except OSError:
            pass


def parse_if_inet6(text: str) -> dict[str, list[str]]:
    addresses: dict[str, list[str]] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 6 or len(parts[0]) != 32:
            continue
        iface = parts[5]
        try:
            packed = bytes.fromhex(parts[0])
            address = socket.inet_ntop(socket.AF_INET6, packed)
        except (ValueError, OSError):
            continue
        addresses.setdefault(iface, []).append(address)
    return addresses


def _ipv6_map(proc_root: Path) -> dict[str, list[str]]:
    text = read_text(proc_root / "net" / "if_inet6")
    if text is None:
        return {}
    return parse_if_inet6(text)


def list_network_interfaces(
    sys_root: Path = SYSFS_ROOT, proc_root: Path = PROC_ROOT
) -> list[NetworkInterface]:
    net_dir = sys_root / "class" / "net"
    try:
        names = sorted(p.name for p in net_dir.iterdir())
    except OSError as exc:
        log.warning("cannot list interfaces in %s: %s", net_dir, exc)
        return []
    ipv6_map = _ipv6_map(proc_root)
    interfaces: list[NetworkInterface] = []
    for name in names:
        state = (read_text(net_dir / name / "operstate") or "").strip() or "unknown"
        mac = (read_text(net_dir / name / "address") or "").strip() or None
        ipv4 = _ipv4_for_interface(name)
        interfaces.append(
            NetworkInterface(
                name=name,
                state=state,
                is_up=state == "up",
                mac=mac,
                ipv4_addresses=[ipv4] if ipv4 else [],
                ipv6_addresses=sorted(ipv6_map.get(name, [])),
            )
        )
    return interfaces


def gather_system_snapshot(
    proc_root: Path = PROC_ROOT,
    etc_root: Path = ETC_ROOT,
    errors: list[str] | None = None,
) -> SystemSnapshot:
    """Best-effort snapshot; appends human-readable problems to *errors*."""
    issues = errors if errors is not None else []

    hostname = get_hostname()
    if hostname is None:
        issues.append("hostname: unavailable")

    memory = get_memory(proc_root)
    if memory is None:
        issues.append("memory: unable to read memory information")

    uptime = get_uptime(proc_root)
    if uptime is None:
        issues.append("uptime: unavailable")

    return SystemSnapshot(
        hostname=hostname,
        os_pretty_name=get_os_pretty_name(etc_root),
        kernel=platform.release(),
        arch=platform.machine(),
        cpu_model=get_cpu_model(proc_root),
        cpu_cores=get_cpu_core_count(proc_root),
        memory=memory,
        uptime_seconds=uptime,
        boot_time_epoch=get_boot_time(proc_root),
    )