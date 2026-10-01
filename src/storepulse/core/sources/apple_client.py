"""App Store Connect API client: ES256 JWT auth, token refresh, retries, error mapping."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from storepulse.core.secrets import redact, register_secret

log = logging.getLogger(__name__)

BASE_URL = "https://api.appstoreconnect.apple.com"
AUDIENCE = "appstoreconnect-v1"
TOKEN_LIFETIME = 19 * 60  # Apple rejects tokens that live longer than 20 minutes.
TOKEN_REFRESH_AFTER = 15 * 60  # Re-sign well before expiry so long backfills never see a 401.
MAX_ATTEMPTS = 3
DOCS_HINT = "See README, 'Apple API key'."


@dataclass(frozen=True)
class AppleErrorDetail:
    status: str = ""
    code: str = ""
    title: str = ""
    detail: str = ""


class AppleError(Exception):
    """A non-2xx response from App Store Connect. Never carries credential material."""

    def __init__(self, message: str, status: int, errors: list[AppleErrorDetail]) -> None:
        super().__init__(message)
        self.status = status
        self.errors = errors

    @property
    def detail(self) -> str:
        return " ".join(e.detail or e.title for e in self.errors)


class AppleAuthError(AppleError):
    """401/403: the key, its IDs, or its role is wrong. Not retried."""


class AppleAgreementError(AppleAuthError):
    """403 because the Account Holder hasn't accepted an updated agreement."""


class AppleVendorError(AppleError):
    """The vendor number was rejected."""


AGREEMENT_CODE = "FORBIDDEN.REQUIRED_AGREEMENTS_MISSING_OR_EXPIRED"


def _as_sentence(text: str) -> str:
    """End ``text`` with one full stop, so the hint appended after it reads correctly."""
    return text if text.endswith((".", "!", "?")) else f"{text}."


def forbidden_error(purpose: str, errors: list[AppleErrorDetail]) -> AppleAuthError:
    """Map a 403 to the right fix, keeping Apple's own reason in the message."""
    detail = redact(" ".join(e.detail or e.title for e in errors).strip())
    said = f" Apple said: {_as_sentence(detail)}" if detail else ""
    if any(e.code == AGREEMENT_CODE for e in errors):
        return AppleAgreementError(
            f"App Store Connect denied {purpose} (HTTP 403) because a required agreement is "
            "missing or has expired. The Account Holder must sign in to App Store Connect "
            "and accept the pending agreement (Business, or the banner on the home page); "
            f"the API key itself is fine.{said} {DOCS_HINT}",
            403,
            errors,
        )
    return AppleAuthError(
        f"App Store Connect denied {purpose} (HTTP 403). Check that the API key has the Sales "
        f"and Reports role (Users and Access > Integrations > edit the key).{said} {DOCS_HINT}",
        403,
        errors,
    )


def load_private_key(p8_pem: str) -> ec.EllipticCurvePrivateKey:
    try:
        key = load_pem_private_key(p8_pem.encode("utf-8"), password=None)
    except (ValueError, TypeError):
        raise ValueError(
            "the .p8 file is not a valid private key; download it again from App Store "
            f"Connect > Users and Access > Integrations. {DOCS_HINT}"
        ) from None
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise ValueError(f"the .p8 file is not an EC (ES256) key. {DOCS_HINT}")
    return key


def make_jwt(issuer_id: str, key_id: str, p8_pem: str, now: float | None = None) -> str:
    """Sign an App Store Connect API token (ES256, kid header, 19-minute lifetime)."""
    key = load_private_key(p8_pem)
    issued = int(time.time() if now is None else now)
    payload = {"iss": issuer_id, "iat": issued, "exp": issued + TOKEN_LIFETIME, "aud": AUDIENCE}
    token = jwt.encode(payload, key, algorithm="ES256", headers={"kid": key_id, "typ": "JWT"})
    register_secret(token)
    return token


def _parse_errors(response: httpx.Response) -> list[AppleErrorDetail]:
    try:
        body = response.json()
    except ValueError:
        return []
    errors = body.get("errors") if isinstance(body, dict) else None
    if not isinstance(errors, list):
        return []
    return [
        AppleErrorDetail(
            status=str(e.get("status", "")),
            code=str(e.get("code", "")),
            title=str(e.get("title", "")),
            detail=str(e.get("detail", "")),
        )
        for e in errors
        if isinstance(e, dict)
    ]


class AppleClient:
    def __init__(
        self,
        issuer_id: str,
        key_id: str,
        p8_pem: str,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] | None = None,
        base_url: str = BASE_URL,
        timeout: float = 60.0,
    ) -> None:
        load_private_key(p8_pem)  # fail fast on a bad key file
        register_secret(p8_pem)
        self._issuer_id = issuer_id
        self._key_id = key_id
        self._p8 = p8_pem
        self._clock = clock
        self._sleep = sleep or time.sleep
        self._token: str | None = None
        self._token_issued = 0.0
        self.tokens_signed = 0
        self._http = httpx.Client(base_url=base_url, transport=transport, timeout=timeout)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> AppleClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _auth_header(self, force: bool = False) -> str:
        now = self._clock()
        if force or self._token is None or now - self._token_issued >= TOKEN_REFRESH_AFTER:
            self._token = make_jwt(self._issuer_id, self._key_id, self._p8, now=now)
            self._token_issued = now
            self.tokens_signed += 1
        return f"Bearer {self._token}"

    def request(
        self,
        method: str,
        path: str,
        params: Mapping[str, str] | None = None,
        *,
        json: Mapping[str, Any] | None = None,
        purpose: str = "this request",
    ) -> httpx.Response:
        """Send a request; retry 429/5xx/network errors with backoff; raise AppleError otherwise."""
        reauthed = False
        attempt = 0
        while True:
            attempt += 1
            headers = {"Authorization": self._auth_header()}
            try:
                response = self._http.request(
                    method, path, params=params, json=json, headers=headers
                )
            except httpx.TransportError as exc:
                if attempt >= MAX_ATTEMPTS:
                    raise AppleError(
                        f"could not reach App Store Connect for {purpose}: "
                        f"{redact(type(exc).__name__ + ': ' + str(exc))}",
                        0,
                        [],
                    ) from None
                self._backoff(attempt, f"network error ({type(exc).__name__})")
                continue

            status = response.status_code
            if status < 400:
                return response
            errors = _parse_errors(response)
            if status == 401 and not reauthed:
                # Clock skew or an expired token: re-sign once before giving up.
                reauthed = True
                self._auth_header(force=True)
                attempt -= 1
                continue
            if status == 401:
                raise AppleAuthError(
                    "App Store Connect rejected the API key (HTTP 401). Check the issuer ID, "
                    "key ID and .p8 file, and that the key hasn't been revoked in App Store "
                    f"Connect > Users and Access > Integrations. {DOCS_HINT}",
                    status,
                    errors,
                )
            if status == 403:
                raise forbidden_error(purpose, errors)
            if (status == 429 or status >= 500) and attempt < MAX_ATTEMPTS:
                self._backoff(attempt, f"HTTP {status}")
                continue
            detail = " ".join(e.detail or e.title for e in errors) or response.reason_phrase
            raise AppleError(
                f"App Store Connect returned HTTP {status} for {purpose}: {redact(detail)}",
                status,
                errors,
            )

    def _backoff(self, attempt: int, why: str) -> None:
        delay = 2.0**attempt
        log.warning("App Store Connect %s; retrying in %.0fs", why, delay)
        self._sleep(delay)

    def get_json(
        self, path: str, params: Mapping[str, str] | None = None, *, purpose: str = "this request"
    ) -> dict[str, Any]:
        body = self.request("GET", path, params, purpose=purpose).json()
        if not isinstance(body, dict):
            raise AppleError(f"unexpected response for {purpose}", 200, [])
        return body

    def get_bytes(
        self, path: str, params: Mapping[str, str] | None = None, *, purpose: str = "this request"
    ) -> bytes:
        return self.request("GET", path, params, purpose=purpose).content

    def post_json(
        self, path: str, body: Mapping[str, Any], *, purpose: str = "this request"
    ) -> dict[str, Any]:
        result = self.request("POST", path, json=body, purpose=purpose).json()
        if not isinstance(result, dict):
            raise AppleError(f"unexpected response for {purpose}", 200, [])
        return result
