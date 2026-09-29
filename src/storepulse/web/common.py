"""Shared state and helpers for the hosted app's pages: sessions, CSRF, rendering."""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from storepulse import __version__
from storepulse.core import config, db
from storepulse.core.secrets import DbEncryptedStore
from storepulse.web import security
from storepulse.web.jobs import JobDeps, Jobs
from storepulse.web.scheduler import Scheduler

log = logging.getLogger(__name__)

SETUP_TOKEN_KEY = "hosted.setup_token_hash"  # noqa: S105 (a settings key, not a value)
TIMEZONE_KEY = "hosted.timezone"
BASE_URL_KEY = "hosted.base_url"


class LoginRequired(Exception):
    pass


class SetupRequired(Exception):
    pass


@dataclass
class Hosted:
    """Per-app state, reachable from every route as ``request.app.state.hosted``."""

    deps: JobDeps
    jobs: Jobs
    scheduler: Scheduler | None
    throttle: security.Throttle = field(default_factory=security.Throttle)
    templates: Jinja2Templates | None = None

    @contextlib.contextmanager
    def database(self) -> Iterator[sqlite3.Connection]:
        conn = db.connect(config.db_path())
        try:
            yield conn
        finally:
            conn.close()

    def store(self, conn: sqlite3.Connection) -> DbEncryptedStore:
        return DbEncryptedStore(conn, self.deps.master_key)


def hosted(request: Request) -> Hosted:
    state: Hosted = request.app.state.hosted
    return state


# -- sessions and CSRF ---------------------------------------------------------------------


@dataclass
class Viewer:
    user: db.User
    session: db.Session


def _now() -> str:
    return db.utc_now()


def current_viewer(
    request: Request, conn: sqlite3.Connection, *, allow_pending: bool = False
) -> Viewer:
    """The logged-in admin, or LoginRequired. ``allow_pending`` accepts a session still
    waiting for its TOTP code (only the TOTP page does)."""
    if db.user_count(conn) == 0:
        raise SetupRequired
    token = request.cookies.get(security.SESSION_COOKIE, "")
    if not token:
        raise LoginRequired
    session = db.get_session(conn, security.token_hash(token), _now())
    if session is None or (session.totp_pending and not allow_pending):
        raise LoginRequired
    user = db.get_user(conn, session.user_id)
    if user is None:
        raise LoginRequired
    return Viewer(user, session)


def start_session(
    request: Request,
    response: Response,
    conn: sqlite3.Connection,
    user_id: int,
    *,
    totp_pending: bool,
) -> None:
    token = security.new_token()
    expires = datetime.now(UTC) + timedelta(hours=security.SESSION_HOURS)
    db.create_session(
        conn,
        security.token_hash(token),
        user_id,
        security.new_token(),
        expires.replace(microsecond=0).isoformat(),
        totp_pending=totp_pending,
    )
    db.delete_expired_sessions(conn, _now())
    response.set_cookie(
        security.SESSION_COOKIE,
        token,
        max_age=security.SESSION_HOURS * 3600,
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
        path="/",
    )


def end_session(request: Request, response: Response, conn: sqlite3.Connection) -> None:
    token = request.cookies.get(security.SESSION_COOKIE, "")
    if token:
        db.delete_session(conn, security.token_hash(token))
    response.delete_cookie(security.SESSION_COOKIE, path="/")


def anon_csrf(request: Request) -> str:
    """The double-submit CSRF token for forms shown before login (setup, login)."""
    return request.cookies.get(security.ANON_CSRF_COOKIE) or security.new_token()


def set_anon_csrf(request: Request, response: Response, token: str) -> None:
    response.set_cookie(
        security.ANON_CSRF_COOKIE,
        token,
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
        path="/",
    )


def check_anon_csrf(request: Request, form_token: str) -> bool:
    cookie = request.cookies.get(security.ANON_CSRF_COOKIE, "")
    return bool(cookie) and security.same(cookie, form_token)


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def render(
    request: Request, template: str, context: dict[str, Any], status_code: int = 200
) -> HTMLResponse:
    templates = hosted(request).templates
    assert templates is not None
    return templates.TemplateResponse(
        request, template, {"version": __version__, **context}, status_code=status_code
    )


# -- setup token ------------------------------------------------------------------------------


def issue_setup_token_if_needed(conn: sqlite3.Connection, out: Any = None) -> str | None:
    """While no admin exists, a fresh one-time setup token is printed to the container log
    (docs/SPEC.md, Credentials and setup). Only its hash is stored."""
    if db.user_count(conn) > 0:
        db.set_settings(conn, {}, remove=[SETUP_TOKEN_KEY])
        return None
    token = security.new_token()
    db.set_settings(conn, {SETUP_TOKEN_KEY: security.token_hash(token)})
    stream = out or sys.stdout
    print(
        "\n"
        "  Storepulse is not set up yet. Open /setup in your browser and use this one-time\n"
        f"  setup token:  {token}\n"
        "  (A new token is printed each time the app starts until an admin exists.)\n",
        file=stream,
        flush=True,
    )
    return token


class CsrfFailed(Exception):
    pass


def require_csrf(viewer: Viewer, form_token: str) -> None:
    """Every logged-in POST form carries the session's CSRF token."""
    if not form_token or not security.same(viewer.session.csrf_token, form_token):
        raise CsrfFailed
