from __future__ import annotations

import io
import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path

import pytest

from conftest import FakeApple, fixture_bytes
from storepulse.cli.main import Env, main
from storepulse.core import config
from storepulse.core.secrets import KeyringStore


@pytest.fixture
def p8_file(tmp_path: Path, p8_pem: str) -> Path:
    path = tmp_path / "AuthKey.p8"
    path.write_text(p8_pem)
    return path


@dataclass
class FakeSMTP:
    sent: list[EmailMessage] = field(default_factory=list)
    fail_login: bool = False

    def starttls(self) -> tuple[int, bytes]:
        return (220, b"ready")

    def login(self, user: str, password: str) -> tuple[int, bytes]:
        if self.fail_login:
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials")
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


def _env(answers: list[str], smtp: FakeSMTP | None = None, apple: FakeApple | None = None) -> Env:
    it = iter(answers)
    return Env(
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        input=lambda prompt: next(it),
        getpass=lambda prompt: "app-password",
        transport=(apple or FakeApple()).transport,
        sleep=lambda _: None,
        smtp_factory=(lambda host, port, timeout: smtp) if smtp is not None else None,
    )


def _out(env: Env) -> str:
    return env.stdout.getvalue()  # type: ignore[attr-defined, no-any-return]


def _err(env: Env) -> str:
    return env.stderr.getvalue()  # type: ignore[attr-defined, no-any-return]


EMAIL_ANSWERS = [
    "n",  # Apple
    "n",  # Google
    "y",  # set up email
    "smtp.example.com", "587", "starttls", "me@example.com",
    "me@example.com", "you1@example.com, you2@example.com",
]  # fmt: skip


def test_email_setup_sends_test_email_before_saving(memory_keyring: object) -> None:
    smtp = FakeSMTP()
    env = _env([*EMAIL_ANSWERS, "", "n"], smtp=smtp)
    assert main(["init", "--no-backfill"], env) == 0, _err(env)
    assert len(smtp.sent) == 1
    assert smtp.sent[0]["To"] == "you1@example.com, you2@example.com"
    cfg = config.load()
    assert cfg.email == config.EmailConfig(
        host="smtp.example.com",
        port=587,
        security="starttls",
        username="me@example.com",
        from_addr="me@example.com",
        to_addrs=["you1@example.com", "you2@example.com"],
    )
    store = KeyringStore(config_dir=config.config_dir())
    assert store.get("smtp_password") == "app-password"
    assert "Test email sent" in _out(env)


def test_email_setup_saves_nothing_if_test_email_fails(memory_keyring: object) -> None:
    smtp = FakeSMTP(fail_login=True)
    env = _env(EMAIL_ANSWERS, smtp=smtp)
    assert main(["init", "--no-backfill"], env) == 1
    assert "username and the saved password" in _err(env)
    assert config.load().email is None
    assert not memory_keyring.store  # type: ignore[attr-defined]
    assert smtp.sent == []


def test_email_setup_rejects_invalid_security() -> None:
    answers = [
        "n", "n", "y",
        "smtp.example.com", "587", "plaintext", "me@example.com",
        "me@example.com", "me@example.com",
    ]  # fmt: skip
    env = _env(answers, smtp=FakeSMTP())
    assert main(["init", "--no-backfill"], env) == 1
    assert "security" in _err(env)


def test_email_setup_requires_at_least_one_to_address() -> None:
    answers = [
        "n", "n", "y",
        "smtp.example.com", "587", "starttls", "me@example.com",
        "me@example.com", "  , ,  ",
    ]  # fmt: skip
    env = _env(answers, smtp=FakeSMTP())
    assert main(["init", "--no-backfill"], env) == 1
    assert "To address" in _err(env)


def test_reinit_email_reuses_saved_password(memory_keyring: object) -> None:
    smtp = FakeSMTP()
    assert main(["init", "--no-backfill"], _env([*EMAIL_ANSWERS, "", "n"], smtp=smtp)) == 0

    # Re-run: change the port, leave the password blank to reuse the saved one, and
    # accept the previous from/to defaults with a blank answer.
    answers = [
        "n", "n", "y",
        "smtp.example.com", "465", "ssl", "me@example.com",
        "", "",  # from, to: accept previous defaults
        "",  # run time: accept default
        "n",  # decline "Install the daily schedule now?"
    ]  # fmt: skip
    it = iter(answers)
    passes = iter([""])  # blank password -> reuse the saved one
    env2 = Env(
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        input=lambda prompt: next(it),
        getpass=lambda prompt: next(passes),
        transport=FakeApple().transport,
        sleep=lambda _: None,
        smtp_factory=lambda host, port, timeout: smtp,
    )
    assert main(["init", "--no-backfill"], env2) == 0, _err(env2)
    loaded = config.load()
    assert loaded.email is not None
    assert loaded.email.port == 465
    store = KeyringStore(config_dir=config.config_dir())
    assert store.get("smtp_password") == "app-password"


# -- doctor ---------------------------------------------------------------------------------


def test_doctor_smtp_ok(memory_keyring: object) -> None:
    smtp = FakeSMTP()
    assert main(["init", "--no-backfill"], _env([*EMAIL_ANSWERS, "", "n"], smtp=smtp)) == 0

    doctor_env = _env([], smtp=smtp)
    assert main(["doctor"], doctor_env) == 0
    assert "login and NOOP succeeded" in _out(doctor_env)


def test_doctor_smtp_failure(memory_keyring: object) -> None:
    smtp = FakeSMTP()
    assert main(["init", "--no-backfill"], _env([*EMAIL_ANSWERS, "", "n"], smtp=smtp)) == 0

    doctor_env = _env([], smtp=FakeSMTP(fail_login=True))
    assert main(["doctor"], doctor_env) == 1
    assert "FAIL" in _out(doctor_env)


def test_doctor_before_init_is_an_error() -> None:
    env = _env([])
    assert main(["doctor"], env) == 1
    assert "storepulse init" in _err(env)


def test_doctor_without_email_is_informational_not_a_warning(
    memory_keyring: object, p8_file: Path
) -> None:
    fake = FakeApple(
        apps=[{"id": "1", "name": "deskFT", "sku": "D", "bundle": "com.example.d"}],
        default_report=fixture_bytes("summary_empty.tsv.gz"),
    )
    init_answers = ["y", "ISSUER-1", "KEYID12345", "85000000", str(p8_file), "n", "n"]
    env = _env(init_answers, apple=fake)
    assert main(["init", "--no-backfill"], env) == 0, _err(env)

    doctor_env = _env([], apple=fake)
    assert main(["doctor"], doctor_env) == 0
    out = _out(doctor_env)
    assert "SMTP: not configured yet" in out
    assert "All checks passed." in out
