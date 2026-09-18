"""The syswatch TUI application (Textual).

Architecture: SyswatchApp owns navigation, timers and background workers.
All data flows through syswatch.tui.state.CollectorHub (UI-independent);
widgets receive plain values and never touch collectors directly.
"""

from __future__ import annotations

import logging
import os
import platform
import time
from collections import deque

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.widgets import Footer, Header, Static, TabbedContent, TabPane

from syswatch import __version__
from syswatch.collectors import network as net_collector
from syswatch.collectors.processes import ProcessInfo, clock_ticks
from syswatch.config import AppConfig
from syswatch.tui.processes import ProcessDetailScreen, ProcessRow, ProcessView
from syswatch.tui.state import CollectorHub
from syswatch.tui.widgets import (
    AboutPanel,
    CPUPanel,
    CpuInfoPanel,
    DiskPanel,
    HealthPanel,
    HealthStrip,
    HostPanel,
    MemoryInfoPanel,
    MemoryPanel,
    NetworkPanel,
)

log = logging.getLogger("syswatch.tui")


class SyswatchApp(App):
    TITLE = "SysWatch"
    ENABLE_COMMAND_PALETTE = False

    BINDINGS = [
        Binding("1", "tab('dashboard')", "Dashboard", show=True),
        Binding("2", "tab('processes')", "Processes", show=True),
        Binding("3", "tab('system')", "System", show=True),
        Binding("4", "tab('health')", "Health", show=True),
        Binding("5", "tab('about')", "About", show=True),
        Binding("r", "refresh_now", "Refresh", show=True),
        Binding("/", "focus_filter", "Filter", show=False),
        Binding("s", "cycle_sort", "Sort", show=False),
        Binding("d", "toggle_detail", "Details", show=False),
        Binding("escape", "clear_filter", "Clear", show=False),
        Binding("q", "quit", "Quit", show=True),
    ]

    DEFAULT_CSS = """
    #views { height: 1fr; }
    TabbedContent Tabs { dock: top; }
    CPUPanel, MemoryPanel, DiskPanel, NetworkPanel {
        border: round $primary;
        width: 1fr;
        height: 1fr;
        margin: 0;
    }
    #dash-top { height: 2fr; }
    #dash-bottom { height: 1fr; }
    #dash-health-strip { height: auto; max-height: 3; }
    Sparkline { height: 1fr; min-height: 3; width: 1fr; }

    /* Processes: table plus a details side pane on medium/wide screens. */
    #proc-table { height: 1fr; width: 1fr; }
    #proc-main { height: 1fr; }

    /* System view: responsive grid over the five panels. */
    #system-grid {
        layout: grid;
        grid-size: 2 3;
        grid-rows: 1fr 1fr 1fr;
        height: 1fr;
    }
    #system-grid > HostPanel, #system-grid > CpuInfoPanel,
    #system-grid > MemoryInfoPanel {
        border: round $primary;
        margin: 0;
    }
    #system-grid NetworkPanel { column-span: 2; }
    SyswatchApp.narrow #system-grid { grid-size: 1 5; grid-rows: 1fr 1fr 1fr 1fr 1fr; }
    SyswatchApp.narrow #system-grid NetworkPanel { column-span: 1; }
    /* Very short terminals: single scrollable column with natural heights
       so every panel stays readable instead of being crushed. */
    SyswatchApp.compact #system-grid {
        layout: vertical;
        grid-size: 1;
        grid-rows: auto;
        overflow-y: auto;
    }
    SyswatchApp.compact #system-grid NetworkPanel { column-span: 1; height: auto; }
    SyswatchApp.compact #system-grid > HostPanel,
    SyswatchApp.compact #system-grid > CpuInfoPanel,
    SyswatchApp.compact #system-grid > MemoryInfoPanel,
    SyswatchApp.compact #system-grid > DiskPanel { height: auto; }
    SyswatchApp.wide #system-grid { grid-size: 6 2; grid-rows: 1fr 1fr; }
    SyswatchApp.wide #system-grid > HostPanel,
    SyswatchApp.wide #system-grid > CpuInfoPanel,
    SyswatchApp.wide #system-grid > MemoryInfoPanel { column-span: 2; }
    SyswatchApp.wide #system-grid > DiskPanel,
    SyswatchApp.wide #system-grid > NetworkPanel { column-span: 3; }
    SyswatchApp.compact .aux { display: none; }

    /* About view cards. */
    AboutPanel { align-horizontal: center; }
    AboutPanel Horizontal { height: auto; max-height: 1fr; }
    AboutPanel .card {
        border: round $primary; padding: 0 1;
        width: 1fr; max-width: 44; height: auto;
    }
    SyswatchApp.narrow AboutPanel .card { max-width: 100%; }

    /* On very wide terminals keep content readable instead of stretching. */
    TabPane { align-horizontal: center; }
    #dash-top, #dash-bottom, #dash-health-strip { max-width: 200; width: 100%; }
    #system-grid { max-width: 220; width: 100%; }
    #about-row1, #about-row2 { max-width: 150; width: 100%; }

    /* Narrow terminals: stack dashboard panels without losing any. */
    SyswatchApp.narrow #dash-top, SyswatchApp.narrow #dash-bottom {
        layout: vertical;
    }
    SyswatchApp.compact Sparkline { min-height: 0; }
"""

    NARROW_WIDTH = 100
    WIDE_WIDTH = 160
    COMPACT_HEIGHT = 26

    def on_resize(self, event) -> None:
        """Responsive behaviour without CSS @media (not supported by Textual)."""
        width = event.size.width
        self.set_class(width < self.NARROW_WIDTH, "narrow")
        self.set_class(self.NARROW_WIDTH <= width < self.WIDE_WIDTH, "medium")
        self.set_class(width >= self.WIDE_WIDTH, "wide")
        self.set_class(event.size.height < self.COMPACT_HEIGHT, "compact")
        self._update_detail_visibility()

    def _update_detail_visibility(self) -> None:
        """Show the process details pane on medium/wide terminals unless
        the user toggled it off; never let it waste space when small."""
        view = self._query_optional(ProcessView)
        if view is None:
            return
        panel = view.query_optional("#proc-detail-panel")
        if panel is None:
            return
        show = (
            self.size.width >= self.NARROW_WIDTH
            and not self.has_class("detail-off")
        )
        panel.styles.display = "block" if show else "none"

    def __init__(
        self,
        config: AppConfig,
        hub: CollectorHub | None = None,
        refresh_interval: float | None = None,
        config_source: str | None = None,
    ) -> None:
        super().__init__()
        self.config = config
        self.config_source = config_source or ""
        self.hub = hub if hub is not None else CollectorHub()
        self.refresh_interval = (
            refresh_interval if refresh_interval is not None
            else config.tui_refresh_interval
        )
        history_len = config.tui_history
        self._cpu_history: deque[float] = deque(maxlen=history_len)
        self._mem_history: deque[float] = deque(maxlen=history_len)
        self._rx_history: deque[float] = deque(maxlen=history_len)
        self._tx_history: deque[float] = deque(maxlen=history_len)
        self._prev_cpu_times: dict | None = None
        self._prev_net_counters: dict | None = None
        self._prev_net_time: float | None = None
        self._prev_proc_ticks: dict[int, float] = {}
        self._proc_clock: float | None = None
        self._memory_info = None
        self._identity: dict[str, object] = {}
        self.dark = False

    # ------------------------------------------------------------------ UI

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent(id="views", initial="dashboard"):
            with TabPane("Dashboard", id="dashboard"):
                yield Horizontal(
                    CPUPanel(id="cpu-panel"),
                    MemoryPanel(id="memory-panel"),
                    id="dash-top",
                )
                yield Horizontal(
                    DiskPanel(id="disk-panel"),
                    NetworkPanel(id="net-panel"),
                    id="dash-bottom",
                )
                yield HealthStrip("", id="dash-health-strip")
            with TabPane("Processes", id="processes"):
                yield ProcessView()
            with TabPane("System", id="system"):
                yield Vertical(
                    HostPanel(id="sys-host"),
                    CpuInfoPanel(id="sys-cpu"),
                    MemoryInfoPanel(id="sys-memory"),
                    DiskPanel(id="sys-disks"),
                    NetworkPanel(id="sys-network"),
                    id="system-grid",
                )
            with TabPane("Health", id="health"):
                yield HealthPanel()
            with TabPane("About", id="about"):
                yield AboutPanel(
                    config_source=self.config_source,
                    refresh_interval=self.refresh_interval,
                    history=self.config.tui_history,
                )
        yield Footer()

    def on_mount(self) -> None:
        if self.config.tui_theme:
            try:
                self.theme = self.config.tui_theme
            except Exception:
                # Textual raises InvalidThemeError for unknown themes.
                log.warning("unknown theme %r; using default", self.config.tui_theme)
        self._identity = self.hub.identity()
        self.set_interval(self.refresh_interval, self._fast_tick)
        self.set_interval(max(3 * self.refresh_interval, 10.0), self._slow_tick)
        # Defer the first refresh until panes have finished mounting;
        # otherwise background workers can query unmounted widgets.
        self.call_after_refresh(self._initial_refresh)
        self.call_after_refresh(self._update_detail_visibility)

    # ------------------------------------------------------------- actions

    def action_tab(self, pane_id: str) -> None:
        tabs = self.query_one("#views", TabbedContent)
        tabs.active = pane_id

    def action_focus_filter(self) -> None:
        self.action_tab("processes")
        view = self.query_one(ProcessView)
        view.query_one("#proc-filter").focus()

    def action_cycle_sort(self) -> None:
        self.action_tab("processes")
        view = self.query_one(ProcessView)
        view.cycle_sort()

    def action_clear_filter(self) -> None:
        focused = self.focused
        if focused is not None and getattr(focused, "id", "") == "proc-filter":
            view = self.query_one(ProcessView)
            view.apply_filter_text("")
        elif isinstance(focused, Static):
            pass
        self.set_focus(None)

    def _initial_refresh(self) -> None:
        self._slow_tick()
        self._fast_tick()
        self._health_worker()

    def action_toggle_detail(self) -> None:
        self.toggle_class("detail-off")
        self._update_detail_visibility()

    def action_refresh_now(self) -> None:
        self._fast_tick()
        self._health_worker()

    # ------------------------------------------------------------- workers

    @work(thread=True, group="procs", exclusive=True)
    def _process_worker(self) -> None:
        """Scan /proc off the UI thread and compute per-process CPU deltas."""
        result = self.hub.processes()
        procs: list[ProcessInfo] = result.value if result.ok else []
        cores = os.cpu_count() or 1
        tps = clock_ticks()
        now = time.monotonic()
        elapsed = (
            now - self._proc_clock if self._proc_clock is not None else None
        )
        prev_ticks = self._prev_proc_ticks
        new_ticks: dict[int, float] = {}
        rows: list[ProcessRow] = []
        total_mem = self._memory_info.total_bytes if self._memory_info else None
        for proc in procs:
            ticks = proc.cpu_ticks
            new_ticks[proc.pid] = ticks
            previous = prev_ticks.get(proc.pid)
            if previous is not None and elapsed and elapsed > 0 and tps > 0:
                delta = max(0.0, ticks - previous)
                percent = min(100.0 * cores, delta / tps / elapsed * 100.0)
            else:
                percent = 0.0
            mem_pct = (
                min(100.0, proc.rss_bytes / total_mem * 100.0)
                if total_mem
                else None
            )
            command = proc.cmdline or ""
            if len(command) > 48:
                command = command[:45] + "..."
            rows.append(
                ProcessRow(
                    pid=proc.pid,
                    name=proc.name,
                    state=proc.state,
                    cpu_percent=percent,
                    memory_percent=mem_pct,
                    rss_bytes=proc.rss_bytes,
                    command=command,
                    raw=proc,
                )
            )
        self._prev_proc_ticks = new_ticks
        self._proc_clock = now
        error = result.error
        self.call_from_thread(self._apply_processes, rows, len(procs), error)

    @work(thread=True, group="health", exclusive=True)
    def _health_worker(self) -> None:
        evaluation, failure = self.hub.health(self.config)
        self.call_from_thread(self._apply_health, evaluation, failure)

    # -------------------------------------------------------------- ticks

    def _fast_tick(self) -> None:
        try:
            self._collect_fast_sample()
            self._update_header()
            self._process_worker()
        except Exception:
            # A single failed collection must never take the TUI down.
            log.exception("fast tick failed")

    def _slow_tick(self) -> None:
        try:
            self._identity = self.hub.identity()
            host = self._query_optional("#sys-host")
            if host is not None:
                uptime = self.hub.uptime()
                boot = self.hub.boot_time()
                procs = self.hub.process_count()
                host.update_host(
                    hostname=self._identity.get("hostname"),
                    os_pretty=self._identity.get("os_pretty_name"),
                    uptime_seconds=uptime.value if uptime.ok else None,
                    boot_epoch=boot.value if boot.ok else None,
                    process_count=procs.value if procs.ok else None,
                )
                proc_view = self._query_optional(ProcessView)
                if proc_view is not None:
                    proc_view.boot_epoch = boot.value if boot.ok else None
                    from syswatch.collectors.processes import clock_ticks

                    proc_view.clk_tck = clock_ticks()
            self._update_header()
        except Exception:
            log.exception("slow tick failed")

    def _collect_fast_sample(self) -> None:
        hub = self.hub
        cfg = self.config

        cur_times = hub.read_cpu_times()
        now = time.monotonic()
        elapsed = (
            now - self._prev_net_time
            if self._prev_net_time is not None
            else None
        )
        percent, per_core = hub.sample_cpu(
            self._prev_cpu_times,
            cur_times.value if cur_times.ok else {},
            elapsed,
        )
        if cur_times.ok:
            self._prev_cpu_times = cur_times.value
        model_r, cores_r, load_r = hub.cpu_facts()
        model = model_r.value if model_r.ok else None
        cores = cores_r.value if cores_r.ok else None
        load = load_r.value if load_r.ok else None

        memory_result = hub.memory()
        memory = memory_result.value if memory_result.ok else None
        self._memory_info = memory
        mem_pct = memory.used_percent if memory else None
        # Each history is appended independently so a failing collector
        # (e.g. meminfo unreadable) does not freeze the CPU sparkline.
        if percent is not None:
            self._cpu_history.append(percent)
        if mem_pct is not None:
            self._mem_history.append(mem_pct)

        disks_result = hub.disks()
        disks = disks_result.value if disks_result.ok else []

        counters_result = hub.net_counters()
        counters = counters_result.value if counters_result.ok else {}
        rates_map: dict[str, tuple[float, float]] = {}
        rx_total = tx_total = 0.0
        if (
            self._prev_net_counters is not None
            and elapsed
            and elapsed > 0
        ):
            rate_objs = net_collector.interface_rates(
                self._prev_net_counters, counters, elapsed
            )
            for name, obj in rate_objs.items():
                rates_map[name] = (
                    obj.rx_bytes_per_second or 0.0,
                    obj.tx_bytes_per_second or 0.0,
                )
        exclude = ("lo",)
        rx_total = sum(v[0] for k, v in rates_map.items() if k not in exclude)
        tx_total = sum(v[1] for k, v in rates_map.items() if k not in exclude)
        self._rx_history.append(round(rx_total, 1))
        self._tx_history.append(round(tx_total, 1))

        interfaces_result = hub.net_interfaces()
        interfaces = interfaces_result.value if interfaces_result.ok else None

        # Dashboard panels (ids fixed at compose time).
        dash_cpu = self._query_optional("#cpu-panel")
        if dash_cpu is not None:
            dash_cpu.update_sample(
                percent, per_core, load, model, cores, list(self._cpu_history),
                warning_threshold=cfg.cpu_warning,
                critical_offset=cfg.critical_offset,
            )
        dash_mem = self._query_optional("#memory-panel")
        if dash_mem is not None:
            dash_mem.update_memory(
                memory, list(self._mem_history), error=memory_result.error,
                warning_threshold=cfg.ram_warning,
                critical_offset=cfg.critical_offset,
            )
        dash_disks = self._query_optional("#disk-panel")
        if dash_disks is not None:
            dash_disks.update_disks(
                disks, cfg.disk_warning, error=disks_result.error,
                critical_offset=cfg.critical_offset,
            )
        dash_net = self._query_optional("#net-panel")
        if dash_net is not None:
            dash_net.update_network(
                interfaces,
                rates_map,
                list(self._rx_history),
                list(self._tx_history),
                error=interfaces_result.error,
            )

        # System view mirrors the same data without duplicating collection.
        sys_cpu = self._query_optional("#sys-cpu")
        if sys_cpu is not None:
            frequency = hub.cpu_frequency()
            sys_cpu.update_cpu(
                percent, per_core, load, model, cores,
                frequency.value if frequency.ok else None,
                warning_threshold=cfg.cpu_warning,
                critical_offset=cfg.critical_offset,
            )
        sys_mem = self._query_optional("#sys-memory")
        if sys_mem is not None:
            sys_mem.update_memory(
                memory,
                warning_threshold=cfg.ram_warning,
                critical_offset=cfg.critical_offset,
            )
        sys_disks = self._query_optional("#sys-disks")
        if sys_disks is not None:
            sys_disks.update_disks(
                disks, cfg.disk_warning, error=disks_result.error,
                critical_offset=cfg.critical_offset,
            )
        sys_net = self._query_optional("#sys-network")
        if sys_net is not None:
            sys_net.update_network(
                interfaces,
                rates_map,
                list(self._rx_history),
                list(self._tx_history),
                error=interfaces_result.error,
            )

        proc_view = self._query_optional(ProcessView)
        if proc_view is not None:
            boot_r = hub.boot_time()
            proc_view.boot_epoch = boot_r.value if boot_r.ok else None

        self._prev_net_counters = counters
        self._prev_net_time = now

    def _query_optional(self, selector):
        try:
            return self.query_one(selector)
        except NoMatches:
            return None

    def _apply_processes(
        self, rows: list[ProcessRow], total: int, error: str | None
    ) -> None:
        view = self._query_optional(ProcessView)
        if view is None:
            return
        if error and not rows:
            view.border_subtitle = error
            return
        view.set_rows(rows, total)

    def _apply_health(self, evaluation, failure: str | None) -> None:
        payload = evaluation if failure is None else None
        panel = self._query_optional(HealthPanel)
        if panel is not None:
            panel.update_evaluation(payload, failure)
        strip = self._query_optional(HealthStrip)
        if strip is not None:
            strip.update_health(
                payload.findings if payload is not None else None,
                failure,
            )

    def _update_header(self) -> None:
        hostname = self._identity.get("hostname") or platform.node() or "?"
        os_name = self._identity.get("os_pretty_name") or platform.system()
        clock = time.strftime("%H:%M:%S")
        errors = self._identity.get("identity_errors") or []
        suffix = "" if not errors else f" \u00b7 {len(errors)} source issue(s)"
        self.sub_title = f"{hostname} \u00b7 {os_name} \u00b7 {clock}{suffix}"