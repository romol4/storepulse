from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pytest

from conftest import PKG, FakeGoogle, play_bytes, standard_objects
from storepulse.core import db, runner
from storepulse.core.sources import play_earnings, play_sales
from storepulse.core.sources.google_auth import load_service_account
from storepulse.core.sources.google_client import GoogleClient
from storepulse.core.sources.play_common import Month, last_months

BUCKET = "pubsite_prod_1234567890"
TODAY = date(2026, 9, 27)
SEP, AUG, JUL = Month(2026, 9), Month(2026, 8), Month(2026, 7)


@pytest.fixture
def app_id(conn: sqlite3.Connection) -> int:
    return db.upsert_app(conn, "android", PKG, "billFT")


def _client(sa_json: str, fake: FakeGoogle) -> GoogleClient:
    return GoogleClient(
        load_service_account(sa_json), transport=fake.transport, sleep=lambda _: None
    )


def _count(conn: sqlite3.Connection, source: str) -> int:
    return int(
        conn.execute("SELECT COUNT(*) FROM daily_metrics WHERE source = ?", (source,)).fetchone()[0]
    )


def _log(conn: sqlite3.Connection, source: str) -> list[tuple[str, str, int | None, str | None]]:
    rows = conn.execute(
        "SELECT report_date, status, rows, error FROM ingest_log WHERE source = ? ORDER BY id",
        (source,),
    )
    return [tuple(r) for r in rows]  # type: ignore[misc]


# -- installs ------------------------------------------------------------------------------


def test_installs_all_and_country_rows_survive_reruns(
    conn: sqlite3.Connection, app_id: int, sa_json: str
) -> None:
    fake = FakeGoogle(objects=standard_objects())
    client = _client(sa_json, fake)
    first = runner.collect_play_installs(conn, client, BUCKET, [SEP], today=TODAY)
    assert first.ok == 1 and first.rows == 22
    countries = {
        r["country"]
        for r in conn.execute("SELECT country FROM daily_metrics WHERE source = 'play_installs'")
    }
    assert countries == {"ALL", "US", "DE"}
    runner.collect_play_installs(conn, client, BUCKET, [SEP], today=TODAY)
    assert _count(conn, "play_installs") == 22
    all_installs = conn.execute(
        "SELECT SUM(value) FROM daily_metrics WHERE source = 'play_installs' "
        "AND country = 'ALL' AND metric = 'installs'"
    ).fetchone()[0]
    assert all_installs == 12 + 10 + 9


def test_installs_only_replaces_its_own_app(
    conn: sqlite3.Connection, app_id: int, sa_json: str
) -> None:
    other = db.upsert_app(conn, "android", "com.example.other", "other")
    db.replace_source_range(
        conn,
        "play_installs",
        "2026-09-01",
        "2026-09-30",
        [db.MetricRow("2026-09-05", other, "ALL", "installs", "", 3)],
        app_ids=[other],
    )
    fake = FakeGoogle(objects=standard_objects())
    runner.collect_play_installs(conn, _client(sa_json, fake), BUCKET, [SEP], today=TODAY)
    kept = conn.execute("SELECT COUNT(*) FROM daily_metrics WHERE app_id = ?", (other,)).fetchone()
    assert kept[0] == 1


def test_months_before_launch_are_not_logged(
    conn: sqlite3.Connection, app_id: int, sa_json: str
) -> None:
    objects = standard_objects()
    july = f"stats/installs/installs_{PKG}_202607_overview.csv"
    objects[july] = play_bytes(f"installs_{PKG}_202609_overview.csv").replace(
        "2026-09".encode("utf-16-le"), "2026-07".encode("utf-16-le")
    )
    fake = FakeGoogle(objects=objects)
    months = last_months(12, TODAY)
    summary = runner.collect_play_installs(
        conn, _client(sa_json, fake), BUCKET, months, today=TODAY
    )
    assert summary.skipped == 9  # Oct 2025 .. Jun 2026: before the first file
    assert summary.ok == 2  # July and September
    assert summary.no_file == [f"{PKG} 2026-08"]
    assert summary.not_ready == []
    logged = {r[0] for r in _log(conn, "play_installs")}
    assert logged == {"2026-07-01", "2026-08-01", "2026-09-01"}


def test_missing_current_month_is_not_ready(
    conn: sqlite3.Connection, app_id: int, sa_json: str
) -> None:
    objects = {
        f"stats/installs/installs_{PKG}_202608_overview.csv": play_bytes(
            f"installs_{PKG}_202609_overview.csv"
        ).replace("2026-09".encode("utf-16-le"), "2026-08".encode("utf-16-le"))
    }
    fake = FakeGoogle(objects=objects)
    summary = runner.collect_play_installs(
        conn, _client(sa_json, fake), BUCKET, [AUG, SEP], today=TODAY
    )
    assert summary.not_ready == [f"{PKG} 2026-09"]
    assert ("2026-09-01", "not_ready", None, None) in _log(conn, "play_installs")


def test_bad_file_is_logged_and_others_continue(
    conn: sqlite3.Connection, app_id: int, sa_json: str
) -> None:
    objects = standard_objects()
    objects[f"stats/installs/installs_{PKG}_202608_overview.csv"] = play_bytes(
        "installs_malformed_overview.csv"
    )
    fake = FakeGoogle(objects=objects)
    summary = runner.collect_play_installs(
        conn, _client(sa_json, fake), BUCKET, [AUG, SEP], today=TODAY
    )
    assert summary.ok == 1 and len(summary.errors) == 1
    assert _log(conn, "play_installs")[0][1] == "error"


# -- sales then earnings -------------------------------------------------------------------


def _aug_sales() -> bytes:
    """The September sales fixture re-dated to August."""
    import io
    import zipfile

    from storepulse.core.sources.play_common import read_zip_csv

    text = read_zip_csv(play_bytes("salesreport_202609.zip"), "x")
    text = text.replace("2026-08-31", "2026-07-31").replace("2026-09-", "2026-08-")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("salesreport_202608.csv", text)
    return buf.getvalue()


def test_provisional_sales_replaced_by_earnings(
    conn: sqlite3.Connection, app_id: int, sa_json: str
) -> None:
    objects = standard_objects()
    objects["sales/salesreport_202608.zip"] = _aug_sales()
    fake = FakeGoogle(objects=objects)
    client = _client(sa_json, fake)

    sales = runner.collect_play_sales(conn, client, BUCKET, [AUG, SEP], today=TODAY)
    assert sales.ok == 2
    assert _count(conn, "play_sales") == 6  # 3 rows per month
    earnings = runner.collect_play_earnings(conn, client, BUCKET, [AUG, SEP], today=TODAY)
    assert earnings.ok == 1  # August; September's report can't exist yet
    rows = conn.execute(
        "SELECT date, source FROM daily_metrics WHERE metric = 'proceeds' ORDER BY date"
    ).fetchall()
    august = [r["source"] for r in rows if r["date"].startswith("2026-08")]
    september = [r["source"] for r in rows if r["date"].startswith("2026-09")]
    assert set(august) == {"play_earnings"}  # provisional rows gone
    assert set(september) == {"play_sales"}  # still provisional
    assert db.get_kv(conn, play_earnings.done_key(AUG)) is not None

    # Re-running sales must not bring August's provisional rows back.
    again = runner.collect_play_sales(conn, client, BUCKET, [AUG, SEP], today=TODAY)
    assert again.already_final == ["2026-08"]
    aug_sources = {
        r["source"]
        for r in conn.execute("SELECT source FROM daily_metrics WHERE date LIKE '2026-08-%'")
    }
    assert aug_sources == {"play_earnings"}
    # Re-running earnings keeps counts stable.
    before = _count(conn, "play_earnings")
    runner.collect_play_earnings(conn, client, BUCKET, [AUG], today=TODAY)
    assert _count(conn, "play_earnings") == before == 2


def test_sales_launch_month_is_account_wide(
    conn: sqlite3.Connection, app_id: int, sa_json: str
) -> None:
    # An older sales file exists for the account even though this app's installs start
    # in September: July must still be collected, not skipped.
    objects = standard_objects()
    objects["sales/salesreport_202607.zip"] = play_bytes("salesreport_empty.zip")
    fake = FakeGoogle(objects=objects)
    summary = runner.collect_play_sales(
        conn, _client(sa_json, fake), BUCKET, [JUL, AUG, SEP], today=TODAY
    )
    assert summary.ok == 2 and summary.no_file == ["2026-08"] and summary.skipped == 0


def test_earnings_not_ready_early_in_month(
    conn: sqlite3.Connection, app_id: int, sa_json: str
) -> None:
    fake = FakeGoogle(
        objects={"earnings/earnings_202607_1-0.zip": play_bytes("earnings_empty.zip")}
    )
    early = date(2026, 9, 4)
    summary = runner.collect_play_earnings(
        conn, _client(sa_json, fake), BUCKET, [AUG, SEP], today=early
    )
    assert summary.not_ready == ["2026-08"] and summary.skipped == 1  # September not expected


def test_sales_source_constant() -> None:
    assert play_sales.SOURCE == "play_sales" and play_earnings.SOURCE == "play_earnings"


# -- vitals --------------------------------------------------------------------------------


def test_vitals_freshness_caps_and_rerun_is_stable(
    conn: sqlite3.Connection, app_id: int, sa_json: str
) -> None:
    fake = FakeGoogle()
    client = _client(sa_json, fake)
    dates = [date(2026, 9, 20) + timedelta(days=i) for i in range(5)]  # 20..24
    summary = runner.collect_play_vitals(conn, client, dates)
    assert summary.ok == 3
    assert summary.not_ready == [f"{PKG} 2026-09-23", f"{PKG} 2026-09-24"]
    count = _count(conn, "play_vitals")
    assert count == 3 * 5 - 1  # day 22 has no daily crash rate
    runner.collect_play_vitals(conn, client, dates)
    assert _count(conn, "play_vitals") == count
    metrics = {
        r["metric"]
        for r in conn.execute(
            "SELECT DISTINCT metric FROM daily_metrics WHERE source='play_vitals'"
        )
    }
    assert metrics == {"crash_rate", "crash_rate_28d", "anr_rate", "anr_rate_28d", "vitals_users"}
    statuses = [r[1] for r in _log(conn, "play_vitals")][:5]
    assert statuses == ["ok", "ok", "ok", "not_ready", "not_ready"]


def test_vitals_error_for_one_app_does_not_stop_others(
    conn: sqlite3.Connection, app_id: int, sa_json: str
) -> None:
    db.upsert_app(conn, "android", "com.example.broken", "broken")
    fake = FakeGoogle()
    summary = runner.collect_play_vitals(conn, _client(sa_json, fake), [date(2026, 9, 20)])
    assert summary.ok == 1 and [label for label, _ in summary.errors] == ["com.example.broken"]
