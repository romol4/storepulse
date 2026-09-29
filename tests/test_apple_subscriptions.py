from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from conftest import FT_APPS, NO_SALES_DETAIL, NOT_READY_DETAIL, TODAY, FakeApple, apple_error
from conftest import subscriptions_fixture_bytes as fixture_bytes
from storepulse.core import db, discovery
from storepulse.core.sources import apple_subscriptions as subs
from storepulse.core.sources.apple_client import AppleClient, AppleVendorError
from storepulse.core.sources.apple_sales import decode_report

DAY = date(2026, 9, 20)


def _parse(name: str) -> list[subs.SubscriptionRow]:
    return subs.parse_summary(decode_report(fixture_bytes(name)))


def _totals(conn: sqlite3.Connection, rows: list[db.MetricRow]) -> dict[tuple[str, ...], float]:
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM apps")}
    return {(names[r.app_id], r.country, r.metric): round(r.value, 2) for r in rows}


EXPECTED = {
    ("deskFT", "US", "active_subscriptions"): 108,
    ("deskFT", "US", "active_trials"): 20,
    ("deskFT", "GB", "active_subscriptions"): 37,
    ("deskFT", "ZZ", "active_subscriptions"): 9,
    ("deskFT", "ALL", "active_subscriptions"): 154,  # 108 + 37 + 9
    ("deskFT", "ALL", "active_trials"): 20,
    ("billFT", "US", "active_subscriptions"): 52,
    ("billFT", "US", "active_trials"): 9,
    ("billFT", "ALL", "active_subscriptions"): 52,
    ("billFT", "ALL", "active_trials"): 9,
}


# -- parser ------------------------------------------------------------------------------


def test_parse_normal_file() -> None:
    rows = _parse("summary_normal.tsv.gz")
    assert len(rows) == 6
    first = rows[0]
    assert (first.apple_id, first.country, first.active_subscriptions, first.active_trials) == (
        "1000000001",
        "US",
        108,
        0,
    )


def test_parse_empty_file() -> None:
    assert _parse("summary_empty.tsv.gz") == []


def test_parse_malformed_file() -> None:
    with pytest.raises(subs.ReportParseError, match="Country"):
        _parse("summary_malformed.tsv.gz")


def test_parse_rejects_blank_and_non_numeric() -> None:
    with pytest.raises(subs.ReportParseError):
        subs.parse_summary("")
    header = "\t".join(subs.REQUIRED_COLUMNS)
    bad = "\t".join(["1000000001", "US", "many"] + ["0"] * (len(subs.REQUIRED_COLUMNS) - 3))
    with pytest.raises(subs.ReportParseError, match="not a number"):
        subs.parse_summary(f"{header}\n{bad}\n")


# -- mapping -----------------------------------------------------------------------------


def _discovered(conn: sqlite3.Connection) -> None:
    discovery.register_apple_apps(conn, [{**a, "bundle_id": a["bundle"]} for a in FT_APPS])


def test_mapping_exact_totals(conn: sqlite3.Connection) -> None:
    _discovered(conn)
    mapped = subs.map_rows(conn, DAY, _parse("summary_normal.tsv.gz"))
    assert _totals(conn, mapped.rows) == EXPECTED
    assert all(r.date == "2026-09-20" for r in mapped.rows)
    assert all(r.currency == "" for r in mapped.rows)


def test_all_row_written_once_per_metric(conn: sqlite3.Connection) -> None:
    _discovered(conn)
    mapped = subs.map_rows(conn, DAY, _parse("summary_normal.tsv.gz"))
    desk_id = db.find_app(conn, "ios", "1000000001")
    all_metrics = {r.metric for r in mapped.rows if r.app_id == desk_id and r.country == "ALL"}
    assert all_metrics == {"active_subscriptions", "active_trials"}


def test_unmapped_rows_counted(conn: sqlite3.Connection) -> None:
    _discovered(conn)
    mapped = subs.map_rows(conn, DAY, _parse("summary_normal.tsv.gz"))
    assert mapped.unmapped_rows == 1  # the app not yet known to Storepulse
    assert mapped.note() is not None and "no matching app" in mapped.note()  # type: ignore[operator]


def test_no_app_is_ever_auto_registered(conn: sqlite3.Connection) -> None:
    # Unlike apple_sales' app rows, this source never adds a new app (docs/SPEC.md).
    mapped = subs.map_rows(conn, DAY, _parse("summary_normal.tsv.gz"))
    assert mapped.rows == []
    assert mapped.unmapped_rows == 6
    assert conn.execute("SELECT COUNT(*) AS n FROM apps").fetchone()["n"] == 0


def test_empty_report_maps_to_nothing(conn: sqlite3.Connection) -> None:
    mapped = subs.map_rows(conn, DAY, _parse("summary_empty.tsv.gz"))
    assert mapped.rows == [] and mapped.note() is None


# -- fetch / 404 classification ------------------------------------------------------------


def _client(fake: FakeApple, p8_pem: str) -> AppleClient:
    return AppleClient("issuer", "KEYID", p8_pem, transport=fake.transport, sleep=lambda _: None)


def test_fetch_day_ok_sends_filters(p8_pem: str) -> None:
    fake = FakeApple(default_subscriptions_report=fixture_bytes("summary_normal.tsv.gz"))
    result = subs.fetch_day(_client(fake, p8_pem), "85000000", DAY, today=TODAY)
    assert result.status == "ok" and result.content
    params = fake.requests[0].url.params
    assert params["filter[frequency]"] == "DAILY"
    assert params["filter[reportType]"] == "SUBSCRIPTION"
    assert params["filter[reportSubType]"] == "SUMMARY"
    assert params["filter[version]"] == "1_3"
    assert params["filter[vendorNumber]"] == "85000000"
    assert params["filter[reportDate]"] == "2026-09-20"


def test_fetch_day_is_a_distinct_request_from_sales(p8_pem: str) -> None:
    """Same /v1/salesReports path as apple_sales; only filter[reportType] tells them apart."""
    fake = FakeApple(default_subscriptions_report=fixture_bytes("summary_normal.tsv.gz"))
    subs.fetch_day(_client(fake, p8_pem), "1", DAY, TODAY)
    # dates_for (unlike sales_dates) is scoped by filter[reportType].
    assert fake.dates_for("SUBSCRIPTION") == ["2026-09-20"]
    assert fake.dates_for("SALES") == []


def test_fetch_day_404s(p8_pem: str) -> None:
    fake = FakeApple(
        subscriptions={
            "2026-09-20": apple_error(404, NO_SALES_DETAIL),
            "2026-09-26": apple_error(404, NOT_READY_DETAIL),
            "2026-09-10": apple_error(404, "Not found"),
        }
    )
    client = _client(fake, p8_pem)
    assert subs.fetch_day(client, "1", DAY, TODAY).status == "no_sales"
    assert subs.fetch_day(client, "1", date(2026, 9, 26), TODAY).status == "not_ready"
    assert subs.fetch_day(client, "1", date(2026, 9, 10), TODAY).status == "no_sales_inferred"


@pytest.mark.parametrize("status", [400, 403])
def test_fetch_day_vendor_error(p8_pem: str, status: int) -> None:
    fake = FakeApple(
        subscriptions={"2026-09-20": apple_error(status, "Invalid vendor number specified")}
    )
    with pytest.raises(AppleVendorError, match="vendor number"):
        subs.fetch_day(_client(fake, p8_pem), "1", DAY, TODAY)
