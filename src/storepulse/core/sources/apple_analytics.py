"""App Store Connect Analytics Reports: one-time setup, then daily impressions/page views.

docs/SPEC.md, Apple / Analytics (Phase 6a). Unlike apple_sales/apple_subscriptions/
apple_subscription_events, this source is per-app, not account-wide: an
``analyticsReportRequests`` resource is created once per app, then walked (its reports,
their instances, an instance's segments) to reach the actual data. Confirmed against
Apple's own docs today (developer.apple.com/help/app-store-connect-analytics/overview/
analytics-reports-api/): the report covering impressions and page views is named "App
Store Discovery and Engagement Report", and ``accessType`` is ``ONGOING`` or
``ONE_TIME_SNAPSHOT``. The nested endpoint paths below follow the App Store Connect API's
standard relationship-link convention (the same shape ``/v1/apps`` uses elsewhere in this
codebase) but were not verified against a reachable live reference for this specific
resource family — verify against a real account or current docs before relying on them,
same caveat docs/SPEC.md states for every source.

Creating the report request needs the App Manager or Admin role, unlike every other Apple
source here (Sales and Reports alone). ``ensure_report_request`` lists existing requests
first — readable with the default role — and only attempts to create one, surfacing the
role requirement, if none exists: a user who prefers not to grant the broader role can
create the request by hand once in App Store Connect and this module adopts it.
"""

from __future__ import annotations

import csv
import io
import sqlite3
from dataclasses import dataclass
from datetime import date

import httpx

from storepulse.core import db
from storepulse.core.secrets import register_secret
from storepulse.core.sources.apple_client import (
    DOCS_HINT,
    AppleAuthError,
    AppleClient,
)
from storepulse.core.sources.apple_sales import UNKNOWN_COUNTRY, ReportParseError

SOURCE = "apple_analytics"
REPORT_NAME = "App Store Discovery and Engagement Report"
GRANULARITY = "DAILY"

# Best-effort guess pending a real segment file to verify against: "Territory" mirrors
# Apple's own Analytics-report convention (distinct from Sales' "Country Code"), and
# "Impressions"/"Page Views" are the metric names Apple's own documentation uses verbatim.
REQUIRED_COLUMNS = ("App Apple Identifier", "Territory", "Impressions", "Page Views")


def request_key(store_id: str) -> str:
    return f"apple_analytics_request:{store_id}"


def report_key(request_id: str) -> str:
    return f"apple_analytics_report:{request_id}"


@dataclass(frozen=True)
class ReportRequestInfo:
    id: str
    access_type: str


def list_report_requests(client: AppleClient, store_id: str) -> list[ReportRequestInfo]:
    """GET the app's analytics report requests. Read-only; works with Sales and Reports."""
    body = client.get_json(
        f"/v1/apps/{store_id}/analyticsReportRequests",
        {"limit": "50"},
        purpose="listing analytics report requests",
    )
    requests = []
    for item in body.get("data") or []:
        item_id = str(item.get("id", ""))
        if item_id:
            attrs = item.get("attributes") or {}
            requests.append(ReportRequestInfo(item_id, str(attrs.get("accessType", ""))))
    return requests


def create_report_request(client: AppleClient, store_id: str) -> str:
    """POST a new ONGOING report request for this app. Raises a specific, actionable
    AppleAuthError on a 403 here, since the generic "check the Sales and Reports role"
    message from AppleClient would be misleading: this call specifically needs App
    Manager or Admin."""
    body = {
        "data": {
            "type": "analyticsReportRequests",
            "attributes": {"accessType": "ONGOING"},
            "relationships": {"app": {"data": {"type": "apps", "id": store_id}}},
        }
    }
    try:
        result = client.post_json(
            "/v1/analyticsReportRequests", body, purpose="creating the analytics report request"
        )
    except AppleAuthError as exc:
        if exc.status != 403:
            raise
        raise AppleAuthError(
            "App Store Connect denied creating the analytics report request (HTTP 403). "
            "Unlike Storepulse's other Apple sources, this one-time setup step needs the "
            "App Manager or Admin role, not just Sales and Reports. Either grant the API "
            "key that role once (Users and Access > Integrations > edit the key), or "
            "create the request yourself in App Store Connect (App Analytics > Reports > "
            f"new ongoing request) and Storepulse will use it. {DOCS_HINT}",
            exc.status,
            exc.errors,
        ) from None
    return str(result["data"]["id"])


def ensure_report_request(client: AppleClient, conn: sqlite3.Connection, store_id: str) -> str:
    """The app's ONGOING request id — the mechanism behind "setup is not repeated"
    (docs/SPEC.md). Checks kv first (the fast path on every normal run). On a cache miss,
    lists existing requests and adopts a matching ONGOING one however it was created — by
    this module earlier, or by a human in the App Store Connect UI — before ever
    attempting to create one, so a reset kv table never produces a duplicate request."""
    cached = db.get_kv(conn, request_key(store_id))
    if cached is not None:
        return cached
    for existing in list_report_requests(client, store_id):
        if existing.access_type == "ONGOING":
            db.set_kv(conn, request_key(store_id), existing.id)
            return existing.id
    request_id = create_report_request(client, store_id)
    db.set_kv(conn, request_key(store_id), request_id)
    return request_id


def find_discovery_engagement_report(
    client: AppleClient, conn: sqlite3.Connection, request_id: str
) -> str | None:
    """The request's Discovery and Engagement report id, kv-cached. Returns None (not an
    error) if the request hasn't produced it yet — a freshly-created request plausibly
    needs time before Apple populates its reports, the same "not ready yet, not an error"
    treatment this codebase gives an unpublished store report."""
    cached = db.get_kv(conn, report_key(request_id))
    if cached is not None:
        return cached
    body = client.get_json(
        f"/v1/analyticsReportRequests/{request_id}/reports",
        {"limit": "50"},
        purpose="listing the request's reports",
    )
    for item in body.get("data") or []:
        attrs = item.get("attributes") or {}
        if attrs.get("name") == REPORT_NAME:
            report_id = str(item.get("id", ""))
            if report_id:
                db.set_kv(conn, report_key(request_id), report_id)
                return report_id
    return None


@dataclass(frozen=True)
class InstanceInfo:
    id: str
    processing_date: date


def list_instances(client: AppleClient, report_id: str) -> list[InstanceInfo]:
    """The report's DAILY instances. Returns one page (200 instances comfortably covers
    any operationally relevant lookback window); deliberately does not assume a per-date
    filter param exists, since one isn't documented — the caller matches against wanted
    dates itself."""
    body = client.get_json(
        f"/v1/analyticsReports/{report_id}/instances",
        {"filter[granularity]": GRANULARITY, "limit": "200"},
        purpose="listing report instances",
    )
    instances = []
    for item in body.get("data") or []:
        item_id = str(item.get("id", ""))
        raw_date = (item.get("attributes") or {}).get("processingDate")
        if not item_id or not raw_date:
            continue
        try:
            processing_date = date.fromisoformat(str(raw_date))
        except ValueError:
            continue
        instances.append(InstanceInfo(item_id, processing_date))
    return instances


def fetch_segments(client: AppleClient, instance_id: str) -> list[bytes]:
    """Lists an instance's segment download URLs and fetches each.

    Each url is fetched on its own terms rather than always through the authenticated
    Apple client: a bulk-download resource like this commonly hands back a pre-signed
    absolute URL to a different host (its signature is its own credential), and routing
    that through client.get_bytes would attach this app's separate Apple API bearer
    token to a request bound for a host that never asked for it — a live credential
    leaked to whatever ends up serving that URL. An absolute URL is therefore always
    fetched plain and unauthenticated; only a relative path (same-service call) uses
    the client. This holds regardless of which shape a real response turns out to use,
    still unverified per the module docstring.

    An absolute URL is also registered as a secret before it's ever used: its query
    string is a credential in its own right (that's the whole point of a pre-signed
    link), and a failed download's exception message would otherwise carry it, unredacted,
    into ingest_log's plaintext error column (docs/SPEC.md, Storage: secrets never appear
    in the database's plaintext columns).
    """
    body = client.get_json(
        f"/v1/analyticsReportInstances/{instance_id}/segments",
        purpose="listing report segments",
    )
    urls = [
        str(url)
        for item in (body.get("data") or [])
        if (url := (item.get("attributes") or {}).get("url"))
    ]
    for url in urls:
        if url.startswith(("http://", "https://")):
            register_secret(url)
    return [_fetch_segment_url(client, url) for url in urls]


def _fetch_segment_url(client: AppleClient, url: str) -> bytes:
    if not url.startswith(("http://", "https://")):
        return client.get_bytes(url, purpose="downloading a report segment")
    response = httpx.get(url, timeout=60.0)
    response.raise_for_status()
    return response.content


@dataclass(frozen=True)
class AnalyticsRow:
    apple_id: str
    country: str
    impressions: int
    page_views: int


def _count(value: str, column: str, line: int) -> int:
    text = (value or "").strip()
    if not text:
        return 0
    try:
        return int(text)
    except ValueError:
        raise ReportParseError(f"line {line}: {column} is not a number: {value!r}") from None


def parse_segment(text: str) -> list[AnalyticsRow]:
    """Parse one Discovery and Engagement report segment. Tab-delimited, matching every
    other Apple report this codebase already parses — docs/SPEC.md's "gzipped CSV"
    wording most likely means "a gzipped delimited file" rather than literally
    comma-separated; verify against a real segment at implementation time, along with
    REQUIRED_COLUMNS' exact names."""
    if not text.strip():
        raise ReportParseError("analytics report segment is empty (no header row)")
    reader = csv.DictReader(io.StringIO(text), delimiter="\t", quoting=csv.QUOTE_NONE)
    header = reader.fieldnames or []
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        raise ReportParseError(f"analytics report segment is missing columns: {', '.join(missing)}")
    rows: list[AnalyticsRow] = []
    for line, raw in enumerate(reader, start=2):
        if None in raw or any(raw.get(c) is None for c in REQUIRED_COLUMNS):
            raise ReportParseError(f"line {line}: wrong number of fields")
        if not any((v or "").strip() for v in raw.values()):
            continue
        rows.append(
            AnalyticsRow(
                apple_id=raw["App Apple Identifier"].strip(),
                country=raw["Territory"].strip().upper(),
                impressions=_count(raw["Impressions"], "Impressions", line),
                page_views=_count(raw["Page Views"], "Page Views", line),
            )
        )
    return rows


@dataclass
class MappedReport:
    rows: list[db.MetricRow]
    unmapped_rows: int = 0

    def note(self) -> str | None:
        if self.unmapped_rows:
            return f"{self.unmapped_rows} rows with no matching app"
        return None


def map_rows(conn: sqlite3.Connection, report_date: date, rows: list[AnalyticsRow]) -> MappedReport:
    """Sum each app's segments into one impressions/page_views pair per country, plus a
    country='ALL' total (mirrors apple_subscriptions' convention). Never registers a new
    app — an app must already be known from apple_sales or discovery."""
    totals: dict[tuple[int, str, str], int] = {}
    all_totals: dict[tuple[int, str], int] = {}
    unmapped_rows = 0
    for row in rows:
        app_id = db.find_app(conn, "ios", row.apple_id) if row.apple_id else None
        if app_id is None:
            unmapped_rows += 1
            continue
        country = row.country or UNKNOWN_COUNTRY
        for metric, value in (("impressions", row.impressions), ("page_views", row.page_views)):
            key = (app_id, country, metric)
            totals[key] = totals.get(key, 0) + value
            all_key = (app_id, metric)
            all_totals[all_key] = all_totals.get(all_key, 0) + value

    iso = report_date.isoformat()
    metric_rows = [
        db.MetricRow(iso, app_id, country, metric, "", float(value))
        for (app_id, country, metric), value in sorted(totals.items())
        if value != 0
    ]
    metric_rows += [
        db.MetricRow(iso, app_id, "ALL", metric, "", float(value))
        for (app_id, metric), value in sorted(all_totals.items())
        if value != 0
    ]
    return MappedReport(rows=metric_rows, unmapped_rows=unmapped_rows)
