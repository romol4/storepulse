"""Overview, App and Sources pages (docs/SPEC.md, Web dashboard)."""

from __future__ import annotations

import json
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from storepulse.core import config, dashboard, db, digest
from storepulse.core.sources import apple_sales
from storepulse.web.common import current_viewer, hosted, render, require_csrf

router = APIRouter()
FormStr = Annotated[str, Form()]
FormInt = Annotated[int, Form()]


def _range(value: str | None) -> int:
    try:
        days = int(value or 30)
    except ValueError:
        days = 30
    return days if days in dashboard.RANGES else 30


def _today(request: Request) -> date:
    today = hosted(request).deps.today
    return today() if today else apple_sales.pacific_today()


def _chart_json(series: dashboard.InstallsSeries) -> str:
    """Chart data for static/dashboard.js; embedded as JSON, never executed (CSP)."""
    data = json.dumps({"days": series.days, "ios": series.ios, "android": series.android})
    return data.replace("<", "\\u003c")  # can never close its <script> element


def _pct(value: tuple[float, str] | None) -> str:
    return "—" if value is None else f"{value[0] * 100:.2f}%"


@router.get("/")
def overview(request: Request, range: str | None = None) -> Response:
    days = _range(range)
    with hosted(request).database() as conn:
        viewer = current_viewer(request, conn)
        cfg = config.load_hosted(conn)
        as_of = digest.compute_as_of(conn, cfg)
        groups = dashboard.app_groups(conn)
        context: dict[str, Any] = {
            "csrf": viewer.session.csrf_token,
            "user": viewer.user,
            "days": days,
            "ranges": dashboard.RANGES,
            "as_of": as_of,
            "configured": cfg.apple is not None or cfg.google is not None,
        }
        if as_of is not None:
            series = dashboard.installs_series(conn, as_of, days)
            rows = []
            for group in groups:
                s = dashboard.installs_series(conn, as_of, days, group.ids)
                vitals = next(
                    (
                        dashboard.latest_vitals(conn, a.id)
                        for a in group.apps
                        if a.platform == "android"
                    ),
                    {},
                )
                rows.append(
                    {
                        "group": group,
                        "installs": s.total,
                        "proceeds": dashboard.money(conn, as_of, days, group.ids),
                        "crash": _pct(vitals.get("crash_rate_28d")),
                        "anr": _pct(vitals.get("anr_rate_28d")),
                    }
                )
            rows.sort(key=lambda r: -float(str(r["installs"])))
            context |= {
                "series": series,
                "chart": _chart_json(series),
                "proceeds": dashboard.money(conn, as_of, days),
                "sales_gross": dashboard.money(conn, as_of, days, metric="sales_gross"),
                "rows": rows,
            }
    return render(request, "overview.html", context)


@router.get("/apps/{key}")
def app_page(request: Request, key: str, range: str | None = None) -> Response:
    days = _range(range)
    with hosted(request).database() as conn:
        viewer = current_viewer(request, conn)
        cfg = config.load_hosted(conn)
        group = dashboard.find_group(conn, key)
        if group is None:
            raise HTTPException(status_code=404)
        as_of = digest.compute_as_of(conn, cfg)
        platforms = []
        for app in group.apps:
            entry: dict[str, Any] = {"app": app}
            if as_of is not None:
                s = dashboard.installs_series(conn, as_of, days, [app.id])
                entry |= {
                    "installs": s.total,
                    "chart": _chart_json(s),
                    "proceeds": dashboard.money(conn, as_of, days, [app.id]),
                    "countries": dashboard.top_countries(conn, as_of, days, [app.id]),
                }
            if app.platform == "android":
                vitals = dashboard.latest_vitals(conn, app.id)
                entry["vitals"] = {m: _pct(vitals.get(m)) for m in dashboard.VITALS_METRICS[:4]}
                users = vitals.get("vitals_users")
                entry["vitals_users"] = f"{users[0]:.0f}" if users else "—"
            platforms.append(entry)
    return render(
        request,
        "app.html",
        {
            "csrf": viewer.session.csrf_token,
            "user": viewer.user,
            "group": group,
            "platforms": platforms,
            "days": days,
            "ranges": dashboard.RANGES,
            "as_of": as_of,
        },
    )


@router.get("/sources")
def sources_page(request: Request, started: str | None = None) -> Response:
    state = hosted(request)
    with state.database() as conn:
        viewer = current_viewer(request, conn)
        cfg = config.load_hosted(conn)
        statuses = digest.source_statuses(conn, cfg, _today(request))
        errors = dashboard.last_errors(conn)
    return render(
        request,
        "sources.html",
        {
            "csrf": viewer.session.csrf_token,
            "user": viewer.user,
            "statuses": statuses,
            "errors": errors,
            "job": state.jobs.status,
            "next_run": state.scheduler.next_run() if state.scheduler else None,
            "started": {"run": "Run started.", "backfill": "Backfill started."}.get(started or ""),
            "busy": started == "busy",
        },
    )


@router.post("/sources/run")
def run_now(request: Request, csrf: FormStr = "") -> Response:
    state = hosted(request)
    with state.database() as conn:
        require_csrf(current_viewer(request, conn), csrf)
    started = state.jobs.run("Run now")
    return RedirectResponse(f"/sources?started={'run' if started else 'busy'}", status_code=303)


@router.post("/sources/backfill")
def backfill(
    request: Request, csrf: FormStr = "", apple_days: FormInt = 90, play_months: FormInt = 3
) -> Response:
    state = hosted(request)
    with state.database() as conn:
        require_csrf(current_viewer(request, conn), csrf)
    started = state.jobs.backfill(max(0, min(apple_days, 365)), max(0, min(play_months, 24)))
    return RedirectResponse(
        f"/sources?started={'backfill' if started else 'busy'}", status_code=303
    )


@router.get("/api/status")
def status(request: Request) -> dict[str, Any]:
    """Job state for the Sources page's refresh; no data, no secrets."""
    state = hosted(request)
    with state.database() as conn:
        current_viewer(request, conn)
        count = len(db.list_apps(conn))
    job = state.jobs.status
    return {
        "running": job.running,
        "last": job.last_label,
        "finished": job.last_finished,
        "apps": count,
    }
