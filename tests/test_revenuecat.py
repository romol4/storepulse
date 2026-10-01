from __future__ import annotations

import sqlite3
from datetime import date

import httpx
import pytest

from conftest import REVENUECAT_PROJECT, FakeRevenueCat, revenuecat_overview
from storepulse.core.checks import check_revenuecat
from storepulse.core.runner import collect_revenuecat
from storepulse.core.sources import revenuecat
from storepulse.core.sources.revenuecat import (
    RevenueCatAuthError,
    RevenueCatClient,
    RevenueCatError,
    RevenueCatParseError,
)


def _client(fake: FakeRevenueCat, sleep: object = None) -> RevenueCatClient:
    return RevenueCatClient(
        "sk_test_123", REVENUECAT_PROJECT, transport=fake.transport, sleep=sleep or (lambda _: None)
    )


# -- parser: normal, empty, malformed (CLAUDE.md's three required cases) --------------------


def test_parse_overview_normal_file() -> None:
    body = revenuecat_overview(mrr=1234.5, active_subscriptions=42, active_trials=3, revenue=5000.0)
    metrics = revenuecat.parse_overview(body)
    assert {(m.id, m.value, m.currency) for m in metrics} == {
        ("mrr", 1234.5, "USD"),
        ("active_subscriptions", 42.0, "USD"),  # currency is unused for count metrics
        ("active_trials", 3.0, "USD"),
        ("revenue", 5000.0, "USD"),
    }


def test_parse_overview_honors_an_explicit_currency() -> None:
    body = revenuecat_overview(mrr=100.0, currency="EUR")
    metrics = revenuecat.parse_overview(body)
    mrr = next(m for m in metrics if m.id == "mrr")
    assert mrr.currency == "EUR"


def test_parse_overview_ignores_unknown_metrics() -> None:
    body = revenuecat_overview(extra=[{"id": "active_users", "value": 999}])
    metrics = revenuecat.parse_overview(body)
    assert "active_users" not in {m.id for m in metrics}
    assert len(metrics) == 4


def test_parse_overview_empty_file() -> None:
    assert revenuecat.parse_overview({"object": "list", "items": []}) == []


def test_parse_overview_malformed_missing_items() -> None:
    with pytest.raises(RevenueCatParseError, match="items"):
        revenuecat.parse_overview({"object": "list"})


def test_parse_overview_malformed_non_numeric_value() -> None:
    body = {"object": "list", "items": [{"id": "mrr", "value": "a lot"}]}
    with pytest.raises(RevenueCatParseError, match="non-numeric"):
        revenuecat.parse_overview(body)


def test_parse_overview_rejects_a_boolean_value() -> None:
    # bool is a subclass of int in Python; a stray True/False must not silently become 1.0/0.0.
    body = {"object": "list", "items": [{"id": "mrr", "value": True}]}
    with pytest.raises(RevenueCatParseError):
        revenuecat.parse_overview(body)


def test_parse_overview_skips_non_dict_items() -> None:
    body = {"object": "list", "items": ["garbage", {"id": "mrr", "value": 1.0}]}
    metrics = revenuecat.parse_overview(body)
    assert [m.id for m in metrics] == ["mrr"]


# -- mapping ----------------------------------------------------------------------------------


def test_map_overview_money_metrics_carry_currency_count_metrics_dont() -> None:
    metrics = revenuecat.parse_overview(revenuecat_overview())
    rows = revenuecat.map_overview(metrics, taken_at="2026-09-20T10:00:00+00:00")
    by_metric = {r.metric: r for r in rows}
    assert by_metric["mrr"].currency == "USD"
    assert by_metric["revenue"].currency == "USD"
    assert by_metric["active_subscriptions"].currency == ""
    assert by_metric["active_trials"].currency == ""
    assert all(r.app_id is None for r in rows)
    assert all(r.taken_at == "2026-09-20T10:00:00+00:00" for r in rows)


def test_map_overview_empty_metrics_maps_to_nothing() -> None:
    assert revenuecat.map_overview([], taken_at="2026-09-20T10:00:00+00:00") == []


# -- client: auth, retries, backoff ----------------------------------------------------------


def test_fetch_overview_success() -> None:
    fake = FakeRevenueCat(default_overview=revenuecat_overview())
    client = _client(fake)
    body = client.fetch_overview()
    metrics = revenuecat.parse_overview(body)
    assert len(metrics) == 4
    assert fake.requests[0].headers["authorization"] == "Bearer sk_test_123"


def test_fetch_overview_401_is_not_retried() -> None:
    fake = FakeRevenueCat(
        overview={REVENUECAT_PROJECT: httpx.Response(401, json={"message": "invalid key"})}
    )
    client = _client(fake)
    with pytest.raises(RevenueCatAuthError, match="revoked"):
        client.fetch_overview()
    assert len(fake.requests) == 1


def test_fetch_overview_404_names_the_project_id() -> None:
    fake = FakeRevenueCat()  # no default_overview and nothing registered -> 404
    client = _client(fake)
    with pytest.raises(RevenueCatAuthError, match="project ID"):
        client.fetch_overview()
    assert len(fake.requests) == 1


def test_fetch_overview_retries_429_then_succeeds() -> None:
    attempts = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 2:
            return httpx.Response(429)
        return httpx.Response(200, json=revenuecat_overview())

    client = RevenueCatClient(
        "sk_test_123",
        REVENUECAT_PROJECT,
        transport=httpx.MockTransport(flaky),
        sleep=lambda _: None,
    )
    body = client.fetch_overview()
    assert attempts["n"] == 2
    assert len(revenuecat.parse_overview(body)) == 4


def test_fetch_overview_persistent_500_gives_up_after_max_attempts() -> None:
    def always_500(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = RevenueCatClient(
        "sk_test_123",
        REVENUECAT_PROJECT,
        transport=httpx.MockTransport(always_500),
        sleep=lambda _: None,
    )
    with pytest.raises(RevenueCatError, match="HTTP 500"):
        client.fetch_overview()


def test_fetch_overview_network_error_retries_then_raises() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = RevenueCatClient(
        "sk_test_123", REVENUECAT_PROJECT, transport=httpx.MockTransport(boom), sleep=lambda _: None
    )
    with pytest.raises(RevenueCatError, match="could not reach RevenueCat"):
        client.fetch_overview()


# -- check_revenuecat (shared by the CLI wizard and the web setup flow) ----------------------


def test_check_revenuecat_ok() -> None:
    fake = FakeRevenueCat(default_overview=revenuecat_overview())
    result = check_revenuecat(_client(fake))
    assert result.checks[0].level == "ok"
    assert "4 metric(s)" in result.checks[0].text


def test_check_revenuecat_reports_a_revoked_key() -> None:
    fake = FakeRevenueCat(
        overview={REVENUECAT_PROJECT: httpx.Response(401, json={"message": "invalid"})}
    )
    result = check_revenuecat(_client(fake))
    assert result.failed and "revoked" in result.failed[0].text


def test_check_revenuecat_reports_a_malformed_response() -> None:
    fake = FakeRevenueCat(default_overview={"object": "list"})  # no 'items'
    result = check_revenuecat(_client(fake))
    assert result.failed


# -- collection: writes a snapshot and logs ingest -------------------------------------------


def test_collect_revenuecat_writes_a_snapshot_and_logs_ok(conn: sqlite3.Connection) -> None:
    fake = FakeRevenueCat(default_overview=revenuecat_overview())
    today = date(2026, 9, 27)
    summary = collect_revenuecat(conn, _client(fake), today)
    assert summary.ok == 1 and summary.rows == 4 and summary.error is None
    assert conn.execute("SELECT COUNT(*) AS n FROM snapshots").fetchone()["n"] == 4
    ingest = conn.execute(
        "SELECT status, report_date, rows FROM ingest_log WHERE source = 'revenuecat'"
    ).fetchone()
    assert (ingest["status"], ingest["report_date"], ingest["rows"]) == ("ok", "2026-09-27", 4)


def test_collect_revenuecat_logs_error_and_never_raises(conn: sqlite3.Connection) -> None:
    fake = FakeRevenueCat(
        overview={REVENUECAT_PROJECT: httpx.Response(401, json={"message": "invalid"})}
    )
    today = date(2026, 9, 27)
    summary = collect_revenuecat(conn, _client(fake), today)
    assert summary.error is not None and "revoked" in summary.error
    ingest = conn.execute("SELECT status FROM ingest_log WHERE source = 'revenuecat'").fetchone()
    assert ingest["status"] == "error"
    assert conn.execute("SELECT COUNT(*) AS n FROM snapshots").fetchone()["n"] == 0
