from __future__ import annotations

import json
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


def fixture_bytes(name: str) -> bytes:
    return (APPLE_FIXTURES / name).read_bytes()


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
def isolated_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("STOREPULSE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("STOREPULSE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("STOREPULSE_PASSPHRASE", raising=False)
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
    """Mock App Store Connect. Sales responses keyed by report date; default is a report."""

    apps: list[dict[str, str]] = field(default_factory=list)
    apps_status: int = 200
    sales: dict[str, httpx.Response | Callable[[], httpx.Response]] = field(default_factory=dict)
    default_report: bytes | None = None
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
            day = request.url.params["filter[reportDate]"]
            response = self.sales.get(day)
            if callable(response):
                return response()
            if response is not None:
                return response
            if self.default_report is not None:
                return httpx.Response(200, content=self.default_report)
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


FT_APPS = [
    {"id": "1000000001", "name": "deskFT", "sku": "DESKFT", "bundle": "com.example.deskft"},
    {"id": "1000000002", "name": "billFT", "sku": "BILLFT", "bundle": "com.example.billft"},
]

TODAY = date(2026, 9, 27)
