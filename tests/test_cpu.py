from tests.conftest import PROC_STAT_TEXT  # noqa: E402

from syswatch.collectors.cpu import (
    CpuTimes,
    cpu_usage_between,
    load_average,
    per_cpu_usage_between,
    read_cpu_times,
)


class TestCpuUsage:
    def test_idle_only_increase_is_zero(self):
        prev = CpuTimes(user=10, idle=100)
        cur = CpuTimes(user=10, idle=200)
        assert cpu_usage_between(prev, cur) == 0.0

    def test_busy_only_increase_is_hundred(self):
        prev = CpuTimes(user=0, idle=100)
        cur = CpuTimes(user=100, idle=100)
        assert cpu_usage_between(prev, cur) == 100.0

    def test_partial_usage(self):
        prev = CpuTimes(user=0, system=0, idle=0)
        cur = CpuTimes(user=30, system=20, idle=50)
        assert cpu_usage_between(prev, cur) == 50.0

    def test_counter_reset_returns_none(self):
        prev = CpuTimes(user=500, idle=5000)
        cur = CpuTimes(user=10, idle=50)
        assert cpu_usage_between(prev, cur) is None

    def test_iowait_counts_as_not_busy(self):
        prev = CpuTimes(idle=0, iowait=0)
        cur = CpuTimes(idle=40, iowait=10, user=25, system=25)
        # busy delta 50 of total 100 -> 50%
        assert cpu_usage_between(prev, cur) == 50.0


class TestReadCpuTimes:
    def test_parse_labels(self):
        from pathlib import Path

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "stat").write_text(PROC_STAT_TEXT)
            result = read_cpu_times(Path(tmp))
        assert set(result.keys()) >= {"cpu", "cpu0", "cpu1"}
        assert result["cpu"].user == 100
        assert result["cpu"].iowait == 40
        assert result["cpu0"].steal == 2
        assert "intr" not in result

    def test_missing_file(self, make_proc):
        root = make_proc({})
        assert read_cpu_times(root) == {}

    def test_short_lines_tolerated(self, make_proc):
        root = make_proc({"stat": "cpu 10\n"})
        result = read_cpu_times(root)
        assert result["cpu"] == CpuTimes(user=10)


def test_per_core_excludes_unknown_labels():
    prev = {"cpu": CpuTimes(), "cpu0": CpuTimes()}
    cur = {"cpu": CpuTimes(), "cpu0": CpuTimes(), "cpu9": CpuTimes()}
    usage = per_cpu_usage_between(prev, cur)
    assert "cpu9" not in usage
    assert "cpu" in usage and "cpu0" in usage


def test_load_average_on_linux():
    result = load_average()
    assert result is not None
    l1, l5, l15 = result
    assert all(v >= 0 for v in (l1, l5, l15))
