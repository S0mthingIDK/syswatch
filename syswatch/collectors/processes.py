"""Process table built by scanning /proc/[pid]."""

from __future__ import annotations

import logging
import os
import pwd
from dataclasses import dataclass
from pathlib import Path

from syswatch.paths import PROC_ROOT
from syswatch.util import read_text

log = logging.getLogger("syswatch.collectors.processes")


def clock_ticks() -> int:
    try:
        return int(os.sysconf("SC_CLK_TCK"))
    except (ValueError, OSError, AttributeError):
        return 100


def page_size() -> int:
    try:
        return int(os.sysconf("SC_PAGE_SIZE"))
    except (ValueError, OSError, AttributeError):
        return 4096


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    name: str
    state: str
    ppid: int
    utime_ticks: float
    stime_ticks: float
    rss_bytes: int
    cmdline: str = ""
    num_threads: int = 0
    starttime_ticks: float = 0.0

    @property
    def cpu_ticks(self) -> float:
        return self.utime_ticks + self.stime_ticks

    @property
    def is_zombie(self) -> bool:
        return self.state == "Z"

    @property
    def display_name(self) -> str:
        return self.cmdline.split(" ")[0] if self.cmdline else self.name


def parse_stat(
    data: str,
) -> tuple[str, str, int, float, float, int, int, float] | None:
    """Parse /proc/[pid]/stat.

    Returns (name, state, ppid, utime, stime, rss_pages, num_threads,
    starttime_ticks). The comm field is wrapped in parentheses and may itself
    contain spaces and parentheses, so it is delimited with the *last* ')'
    in the line.
    """
    start = data.find("(")
    end = data.rfind(")")
    if start < 0 or end <= start:
        return None
    try:
        int(data[:start])
    except ValueError:
        return None
    name = data[start + 1 : end]
    rest = data[end + 1 :].split()
    if len(rest) < 22:
        return None
    try:
        state = rest[0]
        ppid = int(rest[1])
        utime = float(rest[11])
        stime = float(rest[12])
        rss_pages = int(rest[21])
        num_threads = int(rest[17])
        starttime_ticks = float(rest[19])
    except ValueError:
        return None
    return name, state, ppid, utime, stime, rss_pages, num_threads, starttime_ticks


def snapshot(proc_root: Path = PROC_ROOT) -> list[ProcessInfo]:
    """Best-effort list of visible processes; unreadable entries are skipped."""
    try:
        entries = sorted(int(p.name) for p in proc_root.iterdir() if p.name.isdigit())
    except OSError as exc:
        log.warning("cannot scan %s: %s", proc_root, exc)
        return []
    pgsize = page_size()
    processes: list[ProcessInfo] = []
    for pid in entries:
        stat_path = proc_root / str(pid) / "stat"
        data = read_text(stat_path)
        if data is None:
            continue
        parsed = parse_stat(data)
        if parsed is None:
            log.debug("unparsable stat for pid %s", pid)
            continue
        (
                name,
                state,
                ppid,
                utime,
                stime,
                rss_pages,
                num_threads,
                starttime,
            ) = parsed
        cmdline_raw = read_text(proc_root / str(pid) / "cmdline")
        cmdline = (
            cmdline_raw.replace("\x00", " ").strip() if cmdline_raw else ""
        )
        processes.append(
            ProcessInfo(
                pid=pid,
                name=name,
                state=state,
                ppid=ppid,
                utime_ticks=utime,
                stime_ticks=stime,
                rss_bytes=rss_pages * pgsize,
                cmdline=cmdline,
                num_threads=num_threads,
                starttime_ticks=starttime,
            )
        )
    return processes


def count_processes(proc_root: Path = PROC_ROOT) -> int | None:
    """Number of visible processes without reading per-process files."""
    try:
        return sum(1 for p in proc_root.iterdir() if p.name.isdigit())
    except OSError as exc:
        log.warning("cannot count processes in %s: %s", proc_root, exc)
        return None


def cpu_percent_between(
    before: dict[int, ProcessInfo],
    after: dict[int, ProcessInfo],
    elapsed_seconds: float,
    ticks_per_second: int | None = None,
) -> dict[int, float]:
    """Per-pid CPU percentage over the sampling window (top semantics)."""
    tps = ticks_per_second if ticks_per_second is not None else clock_ticks()
    usage: dict[int, float] = {}
    if elapsed_seconds <= 0 or tps <= 0:
        return usage
    # A single process cannot use more than every core simultaneously.
    cores = os.cpu_count() or 1
    cap_percent = 100.0 * cores
    for pid, proc_after in after.items():
        proc_before = before.get(pid)
        if proc_before is None:
            continue
        tick_delta = max(0.0, proc_after.cpu_ticks - proc_before.cpu_ticks)
        usage[pid] = min(cap_percent, tick_delta / tps / elapsed_seconds * 100.0)
    return usage


def zombies(processes: list[ProcessInfo]) -> list[ProcessInfo]:
    return [p for p in processes if p.is_zombie]


def memory_percent(proc: ProcessInfo, total_bytes: int | None) -> float | None:
    if total_bytes is None or total_bytes <= 0:
        return None
    return min(100.0, proc.rss_bytes / total_bytes * 100.0)


def read_process_details(pid: int, proc_root: Path = PROC_ROOT) -> dict[str, object]:
    """Extra per-process facts for the TUI detail panel.

    Best effort: every missing piece becomes None rather than raising.
    """
    details: dict[str, object] = {
        "user": None,
        "uid": None,
        "exe": None,
        "threads": None,
    }
    status_text = read_text(proc_root / str(pid) / "status")
    if status_text:
        uid = None
        threads = None
        for line in status_text.splitlines():
            key, sep, value = line.partition(":")
            if not sep:
                continue
            if key.strip() == "Uid":
                parts = value.split()
                if parts:
                    try:
                        uid = int(parts[0])
                    except ValueError:
                        uid = None
            elif key.strip() == "Threads":
                try:
                    threads = int(value.strip())
                except ValueError:
                    threads = None
        details["uid"] = uid
        details["threads"] = threads
        if uid is not None:
            try:
                details["user"] = pwd.getpwuid(uid).pw_name
            except KeyError:
                details["user"] = str(uid)

    try:
        details["exe"] = os.readlink(str(proc_root / str(pid) / "exe"))
    except OSError:
        details["exe"] = None
    return details