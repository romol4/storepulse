"""List apps from each configured credential."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Literal

from storepulse.core import db
from storepulse.core.sources.apple_client import AppleAgreementError, AppleAuthError, AppleClient
from storepulse.core.sources.apple_sales import sku_key
from storepulse.core.sources.google_client import GoogleClient, GoogleError

DiscoveryStatus = Literal["ok", "permission_denied"]


@dataclass
class DiscoveryResult:
    status: DiscoveryStatus
    apps: list[tuple[str, str]] = field(default_factory=list)  # (store_id, name)
    message: str = ""


def list_apple_apps(client: AppleClient) -> list[dict[str, str]]:
    """Page through GET /v1/apps. Raises AppleAuthError on 401/403."""
    apps: list[dict[str, str]] = []
    path: str | None = "/v1/apps"
    params: dict[str, str] | None = {"fields[apps]": "name,sku,bundleId", "limit": "200"}
    while path:
        body = client.get_json(path, params, purpose="listing apps (GET /v1/apps)")
        for item in body.get("data") or []:
            attrs = item.get("attributes") or {}
            apps.append(
                {
                    "id": str(item.get("id", "")),
                    "name": str(attrs.get("name", "")),
                    "sku": str(attrs.get("sku", "")),
                    "bundle_id": str(attrs.get("bundleId", "")),
                }
            )
        links = body.get("links") or {}
        path = links.get("next") or None
        params = None  # the next link already carries the query
    return apps


def discover_apple(client: AppleClient, conn: sqlite3.Connection) -> DiscoveryResult:
    """Register every app the key can see, plus its SKU for mapping IAP rows.

    A 403 is not fatal: apps are then registered from sales report rows instead.
    """
    try:
        apps = list_apple_apps(client)
    except AppleAuthError as exc:
        if exc.status != 403 or isinstance(exc, AppleAgreementError):
            raise
        return DiscoveryResult(
            "permission_denied",
            message=(
                "This key can't list apps (GET /v1/apps returned 403). Apps will be added "
                "from sales reports as they appear instead."
            ),
        )
    return DiscoveryResult("ok", apps=register_apple_apps(conn, apps))


def register_apple_apps(
    conn: sqlite3.Connection, apps: list[dict[str, str]]
) -> list[tuple[str, str]]:
    registered: list[tuple[str, str]] = []
    for app in apps:
        if not app["id"]:
            continue
        name = app["name"] or app["bundle_id"] or app["id"]
        db.upsert_app(conn, "ios", app["id"], name)
        if app["sku"]:
            db.set_kv(conn, sku_key(app["sku"]), app["id"])
        registered.append((app["id"], name))
    return registered


def discover_play(client: GoogleClient, conn: sqlite3.Connection) -> DiscoveryResult:
    """Register every Play app the service account can see (Reporting API apps:search).

    Auth errors propagate: the caller explains a pending-permission 403.
    """
    return DiscoveryResult("ok", apps=register_play_apps(conn, client.search_apps()))


def register_play_apps(
    conn: sqlite3.Connection, apps: list[dict[str, str]]
) -> list[tuple[str, str]]:
    registered: list[tuple[str, str]] = []
    for app in apps:
        if not app["package"]:
            continue
        name = app["name"] or app["package"]
        db.upsert_app(conn, "android", app["package"], name)
        registered.append((app["package"], name))
    return registered


_INSTALLS_OBJECT = re.compile(r"^stats/installs/installs_(.+)_\d{6}_[a-z_]+\.csv$")


def packages_in_bucket(client: GoogleClient, bucket: str) -> list[str]:
    """Packages that have installs files, from the bucket listing alone."""
    names = (o.name for o in client.list_objects(bucket, "stats/installs/installs_"))
    return sorted({m.group(1) for n in names if (m := _INSTALLS_OBJECT.match(n))})


def refresh_play_apps(
    client: GoogleClient, conn: sqlite3.Connection, bucket: str
) -> DiscoveryResult:
    """Re-run Play discovery so apps launched after setup are collected too.

    Uses the Reporting API's app list; if that fails (API disabled, permission pending),
    falls back to the packages visible in the bucket's installs files. The fallback only
    adds unknown packages, so it never renames apps discovered with a display name.
    """
    try:
        return discover_play(client, conn)
    except GoogleError as exc:
        reason = str(exc)
    known = db.apps_for(conn, "android")
    added = [
        (package, package) for package in packages_in_bucket(client, bucket) if package not in known
    ]
    for package, name in added:
        db.upsert_app(conn, "android", package, name)
    return DiscoveryResult(
        "permission_denied",
        apps=added,
        message=f"couldn't list apps with the Reporting API, used the bucket instead: {reason}",
    )
