from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from email.message import EmailMessage

import pytest

from conftest import (
    BUCKET,
    FT_APPS,
    PKG,
    TODAY,
    FakeApple,
    FakeGoogle,
    apple_error,
    fixture_bytes,
    standard_objects,
)
from storepulse.core import config, db, runner
from storepulse.core.sources.apple_client import AppleClient
from storepulse.core.sources.google_auth import load_service_account
from storepulse.core.sources.google_client import GoogleClient

YESTERDAY = TODAY - timedelta(days=1)


def _cfg(
    *, apple: bool = True, google: bool = True, email: config.EmailConfig | None = None
) -> config.Config:
    return config.Config(
        apple=config.AppleConfig(issuer_id="i", key_id="k", vendor_number="85000000")
        if apple
        else None,
        google=config.GoogleConfig(
            bucket_uri=f"gs://{BUCKET}/", service_account_email="a@b.iam.gserviceaccount.com"
        )
        if google
        else None,
        email=email,
        schedule=config.ScheduleConfig(days=1),
    )


def _apple_client(fake: FakeApple, p8_pem: str) -> AppleClient:
    return AppleClient("i", "k", p8_pem, transport=fake.transport, sleep=lambda _: None)


def _google_client(fake: FakeGoogle, sa_json: str) -> GoogleClient:
    return GoogleClient(
        load_service_account(sa_json), transport=fake.transport, sleep=lambda _: None
    )


@dataclass
class _FakeSMTP:
    sent: list[EmailMessage] = field(default_factory=list)
    fail: bool = False

    def starttls(self, *, context: object = None) -> tuple[int, bytes]:
        return (220, b"ready")

    def login(self, user: str, password: str) -> tuple[int, bytes]:
        return (235, b"accepted")

    def send_message(
        self, msg: EmailMessage, from_addr: str | None, to_addrs: list[str] | None
    ) -> dict[str, tuple[int, bytes]]:
        if self.fail:
            raise OSError("send failed")
        self.sent.append(msg)
        return {}

    def noop(self) -> tuple[int, bytes]:
        return (250, b"ok")

    def quit(self) -> tuple[int, bytes]:
        return (221, b"bye")


def _smtp_factory(fake: _FakeSMTP) -> Callable[[str, int, float], _FakeSMTP]:
    return lambda host, port, timeout: fake


EMAIL_CFG = config.EmailConfig(
    host="smtp.example.com",
    port=587,
    security="starttls",
    username="me@example.com",
    from_addr="me@example.com",
    to_addrs=["you@example.com"],
)


def test_full_run_collects_both_platforms_and_sends_email(
    conn: sqlite3.Connection, p8_pem: str, sa_json: str
) -> None:
    apple = FakeApple(apps=FT_APPS, default_report=fixture_bytes("summary_normal.tsv.gz"))
    google = FakeGoogle(objects=standard_objects())
    smtp = _FakeSMTP()

    result = runner.run_all(
        conn,
        _cfg(email=EMAIL_CFG),
        apple_client=_apple_client(apple, p8_pem),
        google_client=_google_client(google, sa_json),
        smtp_password="hunter2",
        today=TODAY,
        smtp_factory=_smtp_factory(smtp),
    )

    assert result.ok, result.errors
    assert result.email_sent
    assert len(smtp.sent) == 1
    assert result.digest is not None
    assert conn.execute("SELECT COUNT(*) FROM daily_metrics").fetchone()[0] > 0
    assert not (config.data_dir() / runner.LOCK_FILENAME).exists()


def test_failing_source_still_produces_digest_and_sends_email(
    conn: sqlite3.Connection, p8_pem: str, sa_json: str
) -> None:
    apple = FakeApple(sales={YESTERDAY.isoformat(): apple_error(401, "bad key", "NOT_AUTHORIZED")})
    google = FakeGoogle(objects=standard_objects())
    smtp = _FakeSMTP()

    result = runner.run_all(
        conn,
        _cfg(email=EMAIL_CFG),
        apple_client=_apple_client(apple, p8_pem),
        google_client=_google_client(google, sa_json),
        smtp_password="hunter2",
        today=TODAY,
        smtp_factory=_smtp_factory(smtp),
    )

    assert not result.ok
    assert any("apple_sales" in e for e in result.errors)
    assert not any("play_" in e for e in result.errors)
    assert result.digest is not None
    assert result.email_sent
    # Play data still landed even though Apple failed.
    assert (
        conn.execute("SELECT COUNT(*) FROM daily_metrics WHERE source LIKE 'play_%'").fetchone()[0]
        > 0
    )


def test_discovery_refresh_registers_new_apps(conn: sqlite3.Connection, sa_json: str) -> None:
    assert db.apps_for(conn, "android") == {}
    google = FakeGoogle(objects=standard_objects())

    result = runner.run_all(
        conn,
        _cfg(apple=False),
        google_client=_google_client(google, sa_json),
        today=TODAY,
        send_email=False,
    )

    assert result.ok, result.errors
    assert PKG in db.apps_for(conn, "android")
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM daily_metrics WHERE source = 'play_installs'"
        ).fetchone()[0]
        > 0
    )


def test_no_email_flag_skips_sending(conn: sqlite3.Connection, p8_pem: str) -> None:
    apple = FakeApple(apps=FT_APPS, default_report=fixture_bytes("summary_normal.tsv.gz"))
    result = runner.run_all(
        conn,
        _cfg(google=False, email=EMAIL_CFG),
        apple_client=_apple_client(apple, p8_pem),
        smtp_password="hunter2",
        today=TODAY,
        send_email=False,
    )
    assert result.ok
    assert not result.email_sent
    assert result.email_error is None
    assert result.digest is not None


def test_missing_email_config_is_reported_not_raised(conn: sqlite3.Connection, p8_pem: str) -> None:
    apple = FakeApple(apps=FT_APPS, default_report=fixture_bytes("summary_normal.tsv.gz"))
    result = runner.run_all(
        conn,
        _cfg(google=False, email=None),
        apple_client=_apple_client(apple, p8_pem),
        today=TODAY,
    )
    assert result.errors == []
    assert not result.email_sent
    assert result.email_error is not None and "init" in result.email_error
    assert result.digest is not None


def test_email_send_failure_is_captured(conn: sqlite3.Connection, p8_pem: str) -> None:
    apple = FakeApple(apps=FT_APPS, default_report=fixture_bytes("summary_normal.tsv.gz"))
    smtp = _FakeSMTP(fail=True)
    result = runner.run_all(
        conn,
        _cfg(google=False, email=EMAIL_CFG),
        apple_client=_apple_client(apple, p8_pem),
        smtp_password="hunter2",
        today=TODAY,
        smtp_factory=_smtp_factory(smtp),
    )
    assert not result.ok
    assert result.email_error is not None
    assert not (config.data_dir() / runner.LOCK_FILENAME).exists()


# -- locking ---------------------------------------------------------------------------------


def test_lock_blocks_a_second_run(conn: sqlite3.Connection) -> None:
    data_dir = config.data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    lock_path = data_dir / runner.LOCK_FILENAME
    lock_path.write_text("12345 2026-09-27T00:00:00+00:00\n")

    with pytest.raises(runner.LockedError, match="another run"):
        runner.run_all(conn, _cfg(apple=False, google=False), today=TODAY, send_email=False)

    lock_path.unlink()  # not ours to clean up since we never acquired it


def test_stale_lock_is_taken_over(conn: sqlite3.Connection) -> None:
    data_dir = config.data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    lock_path = data_dir / runner.LOCK_FILENAME
    lock_path.write_text("12345 2026-09-20T00:00:00+00:00\n")
    old = time.time() - (runner.STALE_LOCK_HOURS + 1) * 3600
    os.utime(lock_path, (old, old))

    result = runner.run_all(conn, _cfg(apple=False, google=False), today=TODAY, send_email=False)
    assert result.ok
    assert not lock_path.exists()


def test_lock_is_released_after_a_successful_run(conn: sqlite3.Connection) -> None:
    runner.run_all(conn, _cfg(apple=False, google=False), today=TODAY, send_email=False)
    assert not (config.data_dir() / runner.LOCK_FILENAME).exists()
