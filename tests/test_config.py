from pathlib import Path

import pytest

from syswatch.config import (
    ENV_CONFIG_VAR,
    AppConfig,
    load_config,
    parse_config,
    search_paths,
)
from syswatch.exceptions import ConfigError


class TestDefaults:
    def test_defaults(self):
        cfg = parse_config({})
        assert cfg == AppConfig()
        assert 0 < cfg.cpu_warning < 100

    def test_overrides(self):
        cfg = parse_config(
            {
                "thresholds": {"cpu_warning": 90, "disk_warning": 75.5},
                "monitor": {"interval": 2.5},
                "processes": {"rows": 30},
                "general": {"health_timeout": 5},
            }
        )
        assert cfg.cpu_warning == 90
        assert cfg.disk_warning == 75.5
        assert cfg.monitor_interval == 2.5
        assert cfg.ps_rows == 30
        assert cfg.health_timeout == 5
        assert cfg.ram_warning == 85.0


class TestValidation:
    @pytest.mark.parametrize(
        "payload",
        [
            {"thresholds": {"ram_warning": "high"}},
            {"thresholds": {"ram_warning": None}},
            {"thresholds": {"cpu_warning": True}},
            {"thresholds": {"cpu_warning": -1}},
            {"thresholds": {"cpu_warning": 101}},
            {"thresholds": {"disk_warning": 120}},
            {"thresholds": {"load_per_core_warning": 0}},
            {"monitor": {"interval": 0}},
            {"processes": {"rows": -1}},
            {"processes": {"rows": 1.5}},
            {"thresholds": "not a table"},
        ],
    )
    def test_rejects_invalid(self, payload):
        with pytest.raises(ConfigError):
            parse_config(payload)

    def test_unknown_option_ignored(self, caplog):
        cfg = parse_config({"thresholds": {"nope": 1}, "bogus_section": {}})
        assert cfg == AppConfig()


class TestTuiSection:
    def test_defaults(self):
        cfg = parse_config({})
        assert cfg.tui_refresh_interval == 2.0
        assert cfg.tui_history == 120
        assert cfg.tui_theme == ""

    def test_overrides(self):
        cfg = parse_config(
            {"tui": {"interval": 0.5, "history": 30, "theme": "dracula"}}
        )
        assert cfg.tui_refresh_interval == 0.5
        assert cfg.tui_history == 30
        assert cfg.tui_theme == "dracula"

    @pytest.mark.parametrize(
        "payload",
        [
            {"tui": {"interval": 0.1}},
            {"tui": {"interval": "fast"}},
            {"tui": {"history": 5}},
            {"tui": {"history": 99999}},
            {"tui": {"history": 12.5}},
            {"tui": {"theme": 7}},
        ],
    )
    def test_rejects_invalid(self, payload):
        with pytest.raises(ConfigError):
            parse_config(payload)


class TestLoad:
    def test_missing_explicit_path(self, tmp_path):
        with pytest.raises(ConfigError, match="not found"):
            load_config(tmp_path / "missing.toml")

    def test_explicit_path(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text("[thresholds]\ncpu_warning = 70\n")
        cfg, source = load_config(path)
        assert cfg.cpu_warning == 70
        assert source == path

    def test_invalid_toml(self, tmp_path):
        path = tmp_path / "broken.toml"
        path.write_text("[thresholds\ncpu = 1\n")
        with pytest.raises(ConfigError, match="invalid TOML"):
            load_config(path)

    def test_env_var(self, tmp_path, monkeypatch):
        path = tmp_path / "envcfg.toml"
        path.write_text('[thresholds]\nram_warning = 60\n')
        monkeypatch.setenv(ENV_CONFIG_VAR, str(path))
        cfg, source = load_config()
        assert cfg.ram_warning == 60
        assert source == path

    def test_env_var_missing_file_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ENV_CONFIG_VAR, str(tmp_path / "absent.toml"))
        with pytest.raises(ConfigError, match="not found"):
            load_config()

    def test_no_config_found_returns_defaults(self, tmp_path, monkeypatch):
        monkeypatch.delenv(ENV_CONFIG_VAR, raising=False)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        cfg, source = load_config()
        assert source is None
        assert cfg == AppConfig()

    def test_search_paths_order(self, tmp_path, monkeypatch):
        custom = tmp_path / "custom.toml"
        monkeypatch.setenv(ENV_CONFIG_VAR, str(custom))
        paths = search_paths()
        assert paths[0] == custom
        assert paths[-1] == Path("/etc/syswatch/config.toml")
