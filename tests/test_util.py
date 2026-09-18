import pytest

from syswatch.util import (
    format_bytes,
    format_percent,
    format_rate,
    format_uptime,
    parse_float,
    parse_int,
    read_text,
)


class TestFormatBytes:
    def test_bytes(self):
        assert format_bytes(0) == "0 B"
        assert format_bytes(1023) == "1023 B"

    @pytest.mark.parametrize(
        "value,expected",
        [
            (1024, "1.0 KiB"),
            (1536, "1.5 KiB"),
            (1024**2, "1.0 MiB"),
            (1536 * 1024**2, "1.5 GiB"),
            (2.5 * 1024**4, "2.5 TiB"),
        ],
    )
    def test_units(self, value, expected):
        assert format_bytes(value) == expected

    def test_none_and_negative(self):
        assert format_bytes(None) == "unknown"
        assert format_bytes(-2048).endswith("KiB")
        assert format_bytes(-2048).startswith("-")


class TestParsers:
    def test_parse_int(self):
        assert parse_int("42") == 42
        assert parse_int("x") is None
        assert parse_int(None) is None

    def test_parse_float(self):
        assert parse_float("1.5") == 1.5
        assert parse_float("bad") is None


def test_read_text_missing(tmp_path):
    assert read_text(tmp_path / "nope") is None


def test_format_helpers():
    assert format_percent(None) == "unknown"
    assert format_percent(12.34) == "12.3%"
    assert format_rate(2048) == "2.0 KiB/s"


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (None, "unknown"),
        (0, "0s"),
        (45, "45s"),
        (59, "59s"),
        (60, "1m"),
        (3661, "1h 1m"),
        (87864, "1d 0h 24m"),
        (172800 + 7200 + 60, "2d 2h 1m"),
    ],
)
def test_format_uptime(seconds, expected):
    assert format_uptime(seconds) == expected
