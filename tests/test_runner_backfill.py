from __future__ import annotations

import logging
import sqlite3
from datetime import date, timedelta

import httpx
import pytest

from conftest import (
    FT_APPS,
    NO_SALES_DETAIL,
    NOT_READY_DETAIL,
    TODAY,
    FakeApple,
    apple_error,
    fixture_bytes,
    subscription_events_fixture_bytes,
    subscriptions_fixture_bytes,
)
from storepulse.core import db, discovery, runner
from storepulse.core.sources.apple_client import AppleAuthError, AppleClient

ROWS_PER_REPORT = 11  # see EXPECTED in test_apple_sales.py
YESTERDAY = TODAY - timedelta(days=1)


def _client(fake: FakeApple, p8_pem: str) -> AppleClient:
    return AppleClient("i", "k", p8_pem, transport=fake.transport, sleep=lambda _: None)


def _collect(
    conn: sqlite3.Connection, fake: FakeApple, p8_pem: str, dates: list[date]
) -> runner.RunSummary:
    return runner.collect_apple_sales(
        conn, _client(fake, p8_pem), "85000000", dates, delay=0, today=TODAY
    )


def _count(conn: sqlite3.Connection, day: date | None = None) -> int:
    if day is None:
        return int(conn.execute("SELECT COUNT(*) FROM daily_metrics").fetchone()[0])
    return db.count_source_day(conn, "apple_sales", day.isoformat())


def _log(conn: sqlite3.Connection, day: date) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT status, rows, error FROM ingest_log WHERE report_date = ? ORDER BY id",
        (day.isoformat(),),
    ).fetchall()


def _days(n: int, end: date = YESTERDAY) -> list[date]:
    return [end - timedelta(days=i) for i in reversed(range(n))]


@pytest.fixture
def ready_conn(conn: sqlite3.Connection) -> sqlite3.Connection:
    discovery.register_apple_apps(conn, [{**a, "bundle_id": a["bundle"]} for a in FT_APPS])
    return conn


def test_90_day_backfill_is_idempotent(ready_conn: sqlite3.Connection, p8_pem: str) -> None:
    fake = FakeApple(default_report=fixture_bytes("summary_normal.tsv.gz"))
    days = _days(90)
    first = _collect(ready_conn, fake, p8_pem, days)
    assert first.ok == 90 and not first.errors
    assert _count(ready_conn) == 90 * ROWS_PER_REPORT
    second = _collect(ready_conn, fake, p8_pem, days)
    assert second.ok == 90
    assert _count(ready_conn) == 90 * ROWS_PER_REPORT
    assert first.unknown_types == {"ZZ9": 90}
    statuses = {r["status"] for r in ready_conn.execute("SELECT status FROM ingest_log")}
    assert statuses == {"ok"}
    assert "ZZ9" in _log(ready_conn, days[0])[0]["error"]


def test_subscriptions_backfill_is_idempotent(ready_conn: sqlite3.Connection, p8_pem: str) -> None:
    fake = FakeApple(
        default_subscriptions_report=subscriptions_fixture_bytes("summary_normal.tsv.gz")
    )
    client = _client(fake, p8_pem)
    days = _days(30)
    first = runner.collect_apple_subscriptions(
        ready_conn, client, "85000000", days, delay=0, today=TODAY
    )
    assert first.ok == 30 and not first.errors
    count = int(
        ready_conn.execute(
            "SELECT COUNT(*) FROM daily_metrics WHERE source = 'apple_subscriptions'"
        ).fetchone()[0]
    )
    assert count > 0
    second = runner.collect_apple_subscriptions(
        ready_conn, client, "85000000", days, delay=0, today=TODAY
    )
    assert second.ok == 30
    assert (
        int(
            ready_conn.execute(
                "SELECT COUNT(*) FROM daily_metrics WHERE source = 'apple_subscriptions'"
            ).fetchone()[0]
        )
        == count
    )


def test_subscription_events_backfill_is_idempotent(
    ready_conn: sqlite3.Connection, p8_pem: str
) -> None:
    fake = FakeApple(
        default_subscription_events_report=subscription_events_fixture_bytes(
            "summary_normal.tsv.gz"
        )
    )
    client = _client(fake, p8_pem)
    days = _days(30)
    first = runner.collect_apple_subscription_events(
        ready_conn, client, "85000000", days, delay=0, today=TODAY
    )
    assert first.ok == 30 and not first.errors
    count = int(
        ready_conn.execute(
            "SELECT COUNT(*) FROM daily_metrics WHERE source = 'apple_subscription_events'"
        ).fetchone()[0]
    )
    assert count > 0
    second = runner.collect_apple_subscription_events(
        ready_conn, client, "85000000", days, delay=0, today=TODAY
    )
    assert second.ok == 30
    assert (
        int(
            ready_conn.execute(
                "SELECT COUNT(*) FROM daily_metrics WHERE source = 'apple_subscription_events'"
            ).fetchone()[0]
        )
        == count
    )


def test_delay_between_requests(ready_conn: sqlite3.Connection, p8_pem: str) -> None:
    fake = FakeApple(default_report=fixture_bytes("summary_empty.tsv.gz"))
    sleeps: list[float] = []
    runner.collect_apple_sales(
        ready_conn,
        _client(fake, p8_pem),
        "1",
        _days(3),
        delay=1.0,
        sleep=sleeps.append,
        today=TODAY,
    )
    assert sleeps == [1.0, 1.0]


def test_no_sales_and_not_ready_logged(ready_conn: sqlite3.Connection, p8_pem: str) -> None:
    quiet, fresh = YESTERDAY - timedelta(days=5), YESTERDAY
    fake = FakeApple(
        sales={
            quiet.isoformat(): apple_error(404, NO_SALES_DETAIL),
            fresh.isoformat(): apple_error(404, NOT_READY_DETAIL),
        }
    )
    summary = _collect(ready_conn, fake, p8_pem, [quiet, fresh])
    assert summary.no_sales == 1 and summary.not_ready == [fresh]
    assert [tuple(r) for r in _log(ready_conn, quiet)] == [("ok", 0, None)]
    assert [r["status"] for r in _log(ready_conn, fresh)] == ["not_ready"]


def test_unexplained_404_keeps_stored_rows(
    ready_conn: sqlite3.Connection, p8_pem: str, caplog: pytest.LogCaptureFixture
) -> None:
    day = YESTERDAY - timedelta(days=10)
    fake = FakeApple(default_report=fixture_bytes("summary_normal.tsv.gz"))
    _collect(ready_conn, fake, p8_pem, [day])
    assert _count(ready_conn, day) == ROWS_PER_REPORT

    # Re-pull: Apple hiccups with a 404 that doesn't say "no sales".
    fake.sales[day.isoformat()] = apple_error(404, "The specified resource does not exist")
    with caplog.at_level(logging.WARNING, logger="storepulse"):
        summary = _collect(ready_conn, fake, p8_pem, [day])
    assert _count(ready_conn, day) == ROWS_PER_REPORT
    assert summary.kept_rows == [day] and summary.inferred_no_sales == 1
    assert "kept 11 stored rows" in caplog.text
    last = _log(ready_conn, day)[-1]
    assert last["status"] == "ok" and "kept 11 stored rows" in last["error"]


def test_unexplained_404_without_data_is_inferred(
    ready_conn: sqlite3.Connection, p8_pem: str
) -> None:
    day = YESTERDAY - timedelta(days=10)
    fake = FakeApple(sales={day.isoformat(): apple_error(404, "Not found")})
    summary = _collect(ready_conn, fake, p8_pem, [day])
    assert summary.inferred_no_sales == 1 and not summary.kept_rows
    assert _log(ready_conn, day)[-1]["error"] == "no sales (inferred from date)"


def test_explicit_no_sales_clears_rows(ready_conn: sqlite3.Connection, p8_pem: str) -> None:
    day = YESTERDAY - timedelta(days=10)
    fake = FakeApple(default_report=fixture_bytes("summary_normal.tsv.gz"))
    _collect(ready_conn, fake, p8_pem, [day])
    fake.sales[day.isoformat()] = apple_error(404, NO_SALES_DETAIL)
    _collect(ready_conn, fake, p8_pem, [day])
    assert _count(ready_conn, day) == 0


def test_bad_day_does_not_stop_others(ready_conn: sqlite3.Connection, p8_pem: str) -> None:
    days = _days(3)
    fake = FakeApple(
        default_report=fixture_bytes("summary_normal.tsv.gz"),
        sales={
            days[1].isoformat(): httpx.Response(
                200, content=fixture_bytes("summary_malformed.tsv.gz")
            )
        },
    )
    summary = _collect(ready_conn, fake, p8_pem, days)
    assert summary.ok == 2 and [d for d, _ in summary.errors] == [days[1]]
    assert _log(ready_conn, days[1])[0]["status"] == "error"
    assert _count(ready_conn) == 2 * ROWS_PER_REPORT


def test_auth_error_stops_the_pass(ready_conn: sqlite3.Connection, p8_pem: str) -> None:
    fake = FakeApple(sales={YESTERDAY.isoformat(): apple_error(403, "forbidden")})
    with pytest.raises(AppleAuthError):
        _collect(ready_conn, fake, p8_pem, _days(3))
    assert _log(ready_conn, YESTERDAY)[0]["status"] == "error"


def test_range_clamped_to_retention() -> None:
    span = runner.apple_sales_range(date(2025, 1, 1), YESTERDAY, today=TODAY)
    cutoff = TODAY - timedelta(days=365)
    assert span.dates[0] == cutoff and span.dates[-1] == YESTERDAY
    assert span.skipped == (date(2025, 1, 1), cutoff - timedelta(days=1))
    assert len(span.dates) == 365


def test_range_entirely_too_old() -> None:
    span = runner.apple_sales_range(date(2024, 1, 1), date(2024, 1, 31), today=TODAY)
    assert span.dates == [] and span.skipped == (date(2024, 1, 1), date(2024, 1, 31))


def test_range_within_window_untouched() -> None:
    span = runner.apple_sales_range(YESTERDAY - timedelta(days=89), YESTERDAY, today=TODAY)
    assert len(span.dates) == 90 and span.skipped is None
    with pytest.raises(ValueError):
        runner.apple_sales_range(YESTERDAY, YESTERDAY - timedelta(days=1), today=TODAY)
