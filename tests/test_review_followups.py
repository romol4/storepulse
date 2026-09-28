"""Regression tests for the follow-up fixes after the second review round."""

from __future__ import annotations

from datetime import date

import pytest

from conftest import TODAY, FakeApple, apple_error
from storepulse.core.sources import apple_sales
from storepulse.core.sources.apple_client import AppleAuthError, AppleClient


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
