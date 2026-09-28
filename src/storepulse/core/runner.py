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
from storepulse.core.sources import (
    apple_sales,
    play_earnings,
    play_installs,
    play_sales,
    play_vitals,
)
from storepulse.core.sources.apple_client import AppleAuthError, AppleClient, AppleVendorError
from storepulse.core.sources.google_client import GoogleClient
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


def _error(
    conn: sqlite3.Connection,
    summary: PlaySummary,
    label: str,
    report_date: str,
    started: str,
    exc: Exception,
) -> None:
    message = redact(f"{type(exc).__name__}: {exc}")
    log.error("%s %s failed: %s", summary.source, label, message)
    summary.errors.append((label, message))
    db.log_ingest(
        conn,
        source=summary.source,
        report_date=report_date,
        started_at=started,
        status="error",
        note=f"{label}: {message}",
    )


def _month_status(
    conn: sqlite3.Connection,
    summary: PlaySummary,
    label: str,
    month: Month,
    plan: str,
    progress: Callable[[str, str], None] | None,
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
            conn, source=summary.source, report_date=iso, started_at=started, status="not_ready"
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
            _error(conn, summary, package, months[-1].first_day.isoformat(), db.utc_now(), exc)
            continue
        files = play_installs.month_files(names, package)
        for month in months:
            label = f"{package} {month}"
            plan = plan_month(month, set(files), today)
            if not _month_status(conn, summary, label, month, plan, progress):
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
                _error(conn, summary, label, month.first_day.isoformat(), started, exc)
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
            _error(conn, summary, package, dates[-1].isoformat(), started, exc)
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
                )
        if progress:
            progress(package, f"{len(ready)} days ok, {len(dates) - len(ready)} not ready")
    return summary
