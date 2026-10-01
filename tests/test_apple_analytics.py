from __future__ import annotations

import json
import sqlite3
from datetime import date

import httpx
import pytest

from conftest import FT_APPS, FakeApple
from conftest import analytics_fixture_bytes as fixture_bytes
from storepulse.core import db, discovery
from storepulse.core.secrets import redact
from storepulse.core.sources import apple_analytics as analytics
from storepulse.core.sources.apple_client import AppleAuthError, AppleClient
from storepulse.core.sources.apple_sales import decode_report

DAY = date(2026, 9, 20)


def _client(fake: FakeApple, p8_pem: str) -> AppleClient:
    return AppleClient("issuer", "KEYID", p8_pem, transport=fake.transport, sleep=lambda _: None)


def _parse(name: str) -> list[analytics.AnalyticsRow]:
    return analytics.parse_segment(decode_report(fixture_bytes(name)))


def _totals(conn: sqlite3.Connection, rows: list[db.MetricRow]) -> dict[tuple[str, ...], float]:
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM apps")}
    return {(names[r.app_id], r.country, r.metric): round(r.value, 2) for r in rows}


EXPECTED = {
    ("deskFT", "US", "impressions"): 1200,
    ("deskFT", "US", "page_views"): 340,
    ("deskFT", "GB", "impressions"): 300,
    ("deskFT", "GB", "page_views"): 95,
    ("deskFT", "ALL", "impressions"): 1500,  # 1200 + 300
    ("deskFT", "ALL", "page_views"): 435,  # 340 + 95
    ("billFT", "US", "impressions"): 800,
    ("billFT", "US", "page_views"): 210,
    ("billFT", "ZZ", "impressions"): 150,
    ("billFT", "ZZ", "page_views"): 40,
    ("billFT", "ALL", "impressions"): 950,  # 800 + 150
    ("billFT", "ALL", "page_views"): 250,  # 210 + 40
}


# -- parser ------------------------------------------------------------------------------


def test_parse_normal_file() -> None:
    rows = _parse("segment_normal.tsv.gz")
    assert len(rows) == 5
    first = rows[0]
    assert (first.apple_id, first.country, first.impressions, first.page_views) == (
        "1000000001",
        "US",
        1200,
        340,
    )


def test_parse_empty_file() -> None:
    assert _parse("segment_empty.tsv.gz") == []


def test_parse_malformed_file() -> None:
    with pytest.raises(analytics.ReportParseError, match="Territory"):
        _parse("segment_malformed.tsv.gz")


def test_parse_rejects_blank_and_non_numeric() -> None:
    with pytest.raises(analytics.ReportParseError):
        analytics.parse_segment("")
    header = "\t".join(analytics.REQUIRED_COLUMNS)
    bad = "1000000001\tUS\tmany\t0"
    with pytest.raises(analytics.ReportParseError, match="not a number"):
        analytics.parse_segment(f"{header}\n{bad}\n")


# -- mapping -----------------------------------------------------------------------------


def _discovered(conn: sqlite3.Connection) -> None:
    discovery.register_apple_apps(conn, [{**a, "bundle_id": a["bundle"]} for a in FT_APPS])


def test_mapping_exact_totals(conn: sqlite3.Connection) -> None:
    _discovered(conn)
    mapped = analytics.map_rows(conn, DAY, _parse("segment_normal.tsv.gz"))
    assert _totals(conn, mapped.rows) == EXPECTED
    assert all(r.date == "2026-09-20" for r in mapped.rows)
    assert all(r.currency == "" for r in mapped.rows)


def test_all_row_written_once_per_metric(conn: sqlite3.Connection) -> None:
    _discovered(conn)
    mapped = analytics.map_rows(conn, DAY, _parse("segment_normal.tsv.gz"))
    desk_id = db.find_app(conn, "ios", "1000000001")
    all_metrics = {r.metric for r in mapped.rows if r.app_id == desk_id and r.country == "ALL"}
    assert all_metrics == {"impressions", "page_views"}


def test_unmapped_rows_counted(conn: sqlite3.Connection) -> None:
    _discovered(conn)
    mapped = analytics.map_rows(conn, DAY, _parse("segment_normal.tsv.gz"))
    assert mapped.unmapped_rows == 1  # the app not yet known to Storepulse
    assert mapped.note() is not None and "no matching app" in mapped.note()  # type: ignore[operator]


def test_no_app_is_ever_auto_registered(conn: sqlite3.Connection) -> None:
    # Unlike apple_sales' app rows, this source never adds a new app (docs/SPEC.md).
    mapped = analytics.map_rows(conn, DAY, _parse("segment_normal.tsv.gz"))
    assert mapped.rows == []
    assert mapped.unmapped_rows == 5
    assert conn.execute("SELECT COUNT(*) AS n FROM apps").fetchone()["n"] == 0


def test_empty_report_maps_to_nothing(conn: sqlite3.Connection) -> None:
    mapped = analytics.map_rows(conn, DAY, _parse("segment_empty.tsv.gz"))
    assert mapped.rows == [] and mapped.note() is None


# -- report request setup: idempotency and the role-requirement 403 ---------------------


def test_ensure_report_request_creates_once(conn: sqlite3.Connection, p8_pem: str) -> None:
    fake = FakeApple()
    client = _client(fake, p8_pem)
    first = analytics.ensure_report_request(client, conn, "1000000001")
    second = analytics.ensure_report_request(client, conn, "1000000001")
    assert first == second
    assert len(fake.analytics_requests_created()) == 1


def test_ensure_report_request_sends_the_documented_body(
    conn: sqlite3.Connection, p8_pem: str
) -> None:
    fake = FakeApple()
    client = _client(fake, p8_pem)
    analytics.ensure_report_request(client, conn, "1000000001")
    created = fake.analytics_requests_created()
    assert len(created) == 1
    body = json.loads(created[0].content)
    assert body["data"]["type"] == "analyticsReportRequests"
    assert body["data"]["attributes"]["accessType"] == "ONGOING"
    assert body["data"]["relationships"]["app"]["data"] == {"type": "apps", "id": "1000000001"}


def test_ensure_report_request_adopts_an_existing_ongoing_request(
    conn: sqlite3.Connection, p8_pem: str
) -> None:
    """A reset kv table, or a request created by hand in App Store Connect, must never
    produce a duplicate POST: existing requests are listed and adopted first."""
    fake = FakeApple(
        analytics_requests={"1000000001": [{"id": "req-existing", "accessType": "ONGOING"}]}
    )
    client = _client(fake, p8_pem)
    request_id = analytics.ensure_report_request(client, conn, "1000000001")
    assert request_id == "req-existing"
    assert fake.analytics_requests_created() == []
    assert db.get_kv(conn, analytics.request_key("1000000001")) == "req-existing"


def test_ensure_report_request_ignores_a_non_ongoing_request(
    conn: sqlite3.Connection, p8_pem: str
) -> None:
    fake = FakeApple(
        analytics_requests={
            "1000000001": [{"id": "req-snapshot", "accessType": "ONE_TIME_SNAPSHOT"}]
        }
    )
    client = _client(fake, p8_pem)
    request_id = analytics.ensure_report_request(client, conn, "1000000001")
    assert request_id != "req-snapshot"
    assert len(fake.analytics_requests_created()) == 1


def test_create_report_request_403_names_the_role_requirement(
    conn: sqlite3.Connection, p8_pem: str
) -> None:
    fake = FakeApple(create_request_status=403)
    client = _client(fake, p8_pem)
    with pytest.raises(AppleAuthError, match="App Manager or Admin"):
        analytics.ensure_report_request(client, conn, "1000000001")


# -- report / instance / segment discovery ------------------------------------------------


def test_find_discovery_engagement_report_caches_via_kv(
    conn: sqlite3.Connection, p8_pem: str
) -> None:
    fake = FakeApple(
        analytics_reports={
            "req1": [
                {"id": "rep-other", "name": "App Store Downloads Report"},
                {"id": "rep-discovery", "name": analytics.REPORT_NAME},
            ]
        }
    )
    client = _client(fake, p8_pem)
    report_id = analytics.find_discovery_engagement_report(client, conn, "req1")
    assert report_id == "rep-discovery"
    assert db.get_kv(conn, analytics.report_key("req1")) == "rep-discovery"

    analytics.find_discovery_engagement_report(client, conn, "req1")
    listings = [
        r for r in fake.requests if r.url.path == "/v1/analyticsReportRequests/req1/reports"
    ]
    assert len(listings) == 1  # the second call was served from kv, not listed again


def test_find_discovery_engagement_report_not_ready_returns_none(
    conn: sqlite3.Connection, p8_pem: str
) -> None:
    fake = FakeApple(analytics_reports={"req1": []})
    client = _client(fake, p8_pem)
    assert analytics.find_discovery_engagement_report(client, conn, "req1") is None


def test_list_instances_filters_by_granularity_and_parses_dates(p8_pem: str) -> None:
    fake = FakeApple(
        analytics_instances={
            "rep1": [
                {"id": "inst1", "processingDate": "2026-09-19"},
                {"id": "inst2", "processingDate": "2026-09-20"},
            ]
        }
    )
    client = _client(fake, p8_pem)
    instances = analytics.list_instances(client, "rep1")
    assert [(i.id, i.processing_date) for i in instances] == [
        ("inst1", date(2026, 9, 19)),
        ("inst2", date(2026, 9, 20)),
    ]
    assert fake.requests[-1].url.params["filter[granularity]"] == "DAILY"


def test_fetch_segments_downloads_each_url(p8_pem: str) -> None:
    content = fixture_bytes("segment_normal.tsv.gz")
    fake = FakeApple(
        analytics_segments={"inst1": ["/v1/analyticsReportSegments/inst1/download/0"]},
        segment_content={"/v1/analyticsReportSegments/inst1/download/0": content},
    )
    client = _client(fake, p8_pem)
    assert analytics.fetch_segments(client, "inst1") == [content]


def test_fetch_segments_sends_no_apple_credential_to_an_absolute_url(
    monkeypatch: pytest.MonkeyPatch, p8_pem: str
) -> None:
    """An absolute segment url is presumed pre-signed (its own signature is the
    credential): it must be fetched plain, never carrying this app's separate Apple API
    bearer token to a host that never asked for it."""
    content = fixture_bytes("segment_normal.tsv.gz")
    fake = FakeApple(analytics_segments={"inst1": ["https://cdn.example.com/segments/abc?sig=xyz"]})
    client = _client(fake, p8_pem)
    captured: dict[str, object] = {}

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        captured["url"] = url
        captured["kwargs"] = kwargs
        return httpx.Response(200, content=content, request=httpx.Request("GET", url))

    monkeypatch.setattr(analytics.httpx, "get", fake_get)
    assert analytics.fetch_segments(client, "inst1") == [content]
    assert captured["url"] == "https://cdn.example.com/segments/abc?sig=xyz"
    assert "headers" not in captured["kwargs"]  # type: ignore[operator]
    # Never reached the Apple-authenticated fake transport at all.
    assert not any(r.url.path.startswith("/v1/analyticsReportSegments") for r in fake.requests)


def test_fetch_segments_registers_an_absolute_url_so_a_failure_is_redactable(
    monkeypatch: pytest.MonkeyPatch, p8_pem: str
) -> None:
    """A pre-signed segment URL's query string is itself a credential: if the download
    fails, httpx's own exception message embeds the full URL, which would otherwise
    reach ingest_log's plaintext error column unredacted (docs/SPEC.md, Storage:
    secrets never appear in the database's plaintext columns)."""
    url = "https://cdn.example.com/segments/abc?sig=super-secret-signature-value"
    fake = FakeApple(analytics_segments={"inst1": [url]})
    client = _client(fake, p8_pem)

    def raise_http_error(target: str, **kwargs: object) -> httpx.Response:
        request = httpx.Request("GET", target)
        raise httpx.HTTPStatusError(
            f"Client error '403 Forbidden' for url '{target}'",
            request=request,
            response=httpx.Response(403, request=request),
        )

    monkeypatch.setattr(analytics.httpx, "get", raise_http_error)
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        analytics.fetch_segments(client, "inst1")
    message = str(exc_info.value)
    assert url in message  # the scenario is real: httpx really does embed the full url
    assert url not in redact(message)
