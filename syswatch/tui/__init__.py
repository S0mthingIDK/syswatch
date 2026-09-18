"""Interactive terminal user interface for syswatch (Textual based).

The TUI is intentionally optional: importing this package requires the
`textual` dependency (install extra `syswatch[tui]`). All non-interactive
CLI functionality works without it.
"""

from syswatch.tui.app import SyswatchApp

__all__ = ["SyswatchApp"]
