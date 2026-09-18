"""Human-facing terminal rendering: colors, tables, value formatting."""

from __future__ import annotations

import os
import sys

from syswatch.util import format_bytes

_RESET = "\x1b[0m"
_BOLD = "\x1b[1m"
_RED = "\x1b[31m"
_YELLOW = "\x1b[33m"
_GREEN = "\x1b[32m"
_DIM = "\x1b[2m"

_color_enabled = False


def enable_color(enabled: bool) -> None:
    global _color_enabled
    _color_enabled = enabled


def color_is_enabled() -> bool:
    return _color_enabled


def detect_color_support(no_color_flag: bool) -> bool:
    if no_color_flag or os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def paint(text: str, *codes: str) -> str:
    if not _color_enabled or not codes:
        return text
    return "".join(codes) + text + _RESET


def bold(text: str) -> str:
    return paint(text, _BOLD)


def dim(text: str) -> str:
    return paint(text, _DIM)


def red(text: str) -> str:
    return paint(text, _RED)


def yellow(text: str) -> str:
    return paint(text, _YELLOW)


def green(text: str) -> str:
    return paint(text, _GREEN)


def format_table(
    headers: list[str], rows: list[list[str]], aligns: list[str] | None = None
) -> str:
    """Render a plain-text table; aligns entries are 'l' or 'r'."""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    if aligns is None:
        aligns = ["l"] * len(headers)

    def fmt_row(cells: list[str]) -> str:
        parts: list[str] = []
        for i, cell in enumerate(cells):
            pad = widths[i] - len(cell)
            parts.append(" " * pad + cell if aligns[i] == "r" else cell + " " * pad)
        return "  ".join(parts).rstrip()

    lines = [fmt_row(headers)]
    lines.append("  ".join("-" * w for w in widths))
    lines.extend(fmt_row(row) for row in rows)
    return "\n".join(lines)


def bytes_or_dash(value: int | None) -> str:
    return format_bytes(value) if value is not None else "-"
