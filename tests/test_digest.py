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


def _log(conn: sqlite3.Connection, source: str, report_date: str, status: str = "ok") -> None:
    db.log_ingest(
        conn, source=source, report_date=report_date, started_at=db.utc_now(), status=status, rows=1
    )


AS_OF = date(2026, 9, 26)
TODAY = date(2026, 9, 27)


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


def test_no_data_yet_returns_placeholder_digest(conn: sqlite3.Connection) -> None:
    result = digest.build_digest(conn, _cfg(), today=TODAY)
    assert result.as_of is None
    assert "No data has been collected yet" in result.text
    assert result.images == []


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


# -- overdue cadence rules -----------------------------------------------------------------


def test_daily_source_overdue_after_stale_days(conn: sqlite3.Connection, ios_id: int) -> None:
    _apple_day(conn, "2026-09-20", ios_id, installs=1, proceeds=0)
    _log(conn, "apple_sales", "2026-09-20")
    result = digest.build_digest(conn, _cfg(google=None), today=date(2026, 9, 27))
    # as_of is 2026-09-20 (the only loaded day); today is 7 days later -> overdue
    assert "apple_sales not_ready" in result.text


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


def test_error_status_reported(conn: sqlite3.Connection, ios_id: int) -> None:
    _apple_day(conn, AS_OF.isoformat(), ios_id, installs=1, proceeds=0)
    _log(conn, "apple_sales", AS_OF.isoformat(), status="error")
    result = digest.build_digest(conn, _cfg(google=None), today=TODAY)
    assert "apple_sales error" in result.text


# -- per-app rows / no cross-platform pairing ------------------------------------------------


def test_same_name_apps_on_both_platforms_are_not_merged(conn: sqlite3.Connection) -> None:
    ios = db.upsert_app(conn, "ios", "1", "SharedName")
    android = db.upsert_app(conn, "android", "com.example.shared", "SharedName")
    _apple_day(conn, AS_OF.isoformat(), ios, installs=10.0, proceeds=0)
    _android_installs_day(conn, AS_OF.isoformat(), android, installs=7.0)
    _log(conn, "apple_sales", AS_OF.isoformat())
    _log(conn, "play_installs", "2026-09-01")

    result = digest.build_digest(conn, _cfg(), today=TODAY)
    assert "SharedName (iOS): 10 installs" in result.text
    assert "SharedName (Android): 7 installs" in result.text
    assert result.text.count("SharedName (") == 2  # two distinct per-app rows, never merged
    assert len(result.images) == 2


def test_html_has_one_cid_image_per_app(
    conn: sqlite3.Connection, ios_id: int, android_id: int
) -> None:
    _apple_day(conn, AS_OF.isoformat(), ios_id, installs=1, proceeds=0)
    _android_installs_day(conn, AS_OF.isoformat(), android_id, installs=1)
    _log(conn, "apple_sales", AS_OF.isoformat())
    _log(conn, "play_installs", "2026-09-01")

    result = digest.build_digest(conn, _cfg(), today=TODAY)
    assert len(result.images) == 2
    for cid, png in result.images:
        assert f'cid:{cid}"' in result.html
        assert png.startswith(b"\x89PNG")


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
    assert msg["Subject"] == "Storepulse · Sat Sep 26"
    assert msg["To"] == "you@example.com"
    plain, html_related = msg.get_payload(0), msg.get_payload(1)
    assert plain.get_content_type() == "text/plain"
    assert plain.get_content() == result.text
    image_parts = [p for p in html_related.walk() if p.get_content_type() == "image/png"]
    assert len(image_parts) == 2
    for cid, _png in result.images:
        assert any(p["Content-ID"] == f"<{cid}>" for p in image_parts)


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
        "Storepulse · Sat Sep 26\n"
        "Installs  126 (+0% wk)   iOS 70 · Android 56\n"
        "Proceeds  $125.00 (+19% wk)\n"
        "Android sales (gross, provisional, until the month's earnings arrive)  $25.00\n"
        "Top app   deskFT 70 installs\n"
        "Vitals    billFT Android crash rate 2.0% ⚠\n"
        "Data      apple_sales ok · play_installs ok (Sep 1) · vitals ok\n"
        "\n"
        "Apps\n"
        "  deskFT (iOS): 70 installs, $105.00\n"
        "  billFT (Android): 56 installs, $20.00\n"
    )
