"""Network interface counters from /proc/net/dev."""

from __future__ import annotations

import logging
import socket
import struct
from dataclasses import dataclass
from pathlib import Path

from syswatch.paths import PROC_ROOT
from syswatch.util import parse_int, read_text

log = logging.getLogger("syswatch.collectors.network")


@dataclass(frozen=True)
class InterfaceCounters:
    rx_bytes: int = 0
    rx_packets: int = 0
    rx_errors: int = 0
    tx_bytes: int = 0
    tx_packets: int = 0
    tx_errors: int = 0


@dataclass(frozen=True)
class InterfaceRate:
    rx_bytes_per_second: float | None = None
    tx_bytes_per_second: float | None = None


def read_interface_counters(proc_root: Path = PROC_ROOT) -> dict[str, InterfaceCounters]:
    """Parse /proc/net/dev into per-interface byte/packet counters."""
    text = read_text(proc_root / "net" / "dev")
    if text is None:
        log.warning("cannot read %s/net/dev", proc_root)
        return {}
    result: dict[str, InterfaceCounters] = {}
    for line in text.splitlines()[2:]:
        name, sep, rest = line.partition(":")
        if not sep:
            continue
        fields = [parse_int(f) for f in rest.split()]
        if len(fields) < 16 or any(f is None for f in fields[:16]):
            log.debug("skipping malformed /proc/net/dev line: %r", line)
            continue
        nums = [f or 0 for f in fields[:16]]
        result[name.strip()] = InterfaceCounters(
            rx_bytes=nums[0],
            rx_packets=nums[1],
            rx_errors=nums[2],
            tx_bytes=nums[8],
            tx_packets=nums[9],
            tx_errors=nums[10],
        )
    return result


def interface_rates(
    prev: dict[str, InterfaceCounters],
    cur: dict[str, InterfaceCounters],
    interval_seconds: float,
) -> dict[str, InterfaceRate]:
    """Per-interface throughput between two counter snapshots."""
    rates: dict[str, InterfaceRate] = {}
    if interval_seconds <= 0:
        return rates
    for name, cur_c in cur.items():
        prev_c = prev.get(name)
        if prev_c is None:
            continue
        rx_delta = max(0, cur_c.rx_bytes - prev_c.rx_bytes)
        tx_delta = max(0, cur_c.tx_bytes - prev_c.tx_bytes)
        rates[name] = InterfaceRate(
            rx_bytes_per_second=round(rx_delta / interval_seconds, 1),
            tx_bytes_per_second=round(tx_delta / interval_seconds, 1),
        )
    return rates


def total_rates(
    rates: dict[str, InterfaceRate], exclude: tuple[str, ...] = ("lo",)
) -> tuple[float, float]:
    """Aggregate throughput across interfaces, ignoring loopback by default."""
    rx = sum(
        (r.rx_bytes_per_second or 0.0)
        for name, r in rates.items()
        if name not in exclude
    )
    tx = sum(
        (r.tx_bytes_per_second or 0.0)
        for name, r in rates.items()
        if name not in exclude
    )
    return round(rx, 1), round(tx, 1)


def default_gateways(proc_root: Path = PROC_ROOT) -> list[tuple[str, str]]:
    """(interface, gateway_ip) pairs for default routes in /proc/net/route."""
    text = read_text(proc_root / "net" / "route")
    if text is None:
        log.debug("cannot read %s/net/route", proc_root)
        return []
    gateways: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 3:
            continue
        iface, destination, gateway_hex = fields[0], fields[1], fields[2]
        if destination != "00000000" or gateway_hex == "00000000":
            continue
        try:
            # The kernel prints the gateway as the host representation of a
            # big-endian address, so it must be unpacked little-endian.
            packed = struct.pack("<I", int(gateway_hex, 16))
            address = socket.inet_ntoa(packed)
        except (ValueError, OSError):
            continue
        key = (iface, address)
        if key not in seen:
            seen.add(key)
            gateways.append(key)
    return gateways