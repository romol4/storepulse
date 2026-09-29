"""The hosted web app (docs/SPEC.md, Web dashboard; Credentials and setup).

FastAPI with server-rendered Jinja pages. Every page but /healthz, /setup and /login needs
a logged-in session; every POST carries a CSRF token. Stored secrets are never rendered:
pages show a fingerprint and the date saved, nothing more.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from importlib import resources
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from storepulse import __version__
from storepulse.core import config, db, digest
from storepulse.core.secrets import read_master_key
from storepulse.web import pages_auth, pages_dashboard, pages_settings
from storepulse.web.common import (
    TIMEZONE_KEY,
    CsrfFailed,
    Hosted,
    LoginRequired,
    SetupRequired,
    issue_setup_token_if_needed,
)
from storepulse.web.jobs import JobDeps, Jobs
from storepulse.web.scheduler import DEFAULT_TIMEZONE, Scheduler

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'; "
    "object-src 'none'"
)


# -- security headers --------------------------------------------------------------------------


class SecurityHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        if not request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response


# -- factory --------------------------------------------------------------------------------------


def create_app(
    deps: JobDeps | None = None, *, start_scheduler: bool = True, log_stream: Any = None
) -> FastAPI:
    """Build the app. The master key is read (and checked against the database) here, so
    a missing or wrong key stops the server at startup instead of on the first page."""
    deps = deps or JobDeps()
    if deps.master_key is None:
        deps.master_key = read_master_key()
    jobs = Jobs(deps)
    scheduler = Scheduler(jobs) if start_scheduler else None
    state = Hosted(deps=deps, jobs=jobs, scheduler=scheduler)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        with state.database() as conn:
            state.store(conn)  # fails fast on a wrong master key
            issue_setup_token_if_needed(conn, log_stream)
            cfg = config.load_hosted(conn)
            timezone = str(db.get_settings(conn).get(TIMEZONE_KEY) or DEFAULT_TIMEZONE)
        if scheduler is not None:
            scheduler.start(cfg.schedule.run_time, timezone)
        yield
        if scheduler is not None:
            scheduler.shutdown()
        jobs.shutdown()

    app = FastAPI(
        title="Storepulse", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    package = Path(str(resources.files("storepulse.web")))
    state.templates = Jinja2Templates(directory=str(package / "templates"))
    state.templates.env.filters["money"] = lambda amount, currency: digest.format_money(
        currency, amount
    )
    state.templates.env.filters["count"] = lambda n: f"{n:,.0f}"
    app.state.hosted = state
    app.add_middleware(SecurityHeaders)
    app.mount("/static", StaticFiles(directory=str(package / "static")), name="static")

    @app.exception_handler(LoginRequired)
    async def _login(request: Request, exc: LoginRequired) -> Response:
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(CsrfFailed)
    async def _csrf(request: Request, exc: CsrfFailed) -> Response:
        return PlainTextResponse(
            "This form expired or didn't come from this site. Go back, reload and try again.",
            status_code=403,
        )

    @app.exception_handler(SetupRequired)
    async def _setup(request: Request, exc: SetupRequired) -> Response:
        return RedirectResponse("/setup", status_code=303)

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok", "version": __version__})

    app.include_router(pages_auth.router)
    app.include_router(pages_dashboard.router)
    app.include_router(pages_settings.router)
    return app
