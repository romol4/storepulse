from __future__ import annotations

import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage

import pytest

from storepulse.core import mailer
from storepulse.core.config import EmailConfig

STARTTLS_CONFIG = EmailConfig(
    host="smtp.example.com",
    port=587,
    security="starttls",
    username="me@example.com",
    from_addr="me@example.com",
    to_addrs=["you@example.com"],
)
SSL_CONFIG = EmailConfig(
    host="smtp.example.com",
    port=465,
    security="ssl",
    username="me@example.com",
    from_addr="me@example.com",
    to_addrs=["you@example.com", "them@example.com"],
)


@dataclass
class FakeSMTP:
    calls: list[str] = field(default_factory=list)
    fail_connect: bool = False
    fail_starttls: bool = False
    fail_login: bool = False
    fail_send: bool = False
    fail_noop: bool = False
    sent: list[tuple[EmailMessage, str | None, list[str] | None]] = field(default_factory=list)
    starttls_context: ssl.SSLContext | None = None

    def starttls(self, *, context: ssl.SSLContext | None = None) -> tuple[int, bytes]:
        self.calls.append("starttls")
        self.starttls_context = context
        if self.fail_starttls:
            raise smtplib.SMTPException("starttls failed")
        return (220, b"ready")

    def login(self, user: str, password: str) -> tuple[int, bytes]:
        self.calls.append(f"login:{user}:{password}")
        if self.fail_login:
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials")
        return (235, b"accepted")

    def send_message(
        self, msg: EmailMessage, from_addr: str | None, to_addrs: list[str] | None
    ) -> dict[str, tuple[int, bytes]]:
        self.calls.append("send_message")
        if self.fail_send:
            raise smtplib.SMTPException("send failed")
        self.sent.append((msg, from_addr, to_addrs))
        return {}

    def noop(self) -> tuple[int, bytes]:
        self.calls.append("noop")
        if self.fail_noop:
            raise smtplib.SMTPException("noop failed")
        return (250, b"ok")

    def quit(self) -> tuple[int, bytes]:
        self.calls.append("quit")
        return (221, b"bye")


def factory_for(fake: FakeSMTP) -> mailer.SMTPFactory:
    def make(host: str, port: int, timeout: float) -> FakeSMTP:
        fake.calls.append(f"connect:{host}:{port}:{timeout}")
        if fake.fail_connect:
            raise OSError("connection refused")
        return fake

    return make


def test_starttls_flow_calls_starttls_then_login_then_sends() -> None:
    fake = FakeSMTP()
    msg = EmailMessage()
    msg.set_content("hi")
    mailer.send(STARTTLS_CONFIG, "hunter2", msg, factory=factory_for(fake))
    assert fake.calls == [
        "connect:smtp.example.com:587:20.0",
        "starttls",
        "login:me@example.com:hunter2",
        "send_message",
        "quit",
    ]
    assert len(fake.sent) == 1
    sent_msg, from_addr, to_addrs = fake.sent[0]
    assert sent_msg is msg
    assert from_addr == "me@example.com"
    assert to_addrs == ["you@example.com"]


def test_ssl_flow_skips_starttls() -> None:
    fake = FakeSMTP()
    msg = EmailMessage()
    msg.set_content("hi")
    mailer.send(SSL_CONFIG, "hunter2", msg, factory=factory_for(fake))
    assert fake.calls == [
        "connect:smtp.example.com:465:20.0",
        "login:me@example.com:hunter2",
        "send_message",
        "quit",
    ]
    assert fake.sent[0][2] == ["you@example.com", "them@example.com"]


def test_check_logs_in_and_noops_without_sending() -> None:
    fake = FakeSMTP()
    mailer.check(STARTTLS_CONFIG, "hunter2", factory=factory_for(fake))
    assert fake.calls == [
        "connect:smtp.example.com:587:20.0",
        "starttls",
        "login:me@example.com:hunter2",
        "noop",
        "quit",
    ]
    assert fake.sent == []


def test_connect_failure_names_host_and_settings() -> None:
    fake = FakeSMTP(fail_connect=True)
    with pytest.raises(
        mailer.MailerError, match=r"\[email\] host, port.*smtp\.example\.com"
    ) as info:
        mailer.check(STARTTLS_CONFIG, "hunter2", factory=factory_for(fake))
    assert "README" in str(info.value)


def test_starttls_failure_is_reported_and_connection_closed() -> None:
    fake = FakeSMTP(fail_starttls=True)
    with pytest.raises(mailer.MailerError, match="STARTTLS"):
        mailer.check(STARTTLS_CONFIG, "hunter2", factory=factory_for(fake))
    assert "quit" in fake.calls


def test_login_failure_message_redacts_password() -> None:
    fake = FakeSMTP(fail_login=True)
    with pytest.raises(mailer.MailerError, match="username and the saved password") as info:
        mailer.check(STARTTLS_CONFIG, "hunter2-the-secret-password", factory=factory_for(fake))
    assert "hunter2-the-secret-password" not in str(info.value)
    assert "quit" in fake.calls


def test_send_failure_still_quits() -> None:
    fake = FakeSMTP(fail_send=True)
    msg = EmailMessage()
    msg.set_content("hi")
    with pytest.raises(mailer.MailerError, match="could not send mail"):
        mailer.send(STARTTLS_CONFIG, "hunter2", msg, factory=factory_for(fake))
    assert fake.calls[-1] == "quit"


def test_noop_failure_is_reported() -> None:
    fake = FakeSMTP(fail_noop=True)
    with pytest.raises(mailer.MailerError, match="NOOP failed"):
        mailer.check(STARTTLS_CONFIG, "hunter2", factory=factory_for(fake))


def test_default_factory_picks_smtp_class_by_security(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        smtplib, "SMTP", lambda host, port, timeout: calls.append(f"SMTP:{host}:{port}:{timeout}")
    )
    monkeypatch.setattr(
        smtplib,
        "SMTP_SSL",
        lambda host, port, timeout, context=None: calls.append(
            f"SMTP_SSL:{host}:{port}:{timeout}:{context}"
        ),
    )
    mailer.default_factory(STARTTLS_CONFIG)("h", 1, 2.0)
    mailer.default_factory(SSL_CONFIG)("h", 1, 2.0)
    assert calls[0] == "SMTP:h:1:2.0"
    assert calls[1].startswith("SMTP_SSL:h:1:2.0:")


def _is_verifying(context: ssl.SSLContext | None) -> bool:
    return (
        context is not None
        and context.verify_mode == ssl.CERT_REQUIRED
        and context.check_hostname is True
    )


def test_default_factory_ssl_context_verifies_certificates(monkeypatch: pytest.MonkeyPatch) -> None:
    # smtplib.SMTP_SSL silently skips certificate verification when context=None
    # (it falls back to ssl._create_stdlib_context(), not ssl.create_default_context()),
    # so this must always pass a verifying context explicitly.
    captured: dict[str, ssl.SSLContext | None] = {}
    monkeypatch.setattr(
        smtplib,
        "SMTP_SSL",
        lambda host, port, timeout, context=None: captured.__setitem__("context", context),
    )
    mailer.default_factory(SSL_CONFIG)("h", 1, 2.0)
    assert _is_verifying(captured["context"])


def test_starttls_uses_a_verifying_context() -> None:
    fake = FakeSMTP()
    mailer.check(STARTTLS_CONFIG, "hunter2", factory=factory_for(fake))
    assert _is_verifying(fake.starttls_context)


def test_test_message_addressed_to_all_recipients() -> None:
    msg = mailer.test_message(SSL_CONFIG)
    assert msg["From"] == "me@example.com"
    assert msg["To"] == "you@example.com, them@example.com"
    assert "Storepulse" in msg["Subject"]
    assert "test" in msg.get_content().lower()
