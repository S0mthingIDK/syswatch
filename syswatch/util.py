"""Small shared helpers: safe file reads, parsing, human formatting."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("syswatch.util")


def read_text(path: Path) -> str | None:
    """Read a text file, returning None on any I/O or decoding problem."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError) as exc:
        log.debug("could not read %s: %s", path, exc)
        return None


def parse_int(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def format_bytes(num_bytes: float | int | None, decimal_places: int = 1) -> str:
    """Format a byte count as a human-readable string with binary units."""
    if num_bytes is None:
        return "unknown"
    negative = num_bytes < 0
    value = abs(float(num_bytes))
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    unit_idx = 0
    while value >= 1024.0 and unit_idx < len(units) - 1:
        value /= 1024.0
        unit_idx += 1
    if unit_idx == 0:
        text = f"{int(value)} B"
    else:
        text = f"{value:.{decimal_places}f} {units[unit_idx]}"
    return f"-{text}" if negative else text


def format_rate(bytes_per_second: float | None) -> str:
    if bytes_per_second is None:
        return "unknown"
    return f"{format_bytes(bytes_per_second)}/s"


def format_percent(percent: float | None, decimal_places: int = 1) -> str:
    if percent is None:
        return "unknown"
    return f"{percent:.{decimal_places}f}%"


def format_uptime(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 60:
        return f"{int(seconds)}s"
    total_minutes = int(seconds // 60)
    days, remainder = divmod(total_minutes, 1440)
    hours, minutes = divmod(remainder, 60)
    if days > 0:
        return f"{days}d {hours}h {minutes}m"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"
