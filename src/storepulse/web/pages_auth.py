"""First-run setup (admin account), login with optional TOTP, logout."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response

from storepulse.core import db
from storepulse.web import security
from storepulse.web.common import (
    SETUP_TOKEN_KEY,
    anon_csrf,
    check_anon_csrf,
    client_ip,
    current_viewer,
    end_session,
    hosted,
    render,
    require_csrf,
    set_anon_csrf,
    start_session,
)

router = APIRouter()
FormStr = Annotated[str, Form()]


def _anon_page(
    request: Request, template: str, context: Mapping[str, object], status: int = 200
) -> Response:
    token = anon_csrf(request)
    response = render(request, template, {**context, "csrf": token}, status)
    set_anon_csrf(request, response, token)
    return response


def _throttled(
    request: Request, template: str, keys: list[str], context: Mapping[str, object]
) -> Response | None:
    wait = hosted(request).throttle.retry_after(*keys)
    if not wait:
        return None
    response = _anon_page(
        request,
        template,
        {**context, "error": f"Too many attempts. Try again in {wait // 60 + 1} minutes."},
        429,
    )
    response.headers["Retry-After"] = str(wait)
    return response


# -- setup: create the admin (only while none exists) -------------------------------------------


@router.get("/setup")
def setup_page(request: Request) -> Response:
    with hosted(request).database() as conn:
        if db.user_count(conn) > 0:
            return RedirectResponse("/login", status_code=303)
    return _anon_page(request, "setup.html", {})


@router.post("/setup")
def setup_submit(
    request: Request,
    csrf: FormStr = "",
    token: FormStr = "",
    email: FormStr = "",
    password: FormStr = "",
    confirm: FormStr = "",
) -> Response:
    state = hosted(request)
    keys = [f"ip:{client_ip(request)}", "setup"]
    form = {"email": email}
    if blocked := _throttled(request, "setup.html", keys, form):
        return blocked
    if not check_anon_csrf(request, csrf):
        return _anon_page(
            request, "setup.html", {**form, "error": "The form expired; try again."}, 400
        )
    with state.database() as conn:
        if db.user_count(conn) > 0:
            return RedirectResponse("/login", status_code=303)
        stored = str(db.get_settings(conn).get(SETUP_TOKEN_KEY) or "")
        if not stored or not security.same(stored, security.token_hash(token.strip())):
            state.throttle.failed(*keys)
            return _anon_page(
                request,
                "setup.html",
                {**form, "error": "That setup token isn't right. Copy it from the container log."},
                403,
            )
        email = email.strip().casefold()
        if "@" not in email:
            return _anon_page(
                request, "setup.html", {**form, "error": "Enter an email address."}, 400
            )
        if problem := security.password_problem(password, confirm):
            return _anon_page(request, "setup.html", {**form, "error": problem}, 400)
        user_id = db.create_user(conn, email, security.hash_password(password), None)
        db.set_settings(conn, {}, remove=[SETUP_TOKEN_KEY])  # one-time: gone once used
        state.throttle.succeeded(*keys)
        response = RedirectResponse("/settings?welcome=1", status_code=303)
        start_session(request, response, conn, user_id, totp_pending=False)
        return response


# -- login / TOTP / logout -----------------------------------------------------------------------


@router.get("/login")
def login_page(request: Request) -> Response:
    with hosted(request).database() as conn:
        if db.user_count(conn) == 0:
            return RedirectResponse("/setup", status_code=303)
    return _anon_page(request, "login.html", {})


@router.post("/login")
def login_submit(
    request: Request, csrf: FormStr = "", email: FormStr = "", password: FormStr = ""
) -> Response:
    state = hosted(request)
    email = email.strip().casefold()
    keys = [f"ip:{client_ip(request)}", f"user:{email}"]
    form = {"email": email}
    if blocked := _throttled(request, "login.html", keys, form):
        return blocked
    if not check_anon_csrf(request, csrf):
        return _anon_page(
            request, "login.html", {**form, "error": "The form expired; try again."}, 400
        )
    with state.database() as conn:
        user = db.get_user_by_email(conn, email)
        # Verify against a dummy hash when the account doesn't exist, so timing doesn't
        # reveal which emails are real.
        valid = (
            security.verify_password(user.password_hash if user else _DUMMY_HASH, password)
            and user is not None
        )
        if not valid or user is None:
            state.throttle.failed(*keys)
            return _anon_page(
                request, "login.html", {**form, "error": "Wrong email or password."}, 401
            )
        state.throttle.succeeded(*keys)
        pending = user.totp_secret_enc is not None
        response = RedirectResponse("/login/totp" if pending else "/", status_code=303)
        start_session(request, response, conn, user.id, totp_pending=pending)
        return response


_DUMMY_HASH = security.hash_password("storepulse-dummy-password-for-timing")


@router.get("/login/totp")
def totp_page(request: Request) -> Response:
    with hosted(request).database() as conn:
        viewer = current_viewer(request, conn, allow_pending=True)
        if not viewer.session.totp_pending:
            return RedirectResponse("/", status_code=303)
    return render(request, "totp.html", {"csrf": viewer.session.csrf_token})


@router.post("/login/totp")
def totp_submit(request: Request, csrf: FormStr = "", code: FormStr = "") -> Response:
    state = hosted(request)
    with state.database() as conn:
        viewer = current_viewer(request, conn, allow_pending=True)
        require_csrf(viewer, csrf)
        keys = [f"ip:{client_ip(request)}", f"totp:{viewer.user.id}"]
        wait = state.throttle.retry_after(*keys)
        if wait:
            return render(
                request,
                "totp.html",
                {"csrf": viewer.session.csrf_token, "error": "Too many attempts; try later."},
                429,
            )
        secret_enc = viewer.user.totp_secret_enc
        store = state.store(conn)
        step = (
            security.totp_step(store.unseal(f"users/{viewer.user.id}/totp", secret_enc), code)
            if secret_enc is not None
            else None
        )
        # A code that already signed someone in (or an older one) is refused, like a wrong one.
        if step is None or not db.claim_totp_step(conn, viewer.user.id, step):
            state.throttle.failed(*keys)
            return render(
                request,
                "totp.html",
                {"csrf": viewer.session.csrf_token, "error": "That code didn't match."},
                401,
            )
        state.throttle.succeeded(*keys)
        # A fresh session once both factors pass: the pending token is discarded.
        response = RedirectResponse("/", status_code=303)
        end_session(request, response, conn)
        start_session(request, response, conn, viewer.user.id, totp_pending=False)
        return response


@router.post("/logout")
def logout(request: Request, csrf: FormStr = "") -> Response:
    with hosted(request).database() as conn:
        viewer = current_viewer(request, conn, allow_pending=True)
        require_csrf(viewer, csrf)
        response = RedirectResponse("/login", status_code=303)
        end_session(request, response, conn)
        return response
