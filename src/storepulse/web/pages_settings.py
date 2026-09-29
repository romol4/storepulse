"""Settings: credentials, email, preferences, apps (rename/hide/pair) and the account.

The web counterpart of `storepulse init` (docs/SPEC.md, Credentials and setup): each
credential is checked live, with the same core checks the CLI uses, before it's saved.
A saved secret is never shown again — only its fingerprint and the date it was saved —
and replacing or removing one asks for the admin password again.
"""

from __future__ import annotations

import dataclasses
import re
import sqlite3
from datetime import date
from typing import Annotated

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, Response

from storepulse.core import checks, config, db, discovery, mailer, runner
from storepulse.core.secrets import (
    APPLE_P8_SECRET,
    GOOGLE_SA_SECRET,
    SMTP_PASSWORD_SECRET,
    DbEncryptedStore,
    register_secret,
)
from storepulse.core.sources import apple_sales
from storepulse.core.sources.apple_client import AppleClient
from storepulse.core.sources.google_auth import GoogleCredentialError, load_service_account
from storepulse.core.sources.google_client import GoogleClient, parse_bucket_uri
from storepulse.web import security
from storepulse.web.common import (
    BASE_URL_KEY,
    TIMEZONE_KEY,
    Hosted,
    Viewer,
    current_viewer,
    hosted,
    render,
    require_csrf,
    start_session,
)
from storepulse.web.scheduler import DEFAULT_TIMEZONE, zone

router = APIRouter()
FormStr = Annotated[str, Form()]
MAX_UPLOAD_BYTES = 64 * 1024
TOTP_PENDING_KEY = "hosted.totp_pending"  # a settings key; the value is sealed
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

SAVED = {
    "apple": "Apple settings saved.",
    "apple-removed": "Apple removed.",
    "google": "Google Play settings saved.",
    "google-pending": (
        "Google Play saved. The key works, but Play Console hasn't granted access yet — new "
        "service accounts can take 24-48 hours. Use Test connection to check later."
    ),
    "google-removed": "Google Play removed.",
    "email": "Email saved; a test email was sent.",
    "email-removed": "Email removed.",
    "preferences": "Preferences saved.",
    "apps": "Apps updated.",
    "password": "Password changed; other sessions were signed out.",
    "totp-on": "Two-factor authentication is on.",
    "totp-off": "Two-factor authentication is off.",
}


class FormProblem(Exception):
    """A user-facing validation error, shown at the top of the settings section."""

    def __init__(self, section: str, message: str) -> None:
        super().__init__(message)
        self.section = section
        self.message = message


def _today(state: Hosted) -> date:
    return state.deps.today() if state.deps.today else apple_sales.pacific_today()


def _credential_info(store: DbEncryptedStore, name: str) -> dict[str, str] | None:
    fingerprint = store.fingerprint(name)
    if fingerprint is None:
        return None
    return {"fingerprint": fingerprint, "saved": (store.updated_at(name) or "")[:10]}


def _page(
    request: Request,
    conn: sqlite3.Connection,
    viewer: Viewer,
    *,
    saved: str | None = None,
    problem: FormProblem | None = None,
    checks_shown: list[checks.Check] | None = None,
    totp_setup: dict[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    state = hosted(request)
    store = state.store(conn)
    cfg = config.load_hosted(conn)
    settings = db.get_settings(conn)
    apps = db.list_apps(conn)
    return render(
        request,
        "settings.html",
        {
            "csrf": viewer.session.csrf_token,
            "user": viewer.user,
            "cfg": cfg,
            "credentials": {
                "apple": _credential_info(store, APPLE_P8_SECRET),
                "google": _credential_info(store, GOOGLE_SA_SECRET),
                "email": _credential_info(store, SMTP_PASSWORD_SECRET),
            },
            "timezone": settings.get(TIMEZONE_KEY) or DEFAULT_TIMEZONE,
            "base_url": settings.get(BASE_URL_KEY) or "",
            "rates_text": "\n".join(f"{c} = {r}" for c, r in sorted(cfg.digest.rates.items())),
            "ios_apps": [a for a in apps if a.platform == "ios"],
            "android_apps": [a for a in apps if a.platform == "android"],
            "apps": apps,
            "partner": {a.id: _partner(apps, a) for a in apps},
            "totp_enabled": viewer.user.totp_secret_enc is not None,
            "totp_setup": totp_setup,
            "saved": SAVED.get(saved or ""),
            "welcome": request.query_params.get("welcome") == "1",
            "problem": problem,
            "checks_shown": checks_shown,
        },
        status_code=status_code,
    )


def _partner(apps: list[db.AppInfo], app: db.AppInfo) -> db.AppInfo | None:
    if not app.pair_key:
        return None
    return next((a for a in apps if a.pair_key == app.pair_key and a.id != app.id), None)


def _reauth(viewer: Viewer, password: str, section: str) -> None:
    if not security.verify_password(viewer.user.password_hash, password):
        raise FormProblem(section, "Enter your admin password to change a saved credential.")


async def _upload_text(upload: UploadFile | None, section: str, what: str) -> str | None:
    if upload is None or not upload.filename:
        return None
    data = await upload.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise FormProblem(section, f"That {what} is too large to be a key file.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise FormProblem(section, f"That {what} isn't a text file.") from None
    register_secret(text)
    return text


def _handle(
    request: Request, viewer: Viewer, conn: sqlite3.Connection, exc: FormProblem
) -> Response:
    return _page(request, conn, viewer, problem=exc, status_code=400)


@router.get("/settings")
def settings_page(request: Request, saved: str | None = None) -> Response:
    with hosted(request).database() as conn:
        viewer = current_viewer(request, conn)
        return _page(request, conn, viewer, saved=saved)


# -- Apple -------------------------------------------------------------------------------------


@router.post("/settings/apple")
async def save_apple(
    request: Request,
    csrf: FormStr = "",
    issuer_id: FormStr = "",
    key_id: FormStr = "",
    vendor_number: FormStr = "",
    admin_password: FormStr = "",
    p8: Annotated[UploadFile | None, File()] = None,
) -> Response:
    state = hosted(request)
    uploaded = await _upload_text(p8, "apple", ".p8 file") if p8 else None
    with state.database() as conn:
        viewer = current_viewer(request, conn)
        require_csrf(viewer, csrf)
        store = state.store(conn)
        try:
            apple = config.AppleConfig(issuer_id.strip(), key_id.strip(), vendor_number.strip())
            if not all(dataclasses.astuple(apple)):
                raise FormProblem("apple", "Issuer ID, key ID and vendor number are all required.")
            existing = store.get(APPLE_P8_SECRET)
            if uploaded is not None and existing is not None:
                _reauth(viewer, admin_password, "apple")
            p8_text = uploaded or existing
            if p8_text is None:
                raise FormProblem("apple", "Upload the .p8 key file.")
            try:
                client = AppleClient(
                    apple.issuer_id,
                    apple.key_id,
                    p8_text,
                    transport=state.deps.transport,
                    sleep=state.deps.sleep,
                )
            except ValueError as exc:
                raise FormProblem("apple", str(exc)) from None
            with client:
                result = checks.check_apple(client, apple.vendor_number, _today(state))
            if result.failed:
                return _page(
                    request,
                    conn,
                    viewer,
                    problem=FormProblem("apple", result.failed[0].text),
                    checks_shown=result.checks,
                    status_code=400,
                )
        except FormProblem as exc:
            return _handle(request, viewer, conn, exc)
        store.set(APPLE_P8_SECRET, p8_text)
        cfg = config.load_hosted(conn)
        cfg.apple = apple
        config.save_hosted(conn, cfg)
        discovery.register_apple_apps(conn, result.apps)
        discovery.pair_apps(conn)
    return RedirectResponse("/settings?saved=apple#apple", status_code=303)


# -- Google Play ----------------------------------------------------------------------------------


@router.post("/settings/google")
async def save_google(
    request: Request,
    csrf: FormStr = "",
    bucket_uri: FormStr = "",
    admin_password: FormStr = "",
    service_account: Annotated[UploadFile | None, File()] = None,
) -> Response:
    state = hosted(request)
    uploaded = (
        await _upload_text(service_account, "google", "service account file")
        if service_account
        else None
    )
    with state.database() as conn:
        viewer = current_viewer(request, conn)
        require_csrf(viewer, csrf)
        store = state.store(conn)
        try:
            existing = store.get(GOOGLE_SA_SECRET)
            if uploaded is not None and existing is not None:
                _reauth(viewer, admin_password, "google")
            raw = uploaded or existing
            if raw is None:
                raise FormProblem("google", "Upload the service account JSON key.")
            try:
                sa = load_service_account(raw)
                bucket = parse_bucket_uri(bucket_uri)
            except (GoogleCredentialError, ValueError) as exc:
                raise FormProblem("google", str(exc)) from None
            with GoogleClient(sa, transport=state.deps.transport, sleep=state.deps.sleep) as client:
                result = checks.check_google(client, bucket)
            failed = result.failed
            if failed and not all(c.pending_permission for c in failed):
                return _page(
                    request,
                    conn,
                    viewer,
                    problem=FormProblem("google", failed[0].text),
                    checks_shown=result.checks,
                    status_code=400,
                )
        except FormProblem as exc:
            return _handle(request, viewer, conn, exc)
        store.set(GOOGLE_SA_SECRET, raw)
        cfg = config.load_hosted(conn)
        cfg.google = config.GoogleConfig(bucket_uri.strip(), sa.client_email)
        config.save_hosted(conn, cfg)
        discovery.register_play_apps(conn, result.apps)
        discovery.pair_apps(conn)
    saved = "google-pending" if failed else "google"
    return RedirectResponse(f"/settings?saved={saved}#google", status_code=303)


# -- email ----------------------------------------------------------------------------------------


@router.post("/settings/email")
def save_email(
    request: Request,
    csrf: FormStr = "",
    host: FormStr = "",
    port: FormStr = "587",
    security_mode: FormStr = "starttls",
    username: FormStr = "",
    password: FormStr = "",
    from_addr: FormStr = "",
    to_addrs: FormStr = "",
    admin_password: FormStr = "",
) -> Response:
    state = hosted(request)
    with state.database() as conn:
        viewer = current_viewer(request, conn)
        require_csrf(viewer, csrf)
        store = state.store(conn)
        try:
            existing = store.get(SMTP_PASSWORD_SECRET)
            if password and existing is not None:
                _reauth(viewer, admin_password, "email")
            smtp_password = password or existing
            if not smtp_password:
                raise FormProblem("email", "Enter the SMTP password (an app password for Gmail).")
            if not port.isdigit() or not 0 < int(port) < 65536:
                raise FormProblem("email", "The port must be a number, usually 587 or 465.")
            if security_mode not in config.EMAIL_SECURITY_MODES:
                raise FormProblem("email", "Choose starttls or ssl.")
            recipients = [a.strip() for a in to_addrs.replace("\n", ",").split(",") if a.strip()]
            if not host.strip() or not username.strip() or not recipients:
                raise FormProblem(
                    "email", "Host, username and at least one To address are required."
                )
            email_cfg = config.EmailConfig(
                host=host.strip(),
                port=int(port),
                security=security_mode,
                username=username.strip(),
                from_addr=(from_addr.strip() or username.strip()),
                to_addrs=recipients,
            )
            try:
                mailer.send(
                    email_cfg,
                    smtp_password,
                    mailer.test_message(email_cfg),
                    factory=state.deps.smtp_factory,
                )
            except mailer.MailerError as exc:
                raise FormProblem("email", str(exc)) from None
        except FormProblem as exc:
            return _handle(request, viewer, conn, exc)
        store.set(SMTP_PASSWORD_SECRET, smtp_password)
        cfg = config.load_hosted(conn)
        cfg.email = email_cfg
        config.save_hosted(conn, cfg)
    return RedirectResponse("/settings?saved=email#email", status_code=303)


# -- removing a credential ------------------------------------------------------------------------

_REMOVABLE = {
    "apple": (APPLE_P8_SECRET, "apple"),
    "google": (GOOGLE_SA_SECRET, "google"),
    "email": (SMTP_PASSWORD_SECRET, "email"),
}


@router.post("/settings/{section}/remove")
def remove_credential(
    request: Request, section: str, csrf: FormStr = "", admin_password: FormStr = ""
) -> Response:
    if section not in _REMOVABLE:
        return RedirectResponse("/settings", status_code=303)
    secret_name, attr = _REMOVABLE[section]
    state = hosted(request)
    with state.database() as conn:
        viewer = current_viewer(request, conn)
        require_csrf(viewer, csrf)
        try:
            _reauth(viewer, admin_password, section)
        except FormProblem as exc:
            return _handle(request, viewer, conn, exc)
        state.store(conn).delete(secret_name)
        cfg = config.load_hosted(conn)
        setattr(cfg, attr, None)
        config.save_hosted(conn, cfg)
    return RedirectResponse(f"/settings?saved={section}-removed#{section}", status_code=303)


@router.post("/settings/{section}/test")
def test_credential(request: Request, section: str, csrf: FormStr = "") -> Response:
    """Test connection with the saved credential (never re-displaying it)."""
    state = hosted(request)
    with state.database() as conn:
        viewer = current_viewer(request, conn)
        require_csrf(viewer, csrf)
        cfg = config.load_hosted(conn)
        store = state.store(conn)
        shown: list[checks.Check] = []
        clients = runner.clients_from_store(
            store, cfg, transport=state.deps.transport, sleep=state.deps.sleep
        )
        try:
            prefix = {"apple": "apple", "google": "google"}.get(section)
            shown += [
                checks.Check("fail", e) for e in clients.errors if prefix and e.startswith(prefix)
            ]
            if section == "apple" and clients.apple is not None and cfg.apple is not None:
                shown += checks.check_apple(
                    clients.apple, cfg.apple.vendor_number, _today(state)
                ).checks
            elif section == "google" and clients.google is not None and cfg.google is not None:
                result = checks.check_google(
                    clients.google, parse_bucket_uri(cfg.google.bucket_uri)
                )
                shown += result.checks
            elif section == "email" and cfg.email is not None and clients.smtp_password:
                try:
                    mailer.check(cfg.email, clients.smtp_password, factory=state.deps.smtp_factory)
                    shown.append(checks.Check("ok", f"Logged in to {cfg.email.host}."))
                except mailer.MailerError as exc:
                    shown.append(checks.Check("fail", str(exc)))
        finally:
            clients.close()
        if not shown:
            shown.append(checks.Check("warn", "Nothing saved to test yet."))
        return _page(request, conn, viewer, checks_shown=shown)


# -- preferences ----------------------------------------------------------------------------------


def _parse_rates(text: str) -> dict[str, float]:
    rates: dict[str, float] = {}
    for line in text.replace(",", "\n").splitlines():
        if not line.strip():
            continue
        currency, sep, value = line.partition("=")
        code = currency.strip().upper()
        if not sep or not re.fullmatch(r"[A-Z]{3}", code):
            raise FormProblem(
                "preferences",
                f"Write each rate as CODE = rate, e.g. CAD = 0.73 (got {line.strip()!r}).",
            )
        try:
            rates[code] = float(value)
        except ValueError:
            raise FormProblem("preferences", f"{value.strip()!r} isn't a number.") from None
    return rates


@router.post("/settings/preferences")
def save_preferences(
    request: Request,
    csrf: FormStr = "",
    run_time: FormStr = "10:30",
    timezone: FormStr = DEFAULT_TIMEZONE,
    days: FormStr = "7",
    stale_days: FormStr = "4",
    crash_threshold: FormStr = "1.09",
    anr_threshold: FormStr = "0.47",
    display_currency: FormStr = "",
    rates: FormStr = "",
    base_url: FormStr = "",
) -> Response:
    state = hosted(request)
    with state.database() as conn:
        viewer = current_viewer(request, conn)
        require_csrf(viewer, csrf)
        cfg = config.load_hosted(conn)
        try:
            if not _TIME_RE.match(run_time.strip()):
                raise FormProblem("preferences", "Run time must be HH:MM (24-hour).")
            if zone(timezone.strip()).key != (timezone.strip() or DEFAULT_TIMEZONE):
                raise FormProblem(
                    "preferences", f"Unknown time zone {timezone!r}; use e.g. Europe/Kyiv."
                )
            try:
                days_n, stale_n = int(days), int(stale_days)
                crash, anr = float(crash_threshold) / 100, float(anr_threshold) / 100
            except ValueError:
                raise FormProblem("preferences", "Days and thresholds must be numbers.") from None
            if not 1 <= days_n <= 30 or not 1 <= stale_n <= 30:
                raise FormProblem("preferences", "Days must be between 1 and 30.")
            url = base_url.strip().rstrip("/")
            if url and not re.match(r"^https?://[^\s/]+", url):
                raise FormProblem("preferences", "The dashboard URL must start with https://.")
            parsed_rates = _parse_rates(rates)
            currency = display_currency.strip().upper()
            if currency and not re.fullmatch(r"[A-Z]{3}", currency):
                raise FormProblem("preferences", "Display currency is a 3-letter code, e.g. USD.")
        except FormProblem as exc:
            return _handle(request, viewer, conn, exc)
        cfg.schedule = config.ScheduleConfig(run_time=run_time.strip(), days=days_n)
        cfg.digest = config.DigestConfig(
            crash_threshold=crash,
            anr_threshold=anr,
            stale_days=stale_n,
            rates=parsed_rates,
            display_currency=currency,
        )
        config.save_hosted(conn, cfg)
        db.set_settings(
            conn,
            {TIMEZONE_KEY: timezone.strip() or DEFAULT_TIMEZONE, BASE_URL_KEY: url},
        )
    if state.scheduler is not None:
        state.scheduler.reschedule(run_time.strip(), timezone.strip())
    return RedirectResponse("/settings?saved=preferences#preferences", status_code=303)


# -- apps: rename, hide, pair ----------------------------------------------------------------------


@router.post("/settings/apps")
async def save_apps(request: Request) -> Response:
    form = await request.form()
    state = hosted(request)
    with state.database() as conn:
        viewer = current_viewer(request, conn)
        require_csrf(viewer, str(form.get("csrf", "")))
        with db.transaction(conn):
            for app in db.list_apps(conn):
                name = str(form.get(f"name_{app.id}", "")).strip()
                hidden = form.get(f"hidden_{app.id}") == "on"
                db.set_app_display(
                    conn,
                    app.id,
                    display_name=name if name and name != app.name else None,
                    hidden=hidden,
                )
    return RedirectResponse("/settings?saved=apps#apps", status_code=303)


@router.post("/settings/apps/pair")
def pair(
    request: Request, csrf: FormStr = "", ios_id: FormStr = "", android_id: FormStr = ""
) -> Response:
    state = hosted(request)
    with state.database() as conn:
        viewer = current_viewer(request, conn)
        require_csrf(viewer, csrf)
        try:
            discovery.pair_manually(conn, int(ios_id), int(android_id))
        except ValueError:
            return _handle(
                request,
                viewer,
                conn,
                FormProblem("apps", "Choose one iOS app and one Android app."),
            )
    return RedirectResponse("/settings?saved=apps#apps", status_code=303)


@router.post("/settings/apps/unpair")
def unpair(request: Request, csrf: FormStr = "", app_id: FormStr = "") -> Response:
    state = hosted(request)
    with state.database() as conn:
        viewer = current_viewer(request, conn)
        require_csrf(viewer, csrf)
        try:
            discovery.unpair(conn, int(app_id))
        except ValueError:
            return _handle(request, viewer, conn, FormProblem("apps", "That app no longer exists."))
    return RedirectResponse("/settings?saved=apps#apps", status_code=303)


# -- account: password and TOTP -------------------------------------------------------------------


@router.post("/settings/account/password")
def change_password(
    request: Request,
    csrf: FormStr = "",
    current: FormStr = "",
    new: FormStr = "",
    confirm: FormStr = "",
) -> Response:
    state = hosted(request)
    with state.database() as conn:
        viewer = current_viewer(request, conn)
        require_csrf(viewer, csrf)
        try:
            if not security.verify_password(viewer.user.password_hash, current):
                raise FormProblem("account", "Your current password isn't right.")
            if problem := security.password_problem(new, confirm):
                raise FormProblem("account", problem)
        except FormProblem as exc:
            return _handle(request, viewer, conn, exc)
        db.set_user_password_hash(conn, viewer.user.id, security.hash_password(new))
        db.delete_user_sessions(conn, viewer.user.id)  # sign out everywhere else
        response = RedirectResponse("/settings?saved=password#account", status_code=303)
        start_session(request, response, conn, viewer.user.id, totp_pending=False)
        return response


def _pending_label(user_id: int) -> str:
    return f"users/{user_id}/totp-pending"


@router.post("/settings/account/totp/start")
def totp_start(request: Request, csrf: FormStr = "", current: FormStr = "") -> Response:
    state = hosted(request)
    with state.database() as conn:
        viewer = current_viewer(request, conn)
        require_csrf(viewer, csrf)
        if not security.verify_password(viewer.user.password_hash, current):
            return _handle(
                request, viewer, conn, FormProblem("account", "Your password isn't right.")
            )
        secret = security.new_totp_secret()
        store = state.store(conn)
        sealed = store.seal(_pending_label(viewer.user.id), secret)
        db.set_settings(conn, {TOTP_PENDING_KEY: sealed.hex()})
        # Shown once, only to the logged-in admin, to add to an authenticator app.
        return _page(
            request,
            conn,
            viewer,
            totp_setup={"secret": secret, "uri": security.totp_uri(secret, viewer.user.email)},
        )


@router.post("/settings/account/totp/confirm")
def totp_confirm(request: Request, csrf: FormStr = "", code: FormStr = "") -> Response:
    state = hosted(request)
    with state.database() as conn:
        viewer = current_viewer(request, conn)
        require_csrf(viewer, csrf)
        pending = db.get_settings(conn).get(TOTP_PENDING_KEY)
        store = state.store(conn)
        if not pending:
            return _handle(
                request, viewer, conn, FormProblem("account", "Start two-factor setup again.")
            )
        secret = store.unseal(_pending_label(viewer.user.id), bytes.fromhex(str(pending)))
        step = security.totp_step(secret, code)
        if step is None or not db.claim_totp_step(conn, viewer.user.id, step):
            return _page(
                request,
                conn,
                viewer,
                problem=FormProblem("account", "That code didn't match; try the next one."),
                totp_setup={"secret": secret, "uri": security.totp_uri(secret, viewer.user.email)},
                status_code=400,
            )
        db.set_user_totp(conn, viewer.user.id, store.seal(f"users/{viewer.user.id}/totp", secret))
        db.set_settings(conn, {}, remove=[TOTP_PENDING_KEY])
    return RedirectResponse("/settings?saved=totp-on#account", status_code=303)


@router.post("/settings/account/totp/disable")
def totp_disable(request: Request, csrf: FormStr = "", current: FormStr = "") -> Response:
    state = hosted(request)
    with state.database() as conn:
        viewer = current_viewer(request, conn)
        require_csrf(viewer, csrf)
        if not security.verify_password(viewer.user.password_hash, current):
            return _handle(
                request, viewer, conn, FormProblem("account", "Your password isn't right.")
            )
        db.set_user_totp(conn, viewer.user.id, None)
    return RedirectResponse("/settings?saved=totp-off#account", status_code=303)
