"""Command-line interface for syswatch."""

from __future__ import annotations

import argparse
import json
import logging
import platform
import sys
import time
from pathlib import Path
from typing import Sequence

from syswatch import __version__
from syswatch.collectors import cpu as cpu_collector
from syswatch.collectors import network as net_collector
from syswatch.collectors import processes as proc_collector
from syswatch.collectors.memory import MemoryInfo, get_memory
from syswatch.collectors.system import (
    DiskUsage,
    NetworkInterface,
    SystemSnapshot,
    gather_system_snapshot,
    list_filesystems,
    list_network_interfaces,
)
from syswatch.config import AppConfig, load_config, search_paths
from syswatch.exceptions import ConfigError, SyswatchError
from syswatch.health import Severity, exit_code, run_checks, summarize
from syswatch.logging_setup import setup_logging
from syswatch.output import (
    bold,
    bytes_or_dash,
    dim,
    detect_color_support,
    enable_color,
    format_table,
    green,
    red,
    yellow,
)
from syswatch.report import build_report, dump_report_json
from syswatch.util import format_bytes, format_percent, format_uptime

log = logging.getLogger("syswatch.cli")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_INTERRUPTED = 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="syswatch",
        description=(
            "Local Linux system diagnostics and monitoring. Reads /proc and "
            "/sys directly; degrades gracefully when data is unavailable."
        ),
        epilog=(
            "Run 'syswatch' with no command for the interactive TUI.\n"
            "Exit status: 0 success, 1 error or health warnings, "
            "2 critical findings or usage error."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="enable debug logging to stderr"
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true", help="suppress log output"
    )
    parser.add_argument(
        "--no-color", action="store_true", help="disable colored output"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        metavar="PATH",
        help="path to a TOML configuration file",
    )
    def non_negative_int(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{text!r} is not an integer")
        if value < 0:
            raise argparse.ArgumentTypeError("must be >= 0")
        return value

    subparsers = parser.add_subparsers(dest="command", required=False)

    info = subparsers.add_parser("info", help="print static system information")
    info.add_argument("--json", action="store_true", help="emit JSON instead of text")

    monitor = subparsers.add_parser("monitor", help="live resource monitoring")
    monitor.add_argument(
        "-i", "--interval", type=float, default=None, help="refresh interval seconds"
    )
    monitor.add_argument(
        "-n",
        "--count",
        type=non_negative_int,
        default=None,
        metavar="N",
        help="number of refreshes before exiting (default: unlimited)",
    )
    monitor.add_argument(
        "--json", action="store_true", help="emit one JSON object per refresh"
    )
    monitor.add_argument(
        "--no-clear", action="store_true", help="do not clear the screen between updates"
    )

    ps = subparsers.add_parser("ps", help="process viewer")
    ps.add_argument(
        "-s",
        "--sort",
        choices=("cpu", "mem", "pid", "name"),
        default=None,
        help="sort key (default: cpu)",
    )
    ps.add_argument("-f", "--filter", default=None, help="filter by process name substring")
    ps.add_argument(
        "-n",
        "--rows",
        type=non_negative_int,
        default=None,
        metavar="N",
        help="max rows to show; 0 for all (default from config)",
    )
    ps.add_argument("--json", action="store_true", help="emit JSON")

    health = subparsers.add_parser("health", help="run system health checks")
    health.add_argument("--json", action="store_true", help="emit JSON")

    report = subparsers.add_parser("report", help="generate a JSON diagnostic report")
    report.add_argument(
        "-o", "--output", type=Path, default=None, help="write report to file"
    )
    report.add_argument(
        "--pretty", action="store_true", help="pretty-print the JSON output"
    )

    subparsers.add_parser("config", help="show effective configuration")
    subparsers.add_parser("tui", help="launch the interactive terminal UI (default)")

    return parser


def _require_linux() -> bool:
    if platform.system() == "Linux":
        return True
    print(
        f"syswatch: this tool requires Linux; detected {platform.system()}. "
        "System information is collected from /proc and /sys.",
        file=sys.stderr,
    )
    return False


def _render_info_text(
    snapshot: SystemSnapshot, disks: list[DiskUsage], interfaces: list[NetworkInterface]
) -> str:
    lines: list[str] = []
    memory: MemoryInfo | None = snapshot.memory

    lines.append(bold("System"))
    lines.append(f"  Hostname:   {snapshot.hostname or 'unknown'}")
    lines.append(f"  OS:         {snapshot.os_pretty_name or 'unknown'}")
    lines.append(f"  Kernel:     {snapshot.kernel or 'unknown'}")
    lines.append(f"  Arch:       {snapshot.arch or 'unknown'}")
    lines.append(f"  Uptime:     {format_uptime(snapshot.uptime_seconds)}")
    lines.append("")
    lines.append(bold("CPU"))
    lines.append(f"  Model:      {snapshot.cpu_model or 'unknown'}")
    lines.append(f"  Cores:      {snapshot.cpu_cores if snapshot.cpu_cores else 'unknown'}")
    lines.append("")
    lines.append(bold("Memory"))
    if memory:
        used = bytes_or_dash(memory.used_bytes)
        total = bytes_or_dash(memory.total_bytes)
        available = bytes_or_dash(memory.available_bytes)
        pct = format_percent(memory.used_percent)
        lines.append(f"  Total:      {total}")
        lines.append(f"  Available:  {available}")
        lines.append(f"  Used:       {used} ({pct})")
        swap_total = memory.swap_total_bytes or 0
        if swap_total > 0:
            swap_pct = format_percent(memory.swap_used_percent)
            lines.append(f"  Swap:       {format_bytes(swap_total)} ({swap_pct} used)")
    else:
        lines.append(dim("  memory information unavailable"))
    lines.append("")
    lines.append(bold("Disks"))
    if disks:
        rows = [
            [
                d.device,
                d.mountpoint,
                d.fstype,
                format_bytes(d.total_bytes),
                format_bytes(d.used_bytes),
                format_bytes(d.available_bytes),
                format_percent(d.percent),
            ]
            for d in disks
        ]
        lines.append(
            format_table(
                ["Device", "Mount", "Type", "Size", "Used", "Avail", "Use%"],
                rows,
                ["l", "l", "l", "r", "r", "r", "r"],
            )
        )
    else:
        lines.append(dim("  no real filesystems detected"))
    lines.append("")
    lines.append(bold("Network"))
    if interfaces:
        rows = []
        for i in interfaces:
            state = green(i.state) if i.is_up else i.state
            rows.append(
                [
                    i.name,
                    state,
                    i.mac or "-",
                    ", ".join(i.ipv4_addresses) or "-",
                    ", ".join(i.ipv6_addresses) or "-",
                ]
            )
        lines.append(
            format_table(
                ["Interface", "State", "MAC", "IPv4", "IPv6"],
                rows,
            )
        )
    else:
        lines.append(dim("  no interfaces detected"))
    return "\n".join(lines)


def _info_payload(
    snapshot: SystemSnapshot, disks: list[DiskUsage], interfaces: list[NetworkInterface]
) -> dict[str, object]:
    memory = snapshot.memory
    return {
        "hostname": snapshot.hostname,
        "os_pretty_name": snapshot.os_pretty_name,
        "kernel": snapshot.kernel,
        "arch": snapshot.arch,
        "uptime_seconds": snapshot.uptime_seconds,
        "boot_time_epoch": snapshot.boot_time_epoch,
        "cpu": {"model": snapshot.cpu_model, "cores": snapshot.cpu_cores},
        "memory": {
            "total_bytes": memory.total_bytes if memory else None,
            "available_bytes": memory.available_bytes if memory else None,
            "used_bytes": memory.used_bytes if memory else None,
            "used_percent": round(memory.used_percent, 2) if memory else None,
            "swap_total_bytes": memory.swap_total_bytes if memory else None,
        },
        "disks": [
            {
                "device": d.device,
                "mountpoint": d.mountpoint,
                "fstype": d.fstype,
                "total_bytes": d.total_bytes,
                "used_bytes": d.used_bytes,
                "available_bytes": d.available_bytes,
                "percent_used": d.percent,
            }
            for d in disks
        ],
        "network_interfaces": [
            {
                "name": i.name,
                "state": i.state,
                "is_up": i.is_up,
                "mac": i.mac,
                "ipv4_addresses": i.ipv4_addresses,
                "ipv6_addresses": i.ipv6_addresses,
            }
            for i in interfaces
        ],
    }


def cmd_info(args: argparse.Namespace, config: AppConfig) -> int:
    errors: list[str] = []
    snapshot = gather_system_snapshot(errors=errors)
    disks = list_filesystems()
    interfaces = list_network_interfaces()
    if args.json:
        payload = _info_payload(snapshot, disks, interfaces)
        payload["collection_errors"] = errors
        print(json.dumps(payload, indent=2))
        return EXIT_OK
    print(_render_info_text(snapshot, disks, interfaces))
    if errors:
        print()
        print(yellow("Some information could not be collected:"))
        for err in errors:
            print(f"  - {err}")
    return EXIT_OK


def cmd_monitor(args: argparse.Namespace, config: AppConfig) -> int:
    interval = args.interval if args.interval is not None else config.monitor_interval
    interval = max(0.1, interval)
    count = args.count if args.count is not None else None
    use_clear = not args.no_clear and sys.stdout.isatty() and not args.json

    prev_cpu = cpu_collector.read_cpu_times()
    prev_net = net_collector.read_interface_counters()
    ticks_done = 0
    while True:
        try:
            started = time.monotonic()
            time.sleep(interval)
            elapsed = time.monotonic() - started
        except KeyboardInterrupt:
            return EXIT_INTERRUPTED
        cur_cpu = cpu_collector.read_cpu_times()
        cur_net = net_collector.read_interface_counters()

        overall_prev = prev_cpu.get("cpu")
        overall_cur = cur_cpu.get("cpu")
        cpu_percent = (
            cpu_collector.cpu_usage_between(overall_prev, overall_cur)
            if overall_prev and overall_cur
            else None
        )
        per_core = {
            label: value
            for label, value in cpu_collector.per_cpu_usage_between(
                prev_cpu, cur_cpu
            ).items()
            if label != "cpu"
        }

        memory = get_memory()
        proc_count = proc_collector.count_processes()
        load = cpu_collector.load_average()
        rates = net_collector.interface_rates(prev_net, cur_net, elapsed)
        rx_rate, tx_rate = net_collector.total_rates(rates)

        worst_disk: DiskUsage | None = None
        best_percent = -1.0
        for disk in list_filesystems():
            if disk.percent is not None and disk.percent > best_percent:
                worst_disk = disk
                best_percent = disk.percent

        tick_payload: dict[str, object] = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "cpu_percent": round(cpu_percent, 1) if cpu_percent is not None else None,
            "per_core_cpu_percent": {
                label: round(v, 1) if v is not None else None
                for label, v in sorted(per_core.items())
            },
            "memory_percent": round(memory.used_percent, 1) if memory and memory.used_percent is not None else None,
            "memory_used_bytes": memory.used_bytes if memory else None,
            "memory_total_bytes": memory.total_bytes if memory else None,
            "disk_worst_mountpoint": worst_disk.mountpoint if worst_disk else None,
            "disk_worst_percent": round(worst_disk.percent, 1) if worst_disk and worst_disk.percent is not None else None,
            "network_rx_bytes_per_second": rx_rate,
            "network_tx_bytes_per_second": tx_rate,
            "process_count": proc_count,
            "load_1m": round(load[0], 2) if load else None,
            "load_5m": round(load[1], 2) if load else None,
            "load_15m": round(load[2], 2) if load else None,
        }

        if args.json:
            print(json.dumps(tick_payload), flush=True)
        else:
            if use_clear:
                print("\x1b[2J\x1b[H", end="")
            mem_part = (
                f"{tick_payload['memory_percent']}% "
                f"({format_bytes(memory.used_bytes)}/{format_bytes(memory.total_bytes)})"
                if memory and memory.used_percent is not None
                else "unknown"
            )
            cpu_text = (
                f"{cpu_percent:.1f}%" if cpu_percent is not None else "--"
            )
            disk_text = (
                f"{worst_disk.percent:.1f}% ({worst_disk.mountpoint})"
                if worst_disk and worst_disk.percent is not None
                else "unknown"
            )
            load_text = (
                f"{load[0]:.2f}/{load[1]:.2f}/{load[2]:.2f}" if load else "unknown"
            )
            print(
                f"{tick_payload['timestamp']}  "
                f"CPU {cpu_text:>7}  "
                f"MEM {mem_part}  "
                f"DISK {disk_text}  "
                f"RX {format_bytes(rx_rate)}/s  TX {format_bytes(tx_rate)}/s  "
                f"PROCS {proc_count if proc_count is not None else '?'}  "
                f"LOAD {load_text}",
                flush=True,
            )

        prev_cpu = cur_cpu
        prev_net = cur_net
        ticks_done += 1
        if count is not None and ticks_done >= count:
            break
    return EXIT_OK


def cmd_ps(args: argparse.Namespace, config: AppConfig) -> int:
    sort_key = args.sort or "cpu"
    rows_limit = args.rows if args.rows is not None else config.ps_rows

    before_list = proc_collector.snapshot()
    before = {p.pid: p for p in before_list}
    delay = config.sample_delay
    try:
        started = time.monotonic()
        time.sleep(delay)
        elapsed = time.monotonic() - started
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    after_list = proc_collector.snapshot()
    after = {p.pid: p for p in after_list}
    cpu_usage = proc_collector.cpu_percent_between(before, after, elapsed)

    total_ram = None
    memory = get_memory()
    if memory:
        total_ram = memory.total_bytes

    needle = args.filter.lower() if args.filter else None
    entries: list[dict[str, object]] = []
    for proc in after_list:
        name_for_filter = proc.name
        if needle is not None and needle not in name_for_filter.lower():
            continue
        cpu_pct = cpu_usage.get(proc.pid, 0.0)
        mem_pct = proc_collector.memory_percent(proc, total_ram)
        command = proc.cmdline or f"[{proc.name}]"
        if len(command) > 48:
            command = command[:45] + "..."
        entries.append(
            {
                "pid": proc.pid,
                "name": proc.name,
                "state": proc.state,
                "cpu_percent": round(cpu_pct, 1),
                "memory_percent": round(mem_pct, 1) if mem_pct is not None else None,
                "rss_bytes": proc.rss_bytes,
                "command": command,
                "zombie": proc.is_zombie,
            }
        )

    sorters = {
        "cpu": lambda e: float(e["cpu_percent"]),
        "mem": lambda e: e["rss_bytes"],
        "pid": lambda e: int(e["pid"]),  # type: ignore[arg-type]
        "name": lambda e: str(e["name"]).lower(),
    }
    entries.sort(key=sorters[sort_key], reverse=sort_key in ("cpu", "mem", "pid"))

    shown = entries if rows_limit == 0 else entries[:rows_limit]

    if args.json:
        print(json.dumps(shown, indent=2))
        return EXIT_OK

    rows = [
        [
            str(e["pid"]),
            str(e["name"]),
            str(e["state"]),
            f"{e['cpu_percent']:.1f}",
            format_percent(e["memory_percent"]),
            format_bytes(e["rss_bytes"]),  # type: ignore[arg-type]
            str(e["command"]),
        ]
        for e in shown
    ]
    header = f"{len(entries)} processes"
    if needle:
        header += f" matching {args.filter!r}"
    header += f" (sorted by {sort_key})"
    print(bold(header))
    print(
        format_table(
            ["PID", "NAME", "S", "CPU%", "MEM%", "RSS", "COMMAND"],
            rows,
            ["r", "l", "l", "r", "r", "r", "l"],
        )
    )
    return EXIT_OK


def cmd_health(args: argparse.Namespace, config: AppConfig) -> int:
    findings, errors = run_checks(config)
    if args.json:
        print(
            json.dumps(
                {
                    "summary": summarize(findings),
                    "findings": [f.to_dict() for f in findings],
                    "collection_errors": errors,
                },
                indent=2,
            )
        )
    else:
        severity_paint = {
            Severity.CRITICAL: red,
            Severity.WARNING: yellow,
            Severity.INFO: dim,
        }
        if not findings:
            print(green(summarize(findings)))
        else:
            print(bold(summarize(findings)))
            for finding in findings:
                paint = severity_paint.get(finding.severity, lambda t: t)
                marker = {Severity.CRITICAL: "[critical]", Severity.WARNING: "[warning] ", Severity.INFO: "[info]    "}[finding.severity]
                print(paint(f"  {marker} {finding.message}"))
        for err in errors:
            print(dim(f"  note: {err}"), file=sys.stderr)
    return exit_code(findings)


def cmd_report(args: argparse.Namespace, config: AppConfig) -> int:
    report_data = build_report(config)
    text = dump_report_json(report_data, pretty=args.pretty)
    if args.output is not None:
        try:
            args.output.write_text(text, encoding="utf-8")
        except OSError as exc:
            print(f"syswatch: cannot write report to {args.output}: {exc}", file=sys.stderr)
            return EXIT_ERROR
    else:
        sys.stdout.write(text)
    return EXIT_OK


def cmd_config(args: argparse.Namespace, config: AppConfig, source: Path | None) -> int:
    del args
    print(bold("Configuration"))
    print(f"  Source file: {source if source else '(built-in defaults, no config file found)'}")
    print()
    print(bold("Search order"))
    for idx, path in enumerate(search_paths(), start=1):
        marker = " <- in use" if source == path else ""
        print(f"  {idx}. {path}{marker}")
    print()
    print(bold("Effective settings"))
    rows = [[name, repr(getattr(config, name))] for name in config.__dataclass_fields__]
    print(format_table(["Option", "Value"], rows))
    return EXIT_OK


def _launch_tui(config: AppConfig, source: Path | None, verbose: bool) -> int:
    """Start the interactive TUI; explain clearly when it cannot run.

    The effective AppConfig is the one already resolved in main(), so an
    explicit --config path and its search order are honoured.
    """
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if not interactive:
        print(
            "syswatch: the interactive TUI requires an interactive terminal. "
            "Use 'syswatch info', 'monitor', 'ps', 'health' or 'report' for "
            "non-interactive usage.",
            file=sys.stderr,
        )
        return EXIT_ERROR
    try:
        from syswatch.tui import SyswatchApp
    except ImportError as exc:
        print(
            f"syswatch: the TUI requires the 'textual' package ({exc}). "
            "Install it with: pip install 'syswatch[tui]'",
            file=sys.stderr,
        )
        return EXIT_ERROR
    app = SyswatchApp(config, config_source=str(source) if source else "")
    if not verbose:
        logging.getLogger("syswatch").setLevel(logging.ERROR)
    return int(app.run() or 0)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    setup_logging(verbose=args.verbose, quiet=args.quiet)
    enable_color(detect_color_support(args.no_color))

    if not _require_linux():
        return EXIT_ERROR

    try:
        config, source = load_config(args.config)
    except ConfigError as exc:
        print(f"syswatch: configuration error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if args.command in (None, "tui"):
        return _launch_tui(config, source, verbose=args.verbose)

    try:
        if args.command == "info":
            return cmd_info(args, config)
        if args.command == "monitor":
            return cmd_monitor(args, config)
        if args.command == "ps":
            return cmd_ps(args, config)
        if args.command == "health":
            return cmd_health(args, config)
        if args.command == "report":
            return cmd_report(args, config)
        if args.command == "config":
            return cmd_config(args, config, source)
    except SyswatchError as exc:
        print(f"syswatch: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except OSError:
            pass
        return EXIT_OK
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return EXIT_INTERRUPTED

    parser.error(f"unknown command {args.command!r}")
    return EXIT_ERROR