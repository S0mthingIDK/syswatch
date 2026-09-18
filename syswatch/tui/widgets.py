"""Dashboard widgets for the syswatch TUI.

Each widget owns one resource area (CPU, memory, disks, network, health)
and exposes plain update_* methods. Widgets never touch the collectors
themselves; the application feeds them data from syswatch.tui.state.
"""

from __future__ import annotations

import datetime as _dt
import platform

import textual
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import DataTable, Sparkline, Static

from syswatch import __version__
from syswatch.collectors.memory import MemoryInfo
from syswatch.collectors.system import DiskUsage, NetworkInterface
from syswatch.health import (
    CHECK_CRIT,
    CHECK_OK,
    CHECK_UNAVAIL,
    CHECK_WARN,
    Finding,
    HealthEvaluation,
    Severity,
)
from syswatch.util import format_bytes, format_percent, format_rate, format_uptime

CLASS_OK = "m-ok"
CLASS_WARN = "m-warn"
CLASS_CRIT = "m-crit"
CLASS_DIM = "m-dim"

_BAR_WIDTH = 20
_FILLED = "\u2588"
_EMPTY = "\u2591"


def severity_class(
    percent: float | None,
    warning_threshold: float | None = None,
    critical_offset: float = 10.0,
) -> str:
    """Map a percentage to a CSS severity class using configured thresholds.

    When *warning_threshold* is None the caller has not supplied a config
    value; fall back to sensible defaults (warn ≥70, crit ≥90). Otherwise
    the critical level is warning_threshold + critical_offset, matching the
    health module.
    """
    if percent is None:
        return CLASS_DIM
    if warning_threshold is not None:
        if percent >= warning_threshold + critical_offset:
            return CLASS_CRIT
        if percent >= warning_threshold:
            return CLASS_WARN
        return CLASS_OK
    if percent >= 90:
        return CLASS_CRIT
    if percent >= 70:
        return CLASS_WARN
    return CLASS_OK


def meter_bar(percent: float | None, width: int = _BAR_WIDTH) -> str:
    """A text progress bar such as '████░░░░░' for inline metrics."""
    if percent is None:
        return "\u2500" * width
    clamped = max(0.0, min(100.0, percent))
    filled = round(clamped / 100 * width)
    return _FILLED * filled + _EMPTY * (width - filled)


class CPUPanel(Vertical):
    """Current utilisation, per-core bars, load and identity info."""

    DEFAULT_CSS = """
    CPUPanel { padding: 0 1; }
    CPUPanel #cpu-big { text-style: bold; }
    CPUPanel .aux { color: $text-disabled; }
    """

    def compose(self) -> ComposeResult:
        yield Static("", id="cpu-big")
        yield Sparkline([], id="cpu-spark")
        yield Static("", id="cpu-cores", classes="mono aux")
        yield Static("", id="cpu-info", classes="aux")

    def on_mount(self) -> None:
        self.border_title = "CPU"

    def update_sample(
        self,
        percent: float | None,
        per_core: dict[str, float | None],
        load: tuple[float, float, float] | None,
        model: str | None,
        cores: int | None,
        history: list[float],
        warning_threshold: float | None = None,
        critical_offset: float = 10.0,
    ) -> None:
        big = self.query_one("#cpu-big", Static)
        big.remove_class(CLASS_OK, CLASS_WARN, CLASS_CRIT, CLASS_DIM)
        big.add_class(severity_class(percent, warning_threshold, critical_offset))
        big.update(f"{format_percent(percent):>7}  {meter_bar(percent)}")

        spark = self.query_one("#cpu-spark", Sparkline)
        spark.data = history

        core_lines = [
            f"{label:<5} {meter_bar(value)} {format_percent(value)}"
            for label, value in sorted(per_core.items())
        ]
        self.query_one("#cpu-cores", Static).update("\n".join(core_lines))

        load_text = (
            f"load {load[0]:.2f} / {load[1]:.2f} / {load[2]:.2f}" if load else "load unavailable"
        )
        cores_text = f"{cores} cores" if cores else "cores unknown"
        model_text = model or "model unknown"
        self.query_one("#cpu-info", Static).update(
            f"{cores_text} \u00b7 {load_text}\n{model_text}"
        )


class MemoryPanel(Vertical):
    DEFAULT_CSS = """
    MemoryPanel { padding: 0 1; }
    MemoryPanel #mem-big { text-style: bold; }
    MemoryPanel .aux { color: $text-disabled; }
    """

    def compose(self) -> ComposeResult:
        yield Static("", id="mem-big")
        yield Static("", id="mem-detail")
        yield Sparkline([], id="mem-spark")
        yield Static("", id="mem-swap", classes="mono aux")

    def on_mount(self) -> None:
        self.border_title = "Memory"

    def update_memory(
        self,
        memory: MemoryInfo | None,
        history: list[float],
        error: str | None = None,
        warning_threshold: float | None = None,
        critical_offset: float = 10.0,
    ) -> None:
        big = self.query_one("#mem-big", Static)
        detail = self.query_one("#mem-detail", Static)
        swap = self.query_one("#mem-swap", Static)
        spark = self.query_one("#mem-spark", Sparkline)

        if memory is None:
            big.remove_class(CLASS_OK, CLASS_WARN, CLASS_CRIT).add_class(CLASS_DIM)
            big.update("unavailable")
            detail.update(error or "memory information is not readable")
            swap.update("")
            spark.data = []
            return

        percent = memory.used_percent
        big.remove_class(CLASS_OK, CLASS_WARN, CLASS_CRIT, CLASS_DIM)
        big.add_class(severity_class(percent, warning_threshold, critical_offset))
        big.update(
            f"{format_percent(percent):>7}  {meter_bar(percent)}"
        )
        detail.update(
            f"used {format_bytes(memory.used_bytes)} of "
            f"{format_bytes(memory.total_bytes)} \u00b7 available "
            f"{format_bytes(memory.available_bytes)}"
        )
        if memory.swap_total_bytes and memory.swap_total_bytes > 0:
            swap.update(
                f"swap {format_percent(memory.swap_used_percent)} used of "
                f"{format_bytes(memory.swap_total_bytes)}"
            )
        else:
            swap.update("no swap configured")
        spark.data = history


class DiskPanel(Vertical):
    columns = ("Mount", "Type", "Size", "Used", "Avail", "Use%")

    def compose(self) -> ComposeResult:
        table = DataTable(id="disk-table", cursor_type="row", show_cursor=False)
        table.can_focus = False
        yield table

    def on_mount(self) -> None:
        self.border_title = "Filesystems"
        table = self.query_one("#disk-table", DataTable)
        table.add_columns(*self.columns)

    def update_disks(
        self,
        disks: list[DiskUsage],
        warning_threshold: float,
        error: str | None = None,
        critical_offset: float = 10.0,
    ) -> None:
        table = self.query_one("#disk-table", DataTable)
        table.clear()
        if error and not disks:
            table.add_row(Text(error, style="italic"), "", "", "", "", "")
            return
        for disk in disks:
            pct = disk.percent
            pct_text = Text(format_percent(pct), justify="right")
            cls = severity_class(pct, warning_threshold, critical_offset)
            pct_text.style = {
                "m-ok": "green",
                "m-warn": "yellow",
                "m-crit": "red bold",
                "m-dim": "dim",
            }[cls]
            table.add_row(
                disk.mountpoint,
                disk.fstype,
                format_bytes(disk.total_bytes),
                format_bytes(disk.used_bytes),
                format_bytes(disk.available_bytes),
                pct_text,
            )


class NetworkPanel(Vertical):
    def compose(self) -> ComposeResult:
        table = DataTable(id="net-table", cursor_type="row", show_cursor=False)
        table.can_focus = False
        yield table
        yield Sparkline([], id="rx-spark")
        yield Static("", id="tx-line", classes="aux")

    def on_mount(self) -> None:
        self.border_title = "Network"
        table = self.query_one("#net-table", DataTable)
        table.add_columns("IFace", "State", "IPv4", "RX/s", "TX/s")

    def update_network(
        self,
        interfaces: list[NetworkInterface] | None,
        rates: dict[str, tuple[float, float]],
        rx_history: list[float],
        tx_history: list[float],
        error: str | None = None,
    ) -> None:
        table = self.query_one("#net-table", DataTable)
        table.clear()
        if interfaces is None:
            table.add_row(Text(error or "unavailable", style="italic"), "", "", "", "")
            return
        for iface in interfaces:
            rx_rate, tx_rate = rates.get(iface.name, (None, None))
            ipv4 = ", ".join(iface.ipv4_addresses) or "-"
            state_style = "green" if iface.is_up else "dim"
            table.add_row(
                iface.name,
                Text(iface.state, style=state_style),
                ipv4,
                format_rate(rx_rate),
                format_rate(tx_rate),
            )
        self.query_one("#rx-spark", Sparkline).data = rx_history
        total_tx = sum(t for _, t in rates.values() if t is not None)
        self.query_one("#tx-line", Static).update(
            f"TX {format_rate(total_tx)} total"
        )


_STATE_STYLE = {
    CHECK_OK: "green",
    CHECK_WARN: "yellow",
    CHECK_CRIT: "red bold",
    CHECK_UNAVAIL: "dim",
}


def overall_summary_text(evaluation: HealthEvaluation | None) -> Text:
    """Prominent one-line verdict for a health evaluation."""
    if evaluation is None:
        return Text("OVERALL HEALTH: UNAVAILABLE", style="red bold")
    checks = evaluation.checks
    crits = sum(1 for c in checks if c.state == CHECK_CRIT)
    warns = sum(1 for c in checks if c.state == CHECK_WARN)
    unavail = sum(1 for c in checks if c.state == CHECK_UNAVAIL)
    normal = len(checks) - crits - warns - unavail
    if crits:
        return Text(
            f"OVERALL HEALTH: CRITICAL \u2014 {crits} critical,"
            f" {warns} warning(s), {normal} normal"
            + (f", {unavail} unavailable" if unavail else ""),
            style="red bold",
        )
    if warns:
        return Text(
            f"OVERALL HEALTH: ATTENTION NEEDED \u2014 {warns} warning(s),"
            f" {normal} normal"
            + (f", {unavail} unavailable" if unavail else ""),
            style="yellow bold",
        )
    return Text(
        f"OVERALL HEALTH: HEALTHY \u2014 {normal}/{len(checks)} checks normal"
        + (f", {unavail} unavailable" if unavail else ""),
        style="green bold",
    )


class HealthPanel(Vertical):
    DEFAULT_CSS = """
    HealthPanel { padding: 0 1; }
    HealthPanel #health-summary { text-style: bold; margin-bottom: 1; }
    """

    def compose(self) -> ComposeResult:
        yield Static("", id="health-summary")
        yield Static("", id="health-body")

    def on_mount(self) -> None:
        self.border_title = "System Health"

    def update_evaluation(
        self,
        evaluation: HealthEvaluation | None,
        failure: str | None = None,
    ) -> None:
        summary = self.query_one("#health-summary", Static)
        body = self.query_one("#health-body", Static)
        summary.update(overall_summary_text(evaluation))

        if evaluation is None:
            body.update(
                Text(failure or "health checks have not run yet", style="italic")
            )
            return

        table = Table(box=None, show_header=True, pad_edge=False, expand=True)
        table.add_column("", width=2)
        table.add_column("Check", no_wrap=True)
        table.add_column("Value", justify="right", no_wrap=True)
        table.add_column("Threshold", justify="right", no_wrap=True)
        table.add_column("Status", no_wrap=True)
        table.add_column("Notes", ratio=1, overflow="fold")
        state_order = {CHECK_CRIT: 0, CHECK_WARN: 1, CHECK_UNAVAIL: 2, CHECK_OK: 3}
        for check in sorted(
            evaluation.checks, key=lambda c: (state_order.get(c.state, 9), c.name)
        ):
            style = _STATE_STYLE.get(check.state, "white")
            table.add_row(
                Text(check.icon, style=style),
                Text(check.name),
                Text(check.value, style=style, justify="right"),
                Text(check.threshold, style="dim", justify="right"),
                Text(check.status_label, style=style),
                Text(check.note or "", style="dim"),
            )
        for err in evaluation.errors:
            table.add_row(
                Text("–", style="dim"), Text("collector", style="dim"),
                Text("", style="dim"), Text("", style="dim"),
                Text("NOTE", style="dim"), Text(err, style="dim italic"),
            )
        body.update(table)


class HealthStrip(Static):
    """One-line health summary shown on the dashboard."""

    DEFAULT_CSS = """
    HealthStrip { border: round $panel; border-title-color: $text-muted;
                  padding: 0 1; content-align: left middle; }
    """

    def update_health(self, findings: list[Finding] | None, failure: str | None = None) -> None:
        self.border_title = "Health"
        if findings is None:
            self.update(Text(failure or "health checks pending\u2026", style="dim"))
            return
        criticals = sum(1 for f in findings if f.severity is Severity.CRITICAL)
        warnings = sum(1 for f in findings if f.severity is Severity.WARNING)
        if criticals:
            self.update(Text(f"{criticals} critical \u00b7 {warnings} warnings \u00b7 see view [4]", style="red bold"))
        elif warnings:
            self.update(Text(f"{warnings} warnings \u00b7 see view [4]", style="yellow"))
        else:
            self.update(Text("healthy \u00b7 no issues detected", style="green"))

class HostPanel(Vertical):
    """Static host identity plus slowly-changing facts (uptime, procs)."""

    DEFAULT_CSS = """
    HostPanel { padding: 0 1; }
    HostPanel .kv { color: $text; }
    HostPanel .k { color: $text-muted; }
    """

    def compose(self) -> ComposeResult:
        yield Static("", id="host-body")

    def on_mount(self) -> None:
        self.border_title = "Host"

    def update_host(
        self,
        hostname: str | None,
        os_pretty: str | None,
        uptime_seconds: float | None,
        boot_epoch: int | None,
        process_count: int | None,
    ) -> None:
        boot_text = (
            _dt.datetime.fromtimestamp(boot_epoch).strftime("%Y-%m-%d %H:%M")
            if boot_epoch
            else "unknown"
        )
        body = Text()
        for label, value in (
            ("Hostname", hostname or "unknown"),
            ("OS", os_pretty or "unknown"),
            ("Kernel", platform.release()),
            ("Arch", platform.machine()),
            ("Uptime", format_uptime(uptime_seconds)),
            ("Boot time", boot_text),
            ("Processes", str(process_count) if process_count is not None else "-"),
        ):
            body.append(f"{label:<10}", style="dim")
            body.append(f"{value}\n")
        self.query_one("#host-body", Static).update(body)


class CpuInfoPanel(Vertical):
    """CPU identity and utilisation for the System view."""

    DEFAULT_CSS = """
    CpuInfoPanel { padding: 0 1; }
    CpuInfoPanel #cpuinfo-big { text-style: bold; margin-bottom: 1; }
    CpuInfoPanel .aux { color: $text-muted; }
    """

    def compose(self) -> ComposeResult:
        yield Static("", id="cpuinfo-head", classes="aux")
        yield Static("", id="cpuinfo-big")
        yield Static("", id="cpuinfo-cores", classes="mono")
        yield Static("", id="cpuinfo-load", classes="aux")

    def on_mount(self) -> None:
        self.border_title = "CPU"

    def update_cpu(
        self,
        percent: float | None,
        per_core: dict[str, float | None],
        load: tuple[float, float, float] | None,
        model: str | None,
        cores: int | None,
        frequency_mhz: float | None,
        warning_threshold: float | None = None,
        critical_offset: float = 10.0,
    ) -> None:
        freq = f" \u00b7 {frequency_mhz / 1000:.2f} GHz" if frequency_mhz else ""
        self.query_one("#cpuinfo-head", Static).update(
            f"{model or 'model unknown'} \u00b7 {cores if cores else '?'} cores{freq}"
        )
        big = self.query_one("#cpuinfo-big", Static)
        big.remove_class(CLASS_OK, CLASS_WARN, CLASS_CRIT, CLASS_DIM)
        big.add_class(severity_class(percent, warning_threshold, critical_offset))
        big.update(f"{format_percent(percent):>7}  {meter_bar(percent)}")
        core_lines = [
            f"{label:<5} {meter_bar(value)} {format_percent(value)}"
            for label, value in sorted(per_core.items())
        ]
        self.query_one("#cpuinfo-cores", Static).update(
            "\n".join(core_lines) or "per-core utilisation unavailable"
        )
        load_line = (
            f"load {load[0]:.2f} / {load[1]:.2f} / {load[2]:.2f}"
            if load
            else "load unavailable"
        )
        self.query_one("#cpuinfo-load", Static).update(load_line)


class MemoryInfoPanel(Vertical):
    """RAM and swap as paired progress bars with figures (System view)."""

    DEFAULT_CSS = """
    MemoryInfoPanel { padding: 0 1; }
    MemoryInfoPanel .mem-line { margin-bottom: 1; }
    MemoryInfoPanel .lbl { color: $text-muted; }
    """

    def compose(self) -> ComposeResult:
        yield Static("", id="ram-line", classes="mem-line")
        yield Static("", id="swap-line", classes="mem-line")
        yield Static("", id="mem-figures", classes="lbl")

    def on_mount(self) -> None:
        self.border_title = "Memory"

    def update_memory(
        self,
        memory: MemoryInfo | None,
        warning_threshold: float | None = None,
        critical_offset: float = 10.0,
    ) -> None:
        ram = self.query_one("#ram-line", Static)
        swap = self.query_one("#swap-line", Static)
        figures = self.query_one("#mem-figures", Static)
        if memory is None:
            ram.update("RAM   unavailable")
            swap.update("SWAP  unavailable")
            figures.update("")
            return

        pct = memory.used_percent
        style = {
            "m-ok": "green", "m-warn": "yellow", "m-crit": "red bold",
            "m-dim": "dim",
        }[severity_class(pct, warning_threshold, critical_offset)]
        ram.update(
            Text.assemble(
                ("RAM   ", "dim"),
                (f"{format_percent(pct):>7}", style),
                (f"  {meter_bar(pct)}\n", style),
                (f"      used {format_bytes(memory.used_bytes)} of "
                 f"{format_bytes(memory.total_bytes)} \u00b7 available "
                 f"{format_bytes(memory.available_bytes)}", "dim"),
            )
        )
        swap_total = memory.swap_total_bytes or 0
        if swap_total > 0:
            spct = memory.swap_used_percent
            sstyle = {
                "m-ok": "green", "m-warn": "yellow",
                "m-crit": "red bold", "m-dim": "dim",
            }[severity_class(spct, warning_threshold, critical_offset)]
            swap.update(
                Text.assemble(
                    ("SWAP  ", "dim"),
                    (f"{format_percent(spct):>7}", sstyle),
                    (f"  {meter_bar(spct)}\n", sstyle),
                    (f"      used {format_bytes((memory.swap_total_bytes or 0) - (memory.swap_free_bytes or 0))} of "
                     f"{format_bytes(swap_total)}", "dim"),
                )
            )
        else:
            swap.update(Text.assemble(("SWAP  ", "dim"), ("none configured", "dim italic")))
        figures.update("")


class ProcessDetailPanel(Vertical):
    """Details for the currently selected process (Processes view)."""

    DEFAULT_CSS = """
    ProcessDetailPanel {
        width: 36; padding: 0 1; border-left: solid $primary-lighten-2;
    }
    ProcessDetailPanel #proc-detail-text { color: $text; }
    """

    def compose(self) -> ComposeResult:
        yield Static("no process selected", id="proc-detail-text")

    def on_mount(self) -> None:
        self.border_title = "Details"

    def show_row(
        self,
        row: object | None,
        extras: dict[str, object] | None,
        boot_epoch: int | None,
        clk_tck: int,
    ) -> None:
        target = self.query_one("#proc-detail-text", Static)
        if row is None:
            target.update(Text("no process selected", style="dim italic"))
            return

        started = "-"
        raw_start = getattr(row.raw, "starttime_ticks", None)
        if boot_epoch and raw_start:
            epoch = boot_epoch + raw_start / max(1, clk_tck)
            started = _dt.datetime.fromtimestamp(epoch).strftime("%H:%M:%S")

        mem_pct = (
            format_percent(row.memory_percent)
            if row.memory_percent is not None else "-"
        )
        state_style = "bold red" if row.state == "Z" else ""

        lines = Text()
        rows = [
            ("PID", str(row.pid)),
            ("Name", row.name),
            ("State", row.state + (" (zombie)" if row.state == "Z" else "")),
            ("User", str((extras or {}).get("user") or "-")),
            ("Threads", str((extras or {}).get("threads") if (extras or {}).get("threads") is not None else (row.raw.num_threads or "-"))),
            ("Parent PID", str(row.raw.ppid)),
            ("CPU", f"{row.cpu_percent:.1f}%"),
            ("Memory", mem_pct),
            ("RSS", format_bytes(row.rss_bytes)),
            ("Started", started),
        ]
        for label, value in rows:
            lines.append(f"{label:<9}", style="dim")
            if label == "State" and state_style:
                lines.append(value + "\n", style=state_style)
            else:
                lines.append(value + "\n")
        exe = (extras or {}).get("exe") or row.command or "-"
        lines.append("Exe       ", style="dim")
        lines.append(str(exe) + "\n", style=None)
        target.update(lines)


class AboutPanel(Vertical):
    """Product/runtime/configuration information (About view)."""

    DEFAULT_CSS = """
    AboutPanel { padding: 1 2; }
    AboutPanel .card { border: round $primary; padding: 0 1; width: 1fr; height: auto; }
    AboutPanel .card-title { text-style: bold; color: $accent; margin-bottom: 1; }
    """

    def __init__(self, config_source: str = "", refresh_interval: float = 2.0,
                 history: int = 120) -> None:
        super().__init__()
        self._config_source = config_source
        self._refresh_interval = refresh_interval
        self._history = history

    def compose(self) -> ComposeResult:
        from importlib.metadata import version as pkg_version

        try:
            textual_version = textual.__version__
        except AttributeError:
            textual_version = pkg_version("textual")
        try:
            rich_version = pkg_version("rich")
        except Exception:
            rich_version = "?"
        py = platform.python_version()

        product = Text()
        product.append("SysWatch ", style="bold")
        product.append(f"{__version__}\n", style="bold cyan")
        product.append("Local Linux system diagnostics\n", style="dim")
        product.append("and monitoring.\n", style="dim")
        product.append("\nReads live data from /proc and /sys.\nNothing simulated.\n", style="dim")

        runtime = Text()
        for label, value in (
            ("Python", py),
            ("Textual", textual_version),
            ("Rich", rich_version),
            ("Kernel", platform.release()),
            ("Arch", platform.machine()),
        ):
            runtime.append(f"{label:<8}", style="dim")
            runtime.append(f"{value}\n")

        monitoring = Text()
        for label, value in (
            ("Refresh", f"{self._refresh_interval:g}s"),
            ("History", f"{self._history} samples"),
            ("Config", self._config_source or "built-in defaults"),
        ):
            monitoring.append(f"{label:<8}", style="dim")
            monitoring.append(f"{value}\n")

        keys = Text()
        for key, action in (
            ("1..5", "switch view"),
            ("r", "refresh now"),
            ("/", "filter processes"),
            ("s", "cycle sort"),
            ("Enter", "process details"),
            ("d", "toggle details pane"),
            ("q", "quit"),
        ):
            keys.append(f"{key:<6}", style="bold")
            keys.append(f"{action}\n", style="dim")

        with Horizontal(id="about-row1"):
            yield Vertical(
                Static("SysWatch", classes="card-title"),
                Static(product),
                classes="card",
            )
            yield Vertical(
                Static("Runtime", classes="card-title"),
                Static(runtime),
                classes="card",
            )
            yield Vertical(
                Static("Monitoring", classes="card-title"),
                Static(monitoring),
                classes="card",
            )
        with Horizontal(id="about-row2"):
            yield Vertical(
                Static("Keyboard shortcuts", classes="card-title"),
                Static(keys),
                classes="card",
            )