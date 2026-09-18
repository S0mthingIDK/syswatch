"""Live process table with filtering, sorting and a detail popup."""

from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Input, Static

from syswatch.collectors.processes import ProcessInfo, read_process_details
from syswatch.tui.widgets import ProcessDetailPanel
from syswatch.util import format_bytes, format_percent

SORT_KEYS: tuple[str, ...] = ("cpu", "mem", "pid", "name")

_COLUMNS = ("PID", "Name", "S", "CPU%", "MEM%", "RSS", "Command")


@dataclass(frozen=True)
class ProcessRow:
    """Everything the table and detail popup need for one process."""

    pid: int
    name: str
    state: str
    cpu_percent: float
    memory_percent: float | None
    rss_bytes: int
    command: str
    raw: ProcessInfo


class ProcessDetailScreen(ModalScreen):
    BINDINGS = [("escape", "dismiss(None)"), ("q", "dismiss(None)")]

    def __init__(self, row: ProcessRow) -> None:
        super().__init__()
        self.row = row

    def compose(self) -> ComposeResult:
        yield Static(self.build_detail_text(), id="proc-detail")

    def build_detail_text(self) -> Text:
        r = self.row.raw
        state_style = "bold red" if r.is_zombie else ""
        lines = Text()
        lines.append(f"  PID      {r.pid}\n")
        lines.append(f"  Name     {r.name}\n")
        lines.append(Text.assemble(("  State    ", ""), (r.state + (" (zombie)" if r.is_zombie else "") + "\n", state_style)))
        lines.append(f"  Parent   {r.ppid}\n")
        lines.append(f"  CPU      {self.row.cpu_percent:.1f}%\n")
        mem_pct = (
            f"{self.row.memory_percent:.1f}%" if self.row.memory_percent is not None else "-"
        )
        lines.append(f"  Memory   {mem_pct} ({format_bytes(r.rss_bytes)} resident)\n")
        lines.append(
            f"  CPU time {r.utime_ticks:.0f}u + {r.stime_ticks:.0f}s ticks\n"
        )
        cmd = r.cmdline or f"[{r.name}]"
        lines.append(f"  Command  {cmd}\n")
        return lines

    DEFAULT_CSS = """
    ProcessDetailScreen { align: center middle; }
    ProcessDetailScreen #proc-detail {
        width: 70; max-width: 90%; border: round $accent;
        background: $surface; padding: 1 2;
    }
    """


class ProcessView(Vertical):
    DEFAULT_CSS = """
    ProcessView { padding: 0; }
    ProcessView Input { margin: 0 1; }
    #proc-main { height: 1fr; }
    #proc-detail-panel { display: none; }
    /* Visibility is driven programmatically: SyswatchApp updates
       styles.display so terminal size and the 'd' key have one source
       of truth (see SyswatchApp._update_detail_visibility). */
    """

    def __init__(self) -> None:
        super().__init__()
        self._rows: list[ProcessRow] = []
        self._filter_text: str = ""
        self._sort_key: str = "cpu"
        self.boot_epoch: int | None = None
        self.clk_tck: int = 100

    def compose(self) -> ComposeResult:
        yield Input(
            placeholder="Type to filter by process name \u00b7 Esc clears \u00b7 s cycles sort",
            id="proc-filter",
        )
        with Horizontal(id="proc-main"):
            yield DataTable(id="proc-table", cursor_type="row", zebra_stripes=True)
            yield ProcessDetailPanel(id="proc-detail-panel")

    def on_mount(self) -> None:
        self.border_title = "Processes"
        table = self.query_one("#proc-table", DataTable)
        table.add_columns(*_COLUMNS)

    # -- data -------------------------------------------------------------

    def set_rows(
        self,
        rows: list[ProcessRow],
        total_processes: int,
    ) -> None:
        selected_pid = self.selected_pid()
        self._rows = rows
        visible = self.visible_rows()
        table = self.query_one("#proc-table", DataTable)
        table.clear()
        for row in visible:
            state_style = "dim" if row.state == "Z" else ""
            table.add_row(
                str(row.pid),
                row.name,
                Text(row.state, style=state_style),
                f"{row.cpu_percent:.1f}",
                format_percent(row.memory_percent),
                format_bytes(row.rss_bytes),
                row.command or f"[{row.name}]",
                key=str(row.pid),
            )
        if selected_pid is not None:
            self.restore_selection(selected_pid)
        self.update_detail_panel()
        shown = len(visible)
        suffix = f"/{total_processes}" if total_processes != shown else ""
        self.border_subtitle = (
            f"{shown}{suffix} shown \u00b7 sorted by {self._sort_key}"
            + (f" \u00b7 filter {self._filter_text!r}" if self._filter_text else "")
        )

    def visible_rows(self) -> list[ProcessRow]:
        needle = self._filter_text.lower()
        matched = [
            r for r in self._rows if not needle or needle in r.name.lower()
        ]
        keyfuncs = {
            "cpu": lambda r: r.cpu_percent,
            "mem": lambda r: r.rss_bytes,
            "pid": lambda r: r.pid,
            "name": lambda r: r.name.lower(),
        }
        reverse = self._sort_key in ("cpu", "mem", "pid")
        return sorted(matched, key=keyfuncs[self._sort_key], reverse=reverse)

    # -- interaction ------------------------------------------------------

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "proc-filter":
            self._filter_text = event.value.strip()
            self.set_rows(self._rows, total_processes=len(self._rows))

    def apply_filter_text(self, text: str) -> None:
        self._filter_text = text
        self.query_one("#proc-filter", Input).value = text

    def cycle_sort(self) -> str:
        idx = SORT_KEYS.index(self._sort_key)
        self._sort_key = SORT_KEYS[(idx + 1) % len(SORT_KEYS)]
        self.set_rows(self._rows, total_processes=len(self._rows))
        return self._sort_key

    @property
    def sort_key(self) -> str:
        return self._sort_key

    def _cursor_row_key(self) -> object | None:
        table = self.query_one("#proc-table", DataTable)
        keys = list(table.rows.keys())
        index = table.cursor_row
        if 0 <= index < len(keys):
            return keys[index]
        return None

    def selected_pid(self) -> int | None:
        key = self._cursor_row_key()
        if key is None or getattr(key, "value", None) is None:
            return None
        try:
            return int(key.value)
        except (TypeError, ValueError):
            return None

    def restore_selection(self, pid: int) -> None:
        table = self.query_one("#proc-table", DataTable)
        for position, row_key in enumerate(table.rows):
            try:
                if int(row_key.value) == pid:
                    table.move_cursor(row=position)
                    return
            except (TypeError, ValueError):
                continue

    def selected_row(self) -> ProcessRow | None:
        pid = self.selected_pid()
        if pid is None:
            return None
        for row in self._rows:
            if row.pid == pid:
                return row
        return None

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """DataTable consumes Enter; RowSelected opens the detail popup."""
        row = self.selected_row()
        if row is not None:
            self.app.push_screen(ProcessDetailScreen(row))

    def on_data_table_row_highlighted(
        self, event: DataTable.RowHighlighted
    ) -> None:
        if event.data_table.id == "proc-table":
            self.update_detail_panel()

    def _detail_enabled(self) -> bool:
        app = self.app
        return app is not None and not app.has_class("narrow", "detail-off")

    def update_detail_panel(self) -> None:
        """Refresh the side panel for the highlighted process (best effort)."""
        panel = self.query_optional("#proc-detail-panel")
        if panel is None or not self._detail_enabled():
            return
        row = self.selected_row()
        extras = None
        if row is not None:
            try:
                extras = read_process_details(row.pid)
            except Exception:
                extras = None
        panel.show_row(
            row, extras, boot_epoch=self.boot_epoch, clk_tck=self.clk_tck
        )

    def query_optional(self, selector):
        from textual.css.query import NoMatches

        try:
            return self.query_one(selector)
        except NoMatches:
            return None
