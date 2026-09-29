"""Passwords, TOTP, session tokens, CSRF tokens and login throttling for the hosted app.

Deliberately small: the spec asks for one admin account (argon2id password, optional
TOTP), server-side sessions and CSRF protection on every form. Phase 5 audits all of it.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_PASSWORD_LENGTH = 12
SESSION_COOKIE = "sp_session"
ANON_CSRF_COOKIE = "sp_csrf"
SESSION_HOURS = 12
TOTP_ISSUER = "Storepulse"

_hasher = PasswordHasher()  # argon2id with the library's current recommended parameters


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_problem(password: str, confirm: str) -> str | None:
    """A user-facing reason the password can't be used, or None."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Use at least {MIN_PASSWORD_LENGTH} characters."
    if password != confirm:
        return "The two passwords don't match."
    return None


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    """Stored instead of a session or setup token, so a copy of the database can't be
    used to log in."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_uri(secret: str, account: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=account, issuer_name=TOTP_ISSUER)


def totp_step(secret: str, code: str, now: datetime | None = None) -> int | None:
    """The time step ``code`` is valid for (the current one, or one either side for clock
    drift), or None. Callers record the step with ``db.claim_totp_step`` so a code can't be
    used twice: pyotp's own ``verify`` would accept the same code for its whole window."""
    code = "".join(code.split())
    if not code.isdigit():
        return None
    totp = pyotp.TOTP(secret)
    current = totp.timecode(now or datetime.now(UTC))
    for step in (current - 1, current, current + 1):
        if hmac.compare_digest(totp.generate_otp(step).encode(), code.encode()):
            return step
    return None


@dataclass
class Throttle:
    """In-memory backoff for failed logins, TOTP codes and setup-token attempts.

    After ``limit`` failures for a key (an IP address, or an account) within ``window``
    seconds, further attempts are refused until the window passes. Process-local on
    purpose: the hosted app is a single process, and a restart only resets the counters.
    """

    limit: int = 5
    window: float = 15 * 60
    clock: Callable[[], float] = time.monotonic
    _failures: dict[str, list[float]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _recent(self, key: str, now: float) -> list[float]:
        kept = [t for t in self._failures.get(key, []) if now - t < self.window]
        self._failures[key] = kept
        return kept

    def retry_after(self, *keys: str) -> int:
        """Seconds until an attempt is allowed again for every key (0 if allowed now)."""
        now = self.clock()
        wait = 0.0
        with self._lock:
            for key in keys:
                recent = self._recent(key, now)
                if len(recent) >= self.limit:
                    wait = max(wait, self.window - (now - recent[0]))
        return int(wait) + (1 if wait else 0)

    def failed(self, *keys: str) -> None:
        now = self.clock()
        with self._lock:
            for key in keys:
                self._recent(key, now).append(now)

    def succeeded(self, *keys: str) -> None:
        with self._lock:
            for key in keys:
                self._failures.pop(key, None)
