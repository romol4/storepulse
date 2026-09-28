"""Google service-account auth: RS256 JWT assertion exchanged for an OAuth access token."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from storepulse.core.secrets import redact, register_secret

SCOPES = (
    "https://www.googleapis.com/auth/devstorage.read_only",
    "https://www.googleapis.com/auth/playdeveloperreporting",
)
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"  # noqa: S105 (URL, not a secret)
ASSERTION_LIFETIME = 3600
REFRESH_MARGIN = 300  # refresh tokens 5 minutes before they expire
DOCS_HINT = "See README, 'Google service account'."


class GoogleCredentialError(Exception):
    """The service-account JSON is unusable. Messages never contain key material."""


@dataclass(frozen=True)
class ServiceAccount:
    client_email: str
    private_key: str
    token_uri: str
    project_id: str = ""

    def __repr__(self) -> str:  # never print the key
        return f"ServiceAccount(client_email={self.client_email!r})"


def load_service_account(text: str) -> ServiceAccount:
    """Parse and validate a service-account key file."""
    try:
        data = json.loads(text)
    except ValueError:
        raise GoogleCredentialError(
            f"the service account file is not valid JSON. {DOCS_HINT}"
        ) from None
    if not isinstance(data, dict) or data.get("type") != "service_account":
        raise GoogleCredentialError(
            'the file is not a service account key (expected "type": "service_account"); '
            f"create a JSON key for the service account in Google Cloud. {DOCS_HINT}"
        )
    missing = [f for f in ("client_email", "private_key") if not data.get(f)]
    if missing:
        raise GoogleCredentialError(
            f"the service account file is missing {', '.join(missing)}. {DOCS_HINT}"
        )
    private_key = str(data["private_key"])
    register_secret(private_key)
    try:
        key = load_pem_private_key(private_key.encode("utf-8"), password=None)
    except (ValueError, TypeError):
        raise GoogleCredentialError(
            f"the service account's private_key can't be read; download a new key. {DOCS_HINT}"
        ) from None
    if not isinstance(key, rsa.RSAPrivateKey):
        raise GoogleCredentialError(f"the service account key is not an RSA key. {DOCS_HINT}")
    return ServiceAccount(
        client_email=str(data["client_email"]),
        private_key=private_key,
        token_uri=str(data.get("token_uri") or DEFAULT_TOKEN_URI),
        project_id=str(data.get("project_id", "")),
    )


def make_assertion(sa: ServiceAccount, now: float) -> str:
    issued = int(now)
    payload = {
        "iss": sa.client_email,
        "scope": " ".join(SCOPES),
        "aud": sa.token_uri,
        "iat": issued,
        "exp": issued + ASSERTION_LIFETIME,
    }
    token = jwt.encode(payload, sa.private_key, algorithm="RS256", headers={"typ": "JWT"})
    register_secret(token)
    return token


class TokenError(Exception):
    """The token exchange failed. ``status`` is the HTTP status (0 for network errors)."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


class TokenSource:
    """Caches an access token and refreshes it shortly before expiry."""

    def __init__(
        self,
        sa: ServiceAccount,
        http: httpx.Client,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.sa = sa
        self._http = http
        self._clock = clock
        self._token: str | None = None
        self._expires_at = 0.0
        self.exchanges = 0

    def token(self, force: bool = False) -> str:
        now = self._clock()
        if force or self._token is None or now >= self._expires_at - REFRESH_MARGIN:
            self._token, lifetime = self._exchange(now)
            self._expires_at = now + lifetime
            self.exchanges += 1
        return self._token

    def _exchange(self, now: float) -> tuple[str, float]:
        form = {
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": make_assertion(self.sa, now),
        }
        try:
            response = self._http.post(self.sa.token_uri, data=form)
        except httpx.TransportError as exc:
            raise TokenError(
                f"could not reach Google's token endpoint: {type(exc).__name__}", 0
            ) from None
        if response.status_code != 200:
            detail = ""
            try:
                body = response.json()
                detail = f"{body.get('error', '')}: {body.get('error_description', '')}"
            except ValueError:
                detail = response.reason_phrase
            raise TokenError(
                f"Google rejected the service account key for {self.sa.client_email} "
                f"(HTTP {response.status_code}, {redact(detail)}). The key may have been "
                f"deleted or disabled in Google Cloud → IAM → Service accounts. {DOCS_HINT}",
                response.status_code,
            )
        try:
            body = response.json()
            token = str(body["access_token"])
            lifetime = float(body.get("expires_in", 3600))
        except (ValueError, KeyError, TypeError):
            raise TokenError(
                "Google's token endpoint returned an unexpected response", 200
            ) from None
        register_secret(token)
        return token, lifetime
