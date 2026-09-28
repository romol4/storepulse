from __future__ import annotations

import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from conftest import apple_error
from storepulse.core.sources.apple_client import (
    TOKEN_LIFETIME,
    AppleAuthError,
    AppleClient,
    AppleError,
    make_jwt,
)


class Clock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _bearer(request: httpx.Request) -> str:
    return request.headers["Authorization"].removeprefix("Bearer ")


def test_jwt_claims_and_signature(p8_pem: str) -> None:
    token = make_jwt("issuer-123", "ABC123DEFG", p8_pem, now=time.time())
    public = load_pem_private_key(p8_pem.encode(), None).public_key()
    header = jwt.get_unverified_header(token)
    assert header["alg"] == "ES256" and header["kid"] == "ABC123DEFG"
    claims = jwt.decode(
        token,
        public,  # type: ignore[arg-type]
        algorithms=["ES256"],
        audience="appstoreconnect-v1",
        options={"verify_exp": False},
    )
    assert claims["iss"] == "issuer-123"
    assert claims["exp"] - claims["iat"] == TOKEN_LIFETIME
    assert TOKEN_LIFETIME <= 20 * 60


def test_bad_p8_fails_without_echoing_it() -> None:
    bogus = "-----BEGIN PRIVATE KEY-----\nTOTALLYSECRETBYTES\n-----END PRIVATE KEY-----\n"
    with pytest.raises(ValueError) as info:
        AppleClient("i", "k", bogus)
    assert "TOTALLYSECRETBYTES" not in str(info.value)


def test_token_resigned_during_long_backfill(p8_pem: str) -> None:
    clock = Clock()
    seen: list[str] = []
    ages: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        token = _bearer(request)
        claims = jwt.decode(token, options={"verify_signature": False})
        # Apple would reject an expired token; make the mock do the same.
        if clock.now >= claims["exp"]:
            return apple_error(401, "expired")
        ages.append(clock.now - claims["iat"])
        seen.append(token)
        return httpx.Response(200, json={})

    client = AppleClient(
        "i", "k", p8_pem, transport=httpx.MockTransport(handler), clock=clock, sleep=lambda _: None
    )
    # 365 requests, ~5 s apart (≈30 minutes), well past one token's lifetime.
    for _ in range(365):
        client.get_json("/v1/ping")
        clock.now += 5
    assert client.tokens_signed >= 3
    assert len(set(seen)) == client.tokens_signed
    # Every request succeeded with a token less than 15 minutes old.
    assert len(seen) == 365
    assert max(ages) < 15 * 60


def test_token_reused_within_window(p8_pem: str) -> None:
    clock = Clock()
    client = AppleClient(
        "i",
        "k",
        p8_pem,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
        clock=clock,
    )
    client.get_json("/a")
    clock.now += 14 * 60
    client.get_json("/b")
    assert client.tokens_signed == 1
    clock.now += 60
    client.get_json("/c")
    assert client.tokens_signed == 2


def test_401_resigns_once_then_succeeds(p8_pem: str) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(_bearer(request))
        return apple_error(401, "nope") if len(calls) == 1 else httpx.Response(200, json={})

    clock = Clock()
    client = AppleClient("i", "k", p8_pem, transport=httpx.MockTransport(handler), clock=clock)
    clock.now += 1  # a fresh token has a different iat, so a different signature input
    assert client.get_json("/v1/apps") == {}
    assert len(calls) == 2 and client.tokens_signed == 2


def test_401_twice_fails_fast_with_hint(p8_pem: str) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return apple_error(401, "NOT_AUTHORIZED")

    client = AppleClient("i", "k", p8_pem, transport=httpx.MockTransport(handler))
    with pytest.raises(AppleAuthError) as info:
        client.get_json("/v1/apps")
    assert calls == 2
    message = str(info.value)
    assert "issuer ID" in message and "README" in message
    assert "PRIVATE KEY" not in message and "eyJ" not in message


def test_403_names_role_and_leaks_nothing(p8_pem: str) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return apple_error(403, "forbidden " + request.headers["Authorization"])

    client = AppleClient("i", "k", p8_pem, transport=httpx.MockTransport(handler))
    with pytest.raises(AppleAuthError) as info:
        client.get_json("/v1/salesReports", purpose="the sales report")
    assert calls == 1  # not retried
    assert "Sales and Reports" in str(info.value)
    assert "eyJ" not in str(info.value)
    body = p8_pem.splitlines()[1]
    assert body not in str(info.value)


@pytest.mark.parametrize("status", [429, 500, 503])
def test_retries_with_backoff(p8_pem: str, status: int) -> None:
    sleeps: list[float] = []
    responses = [httpx.Response(status), httpx.Response(status), httpx.Response(200, json={})]
    client = AppleClient(
        "i",
        "k",
        p8_pem,
        transport=httpx.MockTransport(lambda r: responses.pop(0)),
        sleep=sleeps.append,
    )
    assert client.get_json("/x") == {}
    assert sleeps == [2.0, 4.0]


def test_gives_up_after_three_attempts(p8_pem: str) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(502)

    client = AppleClient(
        "i", "k", p8_pem, transport=httpx.MockTransport(handler), sleep=lambda _: None
    )
    with pytest.raises(AppleError) as info:
        client.get_json("/x")
    assert calls == 3 and info.value.status == 502


def test_network_errors_retried(p8_pem: str) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json={"ok": True})

    client = AppleClient(
        "i", "k", p8_pem, transport=httpx.MockTransport(handler), sleep=lambda _: None
    )
    assert client.get_json("/x") == {"ok": True}
