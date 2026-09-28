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
from dataclasses import dataclass, field
from datetime import date, timedelta
from email.message import EmailMessage
from html import escape

from storepulse.core import charts, config
from storepulse.core.sources import (
    apple_sales,
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

_WINDOW_DAYS = 7
_SPARKLINE_DAYS = 30
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
    play_installs.SOURCE: "monthly",
    play_sales.SOURCE: "monthly",
    play_earnings.SOURCE: "earnings",
    play_vitals.SOURCE: "daily",
}


@dataclass(frozen=True)
class Window:
    start: date
    end: date


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


def _platform_as_of(conn: sqlite3.Connection, source: str) -> date | None:
    row = conn.execute(
        "SELECT MAX(date) AS d FROM daily_metrics WHERE source = ?", (source,)
    ).fetchone()
    value = row["d"]
    return date.fromisoformat(value) if value else None


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


def _build_app_rows(
    conn: sqlite3.Connection, as_of: date, window: Window, prev_window: Window
) -> list[AppRow]:
    apps = conn.execute("SELECT id, name, platform FROM apps ORDER BY name").fetchall()
    rows: list[AppRow] = []
    for record in apps:
        app_id, name, platform = int(record["id"]), str(record["name"]), str(record["platform"])
        source = apple_sales.SOURCE if platform == "ios" else play_installs.SOURCE
        all_only = platform == "android"
        installs = _window_total(conn, source, "installs", window, all_only=all_only, app_id=app_id)
        series = _daily_series(conn, source, app_id, all_only=all_only, end=as_of)
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
        )
        if platform == "android":
            row.crash_rate = _latest_vitals(conn, app_id, "crash_rate", as_of)
            row.crash_rate_28d = _latest_vitals(conn, app_id, "crash_rate_28d", as_of)
            row.anr_rate = _latest_vitals(conn, app_id, "anr_rate", as_of)
            row.anr_rate_28d = _latest_vitals(conn, app_id, "anr_rate_28d", as_of)
        rows.append(row)
    rows.sort(key=lambda r: (-r.installs, r.name))
    return rows


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


def _configured_sources(conn: sqlite3.Connection, cfg: config.Config) -> list[str]:
    sources: list[str] = []
    if cfg.apple is not None:
        sources.append(apple_sales.SOURCE)
    if cfg.google is not None:
        sources += [play_installs.SOURCE, play_vitals.SOURCE]
        for source in (play_sales.SOURCE, play_earnings.SOURCE):
            seen = conn.execute(
                "SELECT 1 FROM ingest_log WHERE source = ? LIMIT 1", (source,)
            ).fetchone()
            if seen is not None:
                sources.append(source)
    return sources


def _source_note(
    conn: sqlite3.Connection, source: str, as_of: date
) -> tuple[str, str | None, str | None]:
    """(state, latest_ok_date, latest_not_ready_date). state is the *last attempt's* status."""
    latest = conn.execute(
        "SELECT status FROM ingest_log WHERE source = ? ORDER BY started_at DESC LIMIT 1", (source,)
    ).fetchone()
    state = str(latest["status"]) if latest is not None else "not_ready"
    ok_row = conn.execute(
        "SELECT MAX(report_date) AS d FROM ingest_log WHERE source = ? AND status = 'ok'", (source,)
    ).fetchone()
    not_ready_row = conn.execute(
        "SELECT MAX(report_date) AS d FROM ingest_log WHERE source = ? AND status = 'not_ready'",
        (source,),
    ).fetchone()
    return state, ok_row["d"], not_ready_row["d"]


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
    conn: sqlite3.Connection, source: str, today: date, as_of: date, stale_days: int
) -> str:
    label = _SOURCE_LABELS[source]
    last_attempt_state, latest_ok, latest_not_ready = _source_note(conn, source, as_of)
    if last_attempt_state == "error":
        return f"{label} error"
    if _is_overdue(source, latest_ok, today, stale_days):
        return f"{label} not_ready"
    note = ""
    if latest_not_ready is not None and (latest_ok is None or latest_not_ready > latest_ok):
        note = f" ({_short_date(date.fromisoformat(latest_not_ready))} not ready)"
    elif latest_ok is not None and latest_ok != as_of.isoformat():
        note = f" ({_short_date(date.fromisoformat(latest_ok))})"
    return f"{label} ok{note}"


def _data_line(conn: sqlite3.Connection, cfg: config.Config, today: date, as_of: date) -> str:
    sources = _configured_sources(conn, cfg)
    if not sources:
        return ""
    parts = [
        _source_status_text(conn, source, today, as_of, cfg.digest.stale_days) for source in sources
    ]
    return f" {_DOT} ".join(parts)


# -- rendering ---------------------------------------------------------------------------


def _render_text(ctx: _Context) -> str:
    lines = [f"Storepulse {_DOT} {_format_header_date(ctx.as_of)}"]

    combined = ctx.ios_installs + ctx.android_installs
    prev_combined = ctx.prev_ios_installs + ctx.prev_android_installs
    breakdown_parts = []
    if ctx.cfg.apple is not None:
        breakdown_parts.append(f"iOS {ctx.ios_installs:.0f}")
    if ctx.cfg.google is not None:
        breakdown_parts.append(f"Android {ctx.android_installs:.0f}")
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
        lines.append(f"{_label('Top app')}{ctx.top_app.name} {ctx.top_app.installs:.0f} installs")

    if ctx.vitals_warnings:
        lines.append(f"{_label('Vitals')}{'; '.join(ctx.vitals_warnings)}")

    if ctx.data_line:
        lines.append(f"{_label('Data')}{ctx.data_line}")

    if ctx.app_rows:
        lines.append("")
        lines.append("Apps")
        for row in ctx.app_rows:
            platform_label = "iOS" if row.platform == "ios" else "Android"
            proceeds_text = (
                ", ".join(_format_money(c, row.proceeds[c]) for c in sorted(row.proceeds))
                if row.proceeds
                else "no proceeds"
            )
            lines.append(
                f"  {row.name} ({platform_label}): {row.installs:.0f} installs, {proceeds_text}"
            )

    return "\n".join(lines) + "\n"


def _html_escape(text: str) -> str:
    return escape(text, quote=True)


def _render_html(ctx: _Context) -> str:
    combined = ctx.ios_installs + ctx.android_installs
    prev_combined = ctx.prev_ios_installs + ctx.prev_android_installs
    breakdown_parts = []
    if ctx.cfg.apple is not None:
        breakdown_parts.append(f"iOS {ctx.ios_installs:.0f}")
    if ctx.cfg.google is not None:
        breakdown_parts.append(f"Android {ctx.android_installs:.0f}")
    breakdown = f" &mdash; {' &middot; '.join(breakdown_parts)}" if breakdown_parts else ""

    rows_html = []
    for row in ctx.app_rows:
        platform_label = "iOS" if row.platform == "ios" else "Android"
        proceeds_text = (
            ", ".join(_format_money(c, row.proceeds[c]) for c in sorted(row.proceeds))
            if row.proceeds
            else "no proceeds"
        )
        vitals_text = ""
        if row.platform == "android" and row.crash_rate_28d is not None:
            crash_daily = (row.crash_rate or 0) * 100
            anr_daily = (row.anr_rate or 0) * 100
            anr_28d = (row.anr_rate_28d or 0) * 100
            vitals_text = (
                '<div style="color:#666;font-size:12px;">'
                f"crash {crash_daily:.1f}% daily, {row.crash_rate_28d * 100:.1f}% 28d "
                f"&middot; anr {anr_daily:.1f}% daily, {anr_28d:.1f}% 28d</div>"
            )
        rows_html.append(
            "<tr>"
            '<td style="padding:8px 12px;border-bottom:1px solid #eee;">'
            f'<img src="cid:{row.sparkline_cid}" width="{charts.WIDTH}" height="{charts.HEIGHT}" '
            f'alt="30-day installs trend" style="display:block;"></td>'
            '<td style="padding:8px 12px;border-bottom:1px solid #eee;">'
            f"<strong>{_html_escape(row.name)}</strong> ({platform_label})<br>"
            f"{row.installs:.0f} installs, {_html_escape(proceeds_text)}"
            f"{vitals_text}</td>"
            "</tr>"
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

    proceeds_parts = _proceeds_parts(ctx.proceeds, ctx.prev_proceeds)
    converted = _converted_total(ctx.proceeds, ctx.cfg.digest)
    converted_html = ""
    if converted is not None:
        converted_html = (
            f" {_APPROX} {_format_money(ctx.cfg.digest.display_currency, converted)} converted"
        )

    top_app_html = ""
    if ctx.top_app is not None:
        top_app_html = (
            f"<p>Top app: <strong>{_html_escape(ctx.top_app.name)}</strong> "
            f"{ctx.top_app.installs:.0f} installs</p>"
        )

    proceeds_html = ", ".join(proceeds_parts) or "&mdash;"
    return (
        '<div style="font-family:sans-serif;color:#111;max-width:600px;">'
        f'<h2 style="margin-bottom:4px;">Storepulse {_DOT} {_format_header_date(ctx.as_of)}</h2>'
        f'<p style="font-size:18px;margin:4px 0;">Installs: {combined:.0f}'
        f"{_trend_suffix(combined, prev_combined)}{breakdown}</p>"
        f'<p style="margin:4px 0;">Proceeds: {proceeds_html}{converted_html}</p>'
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
    conn: sqlite3.Connection, cfg: config.Config, *, today: date | None = None
) -> Digest:
    today = today if today is not None else apple_sales.pacific_today()
    as_of = compute_as_of(conn, cfg)
    if as_of is None:
        return _empty_digest()

    window = Window(as_of - timedelta(days=_WINDOW_DAYS - 1), as_of)
    prev_window = Window(
        window.start - timedelta(days=_WINDOW_DAYS), window.start - timedelta(days=1)
    )

    app_rows = _build_app_rows(conn, as_of, window, prev_window)
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
        vitals_warnings=_vitals_warnings(app_rows, cfg.digest),
        data_line=_data_line(conn, cfg, today, as_of),
        app_rows=app_rows,
    )
    return Digest(
        as_of=as_of,
        text=_render_text(ctx),
        html=_render_html(ctx),
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
