import os
import stat as statmod
import sys
from pathlib import Path

import pytest

from syswatch.collectors.memory import memory_info_from
from syswatch.config import AppConfig, default_config
from syswatch.health import (
    Finding,
    Severity,
    check_cpu_usage,
    check_disks,
    check_failed_services,
    check_gateways,
    check_load,
    check_memory,
    check_zombies,
    exit_code,
    run_checks,
    summarize,
)
from syswatch.collectors.processes import ProcessInfo


def make_disk(percent: float | None, mountpoint: str = "/") -> object:
    class D:
        pass

    d = D()
    d.device = "/dev/x"
    d.mountpoint = mountpoint
    d.fstype = "ext4"
    d.percent = percent
    return d


class TestThresholdChecks:
    def test_disk_warning(self):
        findings = check_disks([make_disk(90.0)], default_config())
        assert len(findings) == 1
        assert findings[0].severity is Severity.WARNING

    def test_disk_critical_at_offset(self):
        cfg = AppConfig(disk_warning=80.0, critical_offset=10.0)
        findings = check_disks([make_disk(91.0)], cfg)
        assert findings[0].severity is Severity.CRITICAL

    def test_disk_ok_below_threshold(self):
        assert check_disks([make_disk(50.0)], default_config()) == []

    def test_disk_none_percent_ignored(self):
        assert check_disks([make_disk(None)], default_config()) == []

    def test_memory_findings(self):
        info = memory_info_from({"MemTotal": 100 * 1024, "MemAvailable": 5 * 1024})
        findings = check_memory(info, AppConfig(ram_warning=90.0))
        assert findings and findings[0].check == "memory_pressure"

    def test_memory_none_safe(self):
        assert check_memory(None, default_config()) == []

    def test_cpu_findings(self):
        assert check_cpu_usage(99.0, AppConfig(cpu_warning=80.0))[0].severity is Severity.CRITICAL
        assert check_cpu_usage(85.0, AppConfig(cpu_warning=85.0))[0].severity is Severity.WARNING
        assert check_cpu_usage(None, default_config()) == []

    def test_load_finding(self):
        cfg = AppConfig(load_per_core_warning=2.0)
        assert check_load((10.0, 1.0, 0.5), 4, cfg)[0].check == "load_average"
        assert check_load((7.9, 1.0, 0.5), 4, cfg) == []
        assert check_load(None, 4, cfg) == []
        assert check_load((10.0, 1.0, 0.5), None, cfg) == []

    def test_zombies_finding(self):
        z = ProcessInfo(pid=5, name="dead", state="Z", ppid=1, utime_ticks=0, stime_ticks=0, rss_bytes=0)
        findings = check_zombies([z])
        assert len(findings) == 1
        assert "dead" in findings[0].message
        assert check_zombies([]) == []


class TestFailedServices:
    def _write_fake_systemctl(self, tmp_path: Path, output_lines: list[str], exit_code_value: int = 0):
        bindir = tmp_path / "bin"
        bindir.mkdir(exist_ok=True)
        script = bindir / "systemctl"
        body = "#!/bin/sh\n" + "\n".join(f"echo '{line}'" for line in output_lines)
        script.write_text(body + f"\nexit {exit_code_value}\n")
        script.chmod(script.stat().st_mode | statmod.S_IEXEC)
        return str(bindir)

    def test_failed_units_reported(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        bindir = self._write_fake_systemctl(tmp_path, ["foo.service loaded failed failed Foo"])
        monkeypatch.setenv("PATH", bindir + os.pathsep + os.environ["PATH"])
        findings = check_failed_services(timeout=2.0, run_dir=run_dir)
        assert len(findings) == 1
        assert "foo.service" in findings[0].message
        assert findings[0].details["units"] == ["foo.service"]

    def test_pre_collected_units_skip_systemctl(self, tmp_path, monkeypatch):
        """When units are passed in, systemctl must not be invoked at all."""
        bindir = tmp_path / "bin"
        bindir.mkdir()
        script = bindir / "systemctl"
        script.write_text("#!/bin/sh\necho should-not-run\n")
        script.chmod(script.stat().st_mode | statmod.S_IEXEC)
        monkeypatch.setenv("PATH", str(bindir))
        findings = check_failed_services(timeout=2.0, units=["a.service", "b.service"])
        assert len(findings) == 1
        assert findings[0].details["units"] == ["a.service", "b.service"]

    def test_no_failures_no_finding(self, tmp_path, monkeypatch):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        bindir = self._write_fake_systemctl(tmp_path, [""])
        monkeypatch.setenv("PATH", bindir + os.pathsep + os.environ["PATH"])
        assert check_failed_services(timeout=2.0, run_dir=run_dir) == []

    def test_not_systemd_silent(self, tmp_path):
        assert check_failed_services(timeout=2.0, run_dir=tmp_path / "missing") == []

    def test_systemctl_failure_is_silent(self, tmp_path, monkeypatch):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        bindir = self._write_fake_systemctl(tmp_path, ["ignored"], exit_code_value=4)
        monkeypatch.setenv("PATH", bindir + os.pathsep + os.environ["PATH"])
        assert check_failed_services(timeout=2.0, run_dir=run_dir) == []


class TestGateways:
    @pytest.fixture
    def route_proc(self, make_proc):
        return make_proc(
            {
                "net/route": (
                    "Iface\tDestination\tGateway\tFlags\n"
                    "eth0\t00000000\tC0A86401\t0003\n"
                )
            }
        )

    def test_ping_success_no_finding(self, route_proc, tmp_path, monkeypatch):
        bindir = tmp_path / "bin"
        bindir.mkdir()
        script = bindir / "ping"
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(script.stat().st_mode | statmod.S_IEXEC)
        monkeypatch.setenv("PATH", str(bindir))
        assert check_gateways(route_proc, timeout=0.5) == []

    def test_ping_failure_warns_with_method(self, route_proc, tmp_path, monkeypatch):
        bindir = tmp_path / "bin"
        bindir.mkdir()
        script = bindir / "ping"
        script.write_text("#!/bin/sh\nexit 1\n")
        script.chmod(script.stat().st_mode | statmod.S_IEXEC)
        monkeypatch.setenv("PATH", str(bindir))
        findings = check_gateways(route_proc, timeout=0.5)
        assert len(findings) == 1
        assert findings[0].check == "gateway_unreachable"
        assert findings[0].details["method"] == "icmp"

    def test_tcp_probe_used_when_ping_missing(self, route_proc, monkeypatch):
        monkeypatch.setattr("syswatch.health._tcp_reachable", lambda addr, ports, t: False)
        monkeypatch.setenv("PATH", "/nonexistent-dir-for-syswatch-tests")
        findings = check_gateways(route_proc, timeout=0.5)
        assert len(findings) == 1
        assert findings[0].details["method"] == "tcp-probe"


def test_run_checks_on_fake_tree(proc_tree, tmp_path):
    cfg = AppConfig(sample_delay=0.05, health_timeout=0.5)
    findings, errors = run_checks(cfg, proc_root=proc_tree)
    checks_seen = {f.check for f in findings}
    # The fake tree ships a zombie process (pid 99), so check_zombies must fire.
    assert "zombie_processes" in checks_seen
    assert isinstance(errors, list)


def test_summarize_and_exit_codes():
    warn = Finding(check="c", severity=Severity.WARNING, message="m")
    crit = Finding(check="c", severity=Severity.CRITICAL, message="m")
    assert exit_code([]) == 0
    assert exit_code([warn]) == 1
    assert exit_code([crit, warn]) == 2
    assert "HEALTHY" in summarize([])
    assert "UNHEALTHY" in summarize([crit])