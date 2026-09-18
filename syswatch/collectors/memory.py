"""Memory information from /proc/meminfo."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from syswatch.paths import PROC_ROOT
from syswatch.util import read_text

log = logging.getLogger("syswatch.collectors.memory")


@dataclass(frozen=True)
class MemoryInfo:
    total_bytes: int
    available_bytes: int | None = None
    free_bytes: int | None = None
    used_bytes: int | None = None
    used_percent: float | None = None
    swap_total_bytes: int | None = None
    swap_free_bytes: int | None = None
    swap_used_percent: float | None = None


def parse_meminfo(text: str) -> dict[str, int]:
    """Parse /proc/meminfo content into a mapping of bytes values."""
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        key = key.strip()
        if not key:
            continue
        fields = rest.split()
        if not fields:
            continue
        try:
            amount = int(fields[0])
        except ValueError:
            log.debug("unparsable meminfo line ignored: %r", line)
            continue
        unit = fields[1].lower() if len(fields) > 1 else "kb"
        multiplier = 1024 if unit == "kb" else 1
        values[key] = amount * multiplier
    return values


def memory_info_from(meminfo: dict[str, int]) -> MemoryInfo | None:
    total = meminfo.get("MemTotal")
    if total is None or total <= 0:
        return None
    free = meminfo.get("MemFree")
    available = meminfo.get("MemAvailable")
    if available is None:
        reclaimable = (
            (meminfo.get("Buffers") or 0)
            + (meminfo.get("Cached") or 0)
            + (meminfo.get("SReclaimable") or 0)
        )
        available = (free or 0) + reclaimable
    used = max(0, total - available)
    used_percent = used / total * 100 if total else None

    swap_total = meminfo.get("SwapTotal", 0) or 0
    swap_free = meminfo.get("SwapFree")
    swap_used_percent = None
    if swap_total > 0 and swap_free is not None:
        swap_used_percent = max(0.0, (swap_total - swap_free) / swap_total * 100)

    return MemoryInfo(
        total_bytes=total,
        available_bytes=available,
        free_bytes=free,
        used_bytes=used,
        used_percent=used_percent,
        swap_total_bytes=swap_total,
        swap_free_bytes=swap_free,
        swap_used_percent=swap_used_percent,
    )


def get_memory(proc_root: Path = PROC_ROOT) -> MemoryInfo | None:
    text = read_text(proc_root / "meminfo")
    if text is None:
        log.warning("cannot read %s", proc_root / "meminfo")
        return None
    info = memory_info_from(parse_meminfo(text))
    if info is None:
        log.warning("meminfo did not contain a usable MemTotal value")
    return info
