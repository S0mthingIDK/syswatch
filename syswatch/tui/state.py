"""UI-independent data layer for the TUI.

Wraps the existing collectors so that every call either returns usable data
or (None + a human-readable problem description). No Textual imports here:
this module is unit-testable without a running application.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from syswatch.collectors import cpu as cpu_collector
from syswatch.collectors import network as net_collector
from syswatch.collectors import processes as proc_collector
from syswatch.collectors.memory import MemoryInfo, get_memory
from syswatch.collectors import system as system_module
from syswatch.collectors.system import (
    DiskUsage,
    NetworkInterface,
    get_boot_time,
    get_cpu_core_count,
    get_cpu_model,
    get_hostname,
    get_os_pretty_name,
    get_uptime,
    list_filesystems,
    list_network_interfaces,
)
from syswatch.config import AppConfig
from syswatch.health import HealthEvaluation, evaluate_health
from syswatch.paths import PROC_ROOT, SYSFS_ROOT

log = logging.getLogger("syswatch.tui.state")


@dataclass(frozen=True)
class Result:
    """A collector outcome: value or error, never an exception."""

    value: object = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def guarded(description: str, fn, *args):
    """Run a collector, converting any failure into Result(error=...)."""
    try:
        return Result(value=fn(*args))
    except Exception as exc:  # noqa: BLE001 - UI must survive any source
        log.debug("%s failed: %s", description, exc)
        return Result(error=f"{description}: {exc}")


class CollectorHub:
    """Facade over the syswatch collectors used by the TUI."""

    def __init__(
        self,
        proc_root: Path = PROC_ROOT,
        sysfs_root: Path = SYSFS_ROOT,
    ) -> None:
        self.proc_root = proc_root
        self.sysfs_root = sysfs_root

    # -- fast samples -----------------------------------------------------

    def sample_cpu(
        self,
        prev_times: dict | None,
        cur_times: dict,
        elapsed: float | None,
    ):
        """Derive utilisation between two /proc/stat snapshots."""
        percent = None
        per_core: dict[str, float | None] = {}
        overall_prev = (prev_times or {}).get("cpu")
        overall_cur = cur_times.get("cpu")
        if overall_prev and overall_cur and elapsed:
            percent = cpu_collector.cpu_usage_between(overall_prev, overall_cur)
        if prev_times and elapsed:
            per_core = {
                label: value
                for label, value in cpu_collector.per_cpu_usage_between(
                    prev_times, cur_times
                ).items()
                if label != "cpu"
            }
        return percent, per_core

    def read_cpu_times(self):
        return guarded("cpu times", cpu_collector.read_cpu_times, self.proc_root)

    def cpu_facts(self):
        return (
            guarded("cpu model", get_cpu_model, self.proc_root),
            guarded("core count", get_cpu_core_count, self.proc_root),
            guarded("load average", cpu_collector.load_average),
        )

    def memory(self) -> Result:
        return guarded("memory", get_memory, self.proc_root)

    def disks(self) -> Result:
        return guarded("filesystems", list_filesystems, self.proc_root)

    def net_counters(self):
        return guarded(
            "interface counters",
            net_collector.read_interface_counters,
            self.proc_root,
        )

    def net_interfaces(self) -> Result:
        return guarded(
            "network interfaces",
            list_network_interfaces,
            self.sysfs_root,
            self.proc_root,
        )

    def uptime(self) -> Result:
        return guarded("uptime", get_uptime, self.proc_root)

    def cpu_frequency(self) -> Result:
        return guarded(
            "cpu frequency",
            system_module.get_cpu_frequency_mhz,
            self.proc_root,
            self.sysfs_root,
        )

    def boot_time(self) -> Result:
        return guarded("boot time", get_boot_time, self.proc_root)

    def process_count(self) -> Result:
        return guarded(
            "process count", proc_collector.count_processes, self.proc_root
        )

    def identity(self) -> dict[str, object]:
        hostname = guarded("hostname", get_hostname)
        os_name = guarded("os release", get_os_pretty_name)
        return {
            "hostname": hostname.value if hostname.ok else None,
            "os_pretty_name": os_name.value if os_name.ok else None,
            "identity_errors": [
                r.error for r in (hostname, os_name) if not r.ok
            ],
        }

    # -- heavier samples (run in workers) ---------------------------------

    def processes(self) -> Result:
        return guarded("processes", proc_collector.snapshot, self.proc_root)

    def health(self, config: AppConfig):
        """Run all health checks; slow (probes + sampling), run in a worker.

        Returns (evaluation | None, failure_message | None).
        """
        try:
            return evaluate_health(config, proc_root=self.proc_root), None
        except Exception as exc:  # noqa: BLE001
            log.debug("health checks failed: %s", exc)
            return None, f"health checks failed: {exc}"
