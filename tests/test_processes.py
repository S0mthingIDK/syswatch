import os

import pytest

from syswatch.collectors.processes import (
    count_processes,
    cpu_percent_between,
    memory_percent,
    parse_stat,
    snapshot,
    zombies,
)
from tests.conftest import make_stat_line


class TestParseStat:
    def test_basic(self):
        line = make_stat_line(pid=7, comm="init", state="S", ppid=0, utime=11, stime=22, rss_pages=33)
        name, state, ppid, utime, stime, rss, threads, start = parse_stat(line)
        assert name == "init"
        assert state == "S"
        assert ppid == 0
        assert utime == 11.0
        assert stime == 22.0
        assert rss == 33
        assert threads == 1
        assert start == 500.0

    def test_name_with_parens_and_spaces(self):
        line = make_stat_line(pid=8, comm="(bad) na(me)", state="R")
        name, state, *_ = parse_stat(line)
        assert name == "(bad) na(me)"
        assert state == "R"

    def test_threads_and_starttime(self):
        line = make_stat_line(pid=9, comm="multi", num_threads=7, starttime_ticks=1234.5)
        *_, threads, start = parse_stat(line)
        assert threads == 7
        assert start == 1234.5

    def test_details_reader(self, make_proc):
        from syswatch.collectors.processes import read_process_details

        root = make_proc({
            "42/status": "Name:\tworker\nUid:\t1000\t1000\t1000\t1000\nThreads:\t4\n",
        })
        import pwd

        details = read_process_details(42, root)
        assert details["uid"] == 1000
        assert details["user"] == pwd.getpwuid(1000).pw_name
        assert details["threads"] == 4
        assert details["exe"] is None
        orphan = make_proc({"77/status": "Uid:\t65001\t65001\t65001\t65001\n"})
        assert read_process_details(77, orphan)["user"] == "65001"  # unknown uid
        missing = read_process_details(9999, root)
        assert missing["user"] is None and missing["exe"] is None

    def test_malformed_returns_none(self):
        assert parse_stat("no parens here") is None
        assert parse_stat("abc (x) S") is None
        assert parse_stat("12 (short) S") is None


def test_snapshot_finds_processes(proc_tree):
    procs = {p.pid: p for p in snapshot(proc_tree)}
    assert set(procs.keys()) == {1, 42, 99}
    worker = procs[42]
    assert worker.name == "worker"
    assert worker.cmdline == "/usr/bin/worker --flag"
    assert worker.rss_bytes > 0
    init = procs[1]
    assert init.cmdline == "/sbin/init --user"
    zomb = procs[99]
    assert zomb.is_zombie


def test_snapshot_skips_unreadable(tmp_path):
    root = tmp_path / "proc"
    good = root / "10"
    good.mkdir(parents=True)
    (good / "stat").write_text(make_stat_line(pid=10))
    bad = root / "20"
    bad.mkdir(parents=True)
    (bad / "stat").write_text(make_stat_line(pid=20))
    if os.geteuid() != 0:
        os.chmod(bad / "stat", 0o000)
        procs = snapshot(root)
        assert [p.pid for p in procs] == [10]
    else:
        procs = snapshot(root)
        assert len(procs) >= 1


def test_snapshot_missing_root(tmp_path):
    assert snapshot(tmp_path / "does-not-exist") == []


def test_count_processes(proc_tree):
    assert count_processes(proc_tree) == 3


def test_count_processes_empty_dir(tmp_path):
    tmp_path.mkdir(exist_ok=True)
    empty = tmp_path / "empty"
    empty.mkdir()
    assert count_processes(empty) == 0


def test_cpu_percent_between():
    before_list = [
        type(
            "P",
            (),
            {"pid": 1, "cpu_ticks": 100},
        )()
    ]
    after_list = [
        type("P", (), {"pid": 1, "cpu_ticks": 200})(),
        type("P", (), {"pid": 2, "cpu_ticks": 999})(),
    ]
    usage = cpu_percent_between({p.pid: p for p in before_list}, {p.pid: p for p in after_list}, elapsed_seconds=1.0, ticks_per_second=100)
    assert usage[1] == 100.0
    assert 2 not in usage


def test_memory_percent_bounds():
    from syswatch.collectors.processes import ProcessInfo

    proc = ProcessInfo(pid=1, name="x", state="S", ppid=0, utime_ticks=0, stime_ticks=0, rss_bytes=250 * 1024 * 1024)
    assert memory_percent(proc, 1024**3) == pytest.approx(250 * 1024**2 / 1024**3 * 100)
    assert memory_percent(proc, None) is None


def test_zombies_filter(proc_tree):
    zs = zombies(snapshot(proc_tree))
    assert [z.pid for z in zs] == [99]


def test_live_snapshot_sanity():
    from syswatch.paths import PROC_ROOT

    procs = snapshot(PROC_ROOT)
    assert len(procs) > 5
    me = next(p for p in procs if p.pid == os.getpid())
    assert me.rss_bytes > 0


def test_cpu_percent_cap_with_two_cores(monkeypatch):
    import syswatch.collectors.processes as mod

    from syswatch.collectors.processes import ProcessInfo

    monkeypatch.setattr(mod.os, "cpu_count", lambda: 2)
    before = {7: ProcessInfo(pid=7, name="hog", state="R", ppid=0, utime_ticks=0, stime_ticks=0, rss_bytes=0)}
    after = {7: ProcessInfo(pid=7, name="hog", state="R", ppid=0, utime_ticks=400, stime_ticks=0, rss_bytes=0)}
    usage = cpu_percent_between(before, after, 1.0, ticks_per_second=100)
    # 400 ticks / 100 tps over 1s = 400% of one core; capped at 2 cores => 200%
    assert usage[7] == 200.0


def test_cpu_percent_survives_missing_cpu_count(monkeypatch):
    import syswatch.collectors.processes as mod

    from syswatch.collectors.processes import ProcessInfo

    monkeypatch.setattr(mod.os, "cpu_count", lambda: None)
    before = {7: ProcessInfo(pid=7, name="x", state="R", ppid=0, utime_ticks=0, stime_ticks=0, rss_bytes=0)}
    after = {7: ProcessInfo(pid=7, name="x", state="R", ppid=0, utime_ticks=50, stime_ticks=0, rss_bytes=0)}
    usage = cpu_percent_between(before, after, 1.0, ticks_per_second=100)
    assert usage[7] == 50.0
