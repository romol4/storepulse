"""The hosted web app end to end: setup token, admin, login + TOTP, CSRF, throttling,
credentials (checked live against fakes, never displayed again), dashboard pages, Run
now, and a crawl proving no stored secret ever reaches a response (docs/SPEC.md, Phase 4).
No network: Apple, Google and SMTP are the same fakes the CLI tests use."""

from __future__ import annotations

import io
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path

import pyotp
import pytest
from fastapi.testclient import TestClient

from conftest import (
    BUCKET,
    FT_APPS,
    TODAY,
    FakeApple,
    FakeGoogle,
    fixture_bytes,
    router,
    standard_objects,
)
from storepulse.core import config, db
from storepulse.core.secrets import SecretStoreError
from storepulse.web.app import create_app
from storepulse.web.jobs import JobDeps

MASTER_KEY = "test-master-key-" + "x" * 32
ADMIN = "admin@example.com"
PASSWORD = "correct horse battery"
SMTP_PASSWORD = "smtp-app-password-123"


@dataclass
class FakeSMTP:
    sent: int = 0
    fail_login: bool = False
    sent_messages: list[EmailMessage] = field(default_factory=list)

    def starttls(self, *, context: object = None) -> tuple[int, bytes]:
        return (220, b"ok")

    def login(self, user: str, password: str) -> tuple[int, bytes]:
        if self.fail_login:
            import smtplib

            raise smtplib.SMTPAuthenticationError(535, b"bad credentials")
        return (235, b"ok")

    def send_message(
        self, msg: EmailMessage, from_addr: object, to_addrs: object
    ) -> dict[str, object]:
        self.sent += 1
        self.sent_messages.append(msg)
        return {}

    def noop(self) -> tuple[int, bytes]:
        return (250, b"ok")

    def quit(self) -> tuple[int, bytes]:
        return (221, b"bye")


@dataclass
class Site:
    client: TestClient
    log: io.StringIO
    apple: FakeApple
    google: FakeGoogle
    smtp: FakeSMTP

    @property
    def setup_token(self) -> str:
        match = re.findall(r"setup token:\s+(\S+)", self.log.getvalue())
        assert match, self.log.getvalue()
        return match[-1]

    def csrf(self, path: str) -> str:
        page = self.client.get(path)
        match = re.search(r'name="csrf" value="([^"]+)"', page.text)
        assert match, page.text[:500]
        return match.group(1)


@pytest.fixture
def site() -> Iterator[Site]:
    apple = FakeApple(apps=FT_APPS, default_report=fixture_bytes("summary_normal.tsv.gz"))
    google = FakeGoogle(objects=standard_objects())
    smtp = FakeSMTP()
    deps = JobDeps(
        transport=router(apple, google),
        sleep=lambda _: None,
        smtp_factory=lambda host, port, timeout: smtp,
        today=lambda: TODAY,
        master_key=MASTER_KEY,
    )
    log = io.StringIO()
    app = create_app(deps, start_scheduler=False, log_stream=log)
    with TestClient(app, follow_redirects=False) as client:
        yield Site(client, log, apple, google, smtp)


def _setup_admin(site: Site) -> None:
    csrf = site.csrf("/setup")
    response = site.client.post(
        "/setup",
        data={
            "csrf": csrf,
            "token": site.setup_token,
            "email": ADMIN,
            "password": PASSWORD,
            "confirm": PASSWORD,
        },
    )
    assert response.status_code == 303, response.text
    assert response.headers["location"] == "/settings?welcome=1"


def _login(site: Site) -> None:
    site.client.cookies.clear()
    csrf = site.csrf("/login")
    response = site.client.post("/login", data={"csrf": csrf, "email": ADMIN, "password": PASSWORD})
    assert response.status_code == 303, response.text


# -- health, headers, setup -------------------------------------------------------------------


def test_health_and_security_headers(site: Site) -> None:
    response = site.client.get("/healthz")
    assert response.json()["status"] == "ok"
    csp = response.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cache-control"] == "no-store"


def test_everything_redirects_to_setup_until_an_admin_exists(site: Site) -> None:
    for path in ("/", "/settings", "/sources", "/login"):
        assert site.client.get(path).headers["location"] == "/setup"


def test_setup_needs_the_token_from_the_log(site: Site) -> None:
    csrf = site.csrf("/setup")
    wrong = site.client.post(
        "/setup",
        data={
            "csrf": csrf,
            "token": "nope",
            "email": ADMIN,
            "password": PASSWORD,
            "confirm": PASSWORD,
        },
    )
    assert wrong.status_code == 403
    assert db.user_count(db.connect(config.db_path())) == 0
    _setup_admin(site)
    # One-time: the setup page is gone once an admin exists, and the token no longer works.
    assert site.client.get("/setup").headers["location"] == "/login"
    conn = db.connect(config.db_path())
    assert "hosted.setup_token_hash" not in db.get_settings(conn)


def test_setup_token_guessing_is_throttled(site: Site) -> None:
    csrf = site.csrf("/setup")
    form = {"csrf": csrf, "email": ADMIN, "password": PASSWORD, "confirm": PASSWORD}
    statuses = [
        site.client.post("/setup", data={**form, "token": f"guess{i}"}).status_code
        for i in range(6)
    ]
    assert statuses[:5] == [403] * 5
    assert statuses[5] == 429
    # Even the right token waits out the lockout.
    locked = site.client.post("/setup", data={**form, "token": site.setup_token})
    assert locked.status_code == 429 and "retry-after" in locked.headers


def test_setup_rejects_short_passwords_and_missing_csrf(site: Site) -> None:
    csrf = site.csrf("/setup")
    short = site.client.post(
        "/setup",
        data={
            "csrf": csrf,
            "token": site.setup_token,
            "email": ADMIN,
            "password": "short",
            "confirm": "short",
        },
    )
    assert short.status_code == 400 and "12 characters" in short.text
    no_csrf = site.client.post(
        "/setup",
        data={"token": site.setup_token, "email": ADMIN, "password": PASSWORD, "confirm": PASSWORD},
    )
    assert no_csrf.status_code == 400


def test_a_new_setup_token_each_start_until_an_admin_exists(tmp_path: Path) -> None:
    deps = JobDeps(master_key=MASTER_KEY, today=lambda: TODAY)
    tokens = []
    for _ in range(2):
        log = io.StringIO()
        with TestClient(create_app(deps, start_scheduler=False, log_stream=log)):
            tokens.append(re.findall(r"setup token:\s+(\S+)", log.getvalue())[-1])
    assert tokens[0] != tokens[1]


def test_wrong_master_key_stops_startup(site: Site) -> None:
    _setup_admin(site)
    deps = JobDeps(master_key="another-master-key-" + "y" * 32)
    app = create_app(deps, start_scheduler=False, log_stream=io.StringIO())
    with pytest.raises(SecretStoreError, match="doesn't match"), TestClient(app):
        pass


def test_missing_master_key_is_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(SecretStoreError, match="no master key"):
        create_app(JobDeps(), start_scheduler=False)


# -- login, TOTP, CSRF, throttling -----------------------------------------------------------


def test_login_logout(site: Site) -> None:
    _setup_admin(site)
    site.client.cookies.clear()
    assert site.client.get("/").headers["location"] == "/login"
    csrf = site.csrf("/login")
    bad = site.client.post(
        "/login", data={"csrf": csrf, "email": ADMIN, "password": "wrong password!"}
    )
    assert bad.status_code == 401 and "Wrong email or password" in bad.text
    _login(site)
    assert site.client.get("/").status_code == 200
    # Logout needs the session's CSRF token.
    assert site.client.post("/logout", data={}).status_code == 403
    assert site.client.post("/logout", data={"csrf": site.csrf("/")}).status_code == 303
    assert site.client.get("/").headers["location"] == "/login"


def test_login_email_is_case_insensitive(site: Site) -> None:
    # The one hosted admin account must never be lockable out by autocapitalize or a
    # differently-cased retype: setup and login normalize email the same way.
    csrf = site.csrf("/setup")
    response = site.client.post(
        "/setup",
        data={
            "csrf": csrf,
            "token": site.setup_token,
            "email": "Admin@Example.COM",
            "password": PASSWORD,
            "confirm": PASSWORD,
        },
    )
    assert response.status_code == 303, response.text
    site.client.cookies.clear()
    csrf = site.csrf("/login")
    response = site.client.post(
        "/login", data={"csrf": csrf, "email": "admin@example.com", "password": PASSWORD}
    )
    assert response.status_code == 303, response.text
    assert response.headers["location"] == "/"


def test_session_cookie_flags(site: Site) -> None:
    _setup_admin(site)
    csrf = site.csrf("/login")
    site.client.cookies.clear()
    csrf = site.csrf("/login")
    response = site.client.post("/login", data={"csrf": csrf, "email": ADMIN, "password": PASSWORD})
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie


def test_login_is_throttled(site: Site) -> None:
    _setup_admin(site)
    site.client.cookies.clear()
    csrf = site.csrf("/login")
    codes = [
        site.client.post(
            "/login", data={"csrf": csrf, "email": ADMIN, "password": f"nope{i}"}
        ).status_code
        for i in range(6)
    ]
    assert codes == [401] * 5 + [429]
    blocked = site.client.post("/login", data={"csrf": csrf, "email": ADMIN, "password": PASSWORD})
    assert blocked.status_code == 429


def test_every_logged_in_post_needs_csrf(site: Site) -> None:
    _setup_admin(site)
    for path in (
        "/sources/run",
        "/sources/backfill",
        "/settings/preferences",
        "/settings/apps/pair",
        "/settings/apps/unpair",
        "/settings/account/password",
        "/settings/account/totp/start",
        "/settings/apple/remove",
        "/settings/email",
    ):
        assert site.client.post(path, data={"csrf": "forged"}).status_code == 403, path


def test_totp_enrolment_and_login(site: Site) -> None:
    _setup_admin(site)
    csrf = site.csrf("/settings")
    page = site.client.post(
        "/settings/account/totp/start", data={"csrf": csrf, "current": PASSWORD}
    )
    secret = re.search(r'<code class="secret">([A-Z2-7]+)</code>', page.text).group(1)  # type: ignore[union-attr]
    wrong = site.client.post(
        "/settings/account/totp/confirm", data={"csrf": csrf, "code": "000000"}
    )
    assert wrong.status_code == 400
    ok = site.client.post(
        "/settings/account/totp/confirm", data={"csrf": csrf, "code": pyotp.TOTP(secret).now()}
    )
    assert ok.status_code == 303
    conn = db.connect(config.db_path())
    user = db.get_user_by_email(conn, ADMIN)
    assert user is not None and user.totp_secret_enc is not None
    assert secret.encode() not in user.totp_secret_enc  # stored sealed

    _login(site)
    # Password alone isn't a session: every page waits for the code.
    assert site.client.get("/").headers["location"] == "/login"
    csrf = site.csrf("/login/totp")
    bad = site.client.post("/login/totp", data={"csrf": csrf, "code": "123456"})
    assert bad.status_code == 401
    good = site.client.post("/login/totp", data={"csrf": csrf, "code": pyotp.TOTP(secret).now()})
    assert good.headers["location"] == "/"
    assert site.client.get("/").status_code == 200


def test_changing_the_password_signs_out_other_sessions(site: Site) -> None:
    _setup_admin(site)
    other = TestClient(site.client.app, follow_redirects=False)
    other.cookies.update(site.client.cookies)
    csrf = site.csrf("/settings")
    new = "a brand new passphrase"
    response = site.client.post(
        "/settings/account/password",
        data={"csrf": csrf, "current": PASSWORD, "new": new, "confirm": new},
    )
    assert response.status_code == 303
    assert site.client.get("/").status_code == 200  # this session was renewed
    assert other.get("/").headers["location"] == "/login"


# -- credentials -------------------------------------------------------------------------------


def _save_apple(site: Site, p8_pem: str, **extra: str) -> object:
    return site.client.post(
        "/settings/apple",
        data={
            "csrf": site.csrf("/settings"),
            "issuer_id": "ISSUER-1",
            "key_id": "KEYID12345",
            "vendor_number": "85000000",
            **extra,
        },
        files={"p8": ("AuthKey_KEYID12345.p8", p8_pem.encode(), "application/octet-stream")},
    )


def _save_google(site: Site, sa_json: str) -> object:
    return site.client.post(
        "/settings/google",
        data={"csrf": site.csrf("/settings"), "bucket_uri": f"gs://{BUCKET}/"},
        files={"service_account": ("sa.json", sa_json.encode(), "application/json")},
    )


def _save_email(site: Site, password: str = SMTP_PASSWORD, **extra: str) -> object:
    return site.client.post(
        "/settings/email",
        data={
            "csrf": site.csrf("/settings"),
            "host": "smtp.example.com",
            "port": "587",
            "security_mode": "starttls",
            "username": "me@example.com",
            "password": password,
            "from_addr": "me@example.com",
            "to_addrs": "me@example.com",
            **extra,
        },
    )


def test_apple_is_checked_live_then_saved_and_never_shown(site: Site, p8_pem: str) -> None:
    _setup_admin(site)
    response = _save_apple(site, p8_pem)
    assert response.status_code == 303, getattr(response, "text", "")  # type: ignore[attr-defined]
    page = site.client.get("/settings").text
    assert "fingerprint <code>hmac:" in page
    assert p8_pem.splitlines()[1] not in page
    conn = db.connect(config.db_path())
    assert config.load_hosted(conn).apple == config.AppleConfig(
        "ISSUER-1", "KEYID12345", "85000000"
    )
    assert {a.name for a in db.list_apps(conn)} == {"deskFT", "billFT"}


def test_apple_failure_saves_nothing(site: Site, p8_pem: str) -> None:
    _setup_admin(site)
    site.apple.apps_status = 401
    response = _save_apple(site, p8_pem)
    assert response.status_code == 400  # type: ignore[attr-defined]
    assert "401" in response.text  # type: ignore[attr-defined]
    conn = db.connect(config.db_path())
    assert db.secret_names(conn) == [] and config.load_hosted(conn).apple is None


def test_replacing_a_saved_key_needs_the_admin_password(site: Site, p8_pem: str) -> None:
    _setup_admin(site)
    _save_apple(site, p8_pem)
    again = _save_apple(site, p8_pem)
    assert again.status_code == 400 and "admin password" in again.text  # type: ignore[attr-defined]
    ok = _save_apple(site, p8_pem, admin_password=PASSWORD)
    assert ok.status_code == 303  # type: ignore[attr-defined]


def test_google_saved_and_apps_auto_paired(site: Site, p8_pem: str, sa_json: str) -> None:
    _setup_admin(site)
    _save_apple(site, p8_pem)
    response = _save_google(site, sa_json)
    assert response.headers["location"].startswith("/settings?saved=google")  # type: ignore[attr-defined]
    conn = db.connect(config.db_path())
    paired = [a for a in db.list_apps(conn) if a.pair_key]
    assert sorted((a.platform, a.name) for a in paired) == [
        ("android", "billFT"),
        ("ios", "billFT"),
    ]


def test_google_pending_permission_can_still_be_saved(site: Site, sa_json: str) -> None:
    _setup_admin(site)
    site.google.gcs_status = 403
    response = _save_google(site, sa_json)
    assert response.headers["location"].startswith("/settings?saved=google-pending")  # type: ignore[attr-defined]


def test_email_sends_a_test_before_saving(site: Site) -> None:
    _setup_admin(site)
    site.smtp.fail_login = True
    failed = _save_email(site)
    assert failed.status_code == 400 and "login failed" in failed.text  # type: ignore[attr-defined]
    site.smtp.fail_login = False
    assert _save_email(site).status_code == 303  # type: ignore[attr-defined]
    assert site.smtp.sent == 1


def test_remove_needs_the_admin_password(site: Site) -> None:
    _setup_admin(site)
    _save_email(site)
    csrf = site.csrf("/settings")
    denied = site.client.post("/settings/email/remove", data={"csrf": csrf, "admin_password": "x"})
    assert denied.status_code == 400
    ok = site.client.post("/settings/email/remove", data={"csrf": csrf, "admin_password": PASSWORD})
    assert ok.status_code == 303
    assert config.load_hosted(db.connect(config.db_path())).email is None


# -- no stored secret ever leaves the server -----------------------------------------------------


def test_no_page_or_response_contains_a_stored_secret(
    site: Site, p8_pem: str, sa_json: str, rsa_pem: str, tmp_path: Path
) -> None:
    _setup_admin(site)
    _save_apple(site, p8_pem)
    _save_google(site, sa_json)
    _save_email(site)
    future = site.client.app.state.hosted.jobs.run("test")  # type: ignore[attr-defined]
    future.result(timeout=60)

    secrets = [
        *(line for line in p8_pem.splitlines() if len(line) > 16 and "-----" not in line),
        *(line for line in rsa_pem.splitlines() if len(line) > 16 and "-----" not in line),
        SMTP_PASSWORD,
        MASTER_KEY,
        PASSWORD,
    ]
    conn = db.connect(config.db_path())
    paths = ["/", "/?range=7", "/?range=90", "/sources", "/settings", "/api/status", "/healthz"]
    paths += [f"/apps/{a.id}" for a in db.list_apps(conn)]
    csrf = site.csrf("/settings")
    posts = [
        site.client.post(f"/settings/{s}/test", data={"csrf": csrf})
        for s in ("apple", "google", "email")
    ]
    responses = [site.client.get(p) for p in paths] + posts
    for response in responses:
        assert response.status_code == 200, (response.url, response.status_code)
        body = response.text
        for secret in secrets:
            assert secret not in body, f"{response.url} leaks a secret"
        assert "PRIVATE KEY" not in body
        assert not re.search(r"eyJ[\w-]+\.[\w-]+\.[\w-]+", body), f"{response.url} leaks a JWT"
    # Nor does the database file itself.
    raw = b"".join(p.read_bytes() for p in Path(config.db_path()).parent.glob("storepulse.db*"))
    for secret in (SMTP_PASSWORD, MASTER_KEY, *secrets[:3]):
        assert secret.encode() not in raw


# -- dashboard and jobs --------------------------------------------------------------------------


def test_run_now_collects_pairs_and_emails(site: Site, p8_pem: str, sa_json: str) -> None:
    _setup_admin(site)
    _save_apple(site, p8_pem)
    _save_google(site, sa_json)
    _save_email(site)
    jobs = site.client.app.state.hosted.jobs  # type: ignore[attr-defined]
    response = site.client.post("/sources/run", data={"csrf": site.csrf("/sources")})
    assert response.headers["location"] == "/sources?started=run"
    for _ in range(600):  # the job runs on a worker thread
        if jobs.status.running is None and jobs.status.last_label:
            break
        import time

        time.sleep(0.05)
    assert jobs.status.last_label == "Run now"
    assert jobs.status.last_email == "sent"
    overview = site.client.get("/?range=30").text
    assert "billFT" in overview and "iOS + Android" in overview
    sources = site.client.get("/sources").text
    assert "apple_sales ok" in sources


def test_a_pair_first_valid_this_run_is_still_in_this_run_s_email(
    site: Site, p8_pem: str, sa_json: str
) -> None:
    # save_apple/save_google already pair eagerly on save, so by itself this fixture
    # wouldn't catch a regression in when Jobs._run re-evaluates pairing. Reset billFT to
    # undecided first, the state a store-side rename would leave, so pairing can only be
    # (re-)established as part of the run this test triggers.
    _setup_admin(site)
    _save_apple(site, p8_pem)
    _save_google(site, sa_json)
    _save_email(site)
    conn = db.connect(config.db_path())
    conn.execute("UPDATE apps SET pair_key = NULL, pair_source = NULL WHERE name = 'billFT'")
    conn.close()

    jobs = site.client.app.state.hosted.jobs  # type: ignore[attr-defined]
    site.client.post("/sources/run", data={"csrf": site.csrf("/sources")})
    for _ in range(600):  # the job runs on a worker thread
        if jobs.status.running is None and jobs.status.last_label:
            break
        import time

        time.sleep(0.05)
    assert jobs.status.last_label == "Run now"
    assert site.smtp.sent_messages, "no email was sent"
    body = site.smtp.sent_messages[-1].get_body(preferencelist=("plain",))
    assert body is not None
    # Pairing must run before this run's own digest is built, not only before the next
    # page view, or a pair that first becomes valid this run ships as two rows by email.
    assert "billFT (iOS + Android)" in body.get_content()


def test_app_page_shows_platforms_side_by_side(site: Site, p8_pem: str, sa_json: str) -> None:
    _setup_admin(site)
    _save_apple(site, p8_pem)
    _save_google(site, sa_json)
    site.client.app.state.hosted.jobs.run("test").result(timeout=60)  # type: ignore[attr-defined]
    conn = db.connect(config.db_path())
    ios = next(a for a in db.list_apps(conn) if a.platform == "ios" and a.name == "billFT")
    page = site.client.get(f"/apps/{ios.id}").text
    assert page.count("<h2>iOS") == 1 and page.count("<h2>Android") == 1
    assert "Crash rate, 28 days" in page
    assert site.client.get("/apps/999999").status_code == 404


def test_hidden_apps_and_renames(site: Site, p8_pem: str) -> None:
    _setup_admin(site)
    _save_apple(site, p8_pem)
    conn = db.connect(config.db_path())
    desk = next(a for a in db.list_apps(conn) if a.name == "deskFT")
    response = site.client.post(
        "/settings/apps",
        data={"csrf": site.csrf("/settings"), f"name_{desk.id}": "Desk", f"hidden_{desk.id}": "on"},
    )
    assert response.status_code == 303
    app = next(a for a in db.list_apps(conn) if a.id == desk.id)
    assert (app.display_name, app.hidden, app.pair_source) == ("Desk", True, None)


def test_manual_pair_and_unpair(site: Site, p8_pem: str, sa_json: str) -> None:
    _setup_admin(site)
    _save_apple(site, p8_pem)
    _save_google(site, sa_json)
    conn = db.connect(config.db_path())
    apps = db.list_apps(conn)
    desk = next(a for a in apps if a.name == "deskFT")
    android = next(a for a in apps if a.platform == "android")
    csrf = site.csrf("/settings")
    site.client.post(
        "/settings/apps/pair", data={"csrf": csrf, "ios_id": desk.id, "android_id": android.id}
    )
    assert {a.pair_source for a in db.list_apps(conn) if a.id in (desk.id, android.id)} == {"user"}
    site.client.post("/settings/apps/unpair", data={"csrf": csrf, "app_id": desk.id})
    assert all(a.pair_key is None for a in db.list_apps(conn) if a.id in (desk.id, android.id))


def test_preferences_validation_and_save(site: Site) -> None:
    _setup_admin(site)
    csrf = site.csrf("/settings")
    bad = site.client.post("/settings/preferences", data={"csrf": csrf, "run_time": "25:00"})
    assert bad.status_code == 400 and "HH:MM" in bad.text
    bad_tz = site.client.post(
        "/settings/preferences", data={"csrf": csrf, "run_time": "09:15", "timezone": "Mars/Base"}
    )
    assert bad_tz.status_code == 400
    ok = site.client.post(
        "/settings/preferences",
        data={
            "csrf": csrf,
            "run_time": "09:15",
            "timezone": "Europe/Kyiv",
            "days": "7",
            "stale_days": "4",
            "crash_threshold": "1.5",
            "anr_threshold": "0.47",
            "display_currency": "usd",
            "rates": "CAD = 0.73\nEUR=1.09",
            "base_url": "https://stats.example.com/",
        },
    )
    assert ok.status_code == 303
    conn = db.connect(config.db_path())
    cfg = config.load_hosted(conn)
    assert cfg.schedule.run_time == "09:15"
    assert cfg.digest.crash_threshold == pytest.approx(0.015)
    assert cfg.digest.rates == {"CAD": 0.73, "EUR": 1.09}
    assert cfg.digest.display_currency == "USD"
    assert db.get_settings(conn)["hosted.base_url"] == "https://stats.example.com"
