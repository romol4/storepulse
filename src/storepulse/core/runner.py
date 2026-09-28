"""One collection pass: fetch → parse → store, per source and date."""

from __future__ import annotations

import logging
import sqlite3
import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta

from storepulse.core import db
from storepulse.core.secrets import redact
from storepulse.core.sources import apple_sales
from storepulse.core.sources.apple_client import AppleAuthError, AppleClient, AppleVendorError

log = logging.getLogger(__name__)


@dataclass
class DateRange:
    dates: list[date]
    skipped: tuple[date, date] | None = None  # dates before the retention cutoff


def apple_sales_range(start: date, end: date, today: date | None = None) -> DateRange:
    """Dates from start to end inclusive, clamped to Apple's ~1-year retention window."""
    if end < start:
        raise ValueError("end date is before start date")
    cutoff = apple_sales.retention_cutoff(today)
    skipped = None
    if start < cutoff:
        skipped = (start, min(end, cutoff - timedelta(days=1)))
        start = cutoff
    dates = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    return DateRange(dates, skipped)


@dataclass
class RunSummary:
    ok: int = 0
    rows: int = 0
    not_ready: list[date] = field(default_factory=list)
    no_sales: int = 0
    inferred_no_sales: int = 0
    kept_rows: list[date] = field(default_factory=list)
    errors: list[tuple[date, str]] = field(default_factory=list)
    unknown_types: Counter[str] = field(default_factory=Counter)
    unmapped_rows: int = 0
    unmapped_child_rows: int = 0  # IAP rows whose parent app is still unknown
    remapped: list[date] = field(default_factory=list)
    new_apps: list[str] = field(default_factory=list)


def collect_apple_sales(
    conn: sqlite3.Connection,
    client: AppleClient,
    vendor_number: str,
    dates: Iterable[date],
    *,
    delay: float = 1.0,
    sleep: Callable[[float], None] | None = None,
    today: date | None = None,
    progress: Callable[[date, str], None] | None = None,
) -> RunSummary:
    """Pull and store each date independently; one bad date never stops the rest.

    Auth and vendor-number failures stop the pass, since every later request would fail
    the same way.
    """
    sleep = sleep or time.sleep
    source = apple_sales.SOURCE
    summary = RunSummary()
    # Days whose IAP rows couldn't be mapped because the parent app's SKU wasn't known
    # yet (no discovery). Re-mapped at the end once later days have taught the SKU.
    pending: dict[date, tuple[list[apple_sales.SalesRow], int]] = {}
    for i, day in enumerate(dates):
        if i and delay:
            sleep(delay)
        iso = day.isoformat()
        started = db.utc_now()
        try:
            fetched = apple_sales.fetch_day(client, vendor_number, day, today=today)
            if fetched.status == "ok":
                report = apple_sales.parse_summary(apple_sales.decode_report(fetched.content))
                mapped = apple_sales.map_rows(conn, day, report)
                written = db.replace_source_day(conn, source, iso, mapped.rows)
                if mapped.unmapped_child_rows:
                    pending[day] = (report, mapped.unmapped_rows)
                note = mapped.note()
                for code, count in sorted(mapped.unknown_types.items()):
                    log.warning(
                        "%s %s: unknown product type %r in %d rows (%d units); only its "
                        "proceeds were counted",
                        source,
                        iso,
                        code,
                        count,
                        mapped.unknown_units[code],
                    )
                if mapped.unmapped_rows:
                    log.warning(
                        "%s %s: %d rows matched no known app", source, iso, mapped.unmapped_rows
                    )
                summary.unknown_types.update(mapped.unknown_types)
                summary.unmapped_rows += mapped.unmapped_rows
                summary.new_apps += mapped.new_apps
                summary.ok += 1
                summary.rows += written
                db.log_ingest(
                    conn,
                    source=source,
                    report_date=iso,
                    started_at=started,
                    status="ok",
                    rows=written,
                    note=note,
                )
                outcome = f"ok, {written} rows"
            elif fetched.status == "no_sales":
                # Apple said explicitly there were no sales: clear anything stale.
                db.replace_source_day(conn, source, iso, [])
                summary.ok += 1
                summary.no_sales += 1
                db.log_ingest(
                    conn, source=source, report_date=iso, started_at=started, status="ok", rows=0
                )
                outcome = "no sales"
            elif fetched.status == "no_sales_inferred":
                # A 404 without "no sales" text: never delete what we already have.
                summary.ok += 1
                summary.inferred_no_sales += 1
                existing = db.count_source_day(conn, source, iso)
                if existing:
                    summary.kept_rows.append(day)
                    note = f"404 without 'no sales' text; kept {existing} stored rows"
                    log.warning("%s %s: %s", source, iso, note)
                    outcome = f"404, kept {existing} stored rows"
                else:
                    note = "no sales (inferred from date)"
                    outcome = "no sales (inferred)"
                db.log_ingest(
                    conn,
                    source=source,
                    report_date=iso,
                    started_at=started,
                    status="ok",
                    rows=0,
                    note=note,
                )
            else:
                summary.not_ready.append(day)
                db.log_ingest(
                    conn, source=source, report_date=iso, started_at=started, status="not_ready"
                )
                outcome = "not ready yet"
        except (AppleAuthError, AppleVendorError) as exc:
            db.log_ingest(
                conn,
                source=source,
                report_date=iso,
                started_at=started,
                status="error",
                note=redact(str(exc)),
            )
            raise
        except Exception as exc:
            message = redact(f"{type(exc).__name__}: {exc}")
            log.error("%s %s failed: %s", source, iso, message)
            summary.errors.append((day, message))
            db.log_ingest(
                conn,
                source=source,
                report_date=iso,
                started_at=started,
                status="error",
                note=message,
            )
            outcome = "error"
        if progress:
            progress(day, outcome)
    _remap_pending(conn, pending, summary, progress)
    return summary


def _remap_pending(
    conn: sqlite3.Connection,
    pending: dict[date, tuple[list[apple_sales.SalesRow], int]],
    summary: RunSummary,
    progress: Callable[[date, str], None] | None,
) -> None:
    """Re-map days whose child rows (IAPs) had no known parent SKU, now that later days in
    this run may have registered it. What stays unmapped is counted for the warning."""
    source = apple_sales.SOURCE
    for day, (report, unmapped_before) in sorted(pending.items()):
        mapped = apple_sales.map_rows(conn, day, report)
        if mapped.unmapped_rows >= unmapped_before:
            summary.unmapped_child_rows += mapped.unmapped_child_rows
            continue
        iso = day.isoformat()
        started = db.utc_now()
        written = db.replace_source_day(conn, source, iso, mapped.rows)
        summary.unmapped_rows -= unmapped_before - mapped.unmapped_rows
        summary.unmapped_child_rows += mapped.unmapped_child_rows
        summary.remapped.append(day)
        db.log_ingest(
            conn,
            source=source,
            report_date=iso,
            started_at=started,
            status="ok",
            rows=written,
            note="re-mapped after later days registered the parent app"
            + (f"; {mapped.note()}" if mapped.note() else ""),
        )
        if progress:
            progress(day, f"re-mapped in-app purchases, {written} rows")
