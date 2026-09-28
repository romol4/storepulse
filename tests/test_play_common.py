from __future__ import annotations

import locale
import time
import zipfile
from collections.abc import Iterator
from datetime import date, datetime
from io import BytesIO

import pytest

from storepulse.core.sources import play_common
from storepulse.core.sources.play_common import (
    Month,
    ReportParseError,
    decode_csv,
    parse_number,
    parse_play_date,
    plan_month,
)

TODAY = date(2026, 9, 27)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-09-01", date(2026, 9, 1)),
        ("Sep 1, 2026", date(2026, 9, 1)),
        ("September 17, 2026", date(2026, 9, 17)),
        ("Sept 3, 2026", date(2026, 9, 3)),
        ("aug 31 2026", date(2026, 8, 31)),
        ("May 5, 2026", date(2026, 5, 5)),
    ],
)
def test_parse_play_date(text: str, expected: date) -> None:
    assert parse_play_date(text) == expected


@pytest.mark.parametrize("text", ["", "31/08/2026", "Foo 1, 2026", "Feb 30, 2026", "вер 1, 2026"])
def test_parse_play_date_rejects(text: str) -> None:
    with pytest.raises(ReportParseError):
        parse_play_date(text)


@pytest.fixture
def ukrainian_time_locale() -> Iterator[None]:
    previous = locale.setlocale(locale.LC_TIME)
    for name in ("uk_UA.UTF-8", "uk_UA.utf8", "Ukrainian_Ukraine.1251", "uk_UA"):
        try:
            locale.setlocale(locale.LC_TIME, name)
            break
        except locale.Error:
            continue
    else:
        pytest.skip("no Ukrainian locale installed on this machine")
    yield
    locale.setlocale(locale.LC_TIME, previous)


def test_dates_parse_under_ukrainian_locale(ukrainian_time_locale: None) -> None:
    assert parse_play_date("Aug 3, 2026") == date(2026, 8, 3)
    assert parse_play_date("September 17, 2026") == date(2026, 9, 17)


def test_parse_play_date_never_calls_strptime(monkeypatch: pytest.MonkeyPatch) -> None:
    class NoStrptime(datetime):
        @classmethod
        def strptime(cls, *args: object, **kwargs: object) -> datetime:  # type: ignore[override]
            raise AssertionError("strptime is locale-dependent and must not be used")

    def no_time_strptime(*args: object, **kwargs: object) -> time.struct_time:
        raise AssertionError("strptime is locale-dependent and must not be used")

    monkeypatch.setattr(play_common, "datetime", NoStrptime)
    monkeypatch.setattr(time, "strptime", no_time_strptime)
    assert parse_play_date("Aug 3, 2026") == date(2026, 8, 3)
    assert parse_play_date("2026-09-01") == date(2026, 9, 1)


def test_decode_csv_variants() -> None:
    text = "Date,Value\n2026-09-01,1\n"
    assert decode_csv(text.encode("utf-16")) == text  # with BOM
    assert decode_csv(text.encode("utf-16-le")) == text  # no BOM
    assert decode_csv(text.encode("utf-16-be")) == text
    assert decode_csv(text.encode("utf-8")) == text
    assert decode_csv(b"\xef\xbb\xbf" + text.encode("utf-8")) == text
    with pytest.raises(ReportParseError):
        decode_csv(b"\xff\xfe\x00\xd8")  # truncated surrogate


def test_parse_number() -> None:
    assert parse_number("1,299.00", "x", "f") == 1299
    assert parse_number("-0.75", "x", "f") == -0.75
    assert parse_number("", "x", "f") is None
    assert parse_number("  ", "x", "f") is None
    with pytest.raises(ReportParseError):
        parse_number("n/a", "x", "f")


def test_read_zip_csv_errors() -> None:
    with pytest.raises(ReportParseError, match="not a valid zip"):
        play_common.read_zip_csv(b"nope", "f")
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("a.csv", "x")
        z.writestr("b.csv", "y")
    with pytest.raises(ReportParseError, match="one CSV"):
        play_common.read_zip_csv(buf.getvalue(), "f")


def test_month_helpers() -> None:
    assert Month.parse("2026-09") == Month.parse("202609") == Month(2026, 9)
    assert Month(2026, 12).next() == Month(2027, 1)
    assert Month(2026, 1).prev() == Month(2025, 12)
    assert Month(2024, 2).last_day == date(2024, 2, 29)
    assert play_common.last_months(3, TODAY) == [Month(2026, 7), Month(2026, 8), Month(2026, 9)]
    assert play_common.month_of_object("earnings/earnings_202608_123-0.zip") == Month(2026, 8)
    assert play_common.month_of_object("stats/installs/installs_a.b_202609_overview.csv") == Month(
        2026, 9
    )
    with pytest.raises(ValueError):
        Month.parse("2026-13")


def test_plan_month_daily_files() -> None:
    available = {Month(2026, 7), Month(2026, 9)}
    assert plan_month(Month(2026, 9), available, TODAY) == "collect"
    assert plan_month(Month(2026, 6), available, TODAY) == "skip"  # before launch
    assert plan_month(Month(2026, 8), available, TODAY) == "no_file"  # gap after launch
    assert plan_month(Month(2026, 9), {Month(2026, 7)}, TODAY) == "not_ready"  # current
    assert plan_month(Month(2026, 9), set(), TODAY) == "skip"  # nothing at all yet


def test_plan_month_monthly_reports() -> None:
    available = {Month(2026, 6)}
    kwargs = {"published_after_month_end": True}
    assert plan_month(Month(2026, 9), available, TODAY, **kwargs) == "skip"  # current month
    early = date(2026, 9, 4)
    assert plan_month(Month(2026, 8), available, early, **kwargs) == "not_ready"
    assert plan_month(Month(2026, 8), available, TODAY, **kwargs) == "no_file"  # after 15th
    assert plan_month(Month(2026, 7), available, early, **kwargs) == "no_file"
