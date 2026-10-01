"""Unit tests for core/dashboard.py's read-only queries behind the hosted dashboard."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from storepulse.core import dashboard, db


def _apple_installs(conn: sqlite3.Connection, d: str, app_id: int, count: float) -> None:
    db.replace_source_day(
        conn, "apple_sales", d, [db.MetricRow(d, app_id, "US", "installs", "", count)]
    )


def _play_installs(conn: sqlite3.Connection, d: str, app_id: int, count: float) -> None:
    """Writes both an 'ALL' row and a per-country row, matching play_installs' own
    convention (Storage: readers use its ALL row, never add the countries to it)."""
    db.replace_source_day(
        conn,
        "play_installs",
        d,
        [
            db.MetricRow(d, app_id, "ALL", "installs", "", count),
            db.MetricRow(d, app_id, "US", "installs", "", count),
        ],
    )


def test_lifetime_installs_combines_ios_and_android(conn: sqlite3.Connection) -> None:
    ios = db.upsert_app(conn, "ios", "1", "deskFT")
    android = db.upsert_app(conn, "android", "com.x", "billFT")
    _apple_installs(conn, "2026-09-26", ios, 10.0)
    _play_installs(conn, "2026-09-26", android, 4.0)
    ios_total, android_total = dashboard.lifetime_installs(conn)
    assert (ios_total, android_total) == (10.0, 4.0)


def test_lifetime_installs_android_never_double_counts_its_all_row(
    conn: sqlite3.Connection,
) -> None:
    """_play_installs writes an 'ALL' row plus a per-country row for the same count;
    summing both would double it."""
    android = db.upsert_app(conn, "android", "com.x", "billFT")
    _play_installs(conn, "2026-09-26", android, 4.0)
    _, android_total = dashboard.lifetime_installs(conn)
    assert android_total == 4.0


def test_lifetime_installs_spans_more_than_a_windowed_series(conn: sqlite3.Connection) -> None:
    """40 days of data: installs_series(days=30) only sees the most recent 30, but
    lifetime_installs sums all 40 -- the whole point of the function."""
    ios = db.upsert_app(conn, "ios", "1", "deskFT")
    end = date(2026, 9, 26)
    for i in range(40):
        _apple_installs(conn, (end - timedelta(days=i)).isoformat(), ios, 1.0)
    series = dashboard.installs_series(conn, end, 30)
    ios_total, _ = dashboard.lifetime_installs(conn)
    assert series.total == 30.0
    assert ios_total == 40.0


def test_lifetime_installs_filtered_by_app_id(conn: sqlite3.Connection) -> None:
    a = db.upsert_app(conn, "ios", "1", "deskFT")
    b = db.upsert_app(conn, "ios", "2", "libFT")
    # Both apps' rows for the same date go in one batch: replace_source_day deletes by
    # (source, date) alone, so two separate calls for the same date would each wipe out
    # the other's row -- it replaces one source's whole day, not one app's.
    db.replace_source_day(
        conn,
        "apple_sales",
        "2026-09-26",
        [
            db.MetricRow("2026-09-26", a, "US", "installs", "", 10.0),
            db.MetricRow("2026-09-26", b, "US", "installs", "", 7.0),
        ],
    )
    ios_total, _ = dashboard.lifetime_installs(conn, [a])
    assert ios_total == 10.0
    combined, _ = dashboard.lifetime_installs(conn, [a, b])
    assert combined == 17.0


def test_lifetime_installs_zero_for_an_app_with_no_data(conn: sqlite3.Connection) -> None:
    app = db.upsert_app(conn, "ios", "1", "deskFT")
    assert dashboard.lifetime_installs(conn, [app]) == (0.0, 0.0)
    assert dashboard.lifetime_installs(conn) == (0.0, 0.0)
