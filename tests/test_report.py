import json
import sys

import pytest

sys.path.insert(0, ".")

from syswatch.config import AppConfig
from syswatch.report import SCHEMA_VERSION, build_report, dump_report_json


@pytest.fixture
def report(proc_tree, sys_tree, tmp_path):
    etc = tmp_path / "etc"
    etc.mkdir(exist_ok=True)
    (etc / "os-release").write_text('PRETTY_NAME="TestOS 1.0"\n')
    cfg = AppConfig(sample_delay=0.05, health_timeout=0.5)
    data = build_report(
        cfg, proc_root=proc_tree, sys_root=sys_tree, etc_root=etc, top_processes=5
    )
    return data


def test_report_structure(report):
    expected_keys = {
        "schema_version",
        "tool",
        "generated_at",
        "host",
        "cpu",
        "memory",
        "disks",
        "network",
        "top_processes_by_cpu",
        "top_processes_by_memory",
        "health",
        "configuration",
        "collection_errors",
    }
    assert expected_keys <= set(report.keys())
    assert report["schema_version"] == SCHEMA_VERSION
    assert report["tool"] == "syswatch"


def test_report_host_section(report):
    host = report["host"]
    assert host["hostname"]
    assert host["os_pretty_name"] == "TestOS 1.0"
    assert host["uptime_seconds"] == pytest.approx(3600.25, abs=0.01)
    assert host["boot_time_epoch"] == 1700000000


def test_report_cpu_memory(report):
    assert report["cpu"]["model"] == "Test CPU Model @ 2.0GHz"
    assert report["cpu"]["cores"] == 2
    assert report["memory"]["total_bytes"] == 2048000 * 1024
    assert report["memory"]["used_percent"] is not None


def test_report_disks_and_network(report):
    assert len(report["disks"]) >= 1
    disk = report["disks"][0]
    for key in ("device", "mountpoint", "fstype", "total_bytes", "percent_used"):
        assert key in disk
    names = {i["name"] for i in report["network"]["interfaces"]}
    assert {"eth0", "wlan0"} <= names


def test_report_processes(report):
    cpu_pids = [p["pid"] for p in report["top_processes_by_cpu"]]
    assert set(cpu_pids) <= {1, 42, 99}
    mem_pids = [p["pid"] for p in report["top_processes_by_memory"]]
    assert len(mem_pids) <= 5


def test_report_serializable(report):
    text = dump_report_json(report)
    parsed = json.loads(text)
    assert parsed["schema_version"] == SCHEMA_VERSION


def test_report_pretty_vs_compact(report):
    pretty = dump_report_json(report, pretty=True)
    compact = dump_report_json(report, pretty=False)
    assert "\n" in pretty.strip()
    assert len(pretty) > len(compact)


def test_report_health_section(report):
    assert report["health"]["status"] in {"healthy", "issues"}
    for finding in report["health"]["findings"]:
        assert finding["severity"] in {"info", "warning", "critical"}
