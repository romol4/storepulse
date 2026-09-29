"""Read-only queries behind the hosted dashboard (docs/SPEC.md, Web dashboard).

Same aggregation rules as the digest: iOS installs sum Apple's per-country rows (Apple
writes no ``ALL`` row), Android reads ``play_installs``' ``ALL`` row and never adds the
countries to it, and money stays per currency.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import date, timedelta

from storepulse.core import db
from storepulse.core.sources import apple_sales, play_earnings, play_installs, play_sales

RANGES = (7, 30, 90)


@dataclass
class InstallsSeries:
    days: list[str]
    ios: list[float]
    android: list[float]

    @property
    def total(self) -> float:
        return sum(self.ios) + sum(self.android)


@dataclass
class AppGroup:
    """One logical app: a single app, or an iOS + Android pair."""

    key: str  # URL id: the iOS app's id for a pair, else the app's own id
    label: str
    apps: list[db.AppInfo] = field(default_factory=list)

    @property
    def ids(self) -> list[int]:
        return [a.id for a in self.apps]

    @property
    def platforms(self) -> str:
        names = {"ios": "iOS", "android": "Android"}
        return " + ".join(
            names[a.platform] for a in sorted(self.apps, key=lambda a: a.platform != "ios")
        )


def app_groups(conn: sqlite3.Connection, *, include_hidden: bool = False) -> list[AppGroup]:
    """Apps as the dashboard lists them: pairs merged, hidden apps left out unless asked."""
    apps = [a for a in db.list_apps(conn) if include_hidden or not a.hidden]
    by_pair: dict[str, list[db.AppInfo]] = {}
    for app in apps:
        if app.pair_key:
            by_pair.setdefault(app.pair_key, []).append(app)
    groups: list[AppGroup] = []
    seen: set[str] = set()
    for app in apps:
        members = by_pair.get(app.pair_key or "", [])
        if len(members) == 2 and {m.platform for m in members} == {"ios", "android"}:
            if app.pair_key in seen:
                continue
            seen.add(app.pair_key or "")
            ios = next(m for m in members if m.platform == "ios")
            groups.append(
                AppGroup(str(ios.id), ios.label, sorted(members, key=lambda m: m.platform != "ios"))
            )
        else:
            groups.append(AppGroup(str(app.id), app.label, [app]))
    groups.sort(key=lambda g: g.label.casefold())
    return groups


def find_group(conn: sqlite3.Connection, key: str) -> AppGroup | None:
    for group in app_groups(conn, include_hidden=True):
        if group.key == key or key in {str(i) for i in group.ids}:
            return group
    return None


def _in(ids: Collection[int] | None) -> tuple[str, list[object]]:
    if ids is None:
        return "", []
    ids = list(ids)
    if not ids:
        return " AND 0", []
    return f" AND app_id IN ({', '.join('?' for _ in ids)})", list(ids)


def installs_series(
    conn: sqlite3.Connection, end: date, days: int, app_ids: Collection[int] | None = None
) -> InstallsSeries:
    start = end - timedelta(days=days - 1)
    day_list = [(start + timedelta(days=i)).isoformat() for i in range(days)]
    clause, extra = _in(app_ids)
    out: dict[str, dict[str, float]] = {"ios": {}, "android": {}}
    for platform, source, all_only in (
        ("ios", apple_sales.SOURCE, False),
        ("android", play_installs.SOURCE, True),
    ):
        where = "source = ? AND metric = 'installs' AND date BETWEEN ? AND ?" + clause
        if all_only:
            where += " AND country = 'ALL'"
        rows = conn.execute(
            f"SELECT date, SUM(value) AS v FROM daily_metrics WHERE {where} GROUP BY date",  # noqa: S608
            [source, day_list[0], day_list[-1], *extra],
        )
        out[platform] = {str(r["date"]): float(r["v"]) for r in rows}
    return InstallsSeries(
        day_list,
        [out["ios"].get(d, 0.0) for d in day_list],
        [out["android"].get(d, 0.0) for d in day_list],
    )


def money(
    conn: sqlite3.Connection,
    end: date,
    days: int,
    app_ids: Collection[int] | None = None,
    *,
    metric: str = "proceeds",
) -> dict[str, float]:
    """Money per currency for the range: net ``proceeds`` (Apple + Play earnings), or
    ``sales_gross`` (Play's provisional sales)."""
    start = (end - timedelta(days=days - 1)).isoformat()
    sources = (
        (apple_sales.SOURCE, play_earnings.SOURCE) if metric == "proceeds" else (play_sales.SOURCE,)
    )
    clause, extra = _in(app_ids)
    totals: dict[str, float] = {}
    for source in sources:
        rows = conn.execute(
            "SELECT currency, SUM(value) AS v FROM daily_metrics WHERE source = ? AND metric = ? "  # noqa: S608
            f"AND date BETWEEN ? AND ?{clause} GROUP BY currency",
            [source, metric, start, end.isoformat(), *extra],
        )
        for r in rows:
            if r["currency"]:
                totals[str(r["currency"])] = totals.get(str(r["currency"]), 0.0) + float(r["v"])
    return dict(sorted(totals.items()))


def top_countries(
    conn: sqlite3.Connection,
    end: date,
    days: int,
    app_ids: Collection[int] | None = None,
    limit: int = 10,
) -> list[tuple[str, float, float]]:
    """(country, iOS installs, Android installs), most installs first."""
    start = (end - timedelta(days=days - 1)).isoformat()
    clause, extra = _in(app_ids)
    counts: dict[str, list[float]] = {}
    for index, source in enumerate((apple_sales.SOURCE, play_installs.SOURCE)):
        rows = conn.execute(
            "SELECT country, SUM(value) AS v FROM daily_metrics WHERE source = ? "  # noqa: S608
            f"AND metric = 'installs' AND country != 'ALL' AND date BETWEEN ? AND ?{clause} "
            "GROUP BY country",
            [source, start, end.isoformat(), *extra],
        )
        for r in rows:
            counts.setdefault(str(r["country"]), [0.0, 0.0])[index] += float(r["v"])
    ranked = sorted(counts.items(), key=lambda kv: -(kv[1][0] + kv[1][1]))
    return [(c, v[0], v[1]) for c, v in ranked[:limit]]


VITALS_METRICS = ("crash_rate", "crash_rate_28d", "anr_rate", "anr_rate_28d", "vitals_users")


def latest_vitals(conn: sqlite3.Connection, app_id: int) -> dict[str, tuple[float, str]]:
    """Each vitals metric's latest value and its date; a metric Google withheld is absent
    (shown as "—", never 0)."""
    out: dict[str, tuple[float, str]] = {}
    for metric in VITALS_METRICS:
        row = conn.execute(
            "SELECT value, date FROM daily_metrics WHERE source = 'play_vitals' AND metric = ? "
            "AND app_id = ? ORDER BY date DESC LIMIT 1",
            (metric, app_id),
        ).fetchone()
        if row is not None:
            out[metric] = (float(row["value"]), str(row["date"]))
    return out


def last_errors(conn: sqlite3.Connection, limit: int = 20) -> list[tuple[str, str, str, str]]:
    """Recent collection errors: (source, report_date, finished_at, note). Notes are
    already redacted when logged."""
    rows = conn.execute(
        "SELECT source, report_date, finished_at, error FROM ingest_log WHERE status = 'error' "
        "ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    return [
        (
            str(r["source"]),
            str(r["report_date"]),
            str(r["finished_at"] or ""),
            str(r["error"] or ""),
        )
        for r in rows
    ]
