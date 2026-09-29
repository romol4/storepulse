from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import timedelta
from email.message import EmailMessage
from pathlib import Path

import pytest

from conftest import FT_APPS, TODAY, FakeApple, FakeGoogle, apple_error, fixture_bytes, router
from storepulse.cli.main import Env, main
from storepulse.core import config, db
from storepulse.core.secrets import KeyringStore

YESTERDAY = TODAY - timedelta(days=1)
URI = "gs://pubsite_prod_1234567890/"


@dataclass
class FakeSMTP:
    sent: list[EmailMessage] = field(default_factory=list)

    def starttls(self, *, context: object = None) -> tuple[int, bytes]:
        return (220, b"ready")

    def login(self, user: str, password: str) -> tuple[int, bytes]:
        return (235, b"ok")

    def send_message(
        self, msg: EmailMessage, from_addr: str | None, to_addrs: list[str] | None
    ) -> dict[str, tuple[int, bytes]]:
        self.sent.append(msg)
        return {}

    def noop(self) -> tuple[int, bytes]:
        return (250, b"ok")

    def quit(self) -> tuple[int, bytes]:
        return (221, b"bye")


def _env(
    apple: FakeApple | None = None,
    google: FakeGoogle | None = None,
    smtp: FakeSMTP | None = None,
    answers: list[str] | None = None,
) -> Env:
    it = iter(answers or [])
    return Env(
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        input=lambda prompt: next(it),
        getpass=lambda prompt: "app-password",
        transport=router(apple, google),
        sleep=lambda _: None,
        today=TODAY,
        smtp_factory=(lambda host, port, timeout: smtp) if smtp is not None else None,
    )


def _out(env: Env) -> str:
    return env.stdout.getvalue()  # type: ignore[attr-defined, no-any-return]


def _err(env: Env) -> str:
    return env.stderr.getvalue()  # type: ignore[attr-defined, no-any-return]


def _init_apple_and_email(p8_file: Path) -> None:
    fake = FakeApple(apps=FT_APPS, default_report=fixture_bytes("summary_normal.tsv.gz"))
    answers = [
        "y", "ISSUER-1", "KEYID12345", "85000000", str(p8_file),  # Apple
        "n",  # Google
        "y", "smtp.example.com", "587", "starttls", "me@example.com",  # Email
        "me@example.com", "you@example.com",
        "",  # run time: accept default
        "n",  # decline "Install the daily schedule now?"
    ]  # fmt: skip
    env = _env(apple=fake, smtp=FakeSMTP(), answers=answers)
    assert main(["init", "--no-backfill"], env) == 0, (_out(env), _err(env))


@pytest.fixture
def p8_file(tmp_path: Path, p8_pem: str) -> Path:
    path = tmp_path / "AuthKey.p8"
    path.write_text(p8_pem)
    return path


def test_run_collects_and_sends_email(memory_keyring: object, p8_file: Path) -> None:
    _init_apple_and_email(p8_file)
    fake = FakeApple(apps=FT_APPS, default_report=fixture_bytes("summary_normal.tsv.gz"))
    smtp = FakeSMTP()
    env = _env(apple=fake, smtp=smtp)
    assert main(["run", "--days", "1"], env) == 0, _err(env)
    assert len(smtp.sent) == 1
    assert "Digest sent." in _out(env)


def test_run_no_email_flag(memory_keyring: object, p8_file: Path) -> None:
    _init_apple_and_email(p8_file)
    fake = FakeApple(apps=FT_APPS, default_report=fixture_bytes("summary_normal.tsv.gz"))
    smtp = FakeSMTP()
    env = _env(apple=fake, smtp=smtp)
    assert main(["run", "--days", "1", "--no-email"], env) == 0
    assert smtp.sent == []
    assert "--no-email" in _out(env)


def test_run_before_init_is_an_error() -> None:
    env = _env()
    assert main(["run"], env) == 1
    assert "storepulse init" in _err(env)


def test_run_reports_source_errors_but_still_exits_nonzero(
    memory_keyring: object, p8_file: Path
) -> None:
    _init_apple_and_email(p8_file)
    broken = FakeApple(sales={YESTERDAY.isoformat(): apple_error(401, "bad key", "NOT_AUTHORIZED")})
    smtp = FakeSMTP()
    env = _env(apple=broken, smtp=smtp)
    assert main(["run", "--days", "1"], env) == 1
    assert "source(s) had problems" in _err(env)
    # the digest still goes out even though Apple failed
    assert len(smtp.sent) == 1


def test_run_missing_apple_key_is_a_clear_error(memory_keyring: object, p8_file: Path) -> None:
    _init_apple_and_email(p8_file)
    store = KeyringStore(config_dir=config.config_dir())
    store.delete("apple_p8")
    env = _env(apple=FakeApple())
    assert main(["run"], env) == 1
    assert "no Apple key found" in _err(env)


def test_secrets_never_leak_through_a_failing_run(
    memory_keyring: object, p8_file: Path, p8_pem: str
) -> None:
    """Broad redaction sweep: a real credential, registered for real during init, must
    never reach stdout, stderr, or the SQLite database — even when an upstream error's
    detail text happens to echo it back (e.g. a misbehaving proxy reflecting the request).
    """
    _init_apple_and_email(p8_file)
    # A 5xx is what actually carries the response body's detail text into the raised
    # error's message (after redaction); a 401's message is a fixed, generic string that
    # never echoes the response body, so it wouldn't exercise redaction at all.
    leaking_detail = f"upstream failure; request context included: {p8_pem}"
    broken = FakeApple(
        sales={YESTERDAY.isoformat(): lambda: apple_error(500, leaking_detail, "SERVER_ERROR")}
    )
    env = _env(apple=broken, smtp=FakeSMTP())
    assert main(["run", "--days", "1"], env) == 1

    console = _out(env) + _err(env)
    assert p8_pem not in console
    assert "[REDACTED]" in console

    conn = db.connect(config.db_path())
    try:
        errors = " ".join(
            row["error"] or "" for row in conn.execute("SELECT error FROM ingest_log")
        )
    finally:
        conn.close()
    assert p8_pem not in errors


def test_run_uses_configured_days_by_default(memory_keyring: object, p8_file: Path) -> None:
    _init_apple_and_email(p8_file)
    cfg = config.load()
    cfg.schedule = config.ScheduleConfig(run_time=cfg.schedule.run_time, days=2)
    config.save(cfg)
    fake = FakeApple(apps=FT_APPS, default_report=fixture_bytes("summary_normal.tsv.gz"))
    env = _env(apple=fake, smtp=FakeSMTP())
    assert main(["run"], env) == 0
    conn = db.connect(config.db_path())
    try:
        n = conn.execute(
            "SELECT COUNT(DISTINCT date) FROM daily_metrics WHERE source = 'apple_sales'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == 2
