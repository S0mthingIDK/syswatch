"""Health checks: thresholds, failed services, gateway reachability, zombies."""

from __future__ import annotations

import logging
import math
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from syswatch.collectors import cpu as cpu_collector
from syswatch.collectors import processes as proc_collector
from syswatch.collectors.system import get_cpu_core_count
from syswatch.collectors.memory import MemoryInfo
from syswatch.collectors.network import default_gateways
from syswatch.collectors.system import DiskUsage
from syswatch.config import AppConfig
from syswatch.paths import PROC_ROOT
from syswatch.util import format_bytes

log = logging.getLogger("syswatch.health")


class Severity(Enum):
    INFO = 0
    WARNING = 1
    CRITICAL = 2


@dataclass(frozen=True)
class Finding:
    check: str
    severity: Severity
    message: str
    details: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "check": self.check,
            "severity": self.severity.name.lower(),
            "message": self.message,
            "details": self.details,
        }


CHECK_OK, CHECK_WARN, CHECK_CRIT, CHECK_UNAVAIL = "ok", "warn", "crit", "unavail"

_STATE_ICON = {CHECK_OK: "✓", CHECK_WARN: "!", CHECK_CRIT: "✕", CHECK_UNAVAIL: "–"}
_STATE_LABEL = {
    CHECK_OK: "NORMAL", CHECK_WARN: "WARNING",
    CHECK_CRIT: "CRITICAL", CHECK_UNAVAIL: "UNAVAILABLE",
}


@dataclass(frozen=True)
class CheckStatus:
    """Outcome of a single named health check, healthy or not."""

    name: str
    state: str  # one of CHECK_OK / WARN / CRIT / UNAVAIL
    value: str
    threshold: str
    note: str = ""

    @property
    def icon(self) -> str:
        return _STATE_ICON.get(self.state, "–")

    @property
    def status_label(self) -> str:
        return _STATE_LABEL.get(self.state, self.state.upper())

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "state": self.state,
            "value": self.value,
            "threshold": self.threshold,
            "note": self.note,
        }


@dataclass(frozen=True)
class HealthEvaluation:
    """Full health snapshot: problem findings plus every check's status."""

    findings: list[Finding]
    checks: list[CheckStatus]
    errors: list[str] = field(default_factory=list)

    def overall_state(self) -> str:
        if any(c.state == CHECK_CRIT for c in self.checks):
            return CHECK_CRIT
        if any(c.state == CHECK_WARN for c in self.checks):
            return CHECK_WARN
        return CHECK_OK


@dataclass(frozen=True)
class _Measurements:
    cpu_percent: float | None
    memory: MemoryInfo | None
    disks: list[DiskUsage]
    load: tuple[float, float, float] | None
    cores: int | None
    zombies: list[proc_collector.ProcessInfo]
    failed_units: list[str] | None


def _measure(
    config: AppConfig, proc_root: Path
) -> tuple[_Measurements, list[str]]:
    from syswatch.collectors.memory import get_memory
    from syswatch.collectors.system import list_filesystems

    errors: list[str] = []
    prev_cpu = cpu_collector.read_cpu_times(proc_root)
    time.sleep(config.sample_delay)
    cur_cpu = cpu_collector.read_cpu_times(proc_root)
    overall_prev = prev_cpu.get("cpu")
    overall_cur = cur_cpu.get("cpu")
    cpu_percent = (
        cpu_collector.cpu_usage_between(overall_prev, overall_cur)
        if overall_prev and overall_cur
        else None
    )
    if overall_cur is None:
        errors.append("cpu usage could not be sampled")

    memory = get_memory(proc_root)
    disks = list_filesystems(proc_root)
    load = cpu_collector.load_average()
    cores = get_cpu_core_count(proc_root)

    processes = proc_collector.snapshot(proc_root)
    zombie_list = proc_collector.zombies(processes)

    failed_units = None
    if systemd_available():
        failed_units = failed_services(timeout=config.health_timeout)

    return (
        _Measurements(
            cpu_percent=cpu_percent,
            memory=memory,
            disks=disks,
            load=load,
            cores=cores,
            zombies=zombie_list,
            failed_units=failed_units,
        ),
        errors,
    )


def evaluate_health(
    config: AppConfig, proc_root: Path = PROC_ROOT
) -> HealthEvaluation:
    """Run all checks once; produce problem findings and full statuses."""
    measurements, errors = _measure(config, proc_root)
    findings: list[Finding] = []

    # -- CPU usage -----------------------------------------------------
    cpu_pct = measurements.cpu_percent
    critical_threshold = min(100.0, config.cpu_warning + config.critical_offset)
    if cpu_pct is not None:
        state = (
            CHECK_CRIT if cpu_pct >= critical_threshold
            else CHECK_WARN if cpu_pct >= config.cpu_warning
            else CHECK_OK
        )
        findings.extend(check_cpu_usage(cpu_pct, config))
        threshold_text = f"≥{config.cpu_warning:g}%"
        value_text = f"{cpu_pct:.1f}%"
        note = f"sampled over {config.sample_delay:.1f}s"
    else:
        state, value_text = CHECK_UNAVAIL, "-"
        threshold_text = "-"
        note = "cpu usage could not be sampled"
    checks = [
        CheckStatus(
            name="CPU usage", state=state, value=value_text,
            threshold=threshold_text, note=note,
        )
    ]

    # -- Memory --------------------------------------------------------
    memory = measurements.memory
    mem_pct = memory.used_percent if memory else None
    ram_crit = min(100.0, config.ram_warning + config.critical_offset)
    if mem_pct is not None:
        mem_state = (
            CHECK_CRIT if mem_pct >= ram_crit
            else CHECK_WARN if mem_pct >= config.ram_warning
            else CHECK_OK
        )
        value = f"{mem_pct:.1f}%"
        note = f"{format_bytes(memory.used_bytes)} / {format_bytes(memory.total_bytes)}"
    else:
        mem_state, value, note = CHECK_UNAVAIL, "-", "memory information unavailable"
    findings.extend(check_memory(memory, config))
    checks.append(
        CheckStatus(
            name="Memory usage", state=mem_state, value=value,
            threshold=f"≥{config.ram_warning:g}%", note=note,
        )
    )

    # -- Swap ----------------------------------------------------------
    swap_pct = memory.swap_used_percent if memory else None
    swap_total = memory.swap_total_bytes if memory else None
    swap_crit = min(100.0, config.swap_warning + config.critical_offset)
    if not swap_total:
        swap_state, swap_value, swap_note = (
            CHECK_OK, "-", "no swap configured"
        )
    elif swap_pct is not None:
        swap_state = (
            CHECK_CRIT if swap_pct >= swap_crit
            else CHECK_WARN if swap_pct >= config.swap_warning
            else CHECK_OK
        )
        swap_value = f"{swap_pct:.1f}%"
        swap_note = f"of {format_bytes(swap_total)}"
    else:
        swap_state, swap_value, swap_note = CHECK_UNAVAIL, "-", "-"
    checks.append(
        CheckStatus(
            name="Swap usage", state=swap_state, value=swap_value,
            threshold=f"≥{config.swap_warning:g}%", note=swap_note,
        )
    )

    # -- Filesystems ---------------------------------------------------
    if measurements.disks:
        disk_crit = min(100.0, config.disk_warning + config.critical_offset)
        # Single pass over all disks; the finding list uses the same data.
        findings.extend(check_disks(measurements.disks, config))
        shown = sorted(
            measurements.disks, key=lambda d: (d.percent is None, -(d.percent or 0))
        )[:4]
        for disk in shown:
            pct = disk.percent
            if pct is None:
                state, value = CHECK_UNAVAIL, "-"
            else:
                state = (
                    CHECK_CRIT if pct >= disk_crit
                    else CHECK_WARN if pct >= config.disk_warning
                    else CHECK_OK
                )
                value = f"{pct:.1f}%"
            checks.append(
                CheckStatus(
                    name=f"Filesystem {disk.mountpoint}",
                    state=state,
                    value=value,
                    threshold=f"≥{config.disk_warning:g}%",
                    note=f"{format_bytes(disk.used_bytes)} of "
                         f"{format_bytes(disk.total_bytes)} · {disk.fstype}",
                )
            )
    else:
        checks.append(
            CheckStatus(
                name="Filesystems", state=CHECK_UNAVAIL, value="-",
                threshold="-", note="no mounted filesystems detected",
            )
        )

    # -- Load average --------------------------------------------------
    load = measurements.load
    cores = measurements.cores
    if load and cores:
        limit = config.load_per_core_warning * cores
        load_state = CHECK_WARN if load[0] > limit else CHECK_OK
        findings.extend(check_load(load, cores, config))
        checks.append(
            CheckStatus(
                name="Load average", state=load_state,
                value=f"{load[0]:.2f}",
                threshold=f"≤{limit:g}",
                note=f"1m avg on {cores} cores ({config.load_per_core_warning:g}/core)",
            )
        )
    else:
        checks.append(
            CheckStatus(
                name="Load average", state=CHECK_UNAVAIL, value="-",
                threshold="-", note="unavailable",
            )
        )

    # -- Zombie processes ----------------------------------------------
    zombie_list = measurements.zombies
    zcount = len(zombie_list)
    findings.extend(check_zombies(zombie_list))
    checks.append(
        CheckStatus(
            name="Zombie processes", state=CHECK_WARN if zcount else CHECK_OK,
            value=str(zcount), threshold="0",
            note="processes stuck in state Z" if zcount else "",
        )
    )

    # -- Failed systemd services ---------------------------------------
    units = measurements.failed_units
    if units is None:
        svc_state, svc_value, svc_note = (
            CHECK_UNAVAIL, "-", "systemd not detected"
        )
    else:
        svc_state = CHECK_WARN if units else CHECK_OK
        svc_value = str(len(units))
        svc_note = ", ".join(units[:3]) + ("..." if len(units) > 3 else "")
        findings.extend(check_failed_services(config.health_timeout, units=units))
    checks.append(
        CheckStatus(
            name="Systemd services", state=svc_state, value=svc_value,
            threshold="0 failed", note=svc_note or "all units active",
        )
    )

    # -- Default gateway -----------------------------------------------
    gateways = default_gateways(proc_root) or None
    if not gateways:
        gw_state, gw_value, gw_note = (
            CHECK_UNAVAIL, "-", "no default route detected"
        )
        gateway_findings: list[Finding] = []
    else:
        gateway_findings = probe_gateways(
            gateways, timeout=config.health_timeout
        )
        unreachable = [
            f for f in gateway_findings
            if f.check == "gateway_unreachable"
        ]
        if unreachable:
            gw_state = CHECK_WARN
            gw_value = "DOWN"
            gw_note = "; ".join(f.message for f in unreachable[:2])
        else:
            gw_state = CHECK_OK
            gw_value = "UP"
            gw_note = ", ".join(addr for _, addr in gateways[:2])
    findings.extend(gateway_findings)
    checks.append(
        CheckStatus(
            name="Network gateway", state=gw_state, value=gw_value,
            threshold="reachable", note=gw_note,
        )
    )

    return HealthEvaluation(findings=findings, checks=checks, errors=errors)


def check_disks(
    disks: list[DiskUsage], config: AppConfig
) -> list[Finding]:
    findings: list[Finding] = []
    critical_threshold = min(100.0, config.disk_warning + config.critical_offset)
    for disk in disks:
        if disk.percent is None:
            continue
        if disk.percent >= critical_threshold:
            severity = Severity.CRITICAL
        elif disk.percent >= config.disk_warning:
            severity = Severity.WARNING
        else:
            continue
        findings.append(
            Finding(
                check="disk_full",
                severity=severity,
                message=(
                    f"filesystem {disk.mountpoint} is {disk.percent:.1f}% full "
                    f"(warning threshold {config.disk_warning:.0f}%)"
                ),
                details={
                    "mountpoint": disk.mountpoint,
                    "device": disk.device,
                    "percent_used": disk.percent,
                },
            )
        )
    return findings


def check_memory(memory: MemoryInfo | None, config: AppConfig) -> list[Finding]:
    if memory is None or memory.used_percent is None:
        return []
    critical_threshold = min(100.0, config.ram_warning + config.critical_offset)
    if memory.used_percent >= critical_threshold:
        severity = Severity.CRITICAL
    elif memory.used_percent >= config.ram_warning:
        severity = Severity.WARNING
    else:
        return []
    return [
        Finding(
            check="memory_pressure",
            severity=severity,
            message=(
                f"RAM usage is {memory.used_percent:.1f}% "
                f"(warning threshold {config.ram_warning:.0f}%)"
            ),
            details={"percent_used": round(memory.used_percent, 2)},
        )
    ]


def check_cpu_usage(percent: float | None, config: AppConfig) -> list[Finding]:
    if percent is None:
        return []
    critical_threshold = min(100.0, config.cpu_warning + config.critical_offset)
    if percent >= critical_threshold:
        severity = Severity.CRITICAL
    elif percent >= config.cpu_warning:
        severity = Severity.WARNING
    else:
        return []
    return [
        Finding(
            check="cpu_usage",
            severity=severity,
            message=(
                f"CPU usage is {percent:.1f}% (sampled over "
                f"{config.sample_delay:.1f}s; warning threshold {config.cpu_warning:.0f}%)"
            ),
            details={"percent_used": round(percent, 2)},
        )
    ]


def check_load(
    load: tuple[float, float, float] | None,
    cores: int | None,
    config: AppConfig,
) -> list[Finding]:
    if load is None or not cores:
        return []
    threshold = config.load_per_core_warning * cores
    if load[0] <= threshold:
        return []
    return [
        Finding(
            check="load_average",
            severity=Severity.WARNING,
            message=(
                f"load average {load[0]:.2f} exceeds {threshold:.2f} "
                f"({config.load_per_core_warning:g} x {cores} cores)"
            ),
            details={
                "load1": round(load[0], 2),
                "load5": round(load[1], 2),
                "load15": round(load[2], 2),
                "threshold": round(threshold, 2),
            },
        )
    ]


def check_zombies(zombies: list[proc_collector.ProcessInfo]) -> list[Finding]:
    if not zombies:
        return []
    sample = ", ".join(f"{z.name}(pid {z.pid})" for z in zombies[:5])
    more = f" and {len(zombies) - 5} more" if len(zombies) > 5 else ""
    return [
        Finding(
            check="zombie_processes",
            severity=Severity.WARNING,
            message=f"{len(zombies)} zombie process(es): {sample}{more}",
            details={
                "count": len(zombies),
                "pids": [z.pid for z in zombies[:20]],
            },
        )
    ]


def systemd_available(run_dir: Path | None = None) -> bool:
    """True when the system appears to be booted with systemd."""
    check_dir = run_dir if run_dir is not None else Path("/run/systemd/system")
    if not check_dir.exists():
        return False
    return shutil.which("systemctl") is not None


def failed_services(timeout: float = 5.0) -> list[str] | None:
    """Names of failed systemd units, or None when not detectable."""
    command = [
        "systemctl",
        "list-units",
        "--state=failed",
        "--no-legend",
        "--plain",
        "--no-pager",
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("systemctl query failed: %s", exc)
        return None
    if result.returncode != 0:
        log.debug("systemctl exited %s", result.returncode)
        return None
    units = [line.split()[0] for line in result.stdout.splitlines() if line.strip()]
    return units


def check_failed_services(
    timeout: float,
    run_dir: Path | None = None,
    units: list[str] | None = None,
) -> list[Finding]:
    """Failed systemd services where detectable; silent when not.

    If *units* is provided, it is used directly (no systemctl call). This lets
    evaluate_health reuse the list already gathered by _measure.
    """
    if units is None:
        if not systemd_available(run_dir):
            log.debug("systemd not detected; skipping failed-services check")
            return []
        units = failed_services(timeout=timeout)
        if units is None:
            log.debug("failed services could not be determined")
            return []
    if not units:
        return []
    shown = ", ".join(units[:10])
    extra = f" (+{len(units) - 10} more)" if len(units) > 10 else ""
    return [
        Finding(
            check="failed_services",
            severity=Severity.WARNING,
            message=f"{len(units)} failed systemd unit(s): {shown}{extra}",
            details={"units": units},
        )
    ]


def _ping_reachable(address: str, timeout_seconds: float) -> bool | None:
    ping_binary = shutil.which("ping")
    if ping_binary is None:
        return None
    wait = max(1, math.ceil(timeout_seconds))
    try:
        result = subprocess.run(
            [ping_binary, "-c", "1", "-W", str(wait), address],
            capture_output=True,
            timeout=timeout_seconds + 2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("ping failed for %s: %s", address, exc)
        return None
    return result.returncode == 0


def _tcp_reachable(address: str, ports: tuple[int, ...], timeout_seconds: float) -> bool:
    for port in ports:
        try:
            with socket.create_connection((address, port), timeout=timeout_seconds):
                return True
        except OSError:
            continue
    return False


def probe_gateways(
    gateways: list[tuple[str, str]],
    probe_ports: tuple[int, ...] = (53, 80, 443, 22),
    timeout: float = 3.0,
) -> list[Finding]:
    """Probe already-discovered default gateways; warn about unreachable ones.

    Separate from check_gateways so the caller can reuse the route list it
    already read (avoids a second /proc/net/route read).
    """
    findings: list[Finding] = []
    for iface, address in gateways:
        reachable = _ping_reachable(address, timeout)
        method = "icmp"
        if reachable is None:
            reachable = _tcp_reachable(address, probe_ports, min(timeout, 1.0))
            method = "tcp-probe"
        if reachable:
            log.debug("gateway %s via %s is reachable (%s)", address, iface, method)
            continue
        findings.append(
            Finding(
                check="gateway_unreachable",
                severity=Severity.WARNING,
                message=(
                    f"default gateway {address} on {iface} did not respond "
                    f"to {method}"
                ),
                details={"address": address, "interface": iface, "method": method},
            )
        )
    return findings


def check_gateways(
    proc_root: Path = PROC_ROOT,
    probe_ports: tuple[int, ...] = (53, 80, 443, 22),
    timeout: float = 3.0,
) -> list[Finding]:
    """Convenience wrapper: read routes then probe. Warn about unreachable ones."""
    gateways = default_gateways(proc_root)
    return probe_gateways(gateways, probe_ports, timeout)


def run_checks(
    config: AppConfig,
    proc_root: Path = PROC_ROOT,
) -> tuple[list[Finding], list[str]]:
    """Run all health checks; returns (findings, collection errors)."""
    evaluation = evaluate_health(config, proc_root=proc_root)
    return evaluation.findings, evaluation.errors


def summarize(findings: list[Finding]) -> str:
    counts = {sev: 0 for sev in Severity}
    for finding in findings:
        counts[finding.severity] += 1
    problems = counts[Severity.WARNING] + counts[Severity.CRITICAL]
    if counts[Severity.CRITICAL]:
        return f"UNHEALTHY: {counts[Severity.CRITICAL]} critical, {counts[Severity.WARNING]} warning(s)"
    if problems:
        return f"ATTENTION NEEDED: {problems} warning(s)"
    return "HEALTHY: no issues detected"


def exit_code(findings: list[Finding]) -> int:
    if any(f.severity is Severity.CRITICAL for f in findings):
        return 2
    if any(f.severity is Severity.WARNING for f in findings):
        return 1
    return 0