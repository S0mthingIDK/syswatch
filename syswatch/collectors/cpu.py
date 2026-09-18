"""CPU utilisation from /proc/stat deltas, plus load average."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from syswatch.paths import PROC_ROOT
from syswatch.util import parse_float, read_text

log = logging.getLogger("syswatch.collectors.cpu")

_FIELDS = (
    "user",
    "nice",
    "system",
    "idle",
    "iowait",
    "irq",
    "softirq",
    "steal",
    "guest",
    "guest_nice",
)


@dataclass(frozen=True)
class CpuTimes:
    user: float = 0.0
    nice: float = 0.0
    system: float = 0.0
    idle: float = 0.0
    iowait: float = 0.0
    irq: float = 0.0
    softirq: float = 0.0
    steal: float = 0.0
    guest: float = 0.0
    guest_nice: float = 0.0

    @property
    def busy(self) -> float:
        return self.user + self.nice + self.system + self.irq + self.softirq + self.steal

    @property
    def not_busy(self) -> float:
        return self.idle + self.iowait

    @property
    def total(self) -> float:
        return sum(getattr(self, f) for f in _FIELDS)


def _times_from(values: list[float]) -> CpuTimes:
    kwargs = {f: values[i] for i, f in enumerate(_FIELDS) if i < len(values)}
    return CpuTimes(**kwargs)


def read_cpu_times(proc_root: Path = PROC_ROOT) -> dict[str, CpuTimes]:
    """Read cumulative CPU tick counters, keyed by 'cpu' and 'cpu0', 'cpu1'..."""
    result: dict[str, CpuTimes] = {}
    text = read_text(proc_root / "stat")
    if text is None:
        log.warning("cannot read %s/stat", proc_root)
        return result
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2 or not parts[0].startswith("cpu"):
            continue
        label = parts[0]
        if label != "cpu" and not label[3:].isdigit():
            continue
        values: list[float] = []
        for raw in parts[1:]:
            parsed = parse_float(raw)
            if parsed is None:
                break
            values.append(parsed)
        result[label] = _times_from(values)
    return result


def cpu_usage_between(prev: CpuTimes, cur: CpuTimes) -> float | None:
    """Busy percentage between two samples; None when counters are unusable."""
    busy_delta = cur.busy - prev.busy
    idle_delta = cur.not_busy - prev.not_busy
    total_delta = cur.total - prev.total
    if total_delta <= 0 or (busy_delta + idle_delta) <= 0:
        return None
    return min(100.0, max(0.0, busy_delta / (busy_delta + idle_delta) * 100.0))


def per_cpu_usage_between(
    prev: dict[str, CpuTimes], cur: dict[str, CpuTimes]
) -> dict[str, float | None]:
    usage: dict[str, float | None] = {}
    for label, cur_times in cur.items():
        prev_times = prev.get(label)
        if prev_times is not None:
            usage[label] = cpu_usage_between(prev_times, cur_times)
    return usage


def load_average() -> tuple[float, float, float] | None:
    """1/5/15 minute load averages, or None when unavailable."""
    try:
        return os.getloadavg()
    except OSError:
        log.debug("load average unavailable on this platform")
        return None


def core_count() -> int | None:
    count = os.cpu_count()
    return count
