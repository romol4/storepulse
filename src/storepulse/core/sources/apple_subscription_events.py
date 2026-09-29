"""App Store Connect daily SUBSCRIPTION_EVENT reports: fetch, parse, and map to metrics.

docs/SPEC.md, Apple / Subscription events. Unlike the Subscription (state) report, this is
an event log: one row per distinct event occurrence that day, with a Quantity column
aggregating repeats of the same event/segment. Only the churn-classified events
(Cancel, Canceled from Billing Retry, Refund) feed a metric this phase; every other
recognized event (renewals, offer starts, upgrades, ...) is acknowledged but not counted,
same treatment apple_sales gives e.g. IA3 restores. An event value outside both sets is
logged and counted — it never fails the run, matching how apple_sales handles an
unrecognized Product Type Identifier.

Rows resolve to an app via "App Apple ID" directly, same as the Subscription report — no
parent-SKU indirection, and this source never registers a new app.
"""

from __future__ import annotations

import csv
import io
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
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

SOURCE = "apple_subscription_events"
REPORT_VERSION = "1_3"

# Cancellation, involuntary cancellation, and refund: the events subscription_churn counts.
CHURN_EVENTS = frozenset({"Cancel", "Canceled from Billing Retry", "Refund"})
# Recognized but not counted toward any metric this phase (docs/SPEC.md).
OTHER_KNOWN_EVENTS = frozenset(
    {
        "Start Introductory Price",
        "Paid Subscription from Introductory Price",
        "Renew",
        "Start Promotional Offer",
        "Reactivation to Promotional Offer",
        "Upgrade",
        "Downgrade",
        "Crossgrade",
        "Reactivate",
        "Billing Retry from Paid Subscription",
        "Renewal from Billing Retry",
    }
)
REQUIRED_COLUMNS = ("Event", "App Apple ID", "Country", "Quantity")


@dataclass(frozen=True)
class EventRow:
    event: str
    apple_id: str
    country: str
    quantity: int


def fetch_day(
    client: AppleClient, vendor_number: str, report_date: date, today: date | None = None
) -> FetchResult:
    params = {
        "filter[frequency]": "DAILY",
        "filter[reportType]": "SUBSCRIPTION_EVENT",
        "filter[reportSubType]": "SUMMARY",
        "filter[version]": REPORT_VERSION,
        "filter[vendorNumber]": vendor_number,
        "filter[reportDate]": report_date.isoformat(),
    }
    purpose = f"the subscription event report for {report_date.isoformat()}"
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


def parse_summary(text: str) -> list[EventRow]:
    """Parse a SUBSCRIPTION_EVENT/SUMMARY TSV. Raises ReportParseError on a malformed file."""
    if not text.strip():
        raise ReportParseError("subscription event report is empty (no header row)")
    reader = csv.DictReader(io.StringIO(text), delimiter="\t", quoting=csv.QUOTE_NONE)
    header = reader.fieldnames or []
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        raise ReportParseError(
            f"subscription event report is missing columns: {', '.join(missing)}"
        )
    rows: list[EventRow] = []
    for line, raw in enumerate(reader, start=2):
        if None in raw or any(raw.get(c) is None for c in REQUIRED_COLUMNS):
            raise ReportParseError(f"line {line}: wrong number of fields")
        if not any((v or "").strip() for v in raw.values()):
            continue
        rows.append(
            EventRow(
                event=raw["Event"].strip(),
                apple_id=raw["App Apple ID"].strip(),
                country=raw["Country"].strip().upper(),
                quantity=_count(raw["Quantity"], "Quantity", line),
            )
        )
    return rows


@dataclass
class MappedReport:
    rows: list[db.MetricRow]
    unknown_events: Counter[str] = field(default_factory=Counter)
    unknown_quantities: Counter[str] = field(default_factory=Counter)
    unmapped_rows: int = 0

    def note(self) -> str | None:
        parts = []
        if self.unknown_events:
            codes = ", ".join(f"{c} x{n}" for c, n in sorted(self.unknown_events.items()))
            parts.append(f"unrecognized events: {codes}")
        if self.unmapped_rows:
            parts.append(f"{self.unmapped_rows} rows with no matching app")
        return "; ".join(parts) or None


def map_rows(conn: sqlite3.Connection, report_date: date, rows: list[EventRow]) -> MappedReport:
    """Sum churn-event Quantity into subscription_churn per country, plus a country='ALL'
    total. Non-churn events (recognized or not) contribute nothing; an unrecognized event
    is counted for the run's warning but never fails it."""
    result = MappedReport(rows=[])
    totals: dict[tuple[int, str], int] = {}
    all_totals: dict[int, int] = {}
    for row in rows:
        known = row.event in CHURN_EVENTS or row.event in OTHER_KNOWN_EVENTS
        if not known:
            result.unknown_events[row.event] += 1
            result.unknown_quantities[row.event] += row.quantity
        if row.event not in CHURN_EVENTS:
            continue
        app_id = db.find_app(conn, "ios", row.apple_id) if row.apple_id else None
        if app_id is None:
            result.unmapped_rows += 1
            continue
        country = row.country or UNKNOWN_COUNTRY
        key = (app_id, country)
        totals[key] = totals.get(key, 0) + row.quantity
        all_totals[app_id] = all_totals.get(app_id, 0) + row.quantity

    iso = report_date.isoformat()
    result.rows = [
        db.MetricRow(iso, app_id, country, "subscription_churn", "", float(value))
        for (app_id, country), value in sorted(totals.items())
        if value != 0
    ] + [
        db.MetricRow(iso, app_id, "ALL", "subscription_churn", "", float(value))
        for app_id, value in sorted(all_totals.items())
        if value != 0
    ]
    return result
