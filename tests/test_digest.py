from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pytest

from storepulse.core import config, db, digest

APPLE_CFG = config.AppleConfig(issuer_id="i", key_id="k", vendor_number="1")
GOOGLE_CFG = config.GoogleConfig(
    bucket_uri="gs://x/", service_account_email="a@b.iam.gserviceaccount.com"
)


def _cfg(**kwargs: object) -> config.Config:
    defaults: dict[str, object] = {"apple": APPLE_CFG, "google": GOOGLE_CFG}
    defaults.update(kwargs)
    return config.Config(**defaults)  # type: ignore[arg-type]


def _apple_day(
    conn: sqlite3.Connection, d: str, app_id: int, installs: float, proceeds: float
) -> None:
    rows = []
    if installs:
        rows.append(db.MetricRow(d, app_id, "US", "installs", "", installs))
    if proceeds:
        rows.append(db.MetricRow(d, app_id, "US", "proceeds", "USD", proceeds))
    db.replace_source_day(conn, "apple_sales", d, rows)


def _android_installs_day(conn: sqlite3.Connection, d: str, app_id: int, installs: float) -> None:
    db.replace_source_range(
        conn, "play_installs", d, d, [db.MetricRow(d, app_id, "ALL", "installs", "", installs)]
    )


def _android_earnings_day(
    conn: sqlite3.Connection, d: str, app_id: int, proceeds: float, currency: str = "USD"
) -> None:
    db.replace_source_day(
        conn, "play_earnings", d, [db.MetricRow(d, app_id, "US", "proceeds", currency, proceeds)]
    )


def _log(
    conn: sqlite3.Connection,
    source: str,
    report_date: str,
    status: str = "ok",
    app_id: int | None = None,
) -> None:
    db.log_ingest(
        conn,
        source=source,
        report_date=report_date,
        started_at=db.utc_now(),
        status=status,
        rows=1,
        app_id=app_id,
    )


AS_OF = date(2026, 9, 26)
TODAY = date(2026, 9, 27)


def _table_row(text: str, name: str) -> str:
    """The plain-text per-app table row for ``name``, e.g. "deskFT (iOS)"."""
    (row,) = [line for line in text.splitlines() if line.startswith(f"  {name} ")]
    return row


def _seed_basic(conn: sqlite3.Connection, ios_id: int, android_id: int) -> None:
    """One week (Sep 20-26) plus the previous week (Sep 13-19) of installs/proceeds."""
    for i in range(14):
        d = (AS_OF - timedelta(days=i)).isoformat()
        _apple_day(conn, d, ios_id, installs=10.0, proceeds=5.0)
        _android_installs_day(conn, d, android_id, installs=8.0)
    _log(conn, "apple_sales", AS_OF.isoformat())
    _log(conn, "play_installs", "2026-09-01")
    _log(conn, "play_vitals", AS_OF.isoformat())


@pytest.fixture
def ios_id(conn: sqlite3.Connection) -> int:
    return db.upsert_app(conn, "ios", "100", "deskFT")


@pytest.fixture
def android_id(conn: sqlite3.Connection) -> int:
    return db.upsert_app(conn, "android", "com.example.billft", "billFT")


# -- as-of ---------------------------------------------------------------------------------


def test_as_of_is_minimum_across_configured_platforms(
    conn: sqlite3.Connection, ios_id: int, android_id: int
) -> None:
    _apple_day(conn, "2026-09-26", ios_id, installs=1, proceeds=0)
    _android_installs_day(conn, "2026-09-24", android_id, installs=1)
    assert digest.compute_as_of(conn, _cfg()) == date(2026, 9, 24)


def test_as_of_ignores_unconfigured_platforms(
    conn: sqlite3.Connection, ios_id: int, android_id: int
) -> None:
    _apple_day(conn, "2026-09-26", ios_id, installs=1, proceeds=0)
    _android_installs_day(conn, "2026-09-01", android_id, installs=1)
    assert digest.compute_as_of(conn, _cfg(google=None)) == date(2026, 9, 26)


def test_as_of_recognizes_a_real_zero_sales_day(conn: sqlite3.Connection, ios_id: int) -> None:
    """A genuine zero-installs, zero-proceeds day writes no daily_metrics rows at all
    (there's nothing to store), even though the report was collected successfully — so
    as-of must still advance to it via ingest_log, not understate freshness by reading
    daily_metrics alone (review nit #7).
    """
    _apple_day(conn, "2026-09-25", ios_id, installs=3, proceeds=1)
    _log(conn, "apple_sales", "2026-09-25")
    _log(conn, "apple_sales", "2026-09-26", status="ok")  # zero-sales day: no daily_metrics row
    assert digest.compute_as_of(conn, _cfg(google=None)) == date(2026, 9, 26)


def test_as_of_keeps_older_data_fresh_after_a_later_retry_errors(
    conn: sqlite3.Connection, ios_id: int
) -> None:
    """A later re-attempt for an already-collected day can log a fresh 'error' without
    touching that day's still-good data; as-of must still reflect the real data, not
    fall back to an earlier, staler ingest_log 'ok' date (review nit #7).
    """
    _apple_day(conn, "2026-09-26", ios_id, installs=3, proceeds=1)
    _log(conn, "apple_sales", "2026-09-26")
    _log(conn, "apple_sales", "2026-09-26", status="error")  # same-day retry fails later
    assert digest.compute_as_of(conn, _cfg(google=None)) == date(2026, 9, 26)


def test_no_data_yet_returns_placeholder_digest(conn: sqlite3.Connection) -> None:
    result = digest.build_digest(conn, _cfg(), today=TODAY)
    assert result.as_of is None
    assert "No data has been collected yet" in result.text
    assert result.today is None


# -- installs / week-over-week --------------------------------------------------------------


def test_combined_installs_and_week_over_week(
    conn: sqlite3.Connection, ios_id: int, android_id: int
) -> None:
    for i in range(7):
        d = (AS_OF - timedelta(days=i)).isoformat()
        _apple_day(conn, d, ios_id, installs=10.0, proceeds=0)
        _android_installs_day(conn, d, android_id, installs=10.0)
    for i in range(7, 14):
        d = (AS_OF - timedelta(days=i)).isoformat()
        _apple_day(conn, d, ios_id, installs=5.0, proceeds=0)
        _android_installs_day(conn, d, android_id, installs=5.0)
    _log(conn, "apple_sales", AS_OF.isoformat())
    _log(conn, "play_installs", "2026-09-01")
    _log(conn, "play_vitals", AS_OF.isoformat())

    result = digest.build_digest(conn, _cfg(), today=TODAY)
    assert result.as_of == AS_OF
    # this week: 140 (70 iOS + 70 Android), last week: 70 -> +100%
    assert "Installs  140 (+100% wk)   iOS 70 · Android 70" in result.text


def test_zero_previous_week_shows_new(
    conn: sqlite3.Connection, ios_id: int, android_id: int
) -> None:
    for i in range(7):
        d = (AS_OF - timedelta(days=i)).isoformat()
        _apple_day(conn, d, ios_id, installs=10.0, proceeds=0)
    _log(conn, "apple_sales", AS_OF.isoformat())

    result = digest.build_digest(conn, _cfg(google=None), today=TODAY)
    assert "Installs  70 (new)   iOS 70" in result.text


def test_both_weeks_zero_omits_trend(conn: sqlite3.Connection, ios_id: int) -> None:
    _apple_day(conn, AS_OF.isoformat(), ios_id, installs=0, proceeds=0)
    _log(conn, "apple_sales", AS_OF.isoformat())
    # apple_sales writes no rows for a truly-zero day, so force a row on another metric
    # to make as-of resolvable, then confirm the header line has no parenthetical.
    db.replace_source_day(
        conn,
        "apple_sales",
        AS_OF.isoformat(),
        [db.MetricRow(AS_OF.isoformat(), ios_id, "US", "installs", "", 0.0)],
    )
    result = digest.build_digest(conn, _cfg(google=None), today=TODAY)
    assert "Installs  0   iOS 0" in result.text
    assert "wk)" not in result.text.splitlines()[1]


def test_only_configured_platform_shown_in_breakdown(conn: sqlite3.Connection, ios_id: int) -> None:
    _apple_day(conn, AS_OF.isoformat(), ios_id, installs=12.0, proceeds=0)
    _log(conn, "apple_sales", AS_OF.isoformat())
    result = digest.build_digest(conn, _cfg(google=None), today=TODAY)
    assert "iOS 12" in result.text
    assert "Android" not in result.text.splitlines()[1]


# -- lifetime installs ------------------------------------------------------------------------


def test_lifetime_installs_spans_more_than_the_weekly_window(
    conn: sqlite3.Connection, ios_id: int, android_id: int
) -> None:
    """21 days of data: the weekly line only sees the most recent 7, but the lifetime
    line sums all 21 -- the whole point of the feature."""
    for i in range(21):
        d = (AS_OF - timedelta(days=i)).isoformat()
        _apple_day(conn, d, ios_id, installs=10.0, proceeds=0)
        _android_installs_day(conn, d, android_id, installs=4.0)
    _log(conn, "apple_sales", AS_OF.isoformat())
    _log(conn, "play_installs", "2026-09-01")
    _log(conn, "play_vitals", AS_OF.isoformat())

    result = digest.build_digest(conn, _cfg(), today=TODAY)
    assert "Installs  98 (+0% wk)   iOS 70 · Android 28" in result.text
    assert "Lifetime  294   iOS 210 · Android 84" in result.text
    assert "Lifetime installs: 294" in result.html
    assert "iOS 210" in result.html and "Android 84" in result.html


def test_lifetime_installs_excludes_redownloads(conn: sqlite3.Connection, ios_id: int) -> None:
    """apple_sales' redownloads metric is tracked separately (docs/SPEC.md) and must not
    inflate the lifetime installs figure, the same way it's excluded from the weekly one."""
    db.replace_source_day(
        conn,
        "apple_sales",
        AS_OF.isoformat(),
        [
            db.MetricRow(AS_OF.isoformat(), ios_id, "US", "installs", "", 10.0),
            db.MetricRow(AS_OF.isoformat(), ios_id, "US", "redownloads", "", 500.0),
        ],
    )
    _log(conn, "apple_sales", AS_OF.isoformat())
    result = digest.build_digest(conn, _cfg(google=None), today=TODAY)
    assert "Lifetime  10   iOS 10" in result.text


def test_lifetime_installs_shows_dash_for_unavailable_platform(conn: sqlite3.Connection) -> None:
    _email_like_account(conn)
    result = digest.build_digest(conn, _cfg(), today=TODAY)
    lifetime_line = next(line for line in result.text.splitlines() if line.startswith("Lifetime"))
    assert lifetime_line == "Lifetime  29   iOS 29 · Android —"


def test_lifetime_installs_only_configured_platform_shown(
    conn: sqlite3.Connection, ios_id: int
) -> None:
    _apple_day(conn, AS_OF.isoformat(), ios_id, installs=12.0, proceeds=0)
    _log(conn, "apple_sales", AS_OF.isoformat())
    result = digest.build_digest(conn, _cfg(google=None), today=TODAY)
    lifetime_line = next(line for line in result.text.splitlines() if line.startswith("Lifetime"))
    assert lifetime_line == "Lifetime  12   iOS 12"


# -- proceeds / currency ---------------------------------------------------------------------


def test_per_currency_proceeds_and_android_isnt_double_counted(
    conn: sqlite3.Connection, ios_id: int, android_id: int
) -> None:
    _apple_day(conn, AS_OF.isoformat(), ios_id, installs=1, proceeds=100.0)  # USD
    db.replace_source_day(
        conn,
        "play_earnings",
        AS_OF.isoformat(),
        [
            db.MetricRow(AS_OF.isoformat(), android_id, "US", "proceeds", "USD", 30.0),
            db.MetricRow(AS_OF.isoformat(), android_id, "CA", "proceeds", "CAD", 10.0),
        ],
    )
    # An ALL row would double-count if the renderer summed it in with the countries;
    # play_installs is the only source that ever writes one, and only for installs.
    _android_installs_day(conn, AS_OF.isoformat(), android_id, installs=5.0)
    db.replace_source_range(
        conn,
        "play_installs",
        AS_OF.isoformat(),
        AS_OF.isoformat(),
        [
            db.MetricRow(AS_OF.isoformat(), android_id, "ALL", "installs", "", 5.0),
            db.MetricRow(AS_OF.isoformat(), android_id, "US", "installs", "", 3.0),
            db.MetricRow(AS_OF.isoformat(), android_id, "DE", "installs", "", 2.0),
        ],
    )
    _log(conn, "apple_sales", AS_OF.isoformat())
    _log(conn, "play_installs", "2026-09-01")

    result = digest.build_digest(conn, _cfg(), today=TODAY)
    assert "Android 5" in result.text  # not 5 (ALL) + 3 + 2 = 10
    assert "$130.00" in result.text  # USD: 100 (apple) + 30 (earnings)
    assert "CA$10.00" in result.text


def test_converted_total_only_when_rates_configured(conn: sqlite3.Connection, ios_id: int) -> None:
    _apple_day(conn, AS_OF.isoformat(), ios_id, installs=1, proceeds=100.0)
    _log(conn, "apple_sales", AS_OF.isoformat())

    without_rates = digest.build_digest(conn, _cfg(google=None), today=TODAY)
    assert "converted" not in without_rates.text

    with_rates = digest.build_digest(
        conn,
        _cfg(google=None, digest=config.DigestConfig(rates={"USD": 1.5}, display_currency="CAD")),
        today=TODAY,
    )
    assert "≈ CA$150.00 converted" in with_rates.text


def test_sales_gross_labelled_provisional(
    conn: sqlite3.Connection, ios_id: int, android_id: int
) -> None:
    _apple_day(conn, AS_OF.isoformat(), ios_id, installs=1, proceeds=1)
    db.replace_source_day(
        conn,
        "play_sales",
        AS_OF.isoformat(),
        [db.MetricRow(AS_OF.isoformat(), android_id, "US", "sales_gross", "USD", 42.0)],
    )
    _log(conn, "apple_sales", AS_OF.isoformat())

    result = digest.build_digest(conn, _cfg(), today=TODAY)
    assert (
        "Android sales (gross, provisional, until the month's earnings arrive)  $42.00"
        in result.text
    )
    # sales_gross must never be mixed into the net Proceeds line
    assert "$1.00" in result.text
    assert "$43.00" not in result.text


def test_sales_gross_line_omitted_when_absent(conn: sqlite3.Connection, ios_id: int) -> None:
    _apple_day(conn, AS_OF.isoformat(), ios_id, installs=1, proceeds=1)
    _log(conn, "apple_sales", AS_OF.isoformat())
    result = digest.build_digest(conn, _cfg(google=None), today=TODAY)
    assert "sales_gross" not in result.text
    assert "provisional" not in result.text


# -- vitals ------------------------------------------------------------------------------


def test_28d_vitals_threshold_triggers_warning_daily_does_not(
    conn: sqlite3.Connection, ios_id: int, android_id: int
) -> None:
    _apple_day(conn, AS_OF.isoformat(), ios_id, installs=1, proceeds=0)
    _android_installs_day(conn, AS_OF.isoformat(), android_id, installs=1)
    db.replace_source_range(
        conn,
        "play_vitals",
        AS_OF.isoformat(),
        AS_OF.isoformat(),
        [
            # Daily rate is huge but must never trigger a warning on its own.
            db.MetricRow(AS_OF.isoformat(), android_id, "ALL", "crash_rate", "", 0.50),
            db.MetricRow(AS_OF.isoformat(), android_id, "ALL", "crash_rate_28d", "", 0.02),
        ],
        app_ids=[android_id],
    )
    _log(conn, "apple_sales", AS_OF.isoformat())
    _log(conn, "play_installs", "2026-09-01")

    result = digest.build_digest(conn, _cfg(), today=TODAY)
    assert "billFT Android crash rate 2.0% ⚠" in result.text
    assert "50.0%" not in result.text


def test_vitals_line_omitted_when_under_threshold(
    conn: sqlite3.Connection, ios_id: int, android_id: int
) -> None:
    _apple_day(conn, AS_OF.isoformat(), ios_id, installs=1, proceeds=0)
    _android_installs_day(conn, AS_OF.isoformat(), android_id, installs=1)
    db.replace_source_range(
        conn,
        "play_vitals",
        AS_OF.isoformat(),
        AS_OF.isoformat(),
        [db.MetricRow(AS_OF.isoformat(), android_id, "ALL", "crash_rate_28d", "", 0.001)],
        app_ids=[android_id],
    )
    _log(conn, "apple_sales", AS_OF.isoformat())
    _log(conn, "play_installs", "2026-09-01")
    result = digest.build_digest(conn, _cfg(), today=TODAY)
    assert "Vitals" not in result.text


def test_html_vitals_shown_when_only_one_rate_is_present(
    conn: sqlite3.Connection, ios_id: int, android_id: int
) -> None:
    """Crash and ANR are queried as two separate Play Reporting API metric sets, and
    Google omits a metric set's row when there's insufficient user population for
    statistical confidence — so one can be present while the other is absent. The HTML
    per-app vitals line must still show whichever data exists, not disappear entirely
    because crash_rate_28d specifically is the one that's missing.
    """
    _apple_day(conn, AS_OF.isoformat(), ios_id, installs=1, proceeds=0)
    _android_installs_day(conn, AS_OF.isoformat(), android_id, installs=1)
    db.replace_source_range(
        conn,
        "play_vitals",
        AS_OF.isoformat(),
        AS_OF.isoformat(),
        # No crash_rate/crash_rate_28d rows at all this day, only anr_rate_28d.
        [db.MetricRow(AS_OF.isoformat(), android_id, "ALL", "anr_rate_28d", "", 0.001)],
        app_ids=[android_id],
    )
    _log(conn, "apple_sales", AS_OF.isoformat())
    _log(conn, "play_installs", "2026-09-01")
    result = digest.build_digest(conn, _cfg(), today=TODAY)
    # Missing rates are "—", never 0.0%: Google withheld them, they aren't zero.
    assert "crash — daily, — 28d &middot; anr — daily, 0.1% 28d" in result.html
    assert "crash 0.0%" not in result.html


def test_html_vitals_crash_present_anr_missing(
    conn: sqlite3.Connection, ios_id: int, android_id: int
) -> None:
    """The mirror case: only the crash metric set came back, so ANR shows "—"."""
    _apple_day(conn, AS_OF.isoformat(), ios_id, installs=1, proceeds=0)
    _android_installs_day(conn, AS_OF.isoformat(), android_id, installs=1)
    d = AS_OF.isoformat()
    db.replace_source_range(
        conn,
        "play_vitals",
        d,
        d,
        [
            db.MetricRow(d, android_id, "ALL", "crash_rate", "", 0.004),
            db.MetricRow(d, android_id, "ALL", "crash_rate_28d", "", 0.003),
        ],
        app_ids=[android_id],
    )
    _log(conn, "apple_sales", d)
    _log(conn, "play_installs", "2026-09-01")
    result = digest.build_digest(conn, _cfg(), today=TODAY)
    assert "crash 0.4% daily, 0.3% 28d &middot; anr — daily, — 28d" in result.html


# -- analytics (docs/SPEC.md, Phase 6a) -----------------------------------------------------


def _analytics_day(
    conn: sqlite3.Connection, d: str, app_id: int, impressions: float, page_views: float
) -> None:
    db.replace_source_range(
        conn,
        "apple_analytics",
        d,
        d,
        [
            db.MetricRow(d, app_id, "ALL", "impressions", "", impressions),
            db.MetricRow(d, app_id, "ALL", "page_views", "", page_views),
        ],
        app_ids=[app_id],
    )


def test_text_analytics_line_shown_when_present(
    conn: sqlite3.Connection, ios_id: int, android_id: int
) -> None:
    _apple_day(conn, AS_OF.isoformat(), ios_id, installs=1, proceeds=0)
    _android_installs_day(conn, AS_OF.isoformat(), android_id, installs=1)
    _analytics_day(conn, AS_OF.isoformat(), ios_id, impressions=120, page_views=40)
    _log(conn, "apple_sales", AS_OF.isoformat())
    _log(conn, "play_installs", "2026-09-01")
    result = digest.build_digest(conn, _cfg(), today=TODAY)
    assert "120 impressions, 40 page views" in result.text


def test_html_analytics_shown_when_present(
    conn: sqlite3.Connection, ios_id: int, android_id: int
) -> None:
    _apple_day(conn, AS_OF.isoformat(), ios_id, installs=1, proceeds=0)
    _android_installs_day(conn, AS_OF.isoformat(), android_id, installs=1)
    _analytics_day(conn, AS_OF.isoformat(), ios_id, impressions=120, page_views=40)
    _log(conn, "apple_sales", AS_OF.isoformat())
    _log(conn, "play_installs", "2026-09-01")
    result = digest.build_digest(conn, _cfg(), today=TODAY)
    assert "120 impressions, 40 page views</div>" in result.html


def test_analytics_absent_shows_nothing_not_zero(
    conn: sqlite3.Connection, ios_id: int, android_id: int
) -> None:
    """An iOS app with no apple_analytics rows at all must show no analytics line —
    never "0 impressions", which would misreport a genuinely unmeasured app as one with
    a real but empty week."""
    _apple_day(conn, AS_OF.isoformat(), ios_id, installs=1, proceeds=0)
    _android_installs_day(conn, AS_OF.isoformat(), android_id, installs=1)
    _log(conn, "apple_sales", AS_OF.isoformat())
    _log(conn, "play_installs", "2026-09-01")
    result = digest.build_digest(conn, _cfg(), today=TODAY)
    assert "impressions" not in result.text
    assert "impressions" not in result.html


def test_analytics_hidden_until_seen_on_data_line(conn: sqlite3.Connection, ios_id: int) -> None:
    """Like apple_subscriptions: not every run has an analytics instance yet, so it only
    joins the Data line once ingest_log has actually seen it."""
    _apple_day(conn, AS_OF.isoformat(), ios_id, installs=1, proceeds=0)
    _log(conn, "apple_sales", AS_OF.isoformat())
    result = digest.build_digest(conn, _cfg(google=None), today=TODAY)
    assert "apple_analytics" not in result.text


def test_analytics_shown_on_data_line_once_seen(conn: sqlite3.Connection, ios_id: int) -> None:
    _apple_day(conn, AS_OF.isoformat(), ios_id, installs=1, proceeds=0)
    _log(conn, "apple_sales", AS_OF.isoformat())
    _log(conn, "apple_analytics", AS_OF.isoformat(), app_id=ios_id)
    result = digest.build_digest(conn, _cfg(google=None), today=TODAY)
    assert "apple_analytics ok" in result.text


# -- overdue cadence rules -----------------------------------------------------------------


def test_daily_source_overdue_after_stale_days(conn: sqlite3.Connection, ios_id: int) -> None:
    _apple_day(conn, "2026-09-20", ios_id, installs=1, proceeds=0)
    _log(conn, "apple_sales", "2026-09-20")
    result = digest.build_digest(conn, _cfg(google=None), today=date(2026, 9, 27))
    # as_of is 2026-09-20 (the only loaded day); today is 7 days later -> overdue
    assert "apple_sales not_ready" in result.text


def test_subscriptions_hidden_until_seen(conn: sqlite3.Connection, ios_id: int) -> None:
    """Unlike apple_sales, apple_subscriptions/apple_subscription_events only join the
    Data line once ingest_log has actually seen them (not every app has subscriptions)."""
    _apple_day(conn, AS_OF.isoformat(), ios_id, installs=1, proceeds=0)
    _log(conn, "apple_sales", AS_OF.isoformat())
    result = digest.build_digest(conn, _cfg(google=None), today=TODAY)
    assert "apple_subscriptions" not in result.text
    assert "apple_subscription_events" not in result.text


def test_subscriptions_shown_once_seen(conn: sqlite3.Connection, ios_id: int) -> None:
    _apple_day(conn, AS_OF.isoformat(), ios_id, installs=1, proceeds=0)
    _log(conn, "apple_sales", AS_OF.isoformat())
    _log(conn, "apple_subscriptions", AS_OF.isoformat())
    _log(conn, "apple_subscription_events", AS_OF.isoformat())
    result = digest.build_digest(conn, _cfg(google=None), today=TODAY)
    assert "apple_subscriptions ok" in result.text
    assert "apple_subscription_events ok" in result.text


def test_subscriptions_overdue_after_stale_days(conn: sqlite3.Connection, ios_id: int) -> None:
    _apple_day(conn, "2026-09-20", ios_id, installs=1, proceeds=0)
    _log(conn, "apple_sales", "2026-09-20")
    _log(conn, "apple_subscriptions", "2026-09-20")
    result = digest.build_digest(conn, _cfg(google=None), today=date(2026, 9, 27))
    assert "apple_subscriptions not_ready" in result.text


def test_august_earnings_on_sep_3_not_flagged(conn: sqlite3.Connection, android_id: int) -> None:
    _android_installs_day(conn, "2026-09-01", android_id, installs=1)
    _log(conn, "play_installs", "2026-09-01")
    _log(conn, "play_earnings", "2026-08-01")  # no earnings row logged for August at all
    cfg = _cfg(apple=None)
    result = digest.build_digest(conn, cfg, today=date(2026, 9, 3))
    # play_earnings has an ingest_log row (from _log) with status ok for report_date
    # 2026-08-01, so it should read as ok regardless; check the not-flagged case directly.
    assert "play_earnings not_ready" not in result.text


def test_earnings_overdue_after_the_15th_when_missing(
    conn: sqlite3.Connection, android_id: int
) -> None:
    _android_installs_day(conn, "2026-09-01", android_id, installs=1)
    _log(conn, "play_installs", "2026-09-01")
    # play_earnings has been attempted (so it's a "configured/relevant" source) but never
    # succeeded for August.
    _log(conn, "play_earnings", "2026-08-01", status="not_ready")
    cfg = _cfg(apple=None)
    result = digest.build_digest(conn, cfg, today=date(2026, 9, 20))
    assert "play_earnings not_ready" in result.text


def test_monthly_source_not_overdue_before_day_4(conn: sqlite3.Connection, android_id: int) -> None:
    _android_installs_day(conn, "2026-09-01", android_id, installs=1)
    # No ingest_log row for this month's play_installs at all yet.
    _log(conn, "play_installs", "2026-08-01")
    result = digest.build_digest(conn, _cfg(apple=None), today=date(2026, 9, 3))
    assert "play_installs not_ready" not in result.text


def test_monthly_source_overdue_after_day_4(conn: sqlite3.Connection, android_id: int) -> None:
    _android_installs_day(conn, "2026-08-01", android_id, installs=1)
    _log(conn, "play_installs", "2026-08-01")
    result = digest.build_digest(conn, _cfg(apple=None), today=date(2026, 9, 10))
    assert "play_installs not_ready" in result.text


def test_monthly_source_lag_note_shows_latest_real_data_day(
    conn: sqlite3.Connection, ios_id: int, android_id: int
) -> None:
    """The Data line's lag note for a monthly source must show the latest day its file
    actually has data for, not ingest_log's report_date (always that month's 1st day for
    a monthly source, never a real day of data) (review nit #6).
    """
    _apple_day(conn, "2026-09-18", ios_id, installs=1, proceeds=0)
    _log(conn, "apple_sales", "2026-09-18")  # limits as-of to Sep 18
    _android_installs_day(conn, "2026-09-22", android_id, installs=1)
    _log(conn, "play_installs", "2026-09-01")
    result = digest.build_digest(conn, _cfg(), today=date(2026, 9, 20))
    assert "play_installs ok (Sep 22)" in result.text
    assert "(Sep 1)" not in result.text


def test_error_status_reported(conn: sqlite3.Connection, ios_id: int) -> None:
    _apple_day(conn, AS_OF.isoformat(), ios_id, installs=1, proceeds=0)
    _log(conn, "apple_sales", AS_OF.isoformat(), status="error")
    result = digest.build_digest(conn, _cfg(google=None), today=TODAY)
    assert "apple_sales error" in result.text


def test_mid_window_error_is_not_hidden_by_a_later_success(
    conn: sqlite3.Connection, ios_id: int
) -> None:
    """An error partway through the window must still surface even though every later
    day (including as-of) succeeded. Picking only the single ingest_log row with the
    globally-latest started_at let a later 'ok' entry hide an earlier 'error' one for a
    different report_date; started_at also only has 1-second resolution, so a fast test
    or backfill run can log same-second rows in either order (review finding #2).
    """
    days = [(AS_OF - timedelta(days=i)).isoformat() for i in reversed(range(7))]  # oldest first
    for d in days:
        _apple_day(conn, d, ios_id, installs=1, proceeds=0)
    error_day = days[2]  # partway through the window, well before as-of
    _log(conn, "apple_sales", error_day, status="error")
    for d in days:
        if d != error_day:
            _log(conn, "apple_sales", d)  # logged 'ok' after the error, like later days would be
    result = digest.build_digest(conn, _cfg(google=None), today=TODAY)
    assert "apple_sales error" in result.text


def test_play_installs_error_on_one_app_is_not_hidden_by_another_apps_success(
    conn: sqlite3.Connection, android_id: int
) -> None:
    """play_installs logs one row per app under the same report_date (that month's
    first day), so grouping the "most recent attempt" query by report_date alone let a
    later 'ok' for one app hide a different app's still-unresolved error for that same
    report_date. app_id must be part of the grouping (review round 2, finding 1).
    """
    other_id = db.upsert_app(conn, "android", "com.example.other", "otherApp")
    _android_installs_day(conn, "2026-09-01", android_id, installs=1)
    _log(conn, "play_installs", "2026-09-01", status="error", app_id=other_id)
    _log(conn, "play_installs", "2026-09-01", app_id=android_id)  # a different app, logged after
    result = digest.build_digest(conn, _cfg(apple=None), today=date(2026, 9, 3))
    assert "play_installs error" in result.text


def test_play_vitals_error_on_one_app_is_not_hidden_by_another_apps_success(
    conn: sqlite3.Connection, android_id: int
) -> None:
    other_id = db.upsert_app(conn, "android", "com.example.other", "otherApp")
    _android_installs_day(conn, "2026-09-01", android_id, installs=1)
    _log(conn, "play_installs", "2026-09-01", app_id=android_id)
    _log(conn, "play_vitals", AS_OF.isoformat(), status="error", app_id=other_id)
    _log(conn, "play_vitals", AS_OF.isoformat(), app_id=android_id)  # a different app, after
    result = digest.build_digest(conn, _cfg(apple=None), today=TODAY)
    assert "vitals error" in result.text


def test_old_backfill_error_outside_the_window_does_not_flag_forever(
    conn: sqlite3.Connection, ios_id: int
) -> None:
    """An error from a one-off backfill day the daily run no longer re-touches (it only
    re-pulls the last `[schedule].days` days) must not flag the digest indefinitely
    (review round 2, finding 2).
    """
    _apple_day(conn, AS_OF.isoformat(), ios_id, installs=1, proceeds=0)
    _log(conn, "apple_sales", AS_OF.isoformat())  # today's run: ok
    _log(conn, "apple_sales", "2026-03-10", status="error")  # old backfill error, never retried
    result = digest.build_digest(conn, _cfg(google=None), today=TODAY)
    assert "apple_sales error" not in result.text
    assert "apple_sales ok" in result.text


# -- per-app rows / no cross-platform pairing ------------------------------------------------


def test_same_name_apps_on_both_platforms_are_not_merged(conn: sqlite3.Connection) -> None:
    ios = db.upsert_app(conn, "ios", "1", "SharedName")
    android = db.upsert_app(conn, "android", "com.example.shared", "SharedName")
    _apple_day(conn, AS_OF.isoformat(), ios, installs=10.0, proceeds=0)
    _android_installs_day(conn, AS_OF.isoformat(), android, installs=7.0)
    _log(conn, "apple_sales", AS_OF.isoformat())
    _log(conn, "play_installs", "2026-09-01")

    result = digest.build_digest(conn, _cfg(), today=TODAY)
    ios_row = _table_row(result.text, "SharedName (iOS)")
    android_row = _table_row(result.text, "SharedName (Android)")
    assert ios_row.split()[-3:] == ["*10*", "*10*", "*10*"]  # day, month, lifetime
    assert android_row.split()[-3:] == ["*7*", "*7*", "*7*"]
    assert result.text.count("SharedName (") == 2  # two distinct per-app rows, never merged


def test_email_has_no_images(conn: sqlite3.Connection, ios_id: int, android_id: int) -> None:
    _apple_day(conn, AS_OF.isoformat(), ios_id, installs=1, proceeds=0)
    _android_installs_day(conn, AS_OF.isoformat(), android_id, installs=1)
    _log(conn, "apple_sales", AS_OF.isoformat())
    _log(conn, "play_installs", "2026-09-01")

    result = digest.build_digest(conn, _cfg(), today=TODAY)
    assert "<img" not in result.html
    assert "cid:" not in result.html


def test_top_app_by_installs(conn: sqlite3.Connection, ios_id: int, android_id: int) -> None:
    _apple_day(conn, AS_OF.isoformat(), ios_id, installs=3.0, proceeds=0)
    _android_installs_day(conn, AS_OF.isoformat(), android_id, installs=9.0)
    _log(conn, "apple_sales", AS_OF.isoformat())
    _log(conn, "play_installs", "2026-09-01")
    result = digest.build_digest(conn, _cfg(), today=TODAY)
    assert "Top app   billFT 9 installs" in result.text


# -- email message -------------------------------------------------------------------------


def test_to_email_message_structure(conn: sqlite3.Connection, ios_id: int, android_id: int) -> None:
    _apple_day(conn, AS_OF.isoformat(), ios_id, installs=1, proceeds=0)
    _android_installs_day(conn, AS_OF.isoformat(), android_id, installs=1)
    _log(conn, "apple_sales", AS_OF.isoformat())
    _log(conn, "play_installs", "2026-09-01")
    result = digest.build_digest(conn, _cfg(), today=TODAY)

    email_cfg = config.EmailConfig(
        host="smtp.example.com",
        port=587,
        security="starttls",
        username="me",
        from_addr="me@example.com",
        to_addrs=["you@example.com"],
    )
    msg = digest.to_email_message(result, email_cfg)
    # Titled with the day it was generated, not the (older) as-of day.
    assert msg["Subject"] == "Storepulse · Sun Sep 27"
    assert msg["To"] == "you@example.com"
    plain, html = msg.get_payload(0), msg.get_payload(1)
    assert plain.get_content_type() == "text/plain"
    assert plain.get_content() == result.text
    assert html.get_content_type() == "text/html"
    assert not [p for p in msg.walk() if p.get_content_maintype() == "image"]


def test_subject_has_no_date_when_no_data(conn: sqlite3.Connection) -> None:
    result = digest.build_digest(conn, _cfg(), today=TODAY)
    email_cfg = config.EmailConfig(
        host="h",
        port=1,
        security="ssl",
        username="u",
        from_addr="me@example.com",
        to_addrs=["y@example.com"],
    )
    msg = digest.to_email_message(result, email_cfg)
    assert msg["Subject"] == "Storepulse"


# -- exact plain text -----------------------------------------------------------------------


def test_exact_plain_text(conn: sqlite3.Connection, ios_id: int, android_id: int) -> None:
    """A fully hand-verified scenario, checked against the complete literal output.

    iOS: 10 installs/day + $15 proceeds/day, both weeks -> 70 installs, $105, flat trend.
    Android: 8 installs/day, both weeks -> 56 installs; $20 earnings and $25 sales_gross
    only in the current week (previous week's proceeds are $0 android + $105 apple = $105,
    so $125 vs $105 is +19%). Top app is deskFT (70 > 56). billFT's 28d crash rate (2.0%)
    is above the 1.09% threshold; its ANR rate (0.1%) is not.

    Only 14 days (Sep 13-26) are seeded, all in September, so the table's month and
    lifetime columns both equal the 14-day totals: 140 (iOS) and 112 (Android).
    """
    for i in range(14):
        d = (AS_OF - timedelta(days=i)).isoformat()
        _apple_day(conn, d, ios_id, installs=10.0, proceeds=15.0)
        _android_installs_day(conn, d, android_id, installs=8.0)
    _android_earnings_day(conn, AS_OF.isoformat(), android_id, proceeds=20.0)
    db.replace_source_day(
        conn,
        "play_sales",
        AS_OF.isoformat(),
        [db.MetricRow(AS_OF.isoformat(), android_id, "US", "sales_gross", "USD", 25.0)],
    )
    db.replace_source_range(
        conn,
        "play_vitals",
        AS_OF.isoformat(),
        AS_OF.isoformat(),
        [
            db.MetricRow(AS_OF.isoformat(), android_id, "ALL", "crash_rate", "", 0.02),
            db.MetricRow(AS_OF.isoformat(), android_id, "ALL", "crash_rate_28d", "", 0.02),
            db.MetricRow(AS_OF.isoformat(), android_id, "ALL", "anr_rate", "", 0.001),
            db.MetricRow(AS_OF.isoformat(), android_id, "ALL", "anr_rate_28d", "", 0.001),
        ],
        app_ids=[android_id],
    )
    _log(conn, "apple_sales", AS_OF.isoformat())
    _log(conn, "play_installs", "2026-09-01")
    _log(conn, "play_vitals", AS_OF.isoformat())

    result = digest.build_digest(conn, _cfg(), today=TODAY)
    assert result.text == (
        "Storepulse · Sun Sep 27\n"
        "Data through Sat Sep 26\n"
        "Installs  126 (+0% wk)   iOS 70 · Android 56\n"
        "Lifetime  252   iOS 140 · Android 112\n"
        "Proceeds  $125.00 (+19% wk)\n"
        "Android sales (gross, provisional, until the month's earnings arrive)  $25.00\n"
        "Top app   deskFT 70 installs\n"
        "Vitals    billFT Android crash rate 2.0% ⚠\n"
        "Data      apple_sales ok · play_installs ok · vitals ok\n"
        "\n"
        "Apps\n"
        "                    Sep 20  Sep 21  Sep 22  Sep 23  Sep 24  Sep 25  *Sep 26*  *Sep total*  *Lifetime*\n"  # noqa: E501
        "  deskFT (iOS)          10      10      10      10      10      10      *10*        *140*       *140*\n"  # noqa: E501
        "    $105.00\n"
        "  billFT (Android)       8       8       8       8       8       8       *8*        *112*       *112*\n"  # noqa: E501
        "    $20.00\n"
    )


# -- dogfood follow-ups (first live run, Sep 28) ------------------------------------------


def _email_like_account(conn: sqlite3.Connection) -> dict[str, int]:
    """Mirrors the first live digest: iOS data only, Play bucket still 403 (no installs
    rows, play_installs in error), two Android apps both named "Bus Wise"."""
    ids = {
        "echo_ios": db.upsert_app(conn, "ios", "10", "echoFT"),
        "lib_ios": db.upsert_app(conn, "ios", "11", "libFT"),
        "quiet_ios": db.upsert_app(conn, "ios", "12", "TAK ft"),
        "bus_a": db.upsert_app(conn, "android", "com.jasonsb.busarrival", "Bus Wise"),
        "bus_b": db.upsert_app(conn, "android", "com.logicftinc.buswise", "Bus Wise"),
    }
    for i in range(7):
        d = (AS_OF - timedelta(days=i)).isoformat()
        rows = [db.MetricRow(d, ids["echo_ios"], "US", "installs", "", 4.0)]
        if i == 0:
            rows.append(db.MetricRow(d, ids["lib_ios"], "US", "installs", "", 1.0))
        db.replace_source_day(conn, "apple_sales", d, rows)
        _log(conn, "apple_sales", d)
    db.log_ingest(
        conn,
        source="play_installs",
        report_date="2026-09-01",
        started_at=db.utc_now(),
        status="error",
        note="403",
        app_id=ids["bus_a"],
    )
    return ids


def test_platform_without_data_is_a_dash_not_zero(conn: sqlite3.Connection) -> None:
    _email_like_account(conn)
    result = digest.build_digest(conn, _cfg(), today=TODAY)
    installs_line = next(line for line in result.text.splitlines() if line.startswith("Installs"))
    assert installs_line == "Installs  29 (new)   iOS 29 · Android —"
    assert "Android —" in result.html
    # The unavailable platform's apps aren't listed as "0 installs"...
    assert "(Android)" not in result.text
    # ...and the Data line says why.
    assert "play_installs error" in result.text


def test_unavailable_platform_is_left_out_of_the_trend(conn: sqlite3.Connection) -> None:
    ios = db.upsert_app(conn, "ios", "1", "deskFT")
    db.upsert_app(conn, "android", "com.x", "billFT")  # registered, but no installs rows
    for i in range(14):
        d = (AS_OF - timedelta(days=i)).isoformat()
        _apple_day(conn, d, ios, installs=10.0 if i < 7 else 5.0, proceeds=0)
        _log(conn, "apple_sales", d)
    result = digest.build_digest(conn, _cfg(), today=TODAY)
    installs_line = next(line for line in result.text.splitlines() if line.startswith("Installs"))
    assert installs_line == "Installs  70 (+100% wk)   iOS 70 · Android —"
    assert "billFT" not in result.text  # not listed, and not counted as a quiet app
    assert "more app" not in result.text


def test_same_named_apps_show_their_store_id(conn: sqlite3.Connection) -> None:
    a = db.upsert_app(conn, "android", "com.jasonsb.busarrival", "Bus Wise")
    b = db.upsert_app(conn, "android", "com.logicftinc.buswise", "Bus Wise")
    ios = db.upsert_app(conn, "ios", "7", "Bus Wise")  # other platform: no clash
    day = AS_OF.isoformat()
    db.replace_source_range(
        conn,
        "play_installs",
        day,
        day,
        [
            db.MetricRow(day, a, "ALL", "installs", "", 3.0),
            db.MetricRow(day, b, "ALL", "installs", "", 2.0),
        ],
    )
    _apple_day(conn, AS_OF.isoformat(), ios, installs=1.0, proceeds=0)
    _log(conn, "apple_sales", AS_OF.isoformat())
    result = digest.build_digest(conn, _cfg(), today=TODAY)
    # Each row ends with its latest-day, month and lifetime installs.
    a_row = _table_row(result.text, "Bus Wise [com.jasonsb.busarrival] (Android)")
    b_row = _table_row(result.text, "Bus Wise [com.logicftinc.buswise] (Android)")
    assert a_row.split()[-3:] == ["*3*", "*3*", "*3*"]
    assert b_row.split()[-3:] == ["*2*", "*2*", "*2*"]
    assert _table_row(result.text, "Bus Wise (iOS)").split()[-3:] == ["*1*", "*1*", "*1*"]


def test_quiet_apps_collapse_into_one_line(conn: sqlite3.Connection) -> None:
    _email_like_account(conn)
    db.upsert_app(conn, "ios", "13", "Bus Wiser")
    result = digest.build_digest(conn, _cfg(), today=TODAY)
    apps = result.text.split("Apps\n", 1)[1].splitlines()
    # A header, one row per listed app (no per-app "no proceeds" noise), then the collapse.
    assert [line.split("(")[0].strip() for line in apps[1:3]] == ["echoFT", "libFT"]
    assert len(apps) == 4
    assert apps[-1] == "  +2 more apps with no installs or proceeds in either week"
    assert "Bus Wise" not in "\n".join(apps[:3])
    assert "+2 more apps with no installs or proceeds in either week" in result.html
    assert result.html.count("<tr>") == 1 + 2 + 1  # header, two listed apps, the collapse


def test_app_active_last_week_only_is_still_listed(conn: sqlite3.Connection) -> None:
    ios = db.upsert_app(conn, "ios", "1", "deskFT")
    other = db.upsert_app(conn, "ios", "2", "libFT")
    d_prev = (AS_OF - timedelta(days=8)).isoformat()
    _apple_day(conn, d_prev, ios, installs=5.0, proceeds=0)
    _apple_day(conn, AS_OF.isoformat(), other, installs=1.0, proceeds=0)
    _log(conn, "apple_sales", AS_OF.isoformat())
    result = digest.build_digest(conn, _cfg(google=None), today=TODAY)
    # Nothing in the last 7 days, but Sep 18 is in the same month and in the lifetime total.
    assert _table_row(result.text, "deskFT (iOS)").split()[-3:] == ["*0*", "*5*", "*5*"]
    assert "more app" not in result.text


def test_singular_install(conn: sqlite3.Connection, ios_id: int) -> None:
    _apple_day(conn, AS_OF.isoformat(), ios_id, installs=1.0, proceeds=0)
    _log(conn, "apple_sales", AS_OF.isoformat())
    result = digest.build_digest(conn, _cfg(google=None), today=TODAY)
    assert "Top app   deskFT 1 install\n" in result.text
    assert _table_row(result.text, "deskFT (iOS)").split()[-3:] == ["*1*", "*1*", "*1*"]
    assert "1 installs" not in result.text + result.html


def test_vitals_without_values_says_no_data(
    conn: sqlite3.Connection, ios_id: int, android_id: int
) -> None:
    _apple_day(conn, AS_OF.isoformat(), ios_id, installs=1.0, proceeds=0)
    _log(conn, "apple_sales", AS_OF.isoformat())
    _android_installs_day(conn, AS_OF.isoformat(), android_id, installs=1.0)
    _log(conn, "play_installs", "2026-09-01")
    # Collected fine (logged ok) but Google returned no values: no daily_metrics rows.
    _log(conn, "play_vitals", AS_OF.isoformat())
    result = digest.build_digest(conn, _cfg(), today=TODAY)
    assert "vitals ok (no data from Google)" in result.text


# -- PR #4 review round 1 ---------------------------------------------------------------


def _vitals(conn: sqlite3.Connection, app_id: int, crash_28d: float) -> None:
    d = AS_OF.isoformat()
    db.replace_source_range(
        conn,
        "play_vitals",
        d,
        d,
        [db.MetricRow(d, app_id, "ALL", "crash_rate_28d", "", crash_28d)],
        app_ids=[app_id],
    )


def test_quiet_app_keeps_its_crash_warning(
    conn: sqlite3.Connection, ios_id: int, android_id: int
) -> None:
    """An app with no installs in either week is collapsed, but its over-threshold
    crash rate must still be warned about: vitals don't depend on installs."""
    _apple_day(conn, AS_OF.isoformat(), ios_id, installs=5.0, proceeds=0)
    _log(conn, "apple_sales", AS_OF.isoformat())
    _android_installs_day(conn, AS_OF.isoformat(), android_id, installs=0.0)
    _log(conn, "play_installs", "2026-09-01")
    _vitals(conn, android_id, 0.02)
    result = digest.build_digest(conn, _cfg(), today=TODAY)
    assert "Vitals    billFT Android crash rate 2.0% ⚠" in result.text
    assert "billFT Android crash rate 2.0%" in result.html
    assert "+1 more app with no installs or proceeds in either week" in result.text


def test_crash_warning_survives_unreachable_installs_bucket(
    conn: sqlite3.Connection, ios_id: int, android_id: int
) -> None:
    """The live account's shape: the Play bucket is still 403 (no installs rows, so
    Android shows "—"), but vitals come from the Reporting API and still load."""
    _apple_day(conn, AS_OF.isoformat(), ios_id, installs=5.0, proceeds=0)
    _log(conn, "apple_sales", AS_OF.isoformat())
    _vitals(conn, android_id, 0.02)
    result = digest.build_digest(conn, _cfg(), today=TODAY)
    assert "Android —" in result.text
    assert "billFT Android crash rate 2.0% ⚠" in result.text


def test_stale_apple_does_not_make_android_a_dash(conn: sqlite3.Connection) -> None:
    """as_of is the earliest platform's latest day, so a stalled Apple drags the window
    back. Android has data (just none in that old window) and must not read "—"."""
    ios = db.upsert_app(conn, "ios", "1", "deskFT")
    android = db.upsert_app(conn, "android", "com.x", "billFT")
    apple_last = date(2026, 9, 6)
    for i in range(14):
        d = (apple_last - timedelta(days=i)).isoformat()
        _apple_day(conn, d, ios, installs=2.0, proceeds=0)
        _log(conn, "apple_sales", d)
    rows = []
    for i in range(7):
        d = (AS_OF - timedelta(days=i)).isoformat()  # Sep 20-26 only
        rows.append(db.MetricRow(d, android, "ALL", "installs", "", 3.0))
    db.replace_source_range(conn, "play_installs", "2026-09-20", "2026-09-26", rows)
    _log(conn, "play_installs", "2026-09-01")
    result = digest.build_digest(conn, _cfg(), today=TODAY)
    assert result.as_of == apple_last
    assert "Android —" not in result.text
    installs_line = next(line for line in result.text.splitlines() if line.startswith("Installs"))
    assert installs_line == "Installs  14 (+0% wk)   iOS 14 · Android 0"


# -- title date and the per-app installs table ------------------------------------------------


def test_title_is_the_generation_date_not_the_as_of_day(
    conn: sqlite3.Connection, ios_id: int, android_id: int
) -> None:
    """The regression: Play stalled at Sep 20 and the digest sent on Oct 6 was titled with
    the stale as-of day, which read as the wrong date for an email received today."""
    stalled, sent = date(2026, 9, 20), date(2026, 10, 6)
    _apple_day(conn, AS_OF.isoformat(), ios_id, installs=1, proceeds=0)
    _log(conn, "apple_sales", AS_OF.isoformat())
    _android_installs_day(conn, stalled.isoformat(), android_id, installs=1)
    _log(conn, "play_installs", "2026-09-01")

    result = digest.build_digest(conn, _cfg(), today=sent)
    assert result.as_of == stalled
    lines = result.text.splitlines()
    assert lines[0] == "Storepulse · Tue Oct 6"
    assert lines[1] == "Data through iOS Sat Sep 26 · Android Sun Sep 20"
    assert lines[2] == "Weekly totals cover the 7 days to Sun Sep 20"
    assert "Storepulse · Tue Oct 6" in result.html
    assert "Data through iOS Sat Sep 26 · Android Sun Sep 20" in result.html


def test_month_column_covers_only_the_as_of_months_days(
    conn: sqlite3.Connection, ios_id: int
) -> None:
    """The 7 day columns straddle Sep/Oct, but the month column counts only Oct 1-2;
    lifetime counts every day."""
    as_of = date(2026, 10, 2)
    for i in range(13):  # Sep 20 - Oct 2
        d = (as_of - timedelta(days=i)).isoformat()
        _apple_day(conn, d, ios_id, installs=3.0, proceeds=0)
    _log(conn, "apple_sales", as_of.isoformat())

    result = digest.build_digest(conn, _cfg(google=None), today=date(2026, 10, 3))
    header = next(line for line in result.text.splitlines() if "*Oct total*" in line)
    assert "Sep 26" in header and "*Oct 2*" in header  # Sep 26 - Oct 2
    assert _table_row(result.text, "deskFT (iOS)").split()[-3:] == ["*3*", "*6*", "*39*"]
    assert "Oct<br>total" in result.html


def test_per_app_totals_exclude_redownloads(conn: sqlite3.Connection, ios_id: int) -> None:
    d = AS_OF.isoformat()
    db.replace_source_day(
        conn,
        "apple_sales",
        d,
        [
            db.MetricRow(d, ios_id, "US", "installs", "", 10.0),
            db.MetricRow(d, ios_id, "US", "redownloads", "", 500.0),
        ],
    )
    _log(conn, "apple_sales", d)
    result = digest.build_digest(conn, _cfg(google=None), today=TODAY)
    assert _table_row(result.text, "deskFT (iOS)").split()[-3:] == ["*10*", "*10*", "*10*"]


def test_android_per_app_totals_use_the_all_row_not_the_countries(
    conn: sqlite3.Connection, android_id: int
) -> None:
    """play_installs writes a worldwide ALL row plus per-country rows; adding them up
    would double-count."""
    d = AS_OF.isoformat()
    db.replace_source_range(
        conn,
        "play_installs",
        d,
        d,
        [
            db.MetricRow(d, android_id, "ALL", "installs", "", 10.0),
            db.MetricRow(d, android_id, "US", "installs", "", 6.0),
            db.MetricRow(d, android_id, "DE", "installs", "", 4.0),
        ],
    )
    _log(conn, "play_installs", "2026-09-01")
    result = digest.build_digest(conn, _cfg(apple=None), today=TODAY)
    assert _table_row(result.text, "billFT (Android)").split()[-3:] == ["*10*", "*10*", "*10*"]


def test_html_table_highlights_the_latest_day_month_and_lifetime(
    conn: sqlite3.Connection, ios_id: int, android_id: int
) -> None:
    _seed_basic(conn, ios_id, android_id)
    result = digest.build_digest(conn, _cfg(), today=TODAY)
    # Three highlighted cells in the header and in each of the two app rows.
    assert result.html.count("background:#f1f5f9") == 3 * (1 + 2)
    # One cell per day, then the month and lifetime, after the app's own cell.
    assert result.html.count("<th ") == 1 + 7 + 2
    assert result.html.count("<td ") == 2 * (1 + 7 + 2)
    assert "<img" not in result.html


def test_zero_days_are_dimmed_in_the_html_table(conn: sqlite3.Connection, ios_id: int) -> None:
    other = db.upsert_app(conn, "ios", "2", "libFT")
    _apple_day(conn, (AS_OF - timedelta(days=8)).isoformat(), ios_id, installs=5.0, proceeds=0)
    _apple_day(conn, AS_OF.isoformat(), other, installs=1.0, proceeds=0)
    _log(conn, "apple_sales", AS_OF.isoformat())
    result = digest.build_digest(conn, _cfg(google=None), today=TODAY)
    assert "color:#9ca3af" in result.html


def test_app_without_proceeds_has_no_proceeds_line(conn: sqlite3.Connection, ios_id: int) -> None:
    _apple_day(conn, AS_OF.isoformat(), ios_id, installs=1, proceeds=0)
    _log(conn, "apple_sales", AS_OF.isoformat())
    result = digest.build_digest(conn, _cfg(google=None), today=TODAY)
    assert "no proceeds" not in result.text + result.html


# -- platforms on different days: the table runs to the newest, "—" after each one's last ------


def _seed_platforms(
    conn: sqlite3.Connection,
    ios_id: int,
    android_id: int,
    *,
    apple_last: date,
    play_last: date,
    start: date = date(2026, 9, 20),
) -> None:
    """iOS installs 2 a day and Android 3 a day, from ``start`` to each platform's own last
    loaded day."""
    for i in range((apple_last - start).days + 1):
        _apple_day(conn, (start + timedelta(days=i)).isoformat(), ios_id, installs=2.0, proceeds=0)
    for i in range((play_last - start).days + 1):
        _android_installs_day(conn, (start + timedelta(days=i)).isoformat(), android_id, 3.0)
    _log(conn, "apple_sales", apple_last.isoformat())
    _log(conn, "play_installs", "2026-09-01")


def _cells(text: str, name: str) -> list[str]:
    """A table row's nine cells: the 7 days, the month, lifetime."""
    return _table_row(text, name).split()[-9:]


def test_table_runs_to_the_newest_platform_and_dashes_the_lagging_one(
    conn: sqlite3.Connection, ios_id: int, android_id: int
) -> None:
    _seed_platforms(
        conn, ios_id, android_id, apple_last=date(2026, 10, 5), play_last=date(2026, 10, 2)
    )
    result = digest.build_digest(conn, _cfg(), today=date(2026, 10, 6))

    header = next(line for line in result.text.splitlines() if "*Oct total*" in line)
    assert "Sep 29" in header and "*Oct 5*" in header
    # iOS has every day through Oct 5. Android stops at its own last day, Oct 2.
    assert _cells(result.text, "deskFT (iOS)") == [*["2"] * 6, "*2*", "*10*", "*32*"]
    assert _cells(result.text, "billFT (Android)") == [
        *["3"] * 4,
        "—",
        "—",
        "*—*",
        "*6*",  # Oct 1-2 only: the days it has
        "*39*",
    ]
    assert ">—</td>" in result.html


def test_header_and_weekly_totals_name_each_platforms_last_day(
    conn: sqlite3.Connection, ios_id: int, android_id: int
) -> None:
    _seed_platforms(
        conn, ios_id, android_id, apple_last=date(2026, 10, 5), play_last=date(2026, 10, 2)
    )
    result = digest.build_digest(conn, _cfg(), today=date(2026, 10, 6))
    lines = result.text.splitlines()
    assert lines[1] == "Data through iOS Mon Oct 5 · Android Fri Oct 2"
    assert lines[2] == "Weekly totals cover the 7 days to Fri Oct 2"
    assert "Weekly totals cover the 7 days to Fri Oct 2" in result.html
    # The combined figures stay on the shared (older) day, Sep 26 - Oct 2, so they compare
    # like with like: 7 days x 2 iOS and 7 days x 3 Android.
    installs_line = next(line for line in lines if line.startswith("Installs"))
    assert "iOS 14 · Android 21" in installs_line


def test_no_weekly_note_when_the_platforms_agree(
    conn: sqlite3.Connection, ios_id: int, android_id: int
) -> None:
    _seed_basic(conn, ios_id, android_id)
    result = digest.build_digest(conn, _cfg(), today=TODAY)
    assert result.text.splitlines()[1] == "Data through Sat Sep 26"
    assert "Weekly totals" not in result.text + result.html


def test_lagging_platform_with_nothing_this_month_dashes_the_month_but_keeps_lifetime(
    conn: sqlite3.Connection, ios_id: int, android_id: int
) -> None:
    """The live case: Play's last day is Sep 25 while Apple has data through Oct 5."""
    _seed_platforms(
        conn, ios_id, android_id, apple_last=date(2026, 10, 5), play_last=date(2026, 9, 25)
    )
    result = digest.build_digest(conn, _cfg(), today=date(2026, 10, 6))
    # Still listed (it has installs in the shared week), with no day or month to show, and
    # its lifetime total (Sep 20-25, 3 a day) intact.
    assert _cells(result.text, "billFT (Android)") == [*["—"] * 6, "*—*", "*—*", "*18*"]
    assert _cells(result.text, "deskFT (iOS)")[-3:] == ["*2*", "*10*", "*32*"]


def test_app_with_installs_only_past_the_shared_day_is_still_listed(
    conn: sqlite3.Connection, android_id: int
) -> None:
    """Apple is ahead of Play, so an iOS app whose only installs fall after the shared
    as-of day has nothing in either week, but plenty in the table: it must not be
    collapsed into "+N more apps"."""
    ios = db.upsert_app(conn, "ios", "1", "newFT")
    _android_installs_day(conn, "2026-09-25", android_id, installs=1.0)
    _log(conn, "play_installs", "2026-09-01")
    _apple_day(conn, "2026-10-03", ios, installs=4.0, proceeds=0)
    _log(conn, "apple_sales", "2026-10-03")
    result = digest.build_digest(conn, _cfg(), today=date(2026, 10, 6))
    assert _cells(result.text, "newFT (iOS)")[-3:] == ["*4*", "*4*", "*4*"]
    assert "more app" not in result.text
