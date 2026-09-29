"""Dogfood follow-ups: repeated per-app failures print once, grouped; vitals days with
no values from Google say so."""

from __future__ import annotations

import io
from datetime import date

from storepulse.cli import collect
from storepulse.cli.common import Env
from storepulse.core import runner

BUCKET_403 = (
    "GoogleAuthError: Google denied listing gs://pubsite_prod_1/stats/installs/"
    "installs_{pkg}_ (HTTP 403) for sa@x.iam.gserviceaccount.com."
)


def _env() -> Env:
    return Env(stdout=io.StringIO(), stderr=io.StringIO())


def test_identical_failures_for_many_apps_become_one_line() -> None:
    packages = ["com.a.one", "com.b.two", "com.c.three"]
    errors = [(p, BUCKET_403.format(pkg=p)) for p in packages]
    lines = runner.group_errors(errors)
    assert lines == [
        "3 failed the same way (com.a.one, com.b.two, com.c.three): " + BUCKET_403.format(pkg="…")
    ]


def test_different_failures_stay_separate_and_keep_their_text() -> None:
    errors = [
        ("com.a.one 2026-09", "ValueError: bad CSV in installs_com.a.one_202609.csv"),
        ("com.b.two", BUCKET_403.format(pkg="com.b.two")),
    ]
    assert runner.group_errors(errors) == [
        "com.a.one 2026-09: ValueError: bad CSV in installs_com.a.one_202609.csv",
        "com.b.two: " + BUCKET_403.format(pkg="com.b.two"),
    ]


def test_apple_day_errors_group_by_date() -> None:
    errors = [
        (date(2026, 9, 1), "AppleError: HTTP 500 for the sales report for 2026-09-01"),
        (date(2026, 9, 2), "AppleError: HTTP 500 for the sales report for 2026-09-02"),
    ]
    assert runner.group_errors(errors) == [
        "2 failed the same way (2026-09-01, 2026-09-02): "
        "AppleError: HTTP 500 for the sales report for …"
    ]


def test_backfill_report_prints_a_repeated_403_once() -> None:
    summary = runner.PlaySummary("play_installs")
    for p in ["com.a.one", "com.b.two"]:
        summary.errors.append((p, BUCKET_403.format(pkg=p)))
    env = _env()
    collect._report_play(env, summary)
    err = env.stderr.getvalue()  # type: ignore[attr-defined]
    assert err.count("HTTP 403") == 1
    assert "2 failed the same way (com.a.one, com.b.two)" in err


def test_backfill_report_explains_vitals_without_values() -> None:
    summary = runner.PlaySummary("play_vitals", ok=29, rows=0)
    env = _env()
    collect._report_play(env, summary)
    assert "Google returned no vitals values" in env.stdout.getvalue()  # type: ignore[attr-defined]


def test_vitals_with_values_has_no_such_note() -> None:
    summary = runner.PlaySummary("play_vitals", ok=29, rows=87)
    env = _env()
    collect._report_play(env, summary)
    assert "no vitals values" not in env.stdout.getvalue()  # type: ignore[attr-defined]
