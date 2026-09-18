"""Tests for the Textual TUI: navigation, live updates, failure handling.

Uses App.run_test() (headless pilot) so no real terminal is required.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager

import pytest

pytest.importorskip("textual")

import io

from rich.console import Console
from rich.text import Text

from syswatch.config import AppConfig
from syswatch.health import Finding, Severity
from syswatch.tui.app import SyswatchApp
from syswatch.tui.processes import ProcessRow, ProcessView
from syswatch.tui.state import CollectorHub, Result
from syswatch.tui.widgets import HealthPanel, severity_class


def make_app(**config_overrides) -> SyswatchApp:
    params = {
        "tui_refresh_interval": 0.05,
        "tui_history": 30,
        "sample_delay": 0.01,
        "health_timeout": 0.5,
    }
    params.update(config_overrides)
    return SyswatchApp(AppConfig(**params))


def widget_text(widget) -> str:
    """Visible text of a Static (Textual wraps rich tables in Content)."""
    visual = getattr(widget, "visual", None)
    if visual is not None and hasattr(visual, "plain"):
        return str(visual.plain)
    renderable = None
    for attr in ("renderable", "_renderable"):
        candidate = getattr(visual, attr, None)
        if candidate is not None:
            renderable = candidate
            break
    if renderable is not None and not isinstance(renderable, str):
        console = Console(file=io.StringIO(), width=220, force_terminal=False)
        console.print(renderable)
        return console.file.getvalue()
    return str(widget.render())


def pilot_test(coro_fn):
    """Run an async pilot scenario under asyncio.run()."""
    return asyncio.run(coro_fn())


@asynccontextmanager
async def boot(size=(80, 24), quiet=False):
    """quiet=True disables periodic ticks so tests can inject data."""
    overrides = {"tui_refresh_interval": 600.0} if quiet else {}
    app = make_app(**overrides)
    async with app.run_test(size=size) as pilot:
        # let the initial (call_after_refresh) workers finish first
        await pilot.pause(0.5)
        yield app, pilot


class TestAppStarts:
    @pytest.mark.parametrize("size", [(80, 24), (100, 30), (120, 40)])
    def test_starts_at_sizes(self, size):
        async def scenario():
            async with boot(size=size) as (app, pilot):
                assert app.is_running
                assert app.query("TabbedContent")
                assert app.query("CPUPanel")
                assert app.query("MemoryPanel")
                assert app.query("DiskPanel")
                assert app.query("NetworkPanel")
                assert app.query("HealthStrip")
                assert app.query("ProcessView")
                await pilot.pause()

        pilot_test(scenario)

    def test_dashboard_populates_from_collectors(self):
        async def scenario():
            async with boot() as (app, pilot):
                await pilot.pause(0.3)
                # at least one tick appended history on this live system
                assert len(app._cpu_history) >= 1

        pilot_test(scenario)


class TestNavigation:
    def test_number_keys_switch_tabs(self):
        async def scenario():
            async with boot() as (app, pilot):
                from textual.widgets import TabbedContent

                tabs = app.query_one("#views", TabbedContent)
                for key, pane in [
                    ("2", "processes"),
                    ("3", "system"),
                    ("4", "health"),
                    ("5", "about"),
                    ("1", "dashboard"),
                ]:
                    await pilot.press(key)
                    await pilot.pause()
                    assert tabs.active == pane, f"key {key}"

        pilot_test(scenario)

    def test_slash_focuses_process_filter(self):
        async def scenario():
            async with boot() as (app, pilot):
                await pilot.press("/")
                await pilot.pause()
                from textual.widgets import Input

                focused = app.focused
                assert isinstance(focused, Input) and focused.id == "proc-filter"

        pilot_test(scenario)


class TestProcessTable:
    def _rows(self):
        from syswatch.collectors.processes import ProcessInfo

        def proc(pid, name, rss, ticks):
            return ProcessInfo(
                pid=pid, name=name, state="S", ppid=1,
                utime_ticks=ticks, stime_ticks=0,
                rss_bytes=rss, cmdline=f"/usr/bin/{name}",
            )

        procs = [proc(10, "alpha", 1024**2, 100), proc(20, "beta", 50 * 1024**2, 900)]
        rows = []
        total = 4 * 1024**3
        for p in procs:
            rows.append(
                ProcessRow(
                    pid=p.pid, name=p.name, state=p.state,
                    cpu_percent=p.cpu_ticks / 10.0,
                    memory_percent=p.rss_bytes / total * 100,
                    rss_bytes=p.rss_bytes,
                    command=p.cmdline, raw=p,
                )
            )
        return rows

    def test_filter_reduces_rows(self):
        async def scenario():
            async with boot(quiet=True) as (app, pilot):
                from textual.widgets import DataTable

                view = app.query_one(ProcessView)
                view.set_rows(self._rows(), total_processes=2)
                await pilot.pause()
                table = app.query_one("#proc-table", DataTable)
                assert table.row_count == 2

                view.apply_filter_text("alp")
                await pilot.pause()
                assert table.row_count == 1
                assert str(table.get_row_at(0)[0]) == "10"

                view.apply_filter_text("zzz")
                await pilot.pause()
                assert table.row_count == 0

        pilot_test(scenario)

    def test_sort_cycles(self):
        async def scenario():
            async with boot(quiet=True) as (app, pilot):
                view = app.query_one(ProcessView)
                view.set_rows(self._rows(), total_processes=2)
                seen = [view.sort_key]
                for _ in range(3):
                    await pilot.press("s")
                    await pilot.pause()
                    seen.append(view.sort_key)
                assert seen == ["cpu", "mem", "pid", "name"]

        pilot_test(scenario)

    def test_enter_opens_detail_screen(self):
        async def scenario():
            async with boot(quiet=True) as (app, pilot):
                from textual.widgets import DataTable

                view = app.query_one(ProcessView)
                view.set_rows(self._rows(), total_processes=2)
                await pilot.pause()
                table = app.query_one("#proc-table", DataTable)
                assert table.row_count == 2
                table.focus()
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                from syswatch.tui.processes import ProcessDetailScreen

                assert isinstance(app.screen, ProcessDetailScreen)
                assert app.screen.row.pid in (10, 20)
                await pilot.press("escape")
                await pilot.pause()
                assert not isinstance(app.screen, ProcessDetailScreen)

        pilot_test(scenario)

    def test_disappearing_pid_is_not_fatal(self):
        async def scenario():
            async with boot(quiet=True) as (app, pilot):
                from textual.widgets import DataTable

                view = app.query_one(ProcessView)
                view.set_rows(self._rows(), total_processes=2)
                await pilot.pause()
                # next refresh contains only a subset: vanished pid handled
                remaining = self._rows()[:1]
                view.set_rows(remaining, total_processes=1)
                await pilot.pause()
                assert app.query_one("#proc-table", DataTable).row_count == 1

        pilot_test(scenario)


class TestFailureHandling:
    def test_memory_collector_failure_shows_unavailable(self, monkeypatch):
        async def scenario():
            app = make_app()
            broken = CollectorHub()
            monkeypatch.setattr(broken, "memory", lambda: Result(error="memory: boom"))
            app.hub = broken
            async with app.run_test(size=(80, 24)) as pilot:
                mem_big = app.query_one("#mem-big")
                deadline = time.monotonic() + 3.0
                text = str(mem_big.render())
                while "unavailable" not in text.lower() and time.monotonic() < deadline:
                    await pilot.pause(0.1)
                    text = str(mem_big.render())
                assert "unavailable" in text.lower()
                assert app.is_running  # app survives

        pilot_test(scenario)

    def test_disk_collector_failure_keeps_app_alive(self, monkeypatch):
        async def scenario():
            app = make_app()
            broken = CollectorHub()
            def explode():
                raise RuntimeError("mounts vanished")
            monkeypatch.setattr(broken, "disks", explode)
            app.hub = broken
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause(0.2)
                assert app.is_running

        pilot_test(scenario)

    def test_health_failure_marks_unavailable(self, monkeypatch):
        async def scenario():
            app = make_app()
            broken = CollectorHub()
            monkeypatch.setattr(
                broken, "health",
                lambda cfg: (None, "health checks failed: systemd unreachable"),
            )
            app.hub = broken
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.press("r")  # force health run
                await pilot.pause(0.3)
                strip = app.query_one("#dash-health-strip")
                rendered = str(strip.render())
                assert (
                    ("pending" in rendered)
                    or ("UNAVAIL" in rendered.upper())
                    or ("failed" in rendered.lower())
                )
                panel_summary = str(app.query_one(HealthPanel).query_one("#health-summary").render())
                assert "UNAVAILABLE" in panel_summary

        pilot_test(scenario)


class TestHealthRendering:
    def _evaluation(self):
        from syswatch.health import CheckStatus, HealthEvaluation

        return HealthEvaluation(
            findings=[],
            errors=[],
            checks=[
                CheckStatus(name="CPU usage", state="ok", value="2.1%",
                            threshold=">=85%", note=""),
                CheckStatus(name="Memory usage", state="ok", value="69.8%",
                            threshold=">=85%", note="2.6 GiB / 3.8 GiB"),
                CheckStatus(name="Systemd services", state="unavail",
                            value="-", threshold="0 failed",
                            note="systemd not detected"),
            ],
        )

    def test_severity_classes_use_configured_thresholds(self):
        cfg = AppConfig(cpu_warning=50.0, critical_offset=10.0)
        assert severity_class(45.0, cfg.cpu_warning) == "m-ok"
        assert severity_class(55.0, cfg.cpu_warning) == "m-warn"
        assert severity_class(61.0, cfg.cpu_warning) == "m-crit"

    def test_panel_renders_all_statuses(self):
        async def scenario():
            app = make_app(tui_refresh_interval=600.0)
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause(0.4)
                # stop the background health worker so injected data sticks
                app.workers.cancel_group(app, "health")
                panel = app.query_one(HealthPanel)
                summary = lambda: str(panel.query_one("#health-summary").render())

                panel.update_evaluation(self._evaluation())
                await pilot.pause()
                body = widget_text(panel.query_one("#health-body"))
                # every check stays visible with its status, even when passing
                for name in ("CPU usage", "Memory usage", "Systemd services"):
                    assert name in body
                assert "NORMAL" in body and "UNAVAILABLE" in body
                assert "HEALTHY" in summary()

                warning_eval = self._evaluation()
                from syswatch.health import CheckStatus

                warning_eval.checks.append(
                    CheckStatus(name="Filesystem /", state="warn",
                                value="91.0%", threshold=">=85%", note="")
                )
                panel.update_evaluation(warning_eval)
                await pilot.pause()
                assert "ATTENTION" in summary()

                crit_eval = self._evaluation()
                crit_eval.checks.append(
                    CheckStatus(name="Load average", state="crit",
                                value="42.00", threshold="<=12", note="")
                )
                panel.update_evaluation(crit_eval)
                await pilot.pause()
                assert "CRITICAL" in summary()

                panel.update_evaluation(None, failure="systemctl missing")
                await pilot.pause()
                assert "UNAVAILABLE" in summary()

        pilot_test(scenario)

    def test_strip_reflects_findings(self):
        async def scenario():
            app = make_app(tui_refresh_interval=600.0)
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause(0.4)
                strip = app.query_one("#dash-health-strip")
                finding = Finding(check="x", severity=Severity.CRITICAL,
                                  message="m")
                strip.update_health([finding])
                await pilot.pause()
                text = str(strip.render())
                assert "critical" in text and "[4]" in text

        pilot_test(scenario)


class TestLiveUpdates:
    def test_history_grows_over_ticks(self):
        async def scenario():
            async with boot() as (app, pilot):
                before = len(app._cpu_history)
                await pilot.pause(0.25)
                after = len(app._cpu_history)
                assert after > before

        pilot_test(scenario)

    def test_header_sub_title_updates(self):
        async def scenario():
            async with boot() as (app, pilot):
                await pilot.pause(0.15)
                assert app.sub_title
                hostname = app._identity.get("hostname")
                if hostname:
                    assert str(hostname) in app.sub_title

        pilot_test(scenario)


class TestThemeConfig:
    def test_valid_theme_applied(self):
        async def scenario():
            app = make_app(tui_theme="nord")
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                assert app.theme == "nord"

        pilot_test(scenario)

    def test_invalid_theme_falls_back(self):
        async def scenario():
            app = make_app(tui_theme="not-a-real-theme")
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                assert app.theme != "not-a-real-theme"

        pilot_test(scenario)


class TestSystemView:
    def test_panels_populate(self):
        async def scenario():
            async with boot(size=(120, 40)) as app_pilot:
                (app, pilot) = app_pilot
                from syswatch.tui.widgets import CpuInfoPanel, HostPanel

                await pilot.pause(0.6)
                host_text = widget_text(app.query_one("#sys-host").query_one("#host-body"))
                assert "Hostname" in host_text
                assert "Kernel" in host_text and "Boot time" in host_text

                cpu = widget_text(
                    app.query_one("#sys-cpu").query_one("#cpuinfo-cores")
                )
                assert "cpu0" in cpu
                load_line = str(
                    app.query_one("#sys-cpu").query_one("#cpuinfo-load").render()
                )
                assert "load" in load_line

        pilot_test(scenario)

    def test_grid_classes_by_width(self):
        async def scenario():
            from textual.widgets import TabbedContent

            for size, expected in [((80, 24), "narrow"), ((120, 40), "medium"),
                                   ((180, 50), "wide")]:
                app = make_app()
                async with app.run_test(size=size) as pilot:
                    await pilot.pause(0.1)
                    assert app.has_class(expected), f"{size} should be {expected}"
                    tabs = app.query_one("#views", TabbedContent)
                    tabs.active = "system"
                    await pilot.pause(0.2)
                    # nothing disappears: all five system panels exist in DOM
                    for cls in ("HostPanel", "CpuInfoPanel", "MemoryInfoPanel",
                                "DiskPanel", "NetworkPanel"):
                        assert app.query(cls)

        pilot_test(scenario)

    def test_swap_and_memory_rendered(self):
        async def scenario():
            async with boot(size=(120, 40)) as pair:
                (app, pilot) = pair
                await pilot.pause(0.5)
                ram = widget_text(app.query_one("#sys-memory").query_one("#ram-line"))
                assert "RAM" in ram and "available" in ram
                swap = widget_text(
                    app.query_one("#sys-memory").query_one("#swap-line")
                )
                assert "SWAP" in swap  # present even when 'none configured'

        pilot_test(scenario)


class TestHealthScreenDensity:
    def test_healthy_shows_every_check(self):
        async def scenario():
            async with boot(size=(120, 40)) as pair:
                (app, pilot) = pair
                await pilot.press("4")
                await pilot.pause(1.2)
                body = widget_text(app.query_one("#health-body"))
                summary = str(app.query_one("#health-summary").render())
                assert "OVERALL HEALTH" in summary
                for check in ("CPU usage", "Memory usage", "Swap usage",
                              "Filesystem", "Load average", "Zombie processes",
                              "Systemd services", "Network gateway"):
                    assert check in body, f"missing check row: {check}"
                assert "NORMAL" in body or "UNAVAILABLE" in body
                assert "%" in body  # real measured values rendered

        pilot_test(scenario)


class TestProcessDetailPane:
    def _rows(self):
        from syswatch.collectors.processes import ProcessInfo

        raw = ProcessInfo(pid=42, name="worker", state="S", ppid=1,
                          utime_ticks=100, stime_ticks=10,
                          rss_bytes=32 * 1024 * 1024,
                          cmdline="/usr/bin/worker --serve",
                          num_threads=4, starttime_ticks=1234.0)
        return [ProcessRow(pid=42, name="worker", state="S", cpu_percent=3.3,
                           memory_percent=1.2, rss_bytes=raw.rss_bytes,
                           command="/usr/bin/worker --serve", raw=raw)]

    def test_detail_updates_on_cursor_move(self):
        async def scenario():
            async with boot(size=(120, 40), quiet=True) as pair:
                (app, pilot) = pair
                view = app.query_one(ProcessView)
                view.set_rows(self._rows(), total_processes=1)
                table = view.query_one("#proc-table")
                table.focus()
                await pilot.pause()
                panel = view.query_one("#proc-detail-panel")
                text = widget_text(panel.query_one("#proc-detail-text"))
                assert "worker" in text and "42" in text
                assert "Threads" in text

        pilot_test(scenario)

    def test_detail_hidden_on_narrow(self):
        async def scenario():
            async with boot(size=(80, 24), quiet=True) as pair:
                (app, pilot) = pair
                assert app.has_class("narrow")
                from textual.css.query import NoMatches

                try:
                    display = app.query_one("#proc-detail-panel").styles.display
                except NoMatches:
                    display = None
                # either not mounted-visible or explicitly none
                if display is not None:
                    assert str(display) in ("none", "hidden")

        pilot_test(scenario)

    def test_toggle_key_hides_detail(self):
        async def scenario():
            async with boot(size=(120, 40), quiet=True) as pair:
                (app, pilot) = pair
                assert not app.has_class("detail-off")
                await pilot.press("d")
                assert app.has_class("detail-off")
                await pilot.press("d")
                assert not app.has_class("detail-off")

        pilot_test(scenario)


class TestAboutView:
    def test_about_cards_render(self):
        async def scenario():
            async with boot(size=(100, 30)) as pair:
                (app, pilot) = pair
                await pilot.press("5")
                await pilot.pause(0.3)
                from textual.widgets import Static

                texts = " ".join(
                    widget_text(s) for s in app.query("AboutPanel Static")
                )
                import syswatch

                assert f"SysWatch {syswatch.__version__}" in texts
                assert "Python" in texts
                assert "Textual" in texts
                assert "Keyboard shortcuts" in texts
                assert "Refresh" in texts

        pilot_test(scenario)