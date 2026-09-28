"""App Store Connect daily SALES/SUMMARY reports: fetch, parse, and map to metrics."""

from __future__ import annotations

import csv
import gzip
import io
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Literal
from zoneinfo import ZoneInfo

from storepulse.core import db
from storepulse.core.sources.apple_client import (
    DOCS_HINT,
    AppleAuthError,
    AppleClient,
    AppleError,
    AppleVendorError,
)

SOURCE = "apple_sales"
UNKNOWN_COUNTRY = "ZZ"
PACIFIC = ZoneInfo("America/Los_Angeles")

# Apple's reporting day is Pacific time; daily reports are kept for about a year.
RETENTION_DAYS = 365
# An unexplained 404 for a date older than this is taken as "no sales" (never deletes data).
INFERRED_NO_SALES_AFTER_DAYS = 2

# 404 detail text as documented by Apple, e.g. "There were no sales for the date specified."
# and "Report is not available yet. Daily reports ... are available by 5 am Pacific Time".
# Verify against real responses while dogfooding; anything unmatched falls back to date age.
NO_SALES_MARKERS = ("no sales",)
NOT_READY_MARKERS = ("not available", "not yet")

# Product Type Identifiers (App Store Connect Help, "Product type identifiers").
FIRST_TIME_TYPES = frozenset({"1", "1F", "1T", "F1", "1E", "1EP", "1EU"})
BUNDLE_TYPES = frozenset({"1-B", "F1-B"})
REDOWNLOAD_TYPES = frozenset({"3", "3F", "3T", "F3"})
UPDATE_TYPES = frozenset({"7", "7F", "7T", "F7"})
# Restored in-app purchases (non-consumables): not purchases, so no unit metric.
RESTORE_TYPES = frozenset({"IA3"})
IAP_TYPES = frozenset({"IA1", "IA1-M", "IA9", "IA9-M", "IAY", "IAY-M", "IAC", "IAC-M", "FI1"})
# Rows whose Apple Identifier is an app (so they may register a new app and its SKU).
APP_ROW_TYPES = FIRST_TIME_TYPES | REDOWNLOAD_TYPES | UPDATE_TYPES

REQUIRED_COLUMNS = (
    "SKU",
    "Title",
    "Product Type Identifier",
    "Units",
    "Developer Proceeds",
    "Country Code",
    "Currency of Proceeds",
    "Apple Identifier",
    "Parent Identifier",
)

FetchStatus = Literal["ok", "no_sales", "no_sales_inferred", "not_ready"]


class ReportParseError(ValueError):
    pass


@dataclass(frozen=True)
class FetchResult:
    status: FetchStatus
    content: bytes = b""
    detail: str = ""


@dataclass(frozen=True)
class SalesRow:
    sku: str
    title: str
    product_type: str
    units: Decimal
    developer_proceeds: Decimal
    country: str
    currency: str
    apple_id: str
    parent_id: str


@dataclass
class MappedReport:
    rows: list[db.MetricRow]
    unknown_types: Counter[str] = field(default_factory=Counter)
    unknown_units: Counter[str] = field(default_factory=Counter)
    unmapped_rows: int = 0
    # Rows (IAPs, restores) whose parent SKU isn't known yet; a later app row may teach it.
    unmapped_child_rows: int = 0
    new_apps: list[str] = field(default_factory=list)

    def note(self) -> str | None:
        parts = []
        if self.unknown_types:
            codes = ", ".join(f"{c} x{n}" for c, n in sorted(self.unknown_types.items()))
            parts.append(f"unknown product types: {codes}")
        if self.unmapped_rows:
            parts.append(f"{self.unmapped_rows} rows with no matching app")
        return "; ".join(parts) or None


def pacific_today() -> date:
    return datetime.now(PACIFIC).date()


def retention_cutoff(today: date | None = None) -> date:
    return (today or pacific_today()) - timedelta(days=RETENTION_DAYS)


def classify_404(detail: str, report_date: date, today: date | None = None) -> FetchStatus:
    text = detail.lower()
    if any(m in text for m in NO_SALES_MARKERS):
        return "no_sales"
    if any(m in text for m in NOT_READY_MARKERS):
        return "not_ready"
    age = ((today or pacific_today()) - report_date).days
    return "no_sales_inferred" if age > INFERRED_NO_SALES_AFTER_DAYS else "not_ready"


def _vendor_error(exc: AppleError) -> AppleVendorError:
    return AppleVendorError(
        "App Store Connect rejected the vendor number. Find it in App Store Connect > "
        f"Payments and Financial Reports (top left). {DOCS_HINT}",
        exc.status,
        exc.errors,
    )


def fetch_day(
    client: AppleClient, vendor_number: str, report_date: date, today: date | None = None
) -> FetchResult:
    params = {
        "filter[frequency]": "DAILY",
        "filter[reportType]": "SALES",
        "filter[reportSubType]": "SUMMARY",
        "filter[vendorNumber]": vendor_number,
        "filter[reportDate]": report_date.isoformat(),
    }
    purpose = f"the sales report for {report_date.isoformat()}"
    try:
        content = client.get_bytes("/v1/salesReports", params, purpose=purpose)
    except AppleAuthError as exc:
        if "vendor" in exc.detail.lower():
            raise _vendor_error(exc) from None
        raise
    except AppleError as exc:
        if exc.status == 404:
            return FetchResult(classify_404(exc.detail, report_date, today), detail=exc.detail)
        if exc.status == 400 and "vendor" in exc.detail.lower():
            raise _vendor_error(exc) from None
        raise
    return FetchResult("ok", content=content)


def decode_report(content: bytes) -> str:
    if content[:2] == b"\x1f\x8b":
        try:
            content = gzip.decompress(content)
        except (OSError, EOFError):
            raise ReportParseError("sales report is not valid gzip") from None
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ReportParseError("sales report is not UTF-8 text") from None


def _decimal(value: str, column: str, line: int) -> Decimal:
    try:
        return Decimal(value.strip() or "0")
    except InvalidOperation:
        raise ReportParseError(f"line {line}: {column} is not a number: {value!r}") from None


def parse_summary(text: str) -> list[SalesRow]:
    """Parse a SALES/SUMMARY TSV. Raises ReportParseError on a malformed file."""
    if not text.strip():
        raise ReportParseError("sales report is empty (no header row)")
    reader = csv.DictReader(io.StringIO(text), delimiter="\t", quoting=csv.QUOTE_NONE)
    header = reader.fieldnames or []
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        raise ReportParseError(f"sales report is missing columns: {', '.join(missing)}")
    rows: list[SalesRow] = []
    for line, raw in enumerate(reader, start=2):
        if None in raw or any(raw.get(c) is None for c in REQUIRED_COLUMNS):
            raise ReportParseError(f"line {line}: wrong number of fields")
        if not any((v or "").strip() for v in raw.values()):
            continue
        rows.append(
            SalesRow(
                sku=raw["SKU"].strip(),
                title=raw["Title"].strip(),
                product_type=raw["Product Type Identifier"].strip(),
                units=_decimal(raw["Units"], "Units", line),
                developer_proceeds=_decimal(raw["Developer Proceeds"], "Developer Proceeds", line),
                country=raw["Country Code"].strip().upper(),
                currency=raw["Currency of Proceeds"].strip().upper(),
                apple_id=raw["Apple Identifier"].strip(),
                parent_id=raw["Parent Identifier"].strip(),
            )
        )
    return rows


def sku_key(sku: str) -> str:
    return f"apple_sku:{sku}"


def map_rows(conn: sqlite3.Connection, report_date: date, rows: list[SalesRow]) -> MappedReport:
    """Turn report rows into metric rows, registering apps seen in app rows.

    Only app rows (first-time, redownload, update) may add an app or its SKU; IAP rows
    carry the purchase's title, not the app's, and resolve through the parent SKU.
    """
    result = MappedReport(rows=[])
    for row in rows:
        if row.product_type in APP_ROW_TYPES and row.apple_id:
            if db.find_app(conn, "ios", row.apple_id) is None:
                db.upsert_app(conn, "ios", row.apple_id, row.title or row.apple_id)
                result.new_apps.append(row.title or row.apple_id)
            if row.sku and db.get_kv(conn, sku_key(row.sku)) is None:
                db.set_kv(conn, sku_key(row.sku), row.apple_id)

    totals: dict[tuple[int, str, str, str], Decimal] = {}

    def add(app_id: int, country: str, metric: str, currency: str, value: Decimal) -> None:
        key = (app_id, country, metric, currency)
        totals[key] = totals.get(key, Decimal(0)) + value

    for row in rows:
        app_id = _resolve_app(conn, row)
        ptype = row.product_type
        known = (
            ptype in APP_ROW_TYPES
            or ptype in BUNDLE_TYPES
            or ptype in IAP_TYPES
            or ptype in RESTORE_TYPES
        )
        if not known:
            result.unknown_types[ptype] += 1
            result.unknown_units[ptype] += int(row.units)
        if app_id is None:
            result.unmapped_rows += 1
            if row.parent_id and ptype not in APP_ROW_TYPES:
                result.unmapped_child_rows += 1
            continue
        # ALL is reserved for source totals; an unknown country is the user-assigned ZZ.
        country = row.country or UNKNOWN_COUNTRY
        if ptype in FIRST_TIME_TYPES or ptype in BUNDLE_TYPES:
            add(app_id, country, "installs", "", row.units)
        elif ptype in REDOWNLOAD_TYPES:
            add(app_id, country, "redownloads", "", row.units)
        elif ptype in IAP_TYPES:
            add(app_id, country, "iap_units", "", row.units)
        # Updates and restores carry no unit metric (not in the vocabulary). Unknown types
        # still contribute proceeds so money totals match App Store Connect.
        proceeds = row.units * row.developer_proceeds
        if proceeds and row.currency:
            add(app_id, country, "proceeds", row.currency, proceeds)

    iso = report_date.isoformat()
    result.rows = [
        db.MetricRow(iso, app_id, country, metric, currency, float(value))
        for (app_id, country, metric, currency), value in sorted(totals.items())
        if value != 0
    ]
    return result


def _resolve_app(conn: sqlite3.Connection, row: SalesRow) -> int | None:
    if row.parent_id and row.product_type not in APP_ROW_TYPES:
        apple_id = db.get_kv(conn, sku_key(row.parent_id))
        return None if apple_id is None else db.find_app(conn, "ios", apple_id)
    if row.apple_id:
        return db.find_app(conn, "ios", row.apple_id)
    return None
