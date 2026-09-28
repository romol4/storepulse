"""`storepulse backfill`: historical loads per source."""

from __future__ import annotations

import argparse
import sqlite3
from datetime import date, timedelta

from storepulse.cli.common import (
    APPLE_P8_SECRET,
    GOOGLE_SA_SECRET,
    CliError,
    Env,
    apple_client,
    google_client,
    store_for_run,
)
from storepulse.core import config, db, runner
from storepulse.core.sources import (
    apple_sales,
    play_earnings,
    play_installs,
    play_sales,
    play_vitals,
)
from storepulse.core.sources.google_auth import GoogleCredentialError, load_service_account
from storepulse.core.sources.google_client import parse_bucket_uri
from storepulse.core.sources.play_common import Month, last_months, month_range

PLAY_MONTHLY = (play_installs.SOURCE, play_sales.SOURCE, play_earnings.SOURCE)
SOURCES = (apple_sales.SOURCE, *PLAY_MONTHLY, play_vitals.SOURCE)


def cmd_backfill(args: argparse.Namespace, env: Env) -> int:
    cfg = config.load()
    source = args.source
    yesterday = env.pacific_today() - timedelta(days=1)
    conn = db.connect(config.db_path())
    try:
        if source == apple_sales.SOURCE:
            if args.months is not None:
                raise CliError("apple_sales takes --days or --from YYYY-MM-DD, not --months")
            start, end = _day_span(args, yesterday)
            return backfill_apple(env, conn, cfg, start, end)
        if source in PLAY_MONTHLY:
            if args.days is not None:
                raise CliError(f"{source} is loaded by month: use --months N or --from YYYY-MM")
            months = _month_span(args, env.pacific_today())
            return backfill_play(env, conn, cfg, [source], months=months)
        start, end = _day_span(args, yesterday)
        dates = [start + timedelta(days=i) for i in range((end - start).days + 1)]
        return backfill_play(env, conn, cfg, [source], dates=dates)
    finally:
        conn.close()


def _day_span(args: argparse.Namespace, yesterday: date) -> tuple[date, date]:
    if args.days is not None:
        if args.days < 1:
            raise CliError("--days must be at least 1")
        return yesterday - timedelta(days=args.days - 1), yesterday
    if args.from_date is None:
        raise CliError("use --days N or --from YYYY-MM-DD")
    try:
        start = date.fromisoformat(args.from_date)
        end = date.fromisoformat(args.to_date) if args.to_date else yesterday
    except ValueError:
        raise CliError("dates must be YYYY-MM-DD") from None
    if end < start:
        raise CliError("--to is before --from")
    return start, end


def _month_span(args: argparse.Namespace, today: date) -> list[Month]:
    if args.months is not None:
        if args.months < 1:
            raise CliError("--months must be at least 1")
        return last_months(args.months, today)
    if args.from_date is None:
        raise CliError("use --months N or --from YYYY-MM")
    try:
        start = Month.parse(args.from_date)
        end = Month.parse(args.to_date) if args.to_date else Month.of(today)
    except ValueError as exc:
        raise CliError(str(exc)) from None
    if end < start:
        raise CliError("--to is before --from")
    return month_range(start, min(end, Month.of(today)))


# -- Apple ---------------------------------------------------------------------------------


def backfill_apple(
    env: Env, conn: sqlite3.Connection, cfg: config.Config, start: date, end: date
) -> int:
    if cfg.apple is None:
        raise CliError("Apple isn't set up yet; run `storepulse init` first")
    store = store_for_run(env, cfg)
    p8 = store.get(APPLE_P8_SECRET)
    if p8 is None:
        raise CliError(f"no Apple key found in {store.describe()}; run `storepulse init` again")
    try:
        span = runner.apple_sales_range(start, end, today=env.pacific_today())
    except ValueError as exc:
        raise CliError(str(exc)) from None
    if span.skipped:
        first, last = span.skipped
        env.warn(
            "Apple keeps daily sales reports for about a year; "
            f"skipped {first} to {last} (not requested, nothing recorded)."
        )
    if not span.dates:
        env.say("Nothing to load.")
        return 0
    env.say(f"Loading apple_sales {span.dates[0]} > {span.dates[-1]} ({len(span.dates)} days)...")
    with apple_client(env, cfg.apple, p8) as client:
        summary = runner.collect_apple_sales(
            conn,
            client,
            cfg.apple.vendor_number,
            span.dates,
            today=env.pacific_today(),
            sleep=env.sleep,
            progress=lambda d, outcome: env.say(f"  {d}  {outcome}"),
        )
    env.say()
    env.say(
        f"Done: {summary.ok} days loaded ({summary.rows} rows), "
        f"{summary.no_sales + summary.inferred_no_sales} with no sales, "
        f"{len(summary.not_ready)} not ready, {len(summary.errors)} errors."
    )
    if summary.new_apps:
        env.say(f"New apps from reports: {', '.join(sorted(set(summary.new_apps)))}")
    if summary.kept_rows:
        env.warn(
            f"{len(summary.kept_rows)} day(s) returned an unexplained 404; stored data was kept: "
            + ", ".join(d.isoformat() for d in summary.kept_rows)
        )
    if summary.unknown_types:
        codes = ", ".join(f"{c} ({n} rows)" for c, n in sorted(summary.unknown_types.items()))
        env.warn(
            f"unrecognized product types (only proceeds counted): {codes}. "
            "Please report them so they can be mapped."
        )
    if summary.remapped:
        env.say(
            f"Re-mapped in-app purchases for {len(summary.remapped)} day(s) once their "
            "parent app appeared in later reports."
        )
    if summary.unmapped_rows:
        env.warn(f"{summary.unmapped_rows} report rows matched no known app and were skipped.")
    if summary.unmapped_child_rows:
        env.warn(
            f"{summary.unmapped_child_rows} in-app purchase rows belong to an app Storepulse "
            "hasn't seen yet. Re-run this backfill after the app has a download in the "
            "range (or once the key can list apps) to pick them up."
        )
    for day, message in summary.errors:
        env.warn(f"{day}: {message}")
    return 1 if summary.errors else 0


# -- Google Play ---------------------------------------------------------------------------


def backfill_play(
    env: Env,
    conn: sqlite3.Connection,
    cfg: config.Config,
    sources: list[str],
    *,
    months: list[Month] | None = None,
    dates: list[date] | None = None,
) -> int:
    if cfg.google is None:
        raise CliError("Google Play isn't set up yet; run `storepulse init` first")
    store = store_for_run(env, cfg)
    raw = store.get(GOOGLE_SA_SECRET)
    if raw is None:
        raise CliError(
            f"no Google service account found in {store.describe()}; run `storepulse init` again"
        )
    try:
        sa = load_service_account(raw)
        bucket = parse_bucket_uri(cfg.google.bucket_uri)
    except (GoogleCredentialError, ValueError) as exc:
        raise CliError(str(exc)) from None
    if not db.apps_for(conn, "android"):
        env.warn("no Android apps are registered yet; run `storepulse init` to discover them.")
    today = env.pacific_today()
    failed = False
    with google_client(env, sa) as client:
        for source in sources:
            env.say(f"Loading {source}...")

            def progress(label: str, outcome: str) -> None:
                env.say(f"  {label}  {outcome}")

            if source == play_installs.SOURCE:
                summary = runner.collect_play_installs(
                    conn, client, bucket, months or [], today=today, progress=progress
                )
            elif source == play_sales.SOURCE:
                summary = runner.collect_play_sales(
                    conn, client, bucket, months or [], today=today, progress=progress
                )
            elif source == play_earnings.SOURCE:
                summary = runner.collect_play_earnings(
                    conn, client, bucket, months or [], today=today, progress=progress
                )
            else:
                summary = runner.collect_play_vitals(conn, client, dates or [], progress=progress)
            _report_play(env, summary)
            failed = failed or bool(summary.errors)
    return 1 if failed else 0


def _report_play(env: Env, summary: runner.PlaySummary) -> None:
    unit = "days" if summary.source == play_vitals.SOURCE else "periods"
    env.say(
        f"Done: {summary.ok} {unit} loaded ({summary.rows} rows), "
        f"{len(summary.not_ready)} not ready, {len(summary.no_file)} without a file, "
        f"{len(summary.errors)} errors."
    )
    if summary.already_final:
        env.say(
            "Final earnings already replace provisional sales for: "
            + ", ".join(summary.already_final)
        )
    for note in summary.notes:
        env.warn(note)
    for label, message in summary.errors:
        env.warn(f"{label}: {message}")
    env.say()
