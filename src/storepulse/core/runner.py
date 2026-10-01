"""One collection pass: fetch → parse → store, per source and date."""

from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import httpx

from storepulse.core import config, db, digest, discovery, mailer
from storepulse.core.secrets import (
    APPLE_P8_SECRET,
    GOOGLE_SA_SECRET,
    SMTP_PASSWORD_SECRET,
    SecretStore,
    redact,
)
from storepulse.core.sources import (
    apple_analytics,
    apple_sales,
    apple_subscription_events,
    apple_subscriptions,
    play_earnings,
    play_installs,
    play_sales,
    play_vitals,
)
from storepulse.core.sources.apple_client import AppleAuthError, AppleClient, AppleVendorError
from storepulse.core.sources.google_auth import GoogleCredentialError, load_service_account
from storepulse.core.sources.google_client import (
    BULK_PERMISSION,
    FINANCIAL_PERMISSION,
    GoogleClient,
    parse_bucket_uri,
)
from storepulse.core.sources.play_common import Month, decode_csv, plan_month, read_zip_csv

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
            # Reported by the caller (grouped with group_errors); info avoids printing twice.
            log.info("%s %s failed: %s", source, iso, message)
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


def collect_apple_subscriptions(
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

    Shaped like collect_apple_sales, but simpler: this source resolves apps directly via
    App Apple ID (docs/SPEC.md), so there is no parent-SKU indirection to re-map and no new
    app is ever registered. RunSummary's unmapped_child_rows, remapped, new_apps and
    unknown_types stay at their defaults for this source.
    """
    sleep = sleep or time.sleep
    source = apple_subscriptions.SOURCE
    summary = RunSummary()
    for i, day in enumerate(dates):
        if i and delay:
            sleep(delay)
        iso = day.isoformat()
        started = db.utc_now()
        try:
            fetched = apple_subscriptions.fetch_day(client, vendor_number, day, today=today)
            if fetched.status == "ok":
                report = apple_subscriptions.parse_summary(
                    apple_sales.decode_report(fetched.content)
                )
                mapped = apple_subscriptions.map_rows(conn, day, report)
                written = db.replace_source_day(conn, source, iso, mapped.rows)
                if mapped.unmapped_rows:
                    log.warning(
                        "%s %s: %d rows matched no known app", source, iso, mapped.unmapped_rows
                    )
                summary.unmapped_rows += mapped.unmapped_rows
                summary.ok += 1
                summary.rows += written
                db.log_ingest(
                    conn,
                    source=source,
                    report_date=iso,
                    started_at=started,
                    status="ok",
                    rows=written,
                    note=mapped.note(),
                )
                outcome = f"ok, {written} rows"
            elif fetched.status == "no_sales":
                db.replace_source_day(conn, source, iso, [])
                summary.ok += 1
                summary.no_sales += 1
                db.log_ingest(
                    conn, source=source, report_date=iso, started_at=started, status="ok", rows=0
                )
                outcome = "no sales"
            elif fetched.status == "no_sales_inferred":
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
            log.info("%s %s failed: %s", source, iso, message)
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
    return summary


def collect_apple_subscription_events(
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

    Shaped like collect_apple_sales, but simpler (see collect_apple_subscriptions). Unlike
    that source, this one does classify a code value (the Event column): an event outside
    the churn and other-recognized sets populates RunSummary.unknown_types, the same
    treatment collect_apple_sales gives an unrecognized Product Type Identifier.
    """
    sleep = sleep or time.sleep
    source = apple_subscription_events.SOURCE
    summary = RunSummary()
    for i, day in enumerate(dates):
        if i and delay:
            sleep(delay)
        iso = day.isoformat()
        started = db.utc_now()
        try:
            fetched = apple_subscription_events.fetch_day(client, vendor_number, day, today=today)
            if fetched.status == "ok":
                report = apple_subscription_events.parse_summary(
                    apple_sales.decode_report(fetched.content)
                )
                mapped = apple_subscription_events.map_rows(conn, day, report)
                written = db.replace_source_day(conn, source, iso, mapped.rows)
                for event, count in sorted(mapped.unknown_events.items()):
                    log.warning(
                        "%s %s: unrecognized event %r in %d rows (quantity %d)",
                        source,
                        iso,
                        event,
                        count,
                        mapped.unknown_quantities[event],
                    )
                if mapped.unmapped_rows:
                    log.warning(
                        "%s %s: %d rows matched no known app", source, iso, mapped.unmapped_rows
                    )
                summary.unknown_types.update(mapped.unknown_events)
                summary.unmapped_rows += mapped.unmapped_rows
                summary.ok += 1
                summary.rows += written
                db.log_ingest(
                    conn,
                    source=source,
                    report_date=iso,
                    started_at=started,
                    status="ok",
                    rows=written,
                    note=mapped.note(),
                )
                outcome = f"ok, {written} rows"
            elif fetched.status == "no_sales":
                db.replace_source_day(conn, source, iso, [])
                summary.ok += 1
                summary.no_sales += 1
                db.log_ingest(
                    conn, source=source, report_date=iso, started_at=started, status="ok", rows=0
                )
                outcome = "no sales"
            elif fetched.status == "no_sales_inferred":
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
            log.info("%s %s failed: %s", source, iso, message)
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
    return summary


@dataclass
class AppleAnalyticsSummary:
    """Not RunSummary: this source's natural error label is an app (a str), not a date,
    which RunSummary.errors (list[tuple[date, str]]) can't hold under mypy --strict."""

    ok: int = 0
    rows: int = 0
    not_ready: list[str] = field(default_factory=list)
    unmapped_rows: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)  # (app store_id, message)


def collect_apple_analytics(
    conn: sqlite3.Connection,
    client: AppleClient,
    dates: Sequence[date],
    *,
    progress: Callable[[str, str], None] | None = None,
) -> AppleAnalyticsSummary:
    """Per-app (docs/SPEC.md: analytics report requests are created per app, unlike this
    file's other three account-wide Apple sources): ensure the ONGOING report request
    exists, find the Discovery and Engagement report, list its DAILY instances, and
    download+parse+map whichever instances fall within ``dates`` for that app in one
    write. One app's failure never stops another's (mirrors collect_play_installs's
    per-app error isolation).

    If no instance matches ``dates`` yet (a freshly-created request, or a transient gap),
    nothing is written at all — never an empty replace_source_range call, which would
    wipe out a previous run's good data for a window this run simply found nothing new
    in (the same "not_ready never deletes stored rows" principle apple_sales applies).

    Unlike collect_apple_sales/collect_apple_subscriptions, an AppleAuthError here never
    stops the whole pass: ensure_report_request adopts an existing request (readable with
    Sales and Reports) before ever attempting to create one (which needs App Manager or
    Admin), so one app's 403 on creation says nothing about whether another app already
    has a request to adopt. Every app gets its own attempt and its own error.
    """
    summary = AppleAnalyticsSummary()
    wanted = set(dates)
    window_start, window_end = min(dates).isoformat(), max(dates).isoformat()
    for store_id, app_id in sorted(db.apps_for(conn, "ios").items()):
        started = db.utc_now()
        try:
            request_id = apple_analytics.ensure_report_request(client, conn, store_id)
            report_id = apple_analytics.find_discovery_engagement_report(client, conn, request_id)
            instances = (
                []
                if report_id is None
                else [
                    i
                    for i in apple_analytics.list_instances(client, report_id)
                    if i.processing_date in wanted
                ]
            )
            if not instances:
                summary.not_ready.append(store_id)
                db.log_ingest(
                    conn,
                    source=apple_analytics.SOURCE,
                    report_date=window_end,
                    started_at=started,
                    status="not_ready",
                    app_id=app_id,
                )
                if progress:
                    progress(store_id, "not ready yet")
                continue
            app_rows: list[db.MetricRow] = []
            app_unmapped = 0
            for instance in instances:
                segments = apple_analytics.fetch_segments(client, instance.id)
                rows = [
                    row
                    for segment in segments
                    for row in apple_analytics.parse_segment(apple_sales.decode_report(segment))
                ]
                mapped = apple_analytics.map_rows(conn, instance.processing_date, rows)
                app_rows.extend(mapped.rows)
                app_unmapped += mapped.unmapped_rows
            written = db.replace_source_range(
                conn, apple_analytics.SOURCE, window_start, window_end, app_rows, app_ids=[app_id]
            )
        except Exception as exc:
            message = redact(f"{type(exc).__name__}: {exc}")
            log.info("%s %s failed: %s", apple_analytics.SOURCE, store_id, message)
            summary.errors.append((store_id, message))
            db.log_ingest(
                conn,
                source=apple_analytics.SOURCE,
                report_date=window_end,
                started_at=started,
                status="error",
                note=message,
                app_id=app_id,
            )
            if progress:
                progress(store_id, "error")
            continue
        summary.ok += 1
        summary.rows += written
        summary.unmapped_rows += app_unmapped
        if app_unmapped:
            log.warning(
                "%s %s: %d rows matched no known app",
                apple_analytics.SOURCE,
                store_id,
                app_unmapped,
            )
        db.log_ingest(
            conn,
            source=apple_analytics.SOURCE,
            report_date=window_end,
            started_at=started,
            status="ok",
            rows=written,
            app_id=app_id,
        )
        if progress:
            progress(store_id, f"ok, {written} rows")
    return summary


def group_errors(errors: Iterable[tuple[object, str]]) -> list[str]:
    """One line per distinct failure.

    The same failure for many apps or days (e.g. a bucket 403 before Play permissions
    take effect) becomes one line naming them all, instead of one wall of text each.
    Each error's subject (its label's first word: a package, date or month) is blanked
    out of the message so identical failures compare equal.
    """
    groups: dict[str, tuple[str, list[str]]] = {}
    for label, message in errors:
        text = str(label)
        subject = text.split()[0] if text.strip() else ""
        key = message.replace(subject, "…") if subject else message
        groups.setdefault(key, (message, []))[1].append(text)
    lines = []
    for key, (first_message, labels) in groups.items():
        if len(labels) == 1:
            lines.append(f"{labels[0]}: {first_message}")
        else:
            lines.append(f"{len(labels)} failed the same way ({', '.join(labels)}): {key}")
    return lines


# -- Google Play ---------------------------------------------------------------------------


@dataclass
class PlaySummary:
    """Outcome of one Play source pass. Periods are months (files) or days (vitals)."""

    source: str
    ok: int = 0
    rows: int = 0
    not_ready: list[str] = field(default_factory=list)
    no_file: list[str] = field(default_factory=list)
    skipped: int = 0
    already_final: list[str] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # Nothing could be collected at all, e.g. a prefix that lists no files (usually a
    # missing permission). Not an ingest status, so reported here instead of the log.
    warnings: list[str] = field(default_factory=list)


def _error(
    conn: sqlite3.Connection,
    summary: PlaySummary,
    label: str,
    report_date: str,
    started: str,
    exc: Exception,
    *,
    app_id: int | None = None,
) -> None:
    message = redact(f"{type(exc).__name__}: {exc}")
    log.info("%s %s failed: %s", summary.source, label, message)  # caller reports it
    summary.errors.append((label, message))
    db.log_ingest(
        conn,
        source=summary.source,
        report_date=report_date,
        started_at=started,
        status="error",
        note=f"{label}: {message}",
        app_id=app_id,
    )


def _month_status(
    conn: sqlite3.Connection,
    summary: PlaySummary,
    label: str,
    month: Month,
    plan: str,
    progress: Callable[[str, str], None] | None,
    *,
    app_id: int | None = None,
) -> bool:
    """Handle every plan except "collect". Returns True if the month needs collecting."""
    if plan == "collect":
        return True
    started = db.utc_now()
    iso = month.first_day.isoformat()
    if plan == "skip":
        summary.skipped += 1
        return False
    if plan == "not_ready":
        summary.not_ready.append(label)
        db.log_ingest(
            conn,
            source=summary.source,
            report_date=iso,
            started_at=started,
            status="not_ready",
            app_id=app_id,
        )
        outcome = "not ready yet"
    else:
        summary.no_file.append(label)
        db.log_ingest(
            conn,
            source=summary.source,
            report_date=iso,
            started_at=started,
            status="ok",
            rows=0,
            note=f"{label}: no file",
            app_id=app_id,
        )
        outcome = "no file"
    if progress:
        progress(label, outcome)
    return False


def collect_play_installs(
    conn: sqlite3.Connection,
    client: GoogleClient,
    bucket: str,
    months: list[Month],
    *,
    today: date,
    progress: Callable[[str, str], None] | None = None,
) -> PlaySummary:
    """Installs per app and month; the overview (ALL) and country files are combined
    into one replacement so neither erases the other."""
    summary = PlaySummary(play_installs.SOURCE)
    client.check_token()
    for package, app_id in sorted(db.apps_for(conn, "android").items()):
        try:
            names = [
                o.name for o in client.list_objects(bucket, play_installs.object_prefix(package))
            ]
        except Exception as exc:
            _error(
                conn,
                summary,
                package,
                months[-1].first_day.isoformat(),
                db.utc_now(),
                exc,
                app_id=app_id,
            )
            continue
        files = play_installs.month_files(names, package)
        if not files:
            summary.warnings.append(
                f"no installs files visible for {package} under "
                f"gs://{bucket}/{play_installs.object_prefix(package)}. New apps take a day or "
                f"two to appear; otherwise check that the service account has "
                f"'{BULK_PERMISSION}' for this app."
            )
            continue
        for month in months:
            label = f"{package} {month}"
            plan = plan_month(month, set(files), today)
            if not _month_status(conn, summary, label, month, plan, progress, app_id=app_id):
                continue
            started = db.utc_now()
            try:
                parsed: dict[str, list[play_installs.InstallsRow]] = {}
                for kind, name in files[month].items():
                    text = decode_csv(client.download(bucket, name))
                    parsed[kind] = play_installs.parse_installs(
                        text, by_country=kind == "country", what=name
                    )
                rows = play_installs.to_metric_rows(
                    app_id, parsed.get("overview", []), parsed.get("country", [])
                )
                written = db.replace_source_range(
                    conn,
                    summary.source,
                    month.first_day.isoformat(),
                    month.last_day.isoformat(),
                    rows,
                    app_ids=[app_id],
                )
            except Exception as exc:
                _error(
                    conn, summary, label, month.first_day.isoformat(), started, exc, app_id=app_id
                )
                if progress:
                    progress(label, "error")
                continue
            summary.ok += 1
            summary.rows += written
            db.log_ingest(
                conn,
                source=summary.source,
                report_date=month.first_day.isoformat(),
                started_at=started,
                status="ok",
                rows=written,
                note=package,
                app_id=app_id,
            )
            if progress:
                progress(label, f"ok, {written} rows")
    return summary


def _collect_account_months(
    conn: sqlite3.Connection,
    client: GoogleClient,
    bucket: str,
    months: list[Month],
    *,
    source: str,
    prefix: str,
    month_files: Callable[[list[str]], dict[Month, list[str]]],
    load: Callable[[Month, list[str], dict[str, int]], tuple[int, str | None]],
    today: date,
    published_after_month_end: bool,
    progress: Callable[[str, str], None] | None,
    skip_if_final: bool,
) -> PlaySummary:
    """Account-wide monthly files (sales, earnings). The first month is the earliest file
    under the prefix, since these files cover every app in the account."""
    summary = PlaySummary(source)
    client.check_token()
    started = db.utc_now()
    try:
        files = month_files([o.name for o in client.list_objects(bucket, prefix)])
        if not files:
            # Listing can't tell "no reports yet" from "not allowed to see them", so the
            # warning names both causes: no Android revenue, or the missing permission.
            summary.warnings.append(
                f"no {source} files are visible under gs://{bucket}/{prefix}. If the account "
                "has no Android revenue yet, ignore this. Otherwise grant the service account "
                f"the optional '{FINANCIAL_PERMISSION}' permission (global) in Play Console > "
                "Users and permissions (see README, 'Google service account')."
            )
            return summary
    except Exception as exc:
        _error(conn, summary, prefix, months[-1].first_day.isoformat(), started, exc)
        return summary
    packages = db.apps_for(conn, "android")
    for month in months:
        label = str(month)
        if skip_if_final and db.get_kv(conn, play_earnings.done_key(month)):
            summary.already_final.append(label)
            if progress:
                progress(label, "final earnings already loaded")
            continue
        plan = plan_month(
            month, set(files), today, published_after_month_end=published_after_month_end
        )
        if not _month_status(conn, summary, label, month, plan, progress):
            continue
        started = db.utc_now()
        try:
            written, note = load(month, files[month], packages)
        except Exception as exc:
            _error(conn, summary, label, month.first_day.isoformat(), started, exc)
            if progress:
                progress(label, "error")
            continue
        if note:
            summary.notes.append(f"{label}: {note}")
            log.warning("%s %s: %s", source, label, note)
        summary.ok += 1
        summary.rows += written
        db.log_ingest(
            conn,
            source=source,
            report_date=month.first_day.isoformat(),
            started_at=started,
            status="ok",
            rows=written,
            note=note,
        )
        if progress:
            progress(label, f"ok, {written} rows")
    return summary


def collect_play_sales(
    conn: sqlite3.Connection,
    client: GoogleClient,
    bucket: str,
    months: list[Month],
    *,
    today: date,
    progress: Callable[[str, str], None] | None = None,
) -> PlaySummary:
    """Provisional proceeds from the daily-updated sales report. Months whose final
    earnings are loaded are skipped."""

    def load(month: Month, names: list[str], packages: dict[str, int]) -> tuple[int, str | None]:
        texts = play_sales.read_texts([client.download(bucket, n) for n in names], str(month))
        mapped = play_sales.map_month(month, texts, packages, f"sales report {month}")
        written = db.replace_source_range(
            conn,
            play_sales.SOURCE,
            month.first_day.isoformat(),
            month.last_day.isoformat(),
            mapped.rows,
        )
        return written, mapped.note()

    return _collect_account_months(
        conn,
        client,
        bucket,
        months,
        source=play_sales.SOURCE,
        prefix=play_sales.PREFIX,
        month_files=play_sales.month_files,
        load=load,
        today=today,
        published_after_month_end=False,
        progress=progress,
        skip_if_final=True,
    )


def collect_play_earnings(
    conn: sqlite3.Connection,
    client: GoogleClient,
    bucket: str,
    months: list[Month],
    *,
    today: date,
    progress: Callable[[str, str], None] | None = None,
) -> PlaySummary:
    """Final net proceeds. Replaces the month's provisional play_sales rows atomically."""

    def load(month: Month, names: list[str], packages: dict[str, int]) -> tuple[int, str | None]:
        texts = [read_zip_csv(client.download(bucket, n), n) for n in sorted(names)]
        mapped = play_earnings.map_month(month, texts, packages, f"earnings {month}")
        written = db.replace_source_range(
            conn,
            play_earnings.SOURCE,
            month.first_day.isoformat(),
            month.last_day.isoformat(),
            mapped.rows,
            clear_sources=(play_sales.SOURCE,),
            kv={play_earnings.done_key(month): db.utc_now()},
        )
        return written, mapped.note()

    return _collect_account_months(
        conn,
        client,
        bucket,
        months,
        source=play_earnings.SOURCE,
        prefix=play_earnings.PREFIX,
        month_files=play_earnings.month_files,
        load=load,
        today=today,
        published_after_month_end=True,
        progress=progress,
        skip_if_final=False,
    )


def collect_play_vitals(
    conn: sqlite3.Connection,
    client: GoogleClient,
    dates: list[date],
    *,
    progress: Callable[[str, str], None] | None = None,
) -> PlaySummary:
    """Daily and 28-day vitals per app. Days past the metric sets' freshness are
    not_ready."""
    summary = PlaySummary(play_vitals.SOURCE)
    client.check_token()
    if not dates:
        return summary
    for package, app_id in sorted(db.apps_for(conn, "android").items()):
        started = db.utc_now()
        try:
            latest_dates = [
                client.latest_daily_date(package, metric_set)
                for metric_set in play_vitals.METRIC_SETS
            ]
            latest = None if None in latest_dates else min(d for d in latest_dates if d)
            ready = [d for d in dates if latest is not None and d <= latest]
            results = (
                {
                    metric_set: client.query_metric_set(
                        package, metric_set, list(mapping), ready[0], ready[-1]
                    )
                    for metric_set, mapping in play_vitals.METRIC_SETS.items()
                }
                if ready
                else {}
            )
            wanted = {d.isoformat() for d in ready}
            rows = [r for r in play_vitals.to_metric_rows(app_id, results) if r.date in wanted]
            if ready:
                db.replace_source_range(
                    conn,
                    summary.source,
                    ready[0].isoformat(),
                    ready[-1].isoformat(),
                    rows,
                    app_ids=[app_id],
                )
        except Exception as exc:
            _error(conn, summary, package, dates[-1].isoformat(), started, exc, app_id=app_id)
            if progress:
                progress(package, "error")
            continue
        per_day = Counter(r.date for r in rows)
        for day in dates:
            iso = day.isoformat()
            if day in ready:
                summary.ok += 1
                summary.rows += per_day[iso]
                db.log_ingest(
                    conn,
                    source=summary.source,
                    report_date=iso,
                    started_at=started,
                    status="ok",
                    rows=per_day[iso],
                    note=package,
                    app_id=app_id,
                )
            else:
                summary.not_ready.append(f"{package} {iso}")
                db.log_ingest(
                    conn,
                    source=summary.source,
                    report_date=iso,
                    started_at=started,
                    status="not_ready",
                    note=package,
                    app_id=app_id,
                )
        if progress:
            no_data = ", no data from Google" if ready and not rows else ""
            progress(package, f"{len(ready)} days ok, {len(dates) - len(ready)} not ready{no_data}")
    return summary


# -- one full run: lock, collect every configured source, then the digest ------------------

LOCK_FILENAME = "run.lock"
STALE_LOCK_HOURS = 6


class LockedError(Exception):
    """Another run appears to be in progress (a non-stale lock file exists)."""


def _lock_is_stale(path: Path) -> bool:
    try:
        age_seconds = time.time() - path.stat().st_mtime
    except OSError:
        return True
    return age_seconds > STALE_LOCK_HOURS * 3600


@contextlib.contextmanager
def _run_lock(data_dir: Path) -> Iterator[None]:
    """A lock file created with O_CREAT|O_EXCL, so only one run proceeds at a time.

    Works the same way on all three OSes. A lock older than STALE_LOCK_HOURS is assumed to
    belong to a crashed run and is taken over; anything younger raises LockedError.
    """
    path = data_dir / LOCK_FILENAME
    data_dir.mkdir(parents=True, exist_ok=True)
    payload = f"{os.getpid()} {db.utc_now()}\n".encode()
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        if not _lock_is_stale(path):
            raise LockedError(
                f"another run appears to be in progress ({path}); if it isn't, delete that file"
            ) from None
        log.warning(
            "%s is older than %d hours; assuming a crashed run and taking it over",
            path,
            STALE_LOCK_HOURS,
        )
        with contextlib.suppress(OSError):
            os.unlink(path)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(fd, payload)
        os.close(fd)
        yield
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)


@dataclass
class RunClients:
    """API clients and the SMTP password for one run, built from a secret store."""

    apple: AppleClient | None = None
    google: GoogleClient | None = None
    smtp_password: str | None = None
    # A platform whose credential is missing or broken: reported like a source error,
    # never raised, so the other platform still runs (docs/SPEC.md).
    errors: list[str] = field(default_factory=list)

    def close(self) -> None:
        for client in (self.apple, self.google):
            if client is not None:
                client.close()


def clients_from_store(
    store: SecretStore,
    cfg: config.Config,
    *,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] | None = None,
) -> RunClients:
    """Build the clients a run needs. Shared by `storepulse run` and the hosted scheduler,
    so both treat a missing or corrupt credential the same way."""
    clients = RunClients()
    if cfg.apple is not None:
        p8 = store.get(APPLE_P8_SECRET)
        if p8 is None:
            clients.errors.append(
                f"apple_sales: no Apple key found in {store.describe()}; set it up again"
            )
        else:
            try:
                clients.apple = AppleClient(
                    cfg.apple.issuer_id, cfg.apple.key_id, p8, transport=transport, sleep=sleep
                )
            except ValueError as exc:
                clients.errors.append(f"apple_sales: {exc}")
    if cfg.google is not None:
        raw = store.get(GOOGLE_SA_SECRET)
        if raw is None:
            clients.errors.append(
                f"google_auth: no Google service account found in {store.describe()}; "
                "set it up again"
            )
        else:
            try:
                sa = load_service_account(raw)
            except GoogleCredentialError as exc:
                clients.errors.append(f"google_auth: {exc}")
            else:
                clients.google = GoogleClient(sa, transport=transport, sleep=sleep)
    if cfg.email is not None:
        clients.smtp_password = store.get(SMTP_PASSWORD_SECRET)
    return clients


@dataclass
class RunAllResult:
    errors: list[str] = field(default_factory=list)
    email_sent: bool = False
    email_error: str | None = None
    digest: digest.Digest | None = None

    @property
    def ok(self) -> bool:
        return not self.errors and self.email_error is None


def _run_apple(
    conn: sqlite3.Connection,
    cfg: config.Config,
    client: AppleClient,
    days: int,
    today: date,
    progress: Callable[[str, str], None],
    sleep: Callable[[float], None] | None = None,
) -> str | None:
    assert cfg.apple is not None
    yesterday = today - timedelta(days=1)
    dates = [yesterday - timedelta(days=i) for i in reversed(range(days))]
    try:
        summary = collect_apple_sales(
            conn,
            client,
            cfg.apple.vendor_number,
            dates,
            today=today,
            progress=lambda d, outcome: progress(d.isoformat(), outcome),
            sleep=sleep,
        )
    except Exception as exc:  # one failing source never blocks the others or the digest
        message = redact(f"{type(exc).__name__}: {exc}")
        log.error("apple_sales failed: %s", message)
        return f"apple_sales: {message}"
    if summary.errors:
        return f"apple_sales: {len(summary.errors)} of {len(dates)} day(s) failed: " + "; ".join(
            group_errors(summary.errors)
        )
    return None


def _run_apple_subscriptions(
    conn: sqlite3.Connection,
    cfg: config.Config,
    client: AppleClient,
    days: int,
    today: date,
    progress: Callable[[str, str], None],
    sleep: Callable[[float], None] | None = None,
) -> str | None:
    assert cfg.apple is not None
    yesterday = today - timedelta(days=1)
    dates = [yesterday - timedelta(days=i) for i in reversed(range(days))]
    try:
        summary = collect_apple_subscriptions(
            conn,
            client,
            cfg.apple.vendor_number,
            dates,
            today=today,
            progress=lambda d, outcome: progress(d.isoformat(), outcome),
            sleep=sleep,
        )
    except Exception as exc:  # one failing source never blocks the others or the digest
        message = redact(f"{type(exc).__name__}: {exc}")
        log.error("apple_subscriptions failed: %s", message)
        return f"apple_subscriptions: {message}"
    if summary.errors:
        return (
            f"apple_subscriptions: {len(summary.errors)} of {len(dates)} day(s) failed: "
            + "; ".join(group_errors(summary.errors))
        )
    return None


def _run_apple_subscription_events(
    conn: sqlite3.Connection,
    cfg: config.Config,
    client: AppleClient,
    days: int,
    today: date,
    progress: Callable[[str, str], None],
    sleep: Callable[[float], None] | None = None,
) -> str | None:
    assert cfg.apple is not None
    yesterday = today - timedelta(days=1)
    dates = [yesterday - timedelta(days=i) for i in reversed(range(days))]
    try:
        summary = collect_apple_subscription_events(
            conn,
            client,
            cfg.apple.vendor_number,
            dates,
            today=today,
            progress=lambda d, outcome: progress(d.isoformat(), outcome),
            sleep=sleep,
        )
    except Exception as exc:  # one failing source never blocks the others or the digest
        message = redact(f"{type(exc).__name__}: {exc}")
        log.error("apple_subscription_events failed: %s", message)
        return f"apple_subscription_events: {message}"
    if summary.errors:
        return (
            f"apple_subscription_events: {len(summary.errors)} of {len(dates)} day(s) failed: "
            + "; ".join(group_errors(summary.errors))
        )
    return None


def _run_apple_analytics(
    conn: sqlite3.Connection,
    cfg: config.Config,
    client: AppleClient,
    days: int,
    today: date,
    progress: Callable[[str, str], None],
    sleep: Callable[[float], None] | None = None,
) -> str | None:
    """Per-app internally (collect_apple_analytics), but this wrapper's own signature is
    identical to its account-wide siblings, since run_all() only needs a uniform
    (conn, cfg, client, days, today, progress, sleep) -> str | None call regardless of what
    happens inside. ``sleep`` is accepted but unused: AppleClient already retries/backs off
    internally, and this source has no per-date loop of its own to pace."""
    assert cfg.apple is not None
    yesterday = today - timedelta(days=1)
    dates = [yesterday - timedelta(days=i) for i in reversed(range(days))]
    try:
        summary = collect_apple_analytics(conn, client, dates, progress=progress)
    except Exception as exc:  # one failing source never blocks the others or the digest
        message = redact(f"{type(exc).__name__}: {exc}")
        log.error("apple_analytics failed: %s", message)
        return f"apple_analytics: {message}"
    if summary.errors:
        return f"apple_analytics: {len(summary.errors)} app(s) failed: " + "; ".join(
            group_errors(summary.errors)
        )
    return None


def _run_play_source(
    label: str,
    collect: Callable[..., PlaySummary],
    conn: sqlite3.Connection,
    client: GoogleClient,
    bucket: str,
    months: list[Month],
    today: date,
    progress: Callable[[str, str], None],
) -> str | None:
    try:
        summary = collect(conn, client, bucket, months, today=today, progress=progress)
    except Exception as exc:
        message = redact(f"{type(exc).__name__}: {exc}")
        log.error("%s failed: %s", label, message)
        return f"{label}: {message}"
    if summary.errors:
        return f"{label}: {len(summary.errors)} failed: " + "; ".join(group_errors(summary.errors))
    return None


def _run_play_vitals(
    conn: sqlite3.Connection,
    client: GoogleClient,
    dates: list[date],
    progress: Callable[[str, str], None],
) -> str | None:
    try:
        summary = collect_play_vitals(conn, client, dates, progress=progress)
    except Exception as exc:
        message = redact(f"{type(exc).__name__}: {exc}")
        log.error("play_vitals failed: %s", message)
        return f"play_vitals: {message}"
    if summary.errors:
        return f"play_vitals: {len(summary.errors)} failed: " + "; ".join(
            group_errors(summary.errors)
        )
    return None


def run_all(
    conn: sqlite3.Connection,
    cfg: config.Config,
    *,
    apple_client: AppleClient | None = None,
    google_client: GoogleClient | None = None,
    smtp_password: str | None = None,
    days: int | None = None,
    send_email: bool = True,
    today: date | None = None,
    smtp_factory: mailer.SMTPFactory | None = None,
    progress: Callable[[str, str], None] | None = None,
    dashboard_url: str | None = None,
    sleep: Callable[[float], None] | None = None,
) -> RunAllResult:
    """Collect every configured source independently, then build and send the digest.

    One failing source never blocks the others or the digest (docs/SPEC.md). Callers
    build ``apple_client``/``google_client`` from the secret store themselves (this module
    never touches secrets directly), and pass ``None`` for a platform that isn't
    configured or whose client couldn't be built.
    """
    days = days if days is not None else cfg.schedule.days
    today = today if today is not None else apple_sales.pacific_today()
    report: Callable[[str, str], None] = progress or (lambda _label, _outcome: None)
    result = RunAllResult()

    with _run_lock(config.data_dir()):
        if cfg.apple is not None and apple_client is not None:
            for run_source in (
                _run_apple,
                _run_apple_subscriptions,
                _run_apple_subscription_events,
                _run_apple_analytics,  # per-app internally; the wrapper's own shape is uniform
            ):
                error = run_source(conn, cfg, apple_client, days, today, report, sleep)
                if error:
                    result.errors.append(error)

        if cfg.google is not None and google_client is not None:
            bucket = parse_bucket_uri(cfg.google.bucket_uri)
            try:
                found = discovery.refresh_play_apps(google_client, conn, bucket)
                if found.status != "ok":
                    report("play_discovery", found.message)
            except Exception as exc:
                message = redact(f"{type(exc).__name__}: {exc}")
                log.error("play_discovery failed: %s", message)
                result.errors.append(f"play_discovery: {message}")

            current_month = Month.of(today)
            months = sorted({current_month.prev(), current_month})
            earnings_months = sorted({current_month.prev().prev(), current_month.prev()})
            # Ends at yesterday, like apple_sales: play_vitals is also a daily-cadence
            # source (docs/SPEC.md), so today's data isn't expected to be ready yet.
            vitals_yesterday = today - timedelta(days=1)
            vitals_dates = [vitals_yesterday - timedelta(days=i) for i in reversed(range(days))]

            for label, collect, periods in (
                ("play_installs", collect_play_installs, months),
                ("play_sales", collect_play_sales, months),
                ("play_earnings", collect_play_earnings, earnings_months),
            ):
                error = _run_play_source(
                    label, collect, conn, google_client, bucket, periods, today, report
                )
                if error:
                    result.errors.append(error)

            vitals_error = _run_play_vitals(conn, google_client, vitals_dates, report)
            if vitals_error:
                result.errors.append(vitals_error)

        built = digest.build_digest(conn, cfg, today=today, dashboard_url=dashboard_url)
        result.digest = built

        if send_email:
            if cfg.email is None or smtp_password is None:
                result.email_error = "email isn't configured yet; run `storepulse init`"
            else:
                try:
                    email_message = digest.to_email_message(built, cfg.email)
                    mailer.send(cfg.email, smtp_password, email_message, factory=smtp_factory)
                    result.email_sent = True
                except mailer.MailerError as exc:
                    result.email_error = str(exc)

    return result
