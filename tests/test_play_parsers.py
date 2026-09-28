from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from conftest import PKG, play_bytes
from storepulse.core import db
from storepulse.core.sources import play_earnings, play_installs, play_sales, play_vitals
from storepulse.core.sources.play_common import (
    Month,
    ReportParseError,
    decode_csv,
    read_zip_csv,
)

SEP = Month(2026, 9)
AUG = Month(2026, 8)


def _installs(name: str, by_country: bool) -> list[play_installs.InstallsRow]:
    return play_installs.parse_installs(
        decode_csv(play_bytes(name)), by_country=by_country, what=name
    )


@pytest.fixture
def app_id(conn: sqlite3.Connection) -> int:
    return db.upsert_app(conn, "android", PKG, "billFT")


# -- installs ------------------------------------------------------------------------------


def test_installs_overview_normal() -> None:
    rows = _installs(f"installs_{PKG}_202609_overview.csv", by_country=False)
    assert [r.day for r in rows] == ["2026-09-01", "2026-09-02", "2026-09-03"]
    assert all(r.country == "ALL" for r in rows)
    assert rows[0].values == {"installs": 12, "uninstalls": 2, "active_devices": 800}


def test_installs_empty_cell_is_missing_not_zero() -> None:
    day3 = _installs(f"installs_{PKG}_202609_overview.csv", by_country=False)[2]
    assert "uninstalls" not in day3.values
    assert day3.values["installs"] == 9


def test_installs_empty_and_malformed() -> None:
    assert _installs("installs_empty_overview.csv", by_country=False) == []
    with pytest.raises(ReportParseError, match="Date"):
        _installs("installs_malformed_overview.csv", by_country=False)
    with pytest.raises(ReportParseError, match="none of the expected columns"):
        play_installs.parse_installs("Date,Other\n2026-09-01,1\n", by_country=False, what="f")


def test_installs_overview_and_country_combined(app_id: int) -> None:
    rows = play_installs.to_metric_rows(
        app_id,
        _installs(f"installs_{PKG}_202609_overview.csv", by_country=False),
        _installs(f"installs_{PKG}_202609_country.csv", by_country=True),
    )
    got = {(r.date, r.country, r.metric): r.value for r in rows}
    # ALL rows come from the overview, never from summing countries.
    assert got[("2026-09-01", "ALL", "installs")] == 12
    assert got[("2026-09-01", "US", "installs")] == 7
    assert got[("2026-09-01", "DE", "installs")] == 5
    assert ("2026-09-03", "ALL", "uninstalls") not in got
    assert ("2026-09-03", "US", "uninstalls") not in got
    assert len(rows) == 3 * 3 - 1 + 2 * 3 * 2 + 3 - 1  # ALL + US/DE days 1-2 + US day 3


def test_installs_month_files() -> None:
    names = [
        f"stats/installs/installs_{PKG}_202609_overview.csv",
        f"stats/installs/installs_{PKG}_202609_country.csv",
        f"stats/installs/installs_{PKG}_202609_device.csv",
        f"stats/installs/installs_{PKG}_202608_overview.csv",
        f"stats/installs/installs_{PKG}.pro_202609_overview.csv",  # another package
    ]
    files = play_installs.month_files(names, PKG)
    assert set(files) == {SEP, AUG}
    assert set(files[SEP]) == {"overview", "country"}


# -- sales (provisional) -------------------------------------------------------------------


def _sales(name: str, app_id: int) -> play_sales.MappedMonth:
    text = read_zip_csv(play_bytes(name), name)
    return play_sales.map_month(SEP, [text], {PKG: app_id}, name)


def test_sales_normal(app_id: int) -> None:
    mapped = _sales("salesreport_202609.zip", app_id)
    got = {(r.date, r.country, r.currency): round(r.value, 2) for r in mapped.rows}
    assert got == {
        ("2026-09-01", "US", "USD"): 9.98,
        ("2026-09-02", "DE", "EUR"): 1298.00,  # 1,299.00 charged, refund of -1.00 subtracts 1
        ("2026-09-02", "US", "USD"): -4.99,  # refund recorded positive still subtracts
    }
    # Gross, provisional: never mixed into net `proceeds`.
    assert all(r.metric == "sales_gross" for r in mapped.rows)
    assert mapped.unmapped_rows == 1
    assert "USD 2.99" in (mapped.note() or "")
    assert mapped.out_of_month_rows == 1
    assert mapped.ignored_statuses == {"Cancelled": 1}


def test_sales_refund_sign_either_way(app_id: int) -> None:
    header = ",".join(play_sales.REQUIRED)
    positive = f"{header}\n2026-09-05,Refund,{PKG},USD,2.00,US\n"
    negative = f"{header}\n2026-09-05,Refund,{PKG},USD,-2.00,US\n"
    for text in (positive, negative):
        rows = play_sales.map_month(SEP, [text], {PKG: app_id}, "f").rows
        assert [r.value for r in rows] == [-2.0]


def test_sales_empty_and_malformed(app_id: int) -> None:
    assert _sales("salesreport_empty.zip", app_id).rows == []
    with pytest.raises(ReportParseError, match="missing columns"):
        _sales("salesreport_malformed.zip", app_id)


# -- earnings (final) ----------------------------------------------------------------------


def _earnings(name: str, app_id: int) -> play_earnings.MappedMonth:
    text = read_zip_csv(play_bytes(name), name)
    return play_earnings.map_month(AUG, [text], {PKG: app_id}, name)


def test_earnings_normal_is_net(app_id: int) -> None:
    mapped = _earnings("earnings_202608_1234567890-0.zip", app_id)
    got = {(r.date, r.country, r.currency): round(r.value, 2) for r in mapped.rows}
    assert got == {
        ("2026-08-01", "US", "USD"): 1.00,  # dated Jul 31: kept, moved to the month's edge
        ("2026-08-03", "US", "USD"): 4.64,  # 4.99 - 0.75 fee + 0.40 tax line
        ("2026-08-17", "DE", "USD"): 8.50,  # 10.00 - 1.50 fee
    }
    assert mapped.unmapped_rows == 1 and mapped.moved_rows == 1
    # The unattributed money is reported per currency so the payout can be reconciled.
    assert "unattributed (no known package): 1 rows, USD 3.00" in (mapped.note() or "")


def test_earnings_empty_and_malformed(app_id: int) -> None:
    assert _earnings("earnings_empty.zip", app_id).rows == []
    with pytest.raises(ReportParseError, match="missing columns"):
        _earnings("earnings_malformed.zip", app_id)


def test_earnings_month_files() -> None:
    files = play_earnings.month_files(
        [
            "earnings/earnings_202608_123-0.zip",
            "earnings/earnings_202608_123-1.zip",
            "earnings/earnings_202607_123-0.zip",
            "earnings/notes.txt",
        ]
    )
    assert {m: len(n) for m, n in files.items()} == {AUG: 2, Month(2026, 7): 1}


# -- vitals --------------------------------------------------------------------------------


def test_vitals_rows_include_28_day_rates(app_id: int) -> None:
    results = {
        "crashRateMetricSet": {
            date(2026, 9, 20): {
                "userPerceivedCrashRate": 0.0142,
                "userPerceivedCrashRate28dUserWeighted": 0.0081,
                "distinctUsers": 5120.0,
            },
            date(2026, 9, 22): {"userPerceivedCrashRate28dUserWeighted": 0.0078},
        },
        "anrRateMetricSet": {
            date(2026, 9, 20): {
                "userPerceivedAnrRate": 0.0021,
                "userPerceivedAnrRate28dUserWeighted": 0.003,
            }
        },
    }
    got = {(r.date, r.metric): r.value for r in play_vitals.to_metric_rows(app_id, results)}
    assert got == {
        ("2026-09-20", "crash_rate"): 0.0142,
        ("2026-09-20", "crash_rate_28d"): 0.0081,
        ("2026-09-20", "vitals_users"): 5120.0,
        ("2026-09-22", "crash_rate_28d"): 0.0078,  # no daily rate: no crash_rate row
        ("2026-09-20", "anr_rate"): 0.0021,
        ("2026-09-20", "anr_rate_28d"): 0.003,
    }
    assert set(db.METRICS) >= {"crash_rate_28d", "anr_rate_28d", "vitals_users"}
