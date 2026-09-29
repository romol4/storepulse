"""App Store Connect daily SUBSCRIPTION reports: fetch, parse, and map to metrics.

docs/SPEC.md, Apple / Subscriptions. This is a snapshot, not an event log: each row is one
(app, subscription, country, device, client, state) segment for that day, and the active
counts are already aggregated into named columns (there is no per-subscriber row to sum).

``active_subscriptions`` sums every "active" column except the four free-trial ones;
``active_trials`` sums exactly those four. The ``Subscribers`` column (a distinct
per-segment headcount, not a subscription-instance count) is not used by either metric.

Rows resolve to an app via "App Apple ID" directly — unlike apple_sales' in-app-purchase
rows, there is no parent-SKU indirection to resolve here. This source never registers a
new app; an app must already be known (from apple_sales or discovery).
"""

from __future__ import annotations

import csv
import io
import sqlite3
from dataclasses import dataclass
from datetime import date

from storepulse.core import db
from storepulse.core.sources.apple_client import AppleAuthError, AppleClient, AppleError
from storepulse.core.sources.apple_sales import (
    UNKNOWN_COUNTRY,
    FetchResult,
    ReportParseError,
    classify_404,
    vendor_error,
)

SOURCE = "apple_subscriptions"
REPORT_VERSION = "1_3"

# Every "active" column except the four free-trial ones (docs/SPEC.md).
PAID_COLUMNS = (
    "Active Standard Price Subscriptions",
    "Active Pay Up Front Introductory Offer Subscriptions",
    "Active Pay As You Go Introductory Offer Subscriptions",
    "Pay Up Front Promotional Offer Subscriptions",
    "Pay As You Go Promotional Offer Subscriptions",
    "Pay Up Front Offer Code Subscriptions",
    "Pay As You Go Offer Code Subscriptions",
    "Marketing Opt-Ins",
    "Billing Retry",
    "Grace Period",
    "Pay Up Front Win-Back Offers",
    "Pay As You Go Win-Back Offers",
)
# The four free-trial columns, one per offer family (Introductory, Promotional, Offer
# Code, Win-Back).
TRIAL_COLUMNS = (
    "Active Free Trial Introductory Offer Subscriptions",
    "Free Trial Promotional Offer Subscriptions",
    "Free Trial Offer Code Subscriptions",
    "Free Trial Win-Back Offers",
)
REQUIRED_COLUMNS = ("App Apple ID", "Country", *PAID_COLUMNS, *TRIAL_COLUMNS)


@dataclass(frozen=True)
class SubscriptionRow:
    apple_id: str
    country: str
    active_subscriptions: int
    active_trials: int


def fetch_day(
    client: AppleClient, vendor_number: str, report_date: date, today: date | None = None
) -> FetchResult:
    params = {
        "filter[frequency]": "DAILY",
        "filter[reportType]": "SUBSCRIPTION",
        "filter[reportSubType]": "SUMMARY",
        "filter[version]": REPORT_VERSION,
        "filter[vendorNumber]": vendor_number,
        "filter[reportDate]": report_date.isoformat(),
    }
    purpose = f"the subscription report for {report_date.isoformat()}"
    try:
        content = client.get_bytes("/v1/salesReports", params, purpose=purpose)
    except AppleAuthError as exc:
        if "vendor" in exc.detail.lower():
            raise vendor_error(exc) from None
        raise
    except AppleError as exc:
        if exc.status == 404:
            return FetchResult(classify_404(exc.detail, report_date, today), detail=exc.detail)
        if exc.status == 400 and "vendor" in exc.detail.lower():
            raise vendor_error(exc) from None
        raise
    return FetchResult("ok", content=content)


def _count(value: str, column: str, line: int) -> int:
    text = (value or "").strip()
    if not text:
        return 0
    try:
        return int(text)
    except ValueError:
        raise ReportParseError(f"line {line}: {column} is not a number: {value!r}") from None


def parse_summary(text: str) -> list[SubscriptionRow]:
    """Parse a SUBSCRIPTION/SUMMARY TSV. Raises ReportParseError on a malformed file."""
    if not text.strip():
        raise ReportParseError("subscription report is empty (no header row)")
    reader = csv.DictReader(io.StringIO(text), delimiter="\t", quoting=csv.QUOTE_NONE)
    header = reader.fieldnames or []
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        raise ReportParseError(f"subscription report is missing columns: {', '.join(missing)}")
    rows: list[SubscriptionRow] = []
    for line, raw in enumerate(reader, start=2):
        if None in raw or any(raw.get(c) is None for c in REQUIRED_COLUMNS):
            raise ReportParseError(f"line {line}: wrong number of fields")
        if not any((v or "").strip() for v in raw.values()):
            continue
        rows.append(
            SubscriptionRow(
                apple_id=raw["App Apple ID"].strip(),
                country=raw["Country"].strip().upper(),
                active_subscriptions=sum(_count(raw[c], c, line) for c in PAID_COLUMNS),
                active_trials=sum(_count(raw[c], c, line) for c in TRIAL_COLUMNS),
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


def map_rows(
    conn: sqlite3.Connection, report_date: date, rows: list[SubscriptionRow]
) -> MappedReport:
    """Sum each app's segments into one active_subscriptions/active_trials pair per
    country, plus a country='ALL' total. A day with no rows for an app means zero active
    subscriptions; either way, nothing is written (SPEC.md's "absence means zero")."""
    totals: dict[tuple[int, str, str], int] = {}
    all_totals: dict[tuple[int, str], int] = {}
    unmapped_rows = 0
    for row in rows:
        app_id = db.find_app(conn, "ios", row.apple_id) if row.apple_id else None
        if app_id is None:
            unmapped_rows += 1
            continue
        country = row.country or UNKNOWN_COUNTRY
        for metric, value in (
            ("active_subscriptions", row.active_subscriptions),
            ("active_trials", row.active_trials),
        ):
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
