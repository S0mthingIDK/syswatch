"""Machine-readable JSON diagnostic reports."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from syswatch.collectors import cpu as cpu_collector
from syswatch.collectors import network as net_collector
from syswatch.collectors import processes as proc_collector
from syswatch.collectors.system import (
    gather_system_snapshot,
    list_filesystems,
    list_network_interfaces,
)
from syswatch.config import AppConfig
from syswatch.health import run_checks
from syswatch.paths import ETC_ROOT, PROC_ROOT, SYSFS_ROOT
from syswatch.util import utc_now_iso

log = logging.getLogger("syswatch.report")

SCHEMA_VERSION = 1


def _round(value: float | None, digits: int = 2) -> float | None:
    return round(value, digits) if value is not None else None


def build_report(
    config: AppConfig,
    proc_root: Path = PROC_ROOT,
    sys_root: Path = SYSFS_ROOT,
    etc_root: Path = ETC_ROOT,
    top_processes: int = 10,
) -> dict[str, object]:
    """Assemble a full diagnostic report as a JSON-serialisable dict."""
    errors: list[str] = []
    snapshot = gather_system_snapshot(
        proc_root=proc_root, etc_root=etc_root, errors=errors
    )
    disks = list_filesystems(proc_root)
    interfaces = list_network_interfaces(sys_root=sys_root, proc_root=proc_root)

    prev_cpu = cpu_collector.read_cpu_times(proc_root)
    prev_net = net_collector.read_interface_counters(proc_root)
    before_procs = {p.pid: p for p in proc_collector.snapshot(proc_root)}
    time.sleep(config.sample_delay)
    cur_cpu = cpu_collector.read_cpu_times(proc_root)
    cur_net = net_collector.read_interface_counters(proc_root)
    after_procs_list = proc_collector.snapshot(proc_root)

    overall_prev = prev_cpu.get("cpu")
    overall_cur = cur_cpu.get("cpu")
    cpu_percent = (
        cpu_collector.cpu_usage_between(overall_prev, overall_cur)
        if overall_prev and overall_cur
        else None
    )
    if overall_cur is None:
        errors.append("cpu usage could not be sampled")

    rates = net_collector.interface_rates(
        prev_net, cur_net, config.sample_delay
    )
    total_rx, total_tx = net_collector.total_rates(rates)

    after_procs = {p.pid: p for p in after_procs_list}
    cpu_usage_map = proc_collector.cpu_percent_between(
        before_procs, after_procs, config.sample_delay
    )

    def proc_row(p: proc_collector.ProcessInfo) -> dict[str, object]:
        return {
            "pid": p.pid,
            "name": p.name,
            "cpu_percent": round(cpu_usage_map.get(p.pid, 0.0), 2),
            "rss_bytes": p.rss_bytes,
            "state": p.state,
        }

    top_by_cpu = sorted(
        after_procs_list,
        key=lambda p: cpu_usage_map.get(p.pid, 0.0),
        reverse=True,
    )[:top_processes]
    top_by_rss = sorted(after_procs_list, key=lambda p: p.rss_bytes, reverse=True)[
        :top_processes
    ]

    findings, health_errors = run_checks(config, proc_root=proc_root)
    errors.extend(health_errors)

    memory = snapshot.memory
    load = cpu_collector.load_average()

    return {
        "schema_version": SCHEMA_VERSION,
        "tool": "syswatch",
        "generated_at": utc_now_iso(),
        "host": {
            "hostname": snapshot.hostname,
            "kernel": snapshot.kernel,
            "arch": snapshot.arch,
            "os_pretty_name": snapshot.os_pretty_name,
            "uptime_seconds": _round(snapshot.uptime_seconds),
            "boot_time_epoch": snapshot.boot_time_epoch,
        },
        "cpu": {
            "model": snapshot.cpu_model,
            "cores": snapshot.cpu_cores,
            "usage_percent": _round(cpu_percent),
            "load_average_1m": _round(load[0]) if load else None,
            "load_average_5m": _round(load[1]) if load else None,
            "load_average_15m": _round(load[2]) if load else None,
            "process_count": len(after_procs_list),
        },
        "memory": {
            "total_bytes": memory.total_bytes if memory else None,
            "available_bytes": memory.available_bytes if memory else None,
            "used_bytes": memory.used_bytes if memory else None,
            "used_percent": _round(memory.used_percent) if memory else None,
            "swap_total_bytes": memory.swap_total_bytes if memory else None,
            "swap_used_percent": _round(memory.swap_used_percent) if memory else None,
        },
        "disks": [
            {
                "device": d.device,
                "mountpoint": d.mountpoint,
                "fstype": d.fstype,
                "total_bytes": d.total_bytes,
                "used_bytes": d.used_bytes,
                "available_bytes": d.available_bytes,
                "percent_used": _round(d.percent),
            }
            for d in disks
        ],
        "network": {
            "interfaces": [
                {
                    "name": i.name,
                    "state": i.state,
                    "mac": i.mac,
                    "ipv4_addresses": i.ipv4_addresses,
                    "ipv6_addresses": i.ipv6_addresses,
                }
                for i in interfaces
            ],
            "rx_bytes_per_second": total_rx,
            "tx_bytes_per_second": total_tx,
        },
        "top_processes_by_cpu": [proc_row(p) for p in top_by_cpu],
        "top_processes_by_memory": [proc_row(p) for p in top_by_rss],
        "health": {
            "status": "healthy" if not findings else "issues",
            "findings": [f.to_dict() for f in findings],
        },
        "configuration": {
            "cpu_warning": config.cpu_warning,
            "ram_warning": config.ram_warning,
            "disk_warning": config.disk_warning,
            "load_per_core_warning": config.load_per_core_warning,
        },
        "collection_errors": errors,
    }


def dump_report_json(report: dict[str, object], pretty: bool = False) -> str:
    if pretty:
        return json.dumps(report, indent=2) + "\n"
    return json.dumps(report, separators=(",", ":")) + "\n"
