
from syswatch.collectors.network import (
    default_gateways,
    interface_rates,
    read_interface_counters,
    total_rates,
)
from tests.conftest import NET_DEV_TEXT, ROUTE_TEXT


def test_read_counters_parses_and_skips_bad_lines():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        net_dir = Path(tmp) / "net"
        net_dir.mkdir()
        (net_dir / "dev").write_text(NET_DEV_TEXT)
        counters = read_interface_counters(Path(tmp))
    assert set(counters.keys()) == {"eth0", "wlan0"}
    eth0 = counters["eth0"]
    assert eth0.rx_bytes == 10_000_000
    assert eth0.tx_bytes == 5_000_000
    assert eth0.rx_packets == 20_000
    assert eth0.tx_errors == 1


def test_counters_missing_file(tmp_path):
    assert read_interface_counters(tmp_path) == {}


def test_interface_rates():
    from syswatch.collectors.network import InterfaceCounters

    prev = {"eth0": InterfaceCounters(rx_bytes=1000, tx_bytes=500)}
    cur = {"eth0": InterfaceCounters(rx_bytes=2000, tx_bytes=1500)}
    rates = interface_rates(prev, cur, interval_seconds=2.0)
    rx, tx = total_rates(rates)
    assert rx == 500.0
    assert tx == 500.0


def test_rates_counter_reset_clamped_to_zero():
    from syswatch.collectors.network import InterfaceCounters

    prev = {"eth0": InterfaceCounters(rx_bytes=5000)}
    cur = {"eth0": InterfaceCounters(rx_bytes=10)}
    rates = interface_rates(prev, cur, interval_seconds=1.0)
    assert rates["eth0"].rx_bytes_per_second == 0.0


def test_rates_zero_interval_no_crash():
    from syswatch.collectors.network import InterfaceCounters

    prev = {"eth0": InterfaceCounters()}
    cur = {"eth0": InterfaceCounters()}
    assert interface_rates(prev, cur, 0) == {}


def test_default_gateways_parse():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        net_dir = Path(tmp) / "net"
        net_dir.mkdir()
        (net_dir / "route").write_text(ROUTE_TEXT)
        gateways = default_gateways(Path(tmp))
    # 0164A8C0 -> 192.168.100.1 ; 010010AC -> 172.16.0.1 (kernel LE hex)
    assert ("eth0", "192.168.100.1") in gateways
    assert ("wlan0", "172.16.0.1") in gateways
    assert all(gw[1] != "0.0.0.0" for gw in gateways)


def test_total_rates_exclude_loopback_by_default():
    from syswatch.collectors.network import InterfaceRate

    rates = {
        "lo": InterfaceRate(rx_bytes_per_second=1_000_000.0, tx_bytes_per_second=900_000.0),
        "eth0": InterfaceRate(rx_bytes_per_second=100.0, tx_bytes_per_second=200.0),
    }
    rx, tx = total_rates(rates)
    assert rx == 100.0
    assert tx == 200.0


def test_total_rates_can_be_overridden():
    from syswatch.collectors.network import InterfaceRate

    rates = {"lo": InterfaceRate(rx_bytes_per_second=5.0, tx_bytes_per_second=6.0)}
    assert total_rates(rates, exclude=()) == (5.0, 6.0)


def test_default_gateways_missing_route(tmp_path):
    assert default_gateways(tmp_path) == []


def test_live_counters_sanity():
    from syswatch.paths import PROC_ROOT

    counters = read_interface_counters(PROC_ROOT)
    assert isinstance(counters, dict)
