"""SMTP sending for the daily digest. The only module that talks to the user's mail server."""

from __future__ import annotations

import contextlib
import smtplib
import ssl
from collections.abc import Callable
from email.message import EmailMessage
from typing import Protocol

from storepulse.core.config import SECURITY_STARTTLS, EmailConfig
from storepulse.core.secrets import redact, register_secret

DEFAULT_TIMEOUT = 20.0
DOCS_HINT = "See README, 'SMTP setup'."


class MailerError(Exception):
    """Raised for SMTP failures. Messages never contain the password."""


class SMTPLike(Protocol):
    """What `send`/`check` need from an SMTP connection (`smtplib.SMTP`/`SMTP_SSL` satisfy it)."""

    def starttls(self, *, context: ssl.SSLContext | None = None) -> tuple[int, bytes]: ...
    def login(self, user: str, password: str) -> tuple[int, bytes]: ...
    def send_message(
        self, msg: EmailMessage, from_addr: str | None, to_addrs: list[str] | None
    ) -> dict[str, tuple[int, bytes]]: ...
    def noop(self) -> tuple[int, bytes]: ...
    def quit(self) -> tuple[int, bytes]: ...


# (host, port, timeout) -> a connected-but-not-authenticated client. Tests inject a fake.
SMTPFactory = Callable[[str, int, float], SMTPLike]


def default_factory(config: EmailConfig) -> SMTPFactory:
    """`SMTP_SSL` for implicit TLS, `SMTP` (then `starttls()`) otherwise.

    Both smtplib.SMTP_SSL and SMTP.starttls() silently skip certificate verification
    when no context is given (they fall back to ssl._create_stdlib_context(), not
    ssl.create_default_context()) — so this always passes one explicitly.
    """
    context = ssl.create_default_context()
    if config.security == "ssl":
        return lambda host, port, timeout: smtplib.SMTP_SSL(
            host, port, timeout=timeout, context=context
        )
    return lambda host, port, timeout: smtplib.SMTP(host, port, timeout=timeout)


def test_message(config: EmailConfig) -> EmailMessage:
    """The message `init` sends to prove the SMTP settings work before saving them."""
    msg = EmailMessage()
    msg["Subject"] = "Storepulse test email"
    msg["From"] = config.from_addr
    msg["To"] = ", ".join(config.to_addrs)
    msg.set_content(
        "This is a test email from Storepulse. If you're reading this, your SMTP settings work.\n"
    )
    return msg


def _quit_quietly(client: SMTPLike) -> None:
    with contextlib.suppress(OSError, smtplib.SMTPException):
        client.quit()


def _wrap(config: EmailConfig, what: str, exc: Exception) -> MailerError:
    return MailerError(
        redact(f"{what} ({type(exc).__name__}: {exc}) for {config.host}:{config.port}. {DOCS_HINT}")
    )


def _connect(config: EmailConfig, factory: SMTPFactory | None) -> SMTPLike:
    make = factory or default_factory(config)
    try:
        client = make(config.host, config.port, DEFAULT_TIMEOUT)
    except (OSError, smtplib.SMTPException) as exc:
        raise _wrap(
            config, "could not connect; check [email] host, port and security", exc
        ) from None
    if config.security == SECURITY_STARTTLS:
        try:
            client.starttls(context=ssl.create_default_context())
        except (OSError, smtplib.SMTPException) as exc:
            _quit_quietly(client)
            raise _wrap(config, "STARTTLS failed; check [email] security and port", exc) from None
    return client


def _login(config: EmailConfig, client: SMTPLike, password: str) -> None:
    try:
        client.login(config.username, password)
    except (OSError, smtplib.SMTPException) as exc:
        _quit_quietly(client)
        raise _wrap(
            config, "login failed; check [email] username and the saved password", exc
        ) from None


def send(
    config: EmailConfig, password: str, msg: EmailMessage, *, factory: SMTPFactory | None = None
) -> None:
    """Send one message through the user's SMTP server."""
    register_secret(password)
    client = _connect(config, factory)
    _login(config, client, password)
    try:
        client.send_message(msg, config.from_addr, list(config.to_addrs))
    except (OSError, smtplib.SMTPException) as exc:
        raise _wrap(config, "could not send mail", exc) from None
    finally:
        _quit_quietly(client)


def check(config: EmailConfig, password: str, *, factory: SMTPFactory | None = None) -> None:
    """Log in and send NOOP, for `doctor`. Never sends mail."""
    register_secret(password)
    client = _connect(config, factory)
    _login(config, client, password)
    try:
        client.noop()
    except (OSError, smtplib.SMTPException) as exc:
        raise _wrap(config, "connected and logged in, but NOOP failed", exc) from None
    finally:
        _quit_quietly(client)
