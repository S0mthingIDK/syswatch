import json
import os
import platform
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def run_cli(capsys):
    def _run(*argv):
        from syswatch.cli import main

        rc = main(list(argv))
        captured = capsys.readouterr()
        return rc, captured.out, captured.err

    return _run


class TestInfo:
    def test_text_output(self, run_cli):
        rc, out, err = run_cli("info")
        assert rc == 0
        assert "Hostname:" in out
        assert "Kernel:" in out
        assert "Memory" in out
        assert "Network" in out

    def test_json_output(self, run_cli):
        rc, out, err = run_cli("info", "--json")
        assert rc == 0
        payload = json.loads(out)
        assert payload["hostname"]
        assert isinstance(payload["disks"], list)
        assert isinstance(payload["network_interfaces"], list)

    def test_json_has_no_log_noise(self, run_cli):
        rc, out, err = run_cli("-v", "info", "--json")
        assert rc == 0
        json.loads(out)


class TestMonitor:
    def test_json_ticks(self, run_cli):
        rc, out, err = run_cli("monitor", "-i", "0.05", "-n", "2", "--json")
        assert rc == 0
        lines = [line for line in out.splitlines() if line.strip()]
        assert len(lines) == 2
        tick = json.loads(lines[0])
        assert "cpu_percent" in tick
        assert "process_count" in tick
        assert tick.get("per_core_cpu_percent") is not None
        assert "cpu" not in tick["per_core_cpu_percent"]

    def test_interval_floor(self, run_cli):
        rc, out, err = run_cli("monitor", "-i", "0.01", "-n", "1", "--json")
        assert rc == 0


class TestPs:
    def test_table(self, run_cli):
        rc, out, err = run_cli("ps", "-s", "cpu", "-n", "5")
        assert rc == 0
        assert "PID" in out
        assert "RSS" in out

    def test_filter(self, run_cli):
        rc, out, err = run_cli("ps", "-f", "python", "-n", "50")
        assert rc == 0
        table_rows = [l for l in out.splitlines() if l and not l.startswith((" ", "-", "=")) ]
        assert any("python" in row.lower() or "matching" in row for row in out.splitlines())

    def test_filter_no_match(self, run_cli):
        rc, out, err = run_cli("ps", "-f", "zzz-no-such-process-zzz", "-n", "10")
        assert rc == 0
        assert "0 processes matching" in out

    def test_json(self, run_cli):
        rc, out, err = run_cli("ps", "-n", "3", "--json")
        assert rc == 0
        rows = json.loads(out)
        assert len(rows) == 3
        assert {"pid", "name", "cpu_percent"} <= set(rows[0].keys())

    def test_sort_by_mem(self, run_cli):
        rc, out, err = run_cli("ps", "-s", "mem", "-n", "4")
        assert rc == 0


class TestHealth:
    def test_healthy_or_issues(self, run_cli):
        rc, out, err = run_cli("health")
        assert rc in (0, 1, 2)
        assert ("HEALTHY" in out) or ("ATTENTION" in out) or ("UNHEALTHY" in out)

    def test_json_mode(self, run_cli):
        rc, out, err = run_cli("health", "--json")
        payload = json.loads(out)
        assert "summary" in payload
        assert isinstance(payload["findings"], list)
        assert rc in (0, 1, 2)


class TestReport:
    def test_stdout_compact(self, run_cli, tmp_path):
        rc, out, err = run_cli("report")
        assert rc == 0
        data = json.loads(out)
        assert data["tool"] == "syswatch"

    def test_file_output(self, run_cli, tmp_path):
        target = tmp_path / "report.json"
        rc, out, err = run_cli("report", "-o", str(target))
        assert rc == 0
        assert out == ""
        data = json.loads(target.read_text())
        assert data["host"]["hostname"]

    def test_unwritable_output(self, run_cli, tmp_path):
        bad_path = tmp_path / "no-such-dir" / "r.json"
        rc, out, err = run_cli("report", "-o", str(bad_path))
        assert rc == 1
        assert "cannot write" in err


class TestConfigCommand:
    def test_shows_settings(self, run_cli):
        rc, out, err = run_cli("config")
        assert rc == 0
        assert "cpu_warning" in out
        assert "Search order" in out


class TestNegativeArguments:
    @pytest.mark.parametrize("argv", [["ps", "-n", "-3"], ["monitor", "-n", "-1"]])
    def test_negative_counts_rejected(self, argv):
        from syswatch.cli import main

        with pytest.raises(SystemExit) as excinfo:
            main(argv)
        assert excinfo.value.code == 2

    def test_zero_rows_means_all(self, run_cli):
        rc, out, err = run_cli("ps", "-n", "0")
        assert rc == 0


class TestErrors:
    def test_bad_config_missing(self, run_cli, tmp_path):
        rc, out, err = run_cli("--config", str(tmp_path / "none.toml"), "info")
        assert rc == 1
        assert "not found" in err

    def test_bad_command(self, run_cli):
        with pytest.raises(SystemExit) as excinfo:
            run_cli("bogus-command")
        assert excinfo.value.code == 2

    def test_non_linux_platform(self, monkeypatch, capsys):
        monkeypatch.setattr(platform, "system", lambda: "Haiku")
        from syswatch.cli import main

        rc = main(["info"])
        captured = capsys.readouterr()
        assert rc == 1
        assert "requires Linux" in captured.err

    def test_version(self, capsys):
        from syswatch.cli import main

        with pytest.raises(SystemExit) as excinfo:
            main(["--version"])
        assert excinfo.value.code == 0
        assert "syswatch" in capsys.readouterr().out


class TestTuiCommand:
    def test_tui_registered_in_help(self, capsys):
        import contextlib

        from syswatch.cli import build_parser

        parser = build_parser()
        with contextlib.redirect_stdout(__import__("io").StringIO()) as buf, pytest.raises(SystemExit) as e:
            parser.parse_args(["--help"])
        assert "tui" in buf.getvalue()

    def test_tui_non_interactive_is_rejected(self, monkeypatch, capsys):
        import sys as _sys

        monkeypatch.setattr(_sys.stdin, "isatty", lambda: False)
        monkeypatch.setattr(_sys.stdout, "isatty", lambda: False)
        from syswatch.cli import main

        rc = main(["tui"])
        captured = capsys.readouterr()
        assert rc == 1
        assert "interactive terminal" in captured.err

    def test_bare_invocation_routes_to_tui_path(self, monkeypatch, capsys):
        """Bare 'syswatch' on a non-tty must hit the same friendly guard."""
        import sys as _sys

        monkeypatch.setattr(_sys.stdin, "isatty", lambda: False)
        monkeypatch.setattr(_sys.stdout, "isatty", lambda: False)
        from syswatch.cli import main

        rc = main([])
        captured = capsys.readouterr()
        assert rc == 1
        assert "interactive terminal" in captured.err


class TestCleanShell:
    """Launch via subprocess with a minimal environment."""

    MINIMAL_ENV = {"PATH": "/usr/bin:/bin", "HOME": "/tmp"}

    def test_module_version(self):
        result = subprocess.run(
            [sys.executable, "-m", "syswatch", "--version"],
            cwd=PROJECT_ROOT,
            env=self.MINIMAL_ENV,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "syswatch" in result.stdout

    def test_module_info(self):
        result = subprocess.run(
            [sys.executable, "-m", "syswatch", "info"],
            cwd=PROJECT_ROOT,
            env=self.MINIMAL_ENV,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "Hostname:" in result.stdout

    def test_launcher_script(self):
        launcher = PROJECT_ROOT / "bin" / "syswatch"
        result = subprocess.run(
            [str(launcher), "health"],
            cwd="/tmp",
            env={**self.MINIMAL_ENV},
            capture_output=True,
            text=True,
        )
        assert result.returncode in (0, 1, 2)

    def test_module_report(self):
        result = subprocess.run(
            [sys.executable, "-m", "syswatch", "report", "--pretty"],
            cwd=PROJECT_ROOT,
            env=self.MINIMAL_ENV,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert "cpu" in data

    def test_broken_pipe_is_graceful(self):
        pipe_to_head = subprocess.Popen(
            ["head", "-c", "200"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
        )
        producer = subprocess.Popen(
            [sys.executable, "-m", "syswatch", "ps", "-n", "0"],
            cwd=PROJECT_ROOT,
            env=self.MINIMAL_ENV,
            stdout=pipe_to_head.stdin,
            stderr=subprocess.PIPE,
        )
        producer.communicate(timeout=30)
        pipe_to_head.stdin.close()
        pipe_to_head.wait(timeout=10)
