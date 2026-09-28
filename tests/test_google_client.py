from __future__ import annotations

import json
from datetime import date

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from conftest import BUCKET, PKG, TOKEN_URI, FakeGoogle, google_error
from storepulse.core.sources.google_auth import (
    SCOPES,
    GoogleCredentialError,
    load_service_account,
    make_assertion,
)
from storepulse.core.sources.google_client import (
    GoogleAuthError,
    GoogleClient,
    GoogleError,
    parse_bucket_uri,
)


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


def _client(sa_json: str, fake: FakeGoogle, clock: Clock | None = None) -> GoogleClient:
    return GoogleClient(
        load_service_account(sa_json),
        transport=fake.transport,
        clock=clock or Clock(),
        sleep=lambda _: None,
    )


def test_assertion_claims_verify(sa_json: str, rsa_pem: str) -> None:
    sa = load_service_account(sa_json)
    token = make_assertion(sa, 1_800_000_000)
    public = load_pem_private_key(rsa_pem.encode(), None).public_key()
    claims = jwt.decode(
        token,
        public,  # type: ignore[arg-type]
        algorithms=["RS256"],
        audience=TOKEN_URI,
        options={"verify_exp": False, "verify_iat": False},
    )
    assert claims["iss"] == sa.client_email
    assert claims["scope"].split() == list(SCOPES)
    assert claims["exp"] - claims["iat"] == 3600


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        json.dumps({"type": "authorized_user"}),
        json.dumps({"type": "service_account", "client_email": "a@b"}),
        json.dumps({"type": "service_account", "client_email": "a@b", "private_key": "junk"}),
    ],
)
def test_bad_service_account_files(text: str) -> None:
    with pytest.raises(GoogleCredentialError) as info:
        load_service_account(text)
    assert "README" in str(info.value)


def test_repr_hides_key(sa_json: str) -> None:
    sa = load_service_account(sa_json)
    assert "PRIVATE" not in repr(sa)


def test_token_cached_and_refreshed(sa_json: str) -> None:
    fake = FakeGoogle()
    clock = Clock()
    client = _client(sa_json, fake, clock)
    client.search_apps()
    client.search_apps()
    assert fake.tokens_issued == 1
    clock.now += 3599 - 300  # within 5 minutes of expiry
    client.search_apps()
    assert fake.tokens_issued == 2
    # The token was sent as a bearer and the assertion used the jwt-bearer grant.
    token_req = next(r for r in fake.requests if r.url.host == "oauth2.googleapis.com")
    assert b"grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Ajwt-bearer" in token_req.content
    api_req = fake.requests[-1]
    assert api_req.headers["Authorization"] == "Bearer ya29.fake-2"


def test_token_failure_names_the_account_and_hides_key(sa_json: str, rsa_pem: str) -> None:
    fake = FakeGoogle(token_status=400)
    with pytest.raises(GoogleAuthError) as info:
        _client(sa_json, fake).check_token()
    message = str(info.value)
    assert "reports@storepulse-test.iam.gserviceaccount.com" in message
    assert "invalid_grant" in message and "README" in message
    assert rsa_pem.splitlines()[1] not in message
    assert not info.value.pending_permission


def test_403_after_token_is_pending_permission(sa_json: str) -> None:
    fake = FakeGoogle(gcs_status=403)
    with pytest.raises(GoogleAuthError) as info:
        _client(sa_json, fake).list_objects(BUCKET, "stats/")
    assert info.value.pending_permission
    assert "24-48 hours" in str(info.value)
    assert "View app information and download bulk reports" in str(info.value)


def test_401_refreshes_token_once(sa_json: str) -> None:
    fake = FakeGoogle()
    calls = {"n": 0}
    base = fake.handler

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "playdeveloperreporting.googleapis.com":
            calls["n"] += 1
            if calls["n"] == 1:
                return google_error(401, "expired")
        return base(request)

    client = GoogleClient(load_service_account(sa_json), transport=httpx.MockTransport(handler))
    assert client.search_apps() == [{"package": PKG, "name": "billFT"}]
    assert fake.tokens_issued == 2


def test_retries_5xx_then_gives_up(sa_json: str) -> None:
    fake = FakeGoogle(reporting_status=503)
    sleeps: list[float] = []
    client = GoogleClient(
        load_service_account(sa_json), transport=fake.transport, sleep=sleeps.append
    )
    with pytest.raises(GoogleError) as info:
        client.search_apps()
    assert info.value.status == 503 and sleeps == [2.0, 4.0]


def test_gcs_list_paginates_and_download_quotes(sa_json: str) -> None:
    fake = FakeGoogle(objects={"sales/salesreport_202609.zip": b"zip-bytes"})
    pages = iter(
        [
            {"items": [{"name": "a", "size": "1"}], "nextPageToken": "p2"},
            {"items": [{"name": "b", "size": "2"}]},
        ]
    )
    base = fake.handler

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/storage/v1/b/{BUCKET}/o":
            return httpx.Response(200, json=next(pages))
        return base(request)

    client = GoogleClient(load_service_account(sa_json), transport=httpx.MockTransport(handler))
    assert [o.name for o in client.list_objects(BUCKET, "x/")] == ["a", "b"]
    assert client.download(BUCKET, "sales/salesreport_202609.zip") == b"zip-bytes"
    assert "sales%2Fsalesreport_202609.zip" in str(fake.requests[-1].url)


def test_freshness_and_query(sa_json: str) -> None:
    fake = FakeGoogle()
    client = _client(sa_json, fake)
    assert client.latest_daily_date(PKG, "crashRateMetricSet") == date(2026, 9, 22)
    values = client.query_metric_set(
        PKG,
        "crashRateMetricSet",
        ["userPerceivedCrashRate"],
        date(2026, 9, 20),
        date(2026, 9, 22),
    )
    assert values[date(2026, 9, 20)]["userPerceivedCrashRate"] == pytest.approx(0.0142)
    assert "userPerceivedCrashRate" not in values[date(2026, 9, 22)]
    body = json.loads(fake.requests[-1].content)
    assert body["timelineSpec"]["aggregationPeriod"] == "DAILY"
    assert body["timelineSpec"]["endTime"]["day"] == 23  # exclusive end
    assert body["timelineSpec"]["startTime"]["timeZone"]["id"] == "America/Los_Angeles"


def test_parse_bucket_uri() -> None:
    assert parse_bucket_uri("gs://pubsite_prod_123/") == "pubsite_prod_123"
    assert parse_bucket_uri(" gs://pubsite_prod_123/stats ") == "pubsite_prod_123"
    for bad in ("pubsite_prod_123", "gs://", "https://storage.googleapis.com/x"):
        with pytest.raises(ValueError):
            parse_bucket_uri(bad)
