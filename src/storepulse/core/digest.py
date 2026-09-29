"""Builds the daily digest: plain-text and HTML bodies, and inline PNG sparklines.

As-of day (docs/SPEC.md): each *configured* platform's own latest loaded day is found
independently, and the digest's as-of day is the minimum of those — the most
conservative shared date. That single date drives the header, the 7-day-vs-previous-7-day
comparison, and every per-platform figure, so the header date and the combined totals
always describe the same window.

Local mode's ``apps`` table has no ``pair_key`` (that is a hosted-mode-only addition), so
an iOS and Android build of the same app are not merged here: "top app" and the per-app
rows rank individual ``apps`` rows independently.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from email.message import EmailMessage
from html import escape

from storepulse.core import charts, config
from storepulse.core.sources import (
    apple_sales,
    apple_subscription_events,
    apple_subscriptions,
    play_earnings,
    play_installs,
    play_sales,
    play_vitals,
)
from storepulse.core.sources.play_common import Month

WARNING = "⚠"
_MINUS = "−"  # noqa: RUF001 (docs/SPEC.md's own example uses U+2212, not a hyphen)
_DOT = "·"
_APPROX = "≈"
_NO_DATA = "—"

_WINDOW_DAYS = 7
_SPARKLINE_DAYS = 30
# _build_app_rows slices both 7-day windows out of the sparkline series.
assert _SPARKLINE_DAYS >= 2 * _WINDOW_DAYS
_LABEL_WIDTH = 10

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS_ABBR = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)  # fmt: skip

# Common ISO 4217 codes shown with a symbol prefix; anything else falls back to "123.45 XYZ".
_CURRENCY_SYMBOLS = {
    "USD": "$", "CAD": "CA$", "AUD": "A$", "NZD": "NZ$", "GBP": "£", "EUR": "€",
    "JPY": "¥", "CNY": "¥", "HKD": "HK$", "SGD": "S$", "INR": "₹",
    "BRL": "R$", "MXN": "MX$", "KRW": "₩", "CHF": "CHF ",
}  # fmt: skip

_SOURCE_LABELS = {
    apple_sales.SOURCE: "apple_sales",
    apple_subscriptions.SOURCE: "apple_subscriptions",
    apple_subscription_events.SOURCE: "apple_subscription_events",
    play_installs.SOURCE: "play_installs",
    play_sales.SOURCE: "play_sales",
    play_earnings.SOURCE: "play_earnings",
    play_vitals.SOURCE: "vitals",
}
# daily: freshness measured in days since the newest 'ok' report_date.
# monthly: this month's file (report_date = month's first day) must be 'ok' by day 4.
# earnings: last month's earnings (report_date = that month's first day) must be 'ok'
# by the 15th of the month following it, i.e. the 15th of the current month.
_SOURCE_CADENCE = {
    apple_sales.SOURCE: "daily",
    apple_subscriptions.SOURCE: "daily",
    apple_subscription_events.SOURCE: "daily",
    play_installs.SOURCE: "monthly",
    play_sales.SOURCE: "monthly",
    play_earnings.SOURCE: "earnings",
    play_vitals.SOURCE: "daily",
}


@dataclass(frozen=True)
class Window:
    start: date
    end: date


_PLATFORM_LABELS = {"ios": "iOS", "android": "Android", "paired": "iOS + Android"}


@dataclass
class AppRow:
    app_id: int
    name: str
    platform: str  # 'ios' or 'android'
    installs: float
    proceeds: dict[str, float]
    sparkline_cid: str
    sparkline_png: bytes
    crash_rate: float | None = None
    crash_rate_28d: float | None = None
    anr_rate: float | None = None
    anr_rate_28d: float | None = None
    store_id: str = ""
    prev_installs: float = 0.0
    hidden: bool = False
    pair_key: str | None = None
    series: list[float] = field(default_factory=list)  # daily installs, oldest first

    @property
    def platform_label(self) -> str:
        return _PLATFORM_LABELS[self.platform]

    @property
    def active(self) -> bool:
        """Anything to show for this app in either week."""
        return bool(self.installs or self.prev_installs or self.proceeds)


@dataclass
class Digest:
    as_of: date | None
    text: str
    html: str
    images: list[tuple[str, bytes]] = field(default_factory=list)


@dataclass
class _Context:
    as_of: date
    cfg: config.Config
    ios_installs: float
    android_installs: float
    prev_ios_installs: float
    prev_android_installs: float
    proceeds: dict[str, float]
    prev_proceeds: dict[str, float]
    sales_gross: dict[str, float]
    top_app: AppRow | None
    vitals_warnings: list[str]
    data_line: str
    app_rows: list[AppRow]
    # Configured platforms that have never loaded data (e.g. Play bucket access pending):
    # shown as "—", never as a real 0, and left out of the combined totals.
    unavailable: frozenset[str] = frozenset()
    quiet_apps: int = 0  # apps collapsed into "+N more" (no installs or proceeds either week)


# -- formatting helpers ----------------------------------------------------------------


def _label(text: str) -> str:
    return text.ljust(_LABEL_WIDTH)


def _format_header_date(d: date) -> str:
    # Hand-rolled, not strftime: %a/%b are locale-dependent, and %-d (no leading zero)
    # isn't portable to Windows (which needs %#d), so this sidesteps both traps.
    return f"{_WEEKDAYS[d.weekday()]} {_MONTHS_ABBR[d.month - 1]} {d.day}"


def _short_date(d: date) -> str:
    return f"{_MONTHS_ABBR[d.month - 1]} {d.day}"


def _format_money(currency: str, amount: float) -> str:
    symbol = _CURRENCY_SYMBOLS.get(currency)
    if symbol is not None:
        return f"{symbol}{amount:,.2f}"
    return f"{amount:,.2f} {currency}".strip()


def format_money(currency: str, amount: float) -> str:
    """Public: the hosted dashboard formats money exactly like the digest."""
    return _format_money(currency, amount)


def _installs_text(count: float) -> str:
    return f"{count:.0f} install" + ("" if round(count) == 1 else "s")


def _pct(ratio: float | None) -> str:
    """A 0-1 ratio as a percentage, or "—" when there's no value (not the same as 0)."""
    return _NO_DATA if ratio is None else f"{ratio * 100:.1f}%"


def _money_with_trend(currency: str, current: float, previous: float) -> str:
    return f"{_format_money(currency, current)}{_trend_suffix(current, previous)}"


def _proceeds_parts(current: dict[str, float], previous: dict[str, float]) -> list[str]:
    return [_money_with_trend(c, current[c], previous.get(c, 0.0)) for c in sorted(current)]


def _trend_suffix(current: float, previous: float) -> str:
    if current == 0 and previous == 0:
        return ""
    if previous == 0:
        return " (new)"
    pct = round((current - previous) / previous * 100)
    sign = "+" if pct >= 0 else _MINUS
    return f" ({sign}{abs(pct)}% wk)"


def _converted_total(proceeds: dict[str, float], digest_cfg: config.DigestConfig) -> float | None:
    if not digest_cfg.rates or not digest_cfg.display_currency:
        return None
    total = 0.0
    counted = False
    for currency, amount in proceeds.items():
        rate = 1.0 if currency == digest_cfg.display_currency else digest_cfg.rates.get(currency)
        if rate is None:
            continue
        total += amount * rate
        counted = True
    return total if counted else None


# -- queries -----------------------------------------------------------------------------


def _latest_data_day(conn: sqlite3.Connection, source: str) -> str | None:
    row = conn.execute(
        "SELECT MAX(date) AS d FROM daily_metrics WHERE source = ?", (source,)
    ).fetchone()
    return str(row["d"]) if row["d"] else None


def _platform_as_of(conn: sqlite3.Connection, source: str) -> date | None:
    """The latest day this source has genuinely finished collecting.

    For a daily source (apple_sales), this is the later of daily_metrics' latest day
    and ingest_log's latest 'ok' report_date, not just one or the other:
    - ingest_log can be ahead of daily_metrics: a true zero-sales day writes no
      daily_metrics rows at all even though the report was collected successfully
      (docs/SPEC.md, Apple 404s), so daily_metrics' MAX(date) alone would understate
      freshness for a low-volume app.
    - daily_metrics can be ahead of ingest_log: a later re-attempt for an
      already-collected day can log a fresh 'error' row (a transient fetch failure)
      without touching that day's still-good data, so ingest_log's latest-'ok' alone
      would make already-collected data look like it was never collected.
    Monthly sources' ingest_log report_date is just the month's first day, not a real
    day, so those read only daily_metrics, which has one row per actual day in the file.
    """
    candidates = []
    data_day = _latest_data_day(conn, source)
    if data_day is not None:
        candidates.append(data_day)
    if _SOURCE_CADENCE.get(source) == "daily":
        row = conn.execute(
            "SELECT MAX(report_date) AS d FROM ingest_log WHERE source = ? AND status = 'ok'",
            (source,),
        ).fetchone()
        if row["d"] is not None:
            candidates.append(str(row["d"]))
    return date.fromisoformat(max(candidates)) if candidates else None


def compute_as_of(conn: sqlite3.Connection, cfg: config.Config) -> date | None:
    """The minimum of each configured platform's latest loaded day, or None if neither
    configured platform has any data yet."""
    candidates: list[date] = []
    if cfg.apple is not None:
        found = _platform_as_of(conn, apple_sales.SOURCE)
        if found is not None:
            candidates.append(found)
    if cfg.google is not None:
        found = _platform_as_of(conn, play_installs.SOURCE)
        if found is not None:
            candidates.append(found)
    return min(candidates) if candidates else None


def _window_total(
    conn: sqlite3.Connection,
    source: str,
    metric: str,
    window: Window,
    *,
    all_only: bool,
    app_id: int | None = None,
) -> float:
    where = "source = ? AND metric = ? AND date BETWEEN ? AND ?"
    params: list[object] = [source, metric, window.start.isoformat(), window.end.isoformat()]
    if all_only:
        where += " AND country = 'ALL'"
    if app_id is not None:
        where += " AND app_id = ?"
        params.append(app_id)
    row = conn.execute(
        f"SELECT COALESCE(SUM(value), 0) AS total FROM daily_metrics WHERE {where}",  # noqa: S608
        params,
    ).fetchone()
    return float(row["total"])


def _window_by_currency(
    conn: sqlite3.Connection, source: str, metric: str, window: Window, *, app_id: int | None = None
) -> dict[str, float]:
    where = "source = ? AND metric = ? AND date BETWEEN ? AND ?"
    params: list[object] = [source, metric, window.start.isoformat(), window.end.isoformat()]
    if app_id is not None:
        where += " AND app_id = ?"
        params.append(app_id)
    rows = conn.execute(
        f"SELECT currency, SUM(value) AS total FROM daily_metrics WHERE {where} GROUP BY currency",  # noqa: S608
        params,
    )
    return {str(r["currency"]): float(r["total"]) for r in rows if r["currency"]}


def _merge_currency(*sources: dict[str, float]) -> dict[str, float]:
    merged: dict[str, float] = {}
    for source in sources:
        for currency, amount in source.items():
            merged[currency] = merged.get(currency, 0.0) + amount
    return merged


def _ios_installs(conn: sqlite3.Connection, window: Window, app_id: int | None = None) -> float:
    return _window_total(
        conn, apple_sales.SOURCE, "installs", window, all_only=False, app_id=app_id
    )


def _android_installs(conn: sqlite3.Connection, window: Window, app_id: int | None = None) -> float:
    return _window_total(
        conn, play_installs.SOURCE, "installs", window, all_only=True, app_id=app_id
    )


def _proceeds_by_currency(
    conn: sqlite3.Connection, window: Window, app_id: int | None = None
) -> dict[str, float]:
    return _merge_currency(
        _window_by_currency(conn, apple_sales.SOURCE, "proceeds", window, app_id=app_id),
        _window_by_currency(conn, play_earnings.SOURCE, "proceeds", window, app_id=app_id),
    )


def _daily_series(
    conn: sqlite3.Connection,
    source: str,
    app_id: int,
    *,
    all_only: bool,
    end: date,
    days: int = _SPARKLINE_DAYS,
) -> list[float]:
    """Daily installs, oldest first, missing days filled with 0."""
    start = end - timedelta(days=days - 1)
    where = "source = ? AND metric = 'installs' AND app_id = ? AND date BETWEEN ? AND ?"
    params: list[object] = [source, app_id, start.isoformat(), end.isoformat()]
    if all_only:
        where += " AND country = 'ALL'"
    rows = conn.execute(
        f"SELECT date, SUM(value) AS total FROM daily_metrics WHERE {where} GROUP BY date",  # noqa: S608
        params,
    )
    by_date = {str(r["date"]): float(r["total"]) for r in rows}
    return [by_date.get((start + timedelta(days=i)).isoformat(), 0.0) for i in range(days)]


def _latest_vitals(conn: sqlite3.Connection, app_id: int, metric: str, as_of: date) -> float | None:
    row = conn.execute(
        "SELECT value FROM daily_metrics WHERE source = ? AND metric = ? AND app_id = ? "
        "AND date <= ? ORDER BY date DESC LIMIT 1",
        (play_vitals.SOURCE, metric, app_id, as_of.isoformat()),
    ).fetchone()
    return float(row["value"]) if row is not None else None


def _build_app_rows(conn: sqlite3.Connection, as_of: date, window: Window) -> list[AppRow]:
    """One row per app (never merged here; see _merge_pairs). A user's display name
    replaces the store name when set (hosted mode's Settings)."""
    apps = conn.execute(
        "SELECT id, name, display_name, platform, store_id, hidden, pair_key FROM apps "
        "ORDER BY name"
    ).fetchall()
    rows: list[AppRow] = []
    for record in apps:
        app_id, platform = int(record["id"]), str(record["platform"])
        name = str(record["display_name"] or record["name"])
        source = apple_sales.SOURCE if platform == "ios" else play_installs.SOURCE
        all_only = platform == "android"
        # The 30-day sparkline series ends on as_of, so it already holds both 7-day
        # windows; slice it instead of querying each window again.
        series = _daily_series(conn, source, app_id, all_only=all_only, end=as_of)
        installs = sum(series[-_WINDOW_DAYS:])
        prev_installs = sum(series[-2 * _WINDOW_DAYS : -_WINDOW_DAYS])
        proceeds = (
            _window_by_currency(conn, apple_sales.SOURCE, "proceeds", window, app_id=app_id)
            if platform == "ios"
            else _window_by_currency(conn, play_earnings.SOURCE, "proceeds", window, app_id=app_id)
        )
        row = AppRow(
            app_id=app_id,
            name=name,
            platform=platform,
            installs=installs,
            proceeds=proceeds,
            sparkline_cid=f"app{app_id}",
            sparkline_png=charts.render_sparkline(series),
            store_id=str(record["store_id"]),
            prev_installs=prev_installs,
            hidden=bool(record["hidden"]),
            pair_key=record["pair_key"],
            series=series,
        )
        if platform == "android":
            row.crash_rate = _latest_vitals(conn, app_id, "crash_rate", as_of)
            row.crash_rate_28d = _latest_vitals(conn, app_id, "crash_rate_28d", as_of)
            row.anr_rate = _latest_vitals(conn, app_id, "anr_rate", as_of)
            row.anr_rate_28d = _latest_vitals(conn, app_id, "anr_rate_28d", as_of)
        rows.append(row)
    _disambiguate(rows)
    rows.sort(key=lambda r: (-r.installs, r.name))
    return rows


def _disambiguate(rows: list[AppRow]) -> None:
    """Append the store id to apps sharing a name on the same platform (e.g. two Android
    packages both called "Bus Wise"), so their rows can be told apart."""
    seen = Counter((r.name, r.platform) for r in rows)
    for row in rows:
        if seen[(row.name, row.platform)] > 1:
            row.name = f"{row.name} [{row.store_id}]"


def _merge_pairs(rows: list[AppRow]) -> list[AppRow]:
    """Combine each iOS + Android pair (same ``pair_key``, hosted mode only) into one row.

    Per docs/SPEC.md (Build phases, Phase 4): "sum" metrics add across the pair, money
    per currency and never converted; vitals stay per platform (the merged row carries the
    Android member's rates, and warnings are computed on the unmerged rows). Callers pass
    only listable rows, so a hidden member is already gone and its partner stays alone.
    """
    groups: dict[str, list[AppRow]] = {}
    for row in rows:
        if row.pair_key:
            groups.setdefault(row.pair_key, []).append(row)
    merged: list[AppRow] = []
    done: set[str] = set()
    for row in rows:
        members = groups.get(row.pair_key or "", [])
        if len(members) != 2 or {m.platform for m in members} != {"ios", "android"}:
            merged.append(row)
            continue
        if row.pair_key in done:
            continue
        done.add(row.pair_key or "")
        ios = next(m for m in members if m.platform == "ios")
        android = next(m for m in members if m.platform == "android")
        series = [a + b for a, b in zip(ios.series, android.series, strict=True)]
        merged.append(
            AppRow(
                app_id=ios.app_id,
                name=ios.name,
                platform="paired",
                installs=ios.installs + android.installs,
                proceeds=_merge_currency(ios.proceeds, android.proceeds),
                sparkline_cid=f"pair{ios.app_id}",
                sparkline_png=charts.render_sparkline(series),
                crash_rate=android.crash_rate,
                crash_rate_28d=android.crash_rate_28d,
                anr_rate=android.anr_rate,
                anr_rate_28d=android.anr_rate_28d,
                store_id=f"{ios.store_id} + {android.store_id}",
                prev_installs=ios.prev_installs + android.prev_installs,
                pair_key=ios.pair_key,
                series=series,
            )
        )
    merged.sort(key=lambda r: (-r.installs, r.name))
    return merged


def _platform_has_data(conn: sqlite3.Connection, platform: str) -> bool:
    """Whether a platform has ever loaded data.

    Deliberately not scoped to the digest window: as_of is the earliest of each
    platform's latest day, so a stalled platform drags the window back, and checking the
    other platform against that stale window would show it as "—" while its Data-line
    entry says ok. Apple writes no rows on zero-sales days, so a successful pull counts.
    """
    source = apple_sales.SOURCE if platform == "ios" else play_installs.SOURCE
    if conn.execute("SELECT 1 FROM daily_metrics WHERE source = ? LIMIT 1", (source,)).fetchone():
        return True
    if platform == "ios":
        return (
            conn.execute(
                "SELECT 1 FROM ingest_log WHERE source = ? AND status = 'ok' LIMIT 1", (source,)
            ).fetchone()
            is not None
        )
    return False


def _vitals_warnings(app_rows: list[AppRow], digest_cfg: config.DigestConfig) -> list[str]:
    warnings: list[str] = []
    for row in app_rows:
        if row.platform != "android":
            continue
        if row.crash_rate_28d is not None and row.crash_rate_28d > digest_cfg.crash_threshold:
            warnings.append(
                f"{row.name} Android crash rate {row.crash_rate_28d * 100:.1f}% {WARNING}"
            )
        if row.anr_rate_28d is not None and row.anr_rate_28d > digest_cfg.anr_threshold:
            warnings.append(f"{row.name} Android ANR rate {row.anr_rate_28d * 100:.1f}% {WARNING}")
    return warnings


# -- source freshness (docs/SPEC.md, Scheduling and reliability) -----------------------


def _seen(conn: sqlite3.Connection, source: str) -> bool:
    row = conn.execute("SELECT 1 FROM ingest_log WHERE source = ? LIMIT 1", (source,)).fetchone()
    return row is not None


def _configured_sources(conn: sqlite3.Connection, cfg: config.Config) -> list[str]:
    sources: list[str] = []
    if cfg.apple is not None:
        sources.append(apple_sales.SOURCE)
        # Unlike apple_sales, shown only once seen: not every app has subscription
        # products, and an account with none may get an ambiguous 404 rather than a
        # clean empty report, so showing these unconditionally risked permanent
        # false "not_ready" noise for developers with no subscriptions at all.
        for source in (apple_subscriptions.SOURCE, apple_subscription_events.SOURCE):
            if _seen(conn, source):
                sources.append(source)
    if cfg.google is not None:
        sources += [play_installs.SOURCE, play_vitals.SOURCE]
        for source in (play_sales.SOURCE, play_earnings.SOURCE):
            if _seen(conn, source):
                sources.append(source)
    return sources


def _window_bounds(source: str, today: date, days: int) -> tuple[str, str]:
    """The report_dates the daily run currently re-attempts for this source.

    Used to scope the error check in _source_note: an old, never-retried error from a
    one-off backfill (the daily run only re-touches the last `days` days) must not flag
    the digest forever.
    """
    cadence = _SOURCE_CADENCE[source]
    if cadence == "daily":
        yesterday = today - timedelta(days=1)
        start = yesterday - timedelta(days=days - 1)
        return start.isoformat(), yesterday.isoformat()
    if cadence == "monthly":
        current = Month.of(today)
        return current.prev().first_day.isoformat(), current.first_day.isoformat()
    if cadence == "earnings":
        prev = Month.of(today).prev()
        return prev.prev().first_day.isoformat(), prev.first_day.isoformat()
    raise AssertionError(f"unknown cadence for {source!r}")


def _source_note(
    conn: sqlite3.Connection, source: str, today: date, days: int
) -> tuple[bool, str | None, str | None]:
    """(has_unresolved_error, latest_ok_date, latest_not_ready_date).

    has_unresolved_error is true if, among the report_dates the daily run currently
    re-attempts (_window_bounds — an old, never-retried backfill error must not flag the
    digest forever), any (report_date, app)'s most recent attempt is an error.

    Grouping includes app_id, not just report_date: play_installs and play_vitals log
    one row per app under the same report_date (that month's first day, for installs),
    so report_date alone would let one app's later success hide a different app's
    error. app_id is NULL for an account-wide source (apple_sales, play_sales,
    play_earnings), where report_date alone already identifies one attempt; "b.app_id
    IS a.app_id" (not "=") so two NULLs still count as the same attempt-group there.

    started_at is not a reliable tie-breaker for "most recent": it has only
    one-second resolution, so id (insertion order) is used instead.
    """
    start, end = _window_bounds(source, today, days)
    has_error = conn.execute(
        """
        SELECT 1 FROM ingest_log a
        WHERE a.source = ? AND a.status = 'error'
        AND a.report_date BETWEEN ? AND ?
        AND a.id = (
            SELECT b.id FROM ingest_log b
            WHERE b.source = a.source AND b.report_date = a.report_date
            AND b.app_id IS a.app_id
            ORDER BY b.id DESC LIMIT 1
        )
        LIMIT 1
        """,
        (source, start, end),
    ).fetchone()
    ok_row = conn.execute(
        "SELECT MAX(report_date) AS d FROM ingest_log WHERE source = ? AND status = 'ok'", (source,)
    ).fetchone()
    not_ready_row = conn.execute(
        "SELECT MAX(report_date) AS d FROM ingest_log WHERE source = ? AND status = 'not_ready'",
        (source,),
    ).fetchone()
    return has_error is not None, ok_row["d"], not_ready_row["d"]


def _has_rows_since(conn: sqlite3.Connection, source: str, start: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM daily_metrics WHERE source = ? AND date >= ? LIMIT 1", (source, start)
    ).fetchone()
    return row is not None


def _is_overdue(source: str, latest_ok: str | None, today: date, stale_days: int) -> bool:
    cadence = _SOURCE_CADENCE[source]
    if cadence == "daily":
        return latest_ok is None or (today - date.fromisoformat(latest_ok)).days > stale_days
    if cadence == "monthly":
        month_start = date(today.year, today.month, 1).isoformat()
        return today.day > 4 and latest_ok != month_start
    if cadence == "earnings":
        last_month_start = Month.of(today).prev().first_day.isoformat()
        return today.day > 15 and latest_ok != last_month_start
    raise AssertionError(f"unknown cadence for {source!r}")


def _source_status_text(
    conn: sqlite3.Connection, source: str, today: date, as_of: date, stale_days: int, days: int
) -> str:
    label = _SOURCE_LABELS[source]
    has_error, latest_ok, latest_not_ready = _source_note(conn, source, today, days)
    if has_error:
        return f"{label} error"
    if _is_overdue(source, latest_ok, today, stale_days):
        return f"{label} not_ready"
    note = ""
    if source == play_vitals.SOURCE and not _has_rows_since(
        conn, source, _window_bounds(source, today, days)[0]
    ):
        # Collected fine, but Google returned no values (common below its minimum
        # user count): say so rather than a bare "ok".
        return f"{label} ok (no data from Google)"
    if latest_not_ready is not None and (latest_ok is None or latest_not_ready > latest_ok):
        note = f" ({_short_date(date.fromisoformat(latest_not_ready))} not ready)"
    else:
        display_date = latest_ok
        if _SOURCE_CADENCE[source] != "daily":
            # ingest_log.report_date for a monthly/earnings source is that month's
            # first day, not a real day of data — show the latest day the file
            # actually covers instead (e.g. "play_installs ok (Sep 22)", not "(Sep 1)").
            display_date = _latest_data_day(conn, source) or latest_ok
        if display_date is not None and display_date != as_of.isoformat():
            note = f" ({_short_date(date.fromisoformat(display_date))})"
    return f"{label} ok{note}"


def source_statuses(
    conn: sqlite3.Connection, cfg: config.Config, today: date | None = None
) -> list[tuple[str, str]]:
    """(source, status text) per configured source, exactly as the digest's Data line
    words it; the hosted Sources page shows the same list."""
    today = today if today is not None else apple_sales.pacific_today()
    as_of = compute_as_of(conn, cfg) or today
    return [
        (
            source,
            _source_status_text(
                conn, source, today, as_of, cfg.digest.stale_days, cfg.schedule.days
            ),
        )
        for source in _configured_sources(conn, cfg)
    ]


def _data_line(conn: sqlite3.Connection, cfg: config.Config, today: date, as_of: date) -> str:
    sources = _configured_sources(conn, cfg)
    if not sources:
        return ""
    parts = [
        _source_status_text(conn, source, today, as_of, cfg.digest.stale_days, cfg.schedule.days)
        for source in sources
    ]
    return f" {_DOT} ".join(parts)


# -- rendering ---------------------------------------------------------------------------


def _installs_summary(ctx: _Context) -> tuple[float, float, list[str]]:
    """(combined, previous combined, per-platform parts). A platform without data is
    shown as "—" and left out of both totals, so the trend compares like with like."""
    combined = prev_combined = 0.0
    parts: list[str] = []
    for platform, label, configured, current, previous in (
        ("ios", "iOS", ctx.cfg.apple, ctx.ios_installs, ctx.prev_ios_installs),
        ("android", "Android", ctx.cfg.google, ctx.android_installs, ctx.prev_android_installs),
    ):
        if configured is None:
            continue
        if platform in ctx.unavailable:
            parts.append(f"{label} {_NO_DATA}")
            continue
        combined += current
        prev_combined += previous
        parts.append(f"{label} {current:.0f}")
    return combined, prev_combined, parts


def _quiet_apps_text(count: int) -> str:
    noun = "app" if count == 1 else "apps"
    return f"+{count} more {noun} with no installs or proceeds in either week"


def _render_text(ctx: _Context) -> str:
    lines = [f"Storepulse {_DOT} {_format_header_date(ctx.as_of)}"]

    combined, prev_combined, breakdown_parts = _installs_summary(ctx)
    breakdown = f"   {f' {_DOT} '.join(breakdown_parts)}" if breakdown_parts else ""
    lines.append(
        f"{_label('Installs')}{combined:.0f}{_trend_suffix(combined, prev_combined)}{breakdown}"
    )

    if ctx.proceeds:
        parts = _proceeds_parts(ctx.proceeds, ctx.prev_proceeds)
        line = f"{_label('Proceeds')}{', '.join(parts)}"
        converted = _converted_total(ctx.proceeds, ctx.cfg.digest)
        if converted is not None:
            line += (
                f" {_APPROX} {_format_money(ctx.cfg.digest.display_currency, converted)} converted"
            )
        lines.append(line)

    if ctx.sales_gross:
        amounts = ", ".join(_format_money(c, ctx.sales_gross[c]) for c in sorted(ctx.sales_gross))
        lines.append(
            "Android sales (gross, provisional, until the month's earnings arrive)  " + amounts
        )

    if ctx.top_app is not None:
        lines.append(
            f"{_label('Top app')}{ctx.top_app.name} {_installs_text(ctx.top_app.installs)}"
        )

    if ctx.vitals_warnings:
        lines.append(f"{_label('Vitals')}{'; '.join(ctx.vitals_warnings)}")

    if ctx.data_line:
        lines.append(f"{_label('Data')}{ctx.data_line}")

    if ctx.app_rows or ctx.quiet_apps:
        lines.append("")
        lines.append("Apps")
        for row in ctx.app_rows:
            platform_label = row.platform_label
            proceeds_text = (
                ", ".join(_format_money(c, row.proceeds[c]) for c in sorted(row.proceeds))
                if row.proceeds
                else "no proceeds"
            )
            lines.append(
                f"  {row.name} ({platform_label}): {_installs_text(row.installs)}, {proceeds_text}"
            )
        if ctx.quiet_apps:
            lines.append(f"  {_quiet_apps_text(ctx.quiet_apps)}")

    return "\n".join(lines) + "\n"


def _html_escape(text: str) -> str:
    return escape(text, quote=True)


def _render_html(ctx: _Context) -> str:
    combined, prev_combined, breakdown_parts = _installs_summary(ctx)
    breakdown = f" &mdash; {' &middot; '.join(breakdown_parts)}" if breakdown_parts else ""

    rows_html = []
    for row in ctx.app_rows:
        platform_label = row.platform_label
        proceeds_text = (
            ", ".join(_format_money(c, row.proceeds[c]) for c in sorted(row.proceeds))
            if row.proceeds
            else "no proceeds"
        )
        vitals_text = ""
        # Either rate can independently be missing: Google's Reporting API omits a
        # metric set's row when there's insufficient user population for statistical
        # confidence, and crash/ANR are queried as two separate metric sets. Gating on
        # crash_rate_28d alone would hide real, valid ANR data on a day crash_rate_28d
        # is absent (or vice versa).
        # (iOS rows carry no vitals; a paired row carries its Android member's.)
        if row.crash_rate_28d is not None or row.anr_rate_28d is not None:
            # A rate Google withheld is shown as "—", never as 0.0%: a missing crash
            # rate must not read as a perfectly stable app.
            vitals_text = (
                '<div style="color:#666;font-size:12px;">'
                f"crash {_pct(row.crash_rate)} daily, {_pct(row.crash_rate_28d)} 28d "
                f"&middot; anr {_pct(row.anr_rate)} daily, {_pct(row.anr_rate_28d)} 28d</div>"
            )
        rows_html.append(
            "<tr>"
            '<td style="padding:8px 12px;border-bottom:1px solid #eee;">'
            f'<img src="cid:{row.sparkline_cid}" width="{charts.WIDTH}" height="{charts.HEIGHT}" '
            f'alt="30-day installs trend" style="display:block;"></td>'
            '<td style="padding:8px 12px;border-bottom:1px solid #eee;">'
            f"<strong>{_html_escape(row.name)}</strong> ({platform_label})<br>"
            f"{_installs_text(row.installs)}, {_html_escape(proceeds_text)}"
            f"{vitals_text}</td>"
            "</tr>"
        )

    if ctx.quiet_apps:
        rows_html.append(
            '<tr><td></td><td style="padding:8px 12px;color:#666;">'
            f"{_quiet_apps_text(ctx.quiet_apps)}</td></tr>"
        )

    warnings_html = ""
    if ctx.vitals_warnings:
        items = "".join(f"<li>{_html_escape(w)}</li>" for w in ctx.vitals_warnings)
        warnings_html = f'<ul style="color:#b91c1c;">{items}</ul>'

    sales_gross_html = ""
    if ctx.sales_gross:
        amounts = ", ".join(_format_money(c, ctx.sales_gross[c]) for c in sorted(ctx.sales_gross))
        sales_gross_html = (
            '<p style="color:#666;">Android sales (gross, provisional, until the month\'s '
            f"earnings arrive): {amounts}</p>"
        )

    proceeds_html = ""
    if ctx.proceeds:
        proceeds_parts = _proceeds_parts(ctx.proceeds, ctx.prev_proceeds)
        converted = _converted_total(ctx.proceeds, ctx.cfg.digest)
        converted_html = ""
        if converted is not None:
            converted_html = (
                f" {_APPROX} {_format_money(ctx.cfg.digest.display_currency, converted)} converted"
            )
        proceeds_html = (
            f'<p style="margin:4px 0;">Proceeds: {", ".join(proceeds_parts)}{converted_html}</p>'
        )

    top_app_html = ""
    if ctx.top_app is not None:
        top_app_html = (
            f"<p>Top app: <strong>{_html_escape(ctx.top_app.name)}</strong> "
            f"{_installs_text(ctx.top_app.installs)}</p>"
        )

    return (
        '<div style="font-family:sans-serif;color:#111;max-width:600px;">'
        f'<h2 style="margin-bottom:4px;">Storepulse {_DOT} {_format_header_date(ctx.as_of)}</h2>'
        f'<p style="font-size:18px;margin:4px 0;">Installs: {combined:.0f}'
        f"{_trend_suffix(combined, prev_combined)}{breakdown}</p>"
        f"{proceeds_html}"
        f"{sales_gross_html}{top_app_html}{warnings_html}"
        f'<p style="color:#666;">Data: {ctx.data_line or "&mdash;"}</p>'
        f'<table style="border-collapse:collapse;width:100%;">{"".join(rows_html)}</table>'
        "</div>"
    )


def _empty_digest() -> Digest:
    text = (
        "Storepulse\n\n"
        "No data has been collected yet. Run `storepulse run` after setup finishes its "
        "first collection.\n"
    )
    html = (
        '<div style="font-family:sans-serif;">'
        "<h2>Storepulse</h2>"
        "<p>No data has been collected yet. Run <code>storepulse run</code> after setup "
        "finishes its first collection.</p></div>"
    )
    return Digest(as_of=None, text=text, html=html, images=[])


def build_digest(
    conn: sqlite3.Connection,
    cfg: config.Config,
    *,
    today: date | None = None,
    dashboard_url: str | None = None,
) -> Digest:
    """``dashboard_url`` (hosted mode) adds a link to the dashboard (docs/SPEC.md,
    Outputs: "In hosted mode the email links to the dashboard")."""
    today = today if today is not None else apple_sales.pacific_today()
    as_of = compute_as_of(conn, cfg)
    if as_of is None:
        return _empty_digest()

    window = Window(as_of - timedelta(days=_WINDOW_DAYS - 1), as_of)
    prev_window = Window(
        window.start - timedelta(days=_WINDOW_DAYS), window.start - timedelta(days=1)
    )

    unavailable = frozenset(
        platform
        for platform, configured in (("ios", cfg.apple), ("android", cfg.google))
        if configured is not None and not _platform_has_data(conn, platform)
    )
    all_rows = _build_app_rows(conn, as_of, window)
    # Listed: not hidden (docs/SPEC.md, Storage: hidden apps leave every list but still
    # count in totals and warnings), and not on a platform without data (the Data line
    # says why). Pairs merge into one row; apps with nothing in either week collapse
    # into one "+N more" line.
    listable = _merge_pairs([r for r in all_rows if not r.hidden and r.platform not in unavailable])
    app_rows = [r for r in listable if r.active]
    quiet_apps = sum(1 for r in listable if not r.active)
    ctx = _Context(
        as_of=as_of,
        cfg=cfg,
        ios_installs=_ios_installs(conn, window),
        android_installs=_android_installs(conn, window),
        prev_ios_installs=_ios_installs(conn, prev_window),
        prev_android_installs=_android_installs(conn, prev_window),
        proceeds=_proceeds_by_currency(conn, window),
        prev_proceeds=_proceeds_by_currency(conn, prev_window),
        sales_gross=_window_by_currency(conn, play_sales.SOURCE, "sales_gross", window),
        top_app=app_rows[0] if app_rows else None,
        # All apps: crash/ANR rates come from the Reporting API, independent of installs
        # (a quiet app, or a platform whose bucket is unreachable, can still be crashing).
        vitals_warnings=_vitals_warnings(all_rows, cfg.digest),
        data_line=_data_line(conn, cfg, today, as_of),
        app_rows=app_rows,
        unavailable=unavailable,
        quiet_apps=quiet_apps,
    )
    text, html = _render_text(ctx), _render_html(ctx)
    if dashboard_url:
        text += f"\nDashboard: {dashboard_url}\n"
        html = html.removesuffix("</div>") + (
            f'<p><a href="{_html_escape(dashboard_url)}">Open the dashboard</a></p></div>'
        )
    return Digest(
        as_of=as_of,
        text=text,
        html=html,
        images=[(row.sparkline_cid, row.sparkline_png) for row in app_rows],
    )


def to_email_message(digest: Digest, email_cfg: config.EmailConfig) -> EmailMessage:
    msg = EmailMessage()
    if digest.as_of is not None:
        msg["Subject"] = f"Storepulse {_DOT} {_format_header_date(digest.as_of)}"
    else:
        msg["Subject"] = "Storepulse"
    msg["From"] = email_cfg.from_addr
    msg["To"] = ", ".join(email_cfg.to_addrs)
    msg.set_content(digest.text)
    msg.add_alternative(digest.html, subtype="html")
    html_part = msg.get_payload(1)
    assert isinstance(html_part, EmailMessage)  # true whenever policy.default builds the parts
    for cid, png in digest.images:
        html_part.add_related(png, "image", "png", cid=f"<{cid}>")
    return msg
