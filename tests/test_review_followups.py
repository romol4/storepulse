"""Regression tests for the follow-up fixes after the second review round."""

from __future__ import annotations

import io
import sqlite3
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import httpx
import keyring
import pytest
from keyring.backends import fail

from conftest import (
    BUCKET,
    PKG,
    TODAY,
    FakeApple,
    FakeGoogle,
    apple_error,
    router,
    standard_objects,
)
from storepulse.cli.checks import check_google
from storepulse.cli.main import Env, main
from storepulse.core import db, runner
from storepulse.core.sources import apple_sales
from storepulse.core.sources.apple_client import AppleAuthError, AppleClient
from storepulse.core.sources.google_auth import load_service_account
from storepulse.core.sources.google_client import (
    FINANCIAL_PERMISSION,
    GoogleAuthError,
    GoogleClient,
)
from storepulse.core.sources.play_common import Month


def _client(fake: FakeApple, p8_pem: str) -> AppleClient:
    return AppleClient("i", "k", p8_pem, transport=fake.transport, sleep=lambda _: None)


@pytest.mark.parametrize(
    ("detail", "expected"),
    [
        # Apple's details don't always end with a full stop.
        ("This request is forbidden for security reasons", "security reasons. See README"),
        ("You are not allowed.", "You are not allowed. See README"),
    ],
)
def test_apple_403_detail_reads_as_a_sentence(p8_pem: str, detail: str, expected: str) -> None:
    fake = FakeApple(sales={"2026-09-20": apple_error(403, detail, "FORBIDDEN_ERROR")})
    with pytest.raises(AppleAuthError) as info:
        apple_sales.fetch_day(_client(fake, p8_pem), "1", date(2026, 9, 20), TODAY)
    message = str(info.value)
    assert expected in message
    assert ".." not in message


# -- Google messages read as sentences --------------------------------------------------------


def _google(sa_json: str, status: int, body: dict[str, object]) -> GoogleClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "ya29.t", "expires_in": 3599})
        return httpx.Response(status, json=body)

    return GoogleClient(
        load_service_account(sa_json), transport=httpx.MockTransport(handler), sleep=lambda _: None
    )


def _error(status: int, message: str, reason: str = "") -> dict[str, object]:
    details = [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": reason}]
    return {"error": {"code": status, "message": message, "details": details if reason else []}}


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        # A hard 403 whose message already ends with a full stop: no "..".
        (
            403,
            _error(403, "Reporting API has not been used in project 123.", "SERVICE_DISABLED"),
            "project 123. This is a Google Cloud project setting",
        ),
        # A permission 403 whose message has no full stop: not "denied See README".
        (403, _error(403, "denied"), "Google said: denied. See README"),
        (401, _error(401, "Request had invalid authentication credentials"), "credentials. See"),
    ],
)
def test_google_denials_read_as_sentences(
    sa_json: str, status: int, body: dict[str, object], expected: str
) -> None:
    with pytest.raises(GoogleAuthError) as info:
        _google(sa_json, status, body).search_apps()
    message = str(info.value)
    assert expected in message
    assert ".." not in message


# -- no sales or earnings files may just mean no Android revenue -------------------------------


def test_invisible_revenue_prefix_mentions_no_revenue(
    conn: sqlite3.Connection, sa_json: str
) -> None:
    db.upsert_app(conn, "android", PKG, "billFT")
    stats_only = {k: v for k, v in standard_objects().items() if k.startswith("stats/")}
    client = GoogleClient(
        load_service_account(sa_json),
        transport=FakeGoogle(objects=stats_only).transport,
        sleep=lambda _: None,
    )
    summary = runner.collect_play_sales(conn, client, BUCKET, [Month(2026, 9)], today=TODAY)
    assert len(summary.warnings) == 1
    assert "no Android revenue yet, ignore this" in summary.warnings[0]
    assert FINANCIAL_PERMISSION in summary.warnings[0]

    result = check_google(client, BUCKET)
    warnings = [c.text for c in result.checks if c.level == "warn"]
    assert len(warnings) == 2  # sales and earnings
    assert all("no Android revenue yet, ignore this" in w for w in warnings)


# -- re-running init with the saved key asks for the passphrase once ---------------------------


@pytest.fixture
def no_keychain() -> Iterator[None]:
    previous = keyring.get_keyring()
    keyring.set_keyring(fail.Keyring())
    yield
    keyring.set_keyring(previous)


def test_reinit_with_saved_key_asks_passphrase_once(
    no_keychain: None, tmp_path: Path, sa_json: str
) -> None:
    key = tmp_path / "key.json"
    key.write_text(sa_json)
    prompts: list[str] = []

    def env(answers: list[str]) -> Env:
        it = iter(answers)

        def getpass(prompt: str) -> str:
            prompts.append(prompt)
            return "correct horse battery"

        return Env(
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            input=lambda prompt: next(it),
            getpass=getpass,
            transport=router(None, FakeGoogle(objects=standard_objects())),
            sleep=lambda _: None,
            today=TODAY,
        )

    uri = f"gs://{BUCKET}/"
    assert main(["init", "--no-backfill"], env(["n", "y", str(key), uri, "n", "n"])) == 0
    assert len(prompts) == 2  # choose + repeat the new passphrase
    prompts.clear()
    # Google again, Enter at the key path to keep the saved key, same bucket.
    assert main(["init", "--no-backfill"], env(["n", "y", "", uri, "n", "n"])) == 0
    assert prompts == ["Secrets file passphrase: "]
