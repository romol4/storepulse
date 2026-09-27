from __future__ import annotations

import sqlite3
from datetime import date

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
)
from storepulse.core import db, discovery
from storepulse.core.sources import apple_sales
from storepulse.core.sources.apple_client import AppleClient, AppleVendorError

DAY = date(2026, 9, 20)


def _parse(name: str) -> list[apple_sales.SalesRow]:
    return apple_sales.parse_summary(apple_sales.decode_report(fixture_bytes(name)))


def _totals(conn: sqlite3.Connection, rows: list[db.MetricRow]) -> dict[tuple[str, ...], float]:
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM apps")}
    return {(names[r.app_id], r.country, r.metric, r.currency): round(r.value, 2) for r in rows}


EXPECTED = {
    ("deskFT", "US", "installs", ""): 10,
    ("deskFT", "GB", "installs", ""): 3,
    ("deskFT", "US", "redownloads", ""): 4,
    ("deskFT", "US", "iap_units", ""): 1,  # 2 purchases, 1 refund
    ("deskFT", "CA", "iap_units", ""): 1,
    ("deskFT", "US", "proceeds", "USD"): 6.99,
    ("deskFT", "CA", "proceeds", "CAD"): 3.49,
    ("billFT", "DE", "installs", ""): 5,
    ("billFT", "US", "installs", ""): 2,
    ("billFT", "DE", "proceeds", "EUR"): 3.50,
    ("billFT", "US", "proceeds", "USD"): 3.30,  # 2.80 app + 0.50 from the unknown code
}


# -- parser ------------------------------------------------------------------------------


def test_parse_normal_file() -> None:
    rows = _parse("summary_normal.tsv.gz")
    assert len(rows) == 12
    first = rows[0]
    assert (first.product_type, first.units, first.country, first.currency) == (
        "1F",
        10,
        "US",
        "USD",
    )
    assert rows[4].parent_id == "DESKFT"


def test_parse_empty_file() -> None:
    assert _parse("summary_empty.tsv.gz") == []


def test_parse_malformed_file() -> None:
    with pytest.raises(apple_sales.ReportParseError, match="Units"):
        _parse("summary_malformed.tsv.gz")


def test_parse_rejects_blank_and_non_numeric() -> None:
    with pytest.raises(apple_sales.ReportParseError):
        apple_sales.parse_summary("")
    header = "\t".join(apple_sales.REQUIRED_COLUMNS)
    bad = "\t".join(["S", "T", "1", "many", "0", "US", "USD", "1", ""])
    with pytest.raises(apple_sales.ReportParseError, match="Units"):
        apple_sales.parse_summary(f"{header}\n{bad}\n")


def test_decode_rejects_corrupt_gzip() -> None:
    with pytest.raises(apple_sales.ReportParseError):
        apple_sales.decode_report(b"\x1f\x8bnot gzip")


# -- mapping -----------------------------------------------------------------------------


def _discovered(conn: sqlite3.Connection) -> None:
    discovery.register_apple_apps(conn, [{**a, "bundle_id": a["bundle"]} for a in FT_APPS])


def test_mapping_exact_totals(conn: sqlite3.Connection) -> None:
    _discovered(conn)
    mapped = apple_sales.map_rows(conn, DAY, _parse("summary_normal.tsv.gz"))
    assert _totals(conn, mapped.rows) == EXPECTED
    assert all(r.date == "2026-09-20" for r in mapped.rows)
    assert not any(r.country == "ALL" for r in mapped.rows)


def test_updates_are_dropped(conn: sqlite3.Connection) -> None:
    _discovered(conn)
    mapped = apple_sales.map_rows(conn, DAY, _parse("summary_normal.tsv.gz"))
    # The update row has 50 units; nothing that large should appear anywhere.
    assert all(r.value != 50 for r in mapped.rows)
    assert "7" not in mapped.unknown_types


def test_unknown_product_type_is_counted(conn: sqlite3.Connection) -> None:
    _discovered(conn)
    mapped = apple_sales.map_rows(conn, DAY, _parse("summary_normal.tsv.gz"))
    assert mapped.unknown_types == {"ZZ9": 1}
    assert mapped.unknown_units == {"ZZ9": 1}
    assert mapped.note() is not None and "ZZ9" in mapped.note()  # type: ignore[operator]


def test_unmapped_rows_counted(conn: sqlite3.Connection) -> None:
    _discovered(conn)
    mapped = apple_sales.map_rows(conn, DAY, _parse("summary_normal.tsv.gz"))
    # The app bundle (unknown id) and the IAP whose parent SKU is unknown.
    assert mapped.unmapped_rows == 2


def test_iap_resolves_via_parent_sku(conn: sqlite3.Connection) -> None:
    _discovered(conn)
    assert db.get_kv(conn, "apple_sku:DESKFT") == "1000000001"
    mapped = apple_sales.map_rows(conn, DAY, _parse("summary_normal.tsv.gz"))
    iap = [r for r in mapped.rows if r.metric == "iap_units"]
    assert {r.app_id for r in iap} == {db.find_app(conn, "ios", "1000000001")}


def test_auto_adds_apps_from_app_rows_only(conn: sqlite3.Connection) -> None:
    # No discovery: apps come from the report's app rows.
    mapped = apple_sales.map_rows(conn, DAY, _parse("summary_normal.tsv.gz"))
    names = sorted(r["name"] for r in conn.execute("SELECT name FROM apps"))
    assert names == ["billFT", "deskFT"]  # never "deskFT Pro", "Other Pack" or the bundle
    assert sorted(mapped.new_apps) == ["billFT", "deskFT"]
    assert db.get_kv(conn, "apple_sku:DESKFT") == "1000000001"
    assert db.get_kv(conn, "apple_sku:OTHERAPP") is None
    assert _totals(conn, mapped.rows) == EXPECTED


def test_empty_report_maps_to_nothing(conn: sqlite3.Connection) -> None:
    mapped = apple_sales.map_rows(conn, DAY, _parse("summary_empty.tsv.gz"))
    assert mapped.rows == [] and mapped.note() is None


# -- fetch / 404 classification ------------------------------------------------------------


@pytest.mark.parametrize(
    ("detail", "days_ago", "expected"),
    [
        (NO_SALES_DETAIL, 1, "no_sales"),
        (NO_SALES_DETAIL, 30, "no_sales"),
        (NOT_READY_DETAIL, 1, "not_ready"),
        (NOT_READY_DETAIL, 30, "not_ready"),
        ("The specified resource does not exist", 1, "not_ready"),
        ("The specified resource does not exist", 2, "not_ready"),
        ("The specified resource does not exist", 3, "no_sales_inferred"),
        ("", 10, "no_sales_inferred"),
    ],
)
def test_classify_404(detail: str, days_ago: int, expected: str) -> None:
    day = date.fromordinal(TODAY.toordinal() - days_ago)
    assert apple_sales.classify_404(detail, day, today=TODAY) == expected


def _client(fake: FakeApple, p8_pem: str) -> AppleClient:
    return AppleClient("issuer", "KEYID", p8_pem, transport=fake.transport, sleep=lambda _: None)


def test_fetch_day_ok_sends_filters(p8_pem: str) -> None:
    fake = FakeApple(default_report=fixture_bytes("summary_normal.tsv.gz"))
    result = apple_sales.fetch_day(_client(fake, p8_pem), "85000000", DAY, today=TODAY)
    assert result.status == "ok" and result.content
    params = fake.requests[0].url.params
    assert params["filter[frequency]"] == "DAILY"
    assert params["filter[reportType]"] == "SALES"
    assert params["filter[reportSubType]"] == "SUMMARY"
    assert params["filter[vendorNumber]"] == "85000000"
    assert params["filter[reportDate]"] == "2026-09-20"


def test_fetch_day_404s(p8_pem: str) -> None:
    fake = FakeApple(
        sales={
            "2026-09-20": apple_error(404, NO_SALES_DETAIL),
            "2026-09-26": apple_error(404, NOT_READY_DETAIL),
            "2026-09-10": apple_error(404, "Not found"),
        }
    )
    client = _client(fake, p8_pem)
    assert apple_sales.fetch_day(client, "1", DAY, TODAY).status == "no_sales"
    assert apple_sales.fetch_day(client, "1", date(2026, 9, 26), TODAY).status == "not_ready"
    assert (
        apple_sales.fetch_day(client, "1", date(2026, 9, 10), TODAY).status == "no_sales_inferred"
    )


@pytest.mark.parametrize("status", [400, 403])
def test_fetch_day_vendor_error(p8_pem: str, status: int) -> None:
    fake = FakeApple(sales={"2026-09-20": apple_error(status, "Invalid vendor number specified")})
    with pytest.raises(AppleVendorError, match="vendor number"):
        apple_sales.fetch_day(_client(fake, p8_pem), "1", DAY, TODAY)


def test_fetch_day_other_400_is_plain_error(p8_pem: str) -> None:
    from storepulse.core.sources.apple_client import AppleError

    fake = FakeApple(sales={"2026-09-20": httpx.Response(400)})
    with pytest.raises(AppleError) as info:
        apple_sales.fetch_day(_client(fake, p8_pem), "1", DAY, TODAY)
    assert not isinstance(info.value, AppleVendorError)
