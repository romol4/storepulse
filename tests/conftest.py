from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import httpx
import keyring
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from keyring.backend import KeyringBackend

from storepulse.core import db

FIXTURES = Path(__file__).parent / "fixtures"
APPLE_FIXTURES = FIXTURES / "apple_sales"
APPLE_SUBSCRIPTIONS_FIXTURES = FIXTURES / "apple_subscriptions"
APPLE_SUBSCRIPTION_EVENTS_FIXTURES = FIXTURES / "apple_subscription_events"


def fixture_bytes(name: str) -> bytes:
    return (APPLE_FIXTURES / name).read_bytes()


def subscriptions_fixture_bytes(name: str) -> bytes:
    return (APPLE_SUBSCRIPTIONS_FIXTURES / name).read_bytes()


def subscription_events_fixture_bytes(name: str) -> bytes:
    return (APPLE_SUBSCRIPTION_EVENTS_FIXTURES / name).read_bytes()


@pytest.fixture(scope="session")
def p8_pem() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


@pytest.fixture
def conn() -> Iterator[db.sqlite3.Connection]:
    c = db.connect(":memory:")
    yield c
    c.close()


class MemoryKeyring(KeyringBackend):
    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.store.pop((service, username), None)


@pytest.fixture
def memory_keyring() -> Iterator[MemoryKeyring]:
    previous = keyring.get_keyring()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(previous)


@pytest.fixture(autouse=True)
def reset_logging() -> Iterator[None]:
    """`main()` configures the storepulse logger; don't let that leak between tests."""
    logger = logging.getLogger("storepulse")
    saved = (list(logger.handlers), logger.level, logger.propagate)
    yield
    logger.handlers[:], logger.level, logger.propagate = saved


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("STOREPULSE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("STOREPULSE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("STOREPULSE_PASSPHRASE", raising=False)
    monkeypatch.delenv("STOREPULSE_PASSPHRASE_FILE", raising=False)
    for name in ("STOREPULSE_MODE", "STOREPULSE_MASTER_KEY", "STOREPULSE_MASTER_KEY_FILE"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def apple_error(status: int, detail: str, code: str = "") -> httpx.Response:
    body = {"errors": [{"status": str(status), "code": code, "title": "", "detail": detail}]}
    return httpx.Response(status, content=json.dumps(body).encode())


NO_SALES_DETAIL = "There were no sales for the date specified."
NOT_READY_DETAIL = (
    "Report is not available yet. Daily reports for the Americas are available by 5 am "
    "Pacific Time."
)


@dataclass
class FakeApple:
    """Mock App Store Connect. Sales responses keyed by report date; default is a report.

    Every /v1/salesReports report type (SALES, SUBSCRIPTION, SUBSCRIPTION_EVENT) shares
    this one endpoint, distinguished only by filter[reportType], so the handler branches
    on it (defaulting to "SALES" when absent, so every existing test — which never set
    this field — keeps working unmodified).
    """

    apps: list[dict[str, str]] = field(default_factory=list)
    apps_status: int = 200
    sales: dict[str, httpx.Response | Callable[[], httpx.Response]] = field(default_factory=dict)
    default_report: bytes | None = None
    subscriptions: dict[str, httpx.Response | Callable[[], httpx.Response]] = field(
        default_factory=dict
    )
    default_subscriptions_report: bytes | None = None
    subscription_events: dict[str, httpx.Response | Callable[[], httpx.Response]] = field(
        default_factory=dict
    )
    default_subscription_events_report: bytes | None = None
    requests: list[httpx.Request] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/v1/apps":
            if self.apps_status != 200:
                return apple_error(self.apps_status, "forbidden", "FORBIDDEN_ERROR")
            data = [
                {
                    "type": "apps",
                    "id": a["id"],
                    "attributes": {"name": a["name"], "sku": a["sku"], "bundleId": a["bundle"]},
                }
                for a in self.apps
            ]
            return httpx.Response(200, json={"data": data, "links": {}})
        if request.url.path == "/v1/salesReports":
            report_type = request.url.params.get("filter[reportType]", "SALES")
            responses, default = {
                "SALES": (self.sales, self.default_report),
                "SUBSCRIPTION": (self.subscriptions, self.default_subscriptions_report),
                "SUBSCRIPTION_EVENT": (
                    self.subscription_events,
                    self.default_subscription_events_report,
                ),
            }[report_type]
            day = request.url.params["filter[reportDate]"]
            response = responses.get(day)
            if callable(response):
                return response()
            if response is not None:
                return response
            if default is not None:
                return httpx.Response(200, content=default)
            return apple_error(404, NO_SALES_DETAIL, "NOT_FOUND")
        return httpx.Response(404)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def sales_dates(self) -> list[str]:
        return [
            r.url.params["filter[reportDate]"]
            for r in self.requests
            if r.url.path == "/v1/salesReports"
        ]

    def dates_for(self, report_type: str) -> list[str]:
        """Like sales_dates(), scoped to one filter[reportType] (SALES, SUBSCRIPTION, ...)."""
        return [
            r.url.params["filter[reportDate]"]
            for r in self.requests
            if r.url.path == "/v1/salesReports"
            and r.url.params.get("filter[reportType]", "SALES") == report_type
        ]


FT_APPS = [
    {"id": "1000000001", "name": "deskFT", "sku": "DESKFT", "bundle": "com.example.deskft"},
    {"id": "1000000002", "name": "billFT", "sku": "BILLFT", "bundle": "com.example.billft"},
]

TODAY = date(2026, 9, 27)


# -- Google ---------------------------------------------------------------------------------

PLAY_FIXTURES = FIXTURES / "play"
PKG = "com.example.billft"
BUCKET = "pubsite_prod_1234567890"
TOKEN_URI = "https://oauth2.googleapis.com/token"


def play_bytes(name: str) -> bytes:
    return (PLAY_FIXTURES / name).read_bytes()


@pytest.fixture(scope="session")
def rsa_pem() -> str:
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


@pytest.fixture(scope="session")
def sa_json(rsa_pem: str) -> str:
    return json.dumps(
        {
            "type": "service_account",
            "project_id": "storepulse-test",
            "private_key_id": "abc123",
            "private_key": rsa_pem,
            "client_email": "reports@storepulse-test.iam.gserviceaccount.com",
            "client_id": "1234567890",
            "token_uri": TOKEN_URI,
        }
    )


def google_error(status: int, message: str = "denied") -> httpx.Response:
    return httpx.Response(
        status, json={"error": {"code": status, "message": message, "status": "DENIED"}}
    )


@dataclass
class FakeGoogle:
    """Mock token endpoint, GCS JSON API and Play Developer Reporting API."""

    objects: dict[str, bytes] = field(default_factory=dict)
    apps: list[dict[str, str]] = field(
        default_factory=lambda: [{"packageName": PKG, "displayName": "billFT"}]
    )
    token_status: int = 200
    gcs_status: int = 200
    reporting_status: int = 200
    freshness: dict[str, object] | None = None
    queries: dict[str, dict[str, object]] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)
    tokens_issued: int = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host, path = request.url.host, request.url.path
        if host == "oauth2.googleapis.com":
            if self.token_status != 200:
                return httpx.Response(
                    self.token_status,
                    json={"error": "invalid_grant", "error_description": "Invalid JWT Signature."},
                )
            self.tokens_issued += 1
            return httpx.Response(
                200, json={"access_token": f"ya29.fake-{self.tokens_issued}", "expires_in": 3599}
            )
        if host == "storage.googleapis.com":
            if self.gcs_status != 200:
                return google_error(self.gcs_status)
            prefix = f"/storage/v1/b/{BUCKET}/o"
            if path == prefix:
                want = request.url.params.get("prefix", "")
                items = [
                    {"name": n, "size": str(len(b))}
                    for n, b in sorted(self.objects.items())
                    if n.startswith(want)
                ]
                return httpx.Response(200, json={"items": items})
            if path.startswith(prefix + "/"):
                from urllib.parse import unquote

                name = unquote(path[len(prefix) + 1 :])
                if name in self.objects:
                    return httpx.Response(200, content=self.objects[name])
                return google_error(404, "No such object")
            return google_error(404, "bucket not found")
        if host == "playdeveloperreporting.googleapis.com":
            if self.reporting_status != 200:
                return google_error(self.reporting_status)
            if path == "/v1beta1/apps:search":
                return httpx.Response(200, json={"apps": self.apps})
            for metric_set in ("crashRateMetricSet", "anrRateMetricSet"):
                base = f"/v1beta1/apps/{PKG}/{metric_set}"
                if path == base:
                    return httpx.Response(
                        200, json=self.freshness or json.loads(play_bytes("vitals_freshness.json"))
                    )
                if path == base + ":query":
                    default = (
                        "vitals_crash_query.json"
                        if "crash" in metric_set.lower()
                        else ("vitals_anr_query.json")
                    )
                    body = self.queries.get(metric_set) or json.loads(play_bytes(default))
                    return httpx.Response(200, json=body)
            return google_error(404, "not found")
        return httpx.Response(599)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def downloads(self) -> list[str]:
        from urllib.parse import unquote

        return [
            unquote(r.url.path.rsplit("/o/", 1)[1])
            for r in self.requests
            if r.url.host == "storage.googleapis.com" and "/o/" in r.url.path
        ]


def standard_objects() -> dict[str, bytes]:
    """The synthetic bucket: September installs + sales, August earnings."""
    return {
        f"stats/installs/installs_{PKG}_202609_overview.csv": play_bytes(
            f"installs_{PKG}_202609_overview.csv"
        ),
        f"stats/installs/installs_{PKG}_202609_country.csv": play_bytes(
            f"installs_{PKG}_202609_country.csv"
        ),
        "sales/salesreport_202609.zip": play_bytes("salesreport_202609.zip"),
        "earnings/earnings_202608_1234567890-0.zip": play_bytes("earnings_202608_1234567890-0.zip"),
    }


def router(apple: FakeApple | None = None, google: FakeGoogle | None = None) -> httpx.MockTransport:
    """One transport serving both fakes, dispatched by host."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.appstoreconnect.apple.com":
            return apple.handler(request) if apple else httpx.Response(599)
        return google.handler(request) if google else httpx.Response(599)

    return httpx.MockTransport(handle)
