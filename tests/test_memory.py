from tests.conftest import MEMINFO_TEXT  # noqa: E402

from syswatch.collectors.memory import get_memory, memory_info_from, parse_meminfo


def test_parse_meminfo():
    values = parse_meminfo(MEMINFO_TEXT)
    assert values["MemTotal"] == 2048000 * 1024
    assert values["MemAvailable"] == 1024000 * 1024
    assert values["SwapTotal"] == 512000 * 1024


def test_parse_meminfo_malformed_lines_ignored():
    values = parse_meminfo("MemTotal: 100 kB\ngarbage\nBad: xx kB\n:\n")
    assert values["MemTotal"] == 100 * 1024
    assert len(values) == 1


def test_memory_math():
    info = memory_info_from(parse_meminfo(MEMINFO_TEXT))
    assert info.total_bytes == 2048000 * 1024
    assert info.available_bytes == 1024000 * 1024
    assert info.used_bytes == 1024000 * 1024
    assert info.used_percent == 50.0
    assert info.swap_used_percent == 50.0


def test_memory_fallback_without_memavailable():
    text = "MemTotal: 1000 kB\nMemFree: 200 kB\nBuffers: 100 kB\nCached: 300 kB\n"
    info = memory_info_from(parse_meminfo(text))
    assert info.available_bytes == 600 * 1024


def test_memory_none_when_total_missing():
    assert memory_info_from({}) is None


def test_get_memory_missing_file(make_proc):
    root = make_proc({"other": "x"})
    assert get_memory(root) is None


def test_get_memory_real_tree(proc_tree):
    info = get_memory(proc_tree)
    assert info is not None
    assert info.total_bytes == 2048000 * 1024
