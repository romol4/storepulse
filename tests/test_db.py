from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from storepulse.core import db


def _row(country: str, value: float, metric: str = "installs") -> db.MetricRow:
    return db.MetricRow("2026-09-20", 1, country, metric, "", value)


def test_schema_created_and_versioned(conn: sqlite3.Connection) -> None:
    tables = {
        r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {"apps", "daily_metrics", "snapshots", "ingest_log", "kv"} <= tables
    assert db.get_kv(conn, "schema_version") == "2"


def test_migrations_idempotent_and_wal(tmp_path: Path) -> None:
    path = tmp_path / "sp.db"
    db.connect(path).close()
    conn = db.connect(path)
    assert db.migrate(conn) == 2
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    conn.close()


def test_unknown_metric_rejected(conn: sqlite3.Connection) -> None:
    db.upsert_app(conn, "ios", "1", "a")
    with pytest.raises(db.UnknownMetricError):
        db.replace_source_day(conn, "apple_sales", "2026-09-20", [_row("US", 1, "downloads")])


def test_bad_date_rejected(conn: sqlite3.Connection) -> None:
    db.upsert_app(conn, "ios", "1", "a")
    with pytest.raises(ValueError):
        db.replace_source_day(
            conn,
            "apple_sales",
            "09/20/2026",
            [db.MetricRow("09/20/2026", 1, "US", "installs", "", 1)],
        )


def test_repull_keeps_row_count(conn: sqlite3.Connection) -> None:
    db.upsert_app(conn, "ios", "1", "a")
    rows = [_row("US", 3), _row("GB", 2)]
    db.replace_source_day(conn, "apple_sales", "2026-09-20", rows)
    db.replace_source_day(conn, "apple_sales", "2026-09-20", rows)
    assert conn.execute("SELECT COUNT(*) FROM daily_metrics").fetchone()[0] == 2


def test_dropped_country_removed(conn: sqlite3.Connection) -> None:
    db.upsert_app(conn, "ios", "1", "a")
    db.replace_source_day(conn, "apple_sales", "2026-09-20", [_row("US", 3), _row("GB", 2)])
    db.replace_source_day(conn, "apple_sales", "2026-09-20", [_row("US", 4)])
    rows = conn.execute("SELECT country, value FROM daily_metrics").fetchall()
    assert [(r["country"], r["value"]) for r in rows] == [("US", 4.0)]


def test_replace_is_atomic(conn: sqlite3.Connection) -> None:
    db.upsert_app(conn, "ios", "1", "a")
    db.replace_source_day(conn, "apple_sales", "2026-09-20", [_row("US", 3)])
    with pytest.raises(sqlite3.IntegrityError):
        # app 99 doesn't exist: the whole replacement must roll back.
        db.replace_source_day(
            conn,
            "apple_sales",
            "2026-09-20",
            [_row("GB", 1), db.MetricRow("2026-09-20", 99, "US", "installs", "", 1)],
        )
    assert conn.execute("SELECT country FROM daily_metrics").fetchall()[0]["country"] == "US"


def test_upsert_app_is_stable(conn: sqlite3.Connection) -> None:
    first = db.upsert_app(conn, "ios", "1", "old")
    second = db.upsert_app(conn, "ios", "1", "new")
    assert first == second
    assert conn.execute("SELECT name FROM apps").fetchone()["name"] == "new"


def test_log_ingest_validates_status(conn: sqlite3.Connection) -> None:
    db.log_ingest(conn, source="s", report_date="2026-09-20", started_at=db.utc_now(), status="ok")
    with pytest.raises(ValueError):
        db.log_ingest(
            conn, source="s", report_date="2026-09-20", started_at=db.utc_now(), status="running"
        )
