from __future__ import annotations

import sqlite3

import httpx

from conftest import apple_error
from storepulse.core import db, discovery
from storepulse.core.sources.apple_client import AppleClient


def _app(i: int) -> dict[str, object]:
    return {"type": "apps", "id": str(i), "attributes": {"name": f"App {i}", "sku": f"SKU{i}"}}


def test_paginates_and_maps_skus(conn: sqlite3.Connection, p8_pem: str) -> None:
    pages = {
        "/v1/apps": {
            "data": [_app(1), _app(2)],
            "links": {"next": "https://api.appstoreconnect.apple.com/v1/apps?cursor=2"},
        },
        "/v1/apps?cursor=2": {"data": [_app(3)], "links": {}},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.url.path + (
            f"?{request.url.query.decode()}" if b"cursor" in request.url.query else ""
        )
        return httpx.Response(200, json=pages[key])

    client = AppleClient("i", "k", p8_pem, transport=httpx.MockTransport(handler))
    result = discovery.discover_apple(client, conn)
    assert result.status == "ok"
    assert [name for _, name in result.apps] == ["App 1", "App 2", "App 3"]
    assert conn.execute("SELECT COUNT(*) FROM apps").fetchone()[0] == 3
    assert db.get_kv(conn, "apple_sku:SKU3") == "3"


def test_403_falls_back_cleanly(conn: sqlite3.Connection, p8_pem: str) -> None:
    client = AppleClient(
        "i", "k", p8_pem, transport=httpx.MockTransport(lambda r: apple_error(403, "forbidden"))
    )
    result = discovery.discover_apple(client, conn)
    assert result.status == "permission_denied"
    assert "sales reports" in result.message
    assert conn.execute("SELECT COUNT(*) FROM apps").fetchone()[0] == 0
