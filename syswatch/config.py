"""Configuration loading (TOML) with validation and sensible defaults."""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

from syswatch.exceptions import ConfigError

log = logging.getLogger("syswatch.config")

ENV_CONFIG_VAR = "SYSWATCH_CONFIG"

_SYSTEM_CONFIG_DIR = Path("/etc/syswatch")
_USER_CONFIG_PATH = Path.home() / ".config" / "syswatch" / "config.toml"


@dataclass(frozen=True)
class AppConfig:
    cpu_warning: float = 85.0
    ram_warning: float = 85.0
    swap_warning: float = 85.0
    disk_warning: float = 85.0
    critical_offset: float = 10.0
    load_per_core_warning: float = 3.0
    monitor_interval: float = 1.0
    ps_rows: int = 15
    sample_delay: float = 0.5
    health_timeout: float = 3.0
    tui_refresh_interval: float = 2.0
    tui_history: int = 120
    tui_theme: str = ""


def default_config() -> AppConfig:
    return AppConfig()


def search_paths() -> list[Path]:
    """Configuration files considered, in precedence order."""
    paths: list[Path] = []
    env_path = os.environ.get(ENV_CONFIG_VAR)
    if env_path:
        paths.append(Path(env_path))
    paths.append(_USER_CONFIG_PATH)
    paths.append(_SYSTEM_CONFIG_DIR / "config.toml")
    return paths


_FIELD_KINDS: dict[str, type] = {
    "cpu_warning": float,
    "ram_warning": float,
    "swap_warning": float,
    "disk_warning": float,
    "critical_offset": float,
    "load_per_core_warning": float,
    "monitor_interval": float,
    "sample_delay": float,
    "ps_rows": int,
    "health_timeout": float,
    "tui_refresh_interval": float,
    "tui_history": int,
    "tui_theme": str,
}

_STRING_FIELDS = frozenset({"tui_theme"})

# Friendly option keys accepted within each section.
_SECTION_ALIASES: dict[tuple[str, str], str] = {
    ("thresholds", "cpu"): "cpu_warning",
    ("thresholds", "ram"): "ram_warning",
    ("thresholds", "swap"): "swap_warning",
    ("thresholds", "disk"): "disk_warning",
    ("monitor", "interval"): "monitor_interval",
    ("processes", "rows"): "ps_rows",
    ("general", "timeout"): "health_timeout",
    ("tui", "interval"): "tui_refresh_interval",
    ("tui", "history"): "tui_history",
    ("tui", "theme"): "tui_theme",
}

_SECTION_BY_FIELD: dict[str, str] = {
    name: ("thresholds" if name.endswith("_warning") or name == "critical_offset"
           or name == "load_per_core_warning"
           else "monitor" if name in ("monitor_interval", "sample_delay")
           else "processes" if name == "ps_rows"
           else "tui" if name.startswith("tui_")
           else "general")
    for name in _FIELD_KINDS
}

_RANGE_RULES: dict[str, tuple[float | None, float | None]] = {
    "cpu_warning": (0.0, 100.0),
    "ram_warning": (0.0, 100.0),
    "swap_warning": (0.0, 100.0),
    "disk_warning": (0.0, 100.0),
    "critical_offset": (0.0, 100.0),
    "load_per_core_warning": (0.1, None),
    "monitor_interval": (0.1, None),
    "ps_rows": (0, None),
    "sample_delay": (0.01, None),
    "health_timeout": (0.1, None),
    "tui_refresh_interval": (0.25, None),
    "tui_history": (10, 3600),
}


def _coerce(section: str, key: str, raw: object, kind: type) -> int | float | str:
    if kind is str:
        if not isinstance(raw, str):
            raise ConfigError(f"option [{section}] {key} must be a string, got {raw!r}")
        return raw
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ConfigError(f"option [{section}] {key} must be a number, got {raw!r}")
    value = float(raw)
    if kind is int:
        if value != int(value):
            raise ConfigError(f"option [{section}] {key} must be an integer, got {raw!r}")
        return int(value)
    return value


def _apply_section(
    values: dict[str, object],
    section_data: dict[str, object],
    section_name: str,
) -> None:
    for key, raw in section_data.items():
        field_name = _SECTION_ALIASES.get((section_name, key)) or next(
            (
                name
                for name, sec in _SECTION_BY_FIELD.items()
                if sec == section_name and name == key
            ),
            None,
        )
        if field_name is None:
            log.warning("ignoring unknown config option [%s] %s", section_name, key)
            continue
        values[field_name] = _coerce(section_name, key, raw, _FIELD_KINDS[field_name])


def parse_config(data: dict[str, object]) -> AppConfig:
    """Build an AppConfig from parsed TOML data, validating every override."""
    values: dict[str, object] = {}
    for section in ("thresholds", "monitor", "processes", "general", "tui"):
        section_data = data.get(section)
        if section_data is None:
            continue
        if not isinstance(section_data, dict):
            raise ConfigError(f"config section [{section}] must be a table")
        _apply_section(values, section_data, section)
    merged: dict[str, object] = {
        f.name: getattr(default_config(), f.name) for f in fields(AppConfig)
    }
    for name, value in values.items():
        rules = _RANGE_RULES.get(name)
        if rules is not None:
            low, high = rules
            if low is not None and value < low:
                raise ConfigError(f"option {name} must be >= {low}, got {value}")
            if high is not None and value > high:
                raise ConfigError(f"option {name} must be <= {high}, got {value}")
        merged[name] = value
    return AppConfig(**merged)


def _read_toml(path: Path) -> dict[str, object]:
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration file not found: {path}") from exc
    except PermissionError as exc:
        raise ConfigError(f"permission denied reading configuration {path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read configuration file {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc


def load_config(explicit_path: Path | None = None) -> tuple[AppConfig, Path | None]:
    """Load configuration; returns effective settings and their source path.

    A missing file at an explicitly requested path is an error. When no path
    is given, well-known locations are probed and defaults apply silently.
    """
    if explicit_path is not None:
        return parse_config(_read_toml(explicit_path)), explicit_path

    env_value = os.environ.get(ENV_CONFIG_VAR)
    for path in search_paths():
        try:
            payload = _read_toml(path)
        except ConfigError as exc:
            cause = exc.__cause__
            if isinstance(cause, FileNotFoundError):
                if env_value == str(path):
                    # An explicit environment request must fail loudly.
                    raise
                continue
            log.warning("%s", exc)
            continue
        return parse_config(payload), path
    return default_config(), None
