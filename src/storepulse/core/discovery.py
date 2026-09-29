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


# -- app pairing (hosted mode only; docs/SPEC.md, Data sources → App discovery) -----------


def _normalized(name: str) -> str:
    return " ".join(name.split()).casefold()


def pair_apps(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Auto-pair iOS and Android apps whose store names match exactly (case and
    whitespace aside), one app per platform. Returns the (iOS, Android) names paired now.

    - Only the store-reported ``name`` is compared, never a user's ``display_name``.
    - An app a user paired or un-paired (``pair_source = 'user'``) is never touched.
    - Auto pairs are re-evaluated each time: one whose names no longer match 1:1 (a
      store rename, or a second same-named app appearing) is dropped back to undecided.
    Local mode never calls this, so its apps stay unpaired.
    """
    apps = db.list_apps(conn)
    by_name: dict[str, dict[str, list[db.AppInfo]]] = {}
    for app in apps:
        by_name.setdefault(_normalized(app.name), {}).setdefault(app.platform, []).append(app)

    wanted: dict[int, str] = {}  # app id -> auto pair_key
    for name, platforms in by_name.items():
        ios, android = platforms.get("ios", []), platforms.get("android", [])
        if len(ios) == 1 and len(android) == 1:
            if "user" in (ios[0].pair_source, android[0].pair_source):
                continue
            auto_key = f"auto:{name}"
            wanted[ios[0].id] = auto_key
            wanted[android[0].id] = auto_key

    paired: list[tuple[str, str]] = []
    with db.transaction(conn):
        for app in apps:
            if app.pair_source == "user":
                continue
            key = wanted.get(app.id)
            if key is not None and app.pair_key != key:
                db.set_app_pair(conn, app.id, key, "auto")
                if app.platform == "ios":
                    paired.append((app.name, app.name))
            elif key is None and app.pair_source == "auto":
                # No longer an unambiguous match: back to undecided.
                conn.execute(
                    "UPDATE apps SET pair_key = NULL, pair_source = NULL WHERE id = ?", (app.id,)
                )
    return paired


def pair_manually(conn: sqlite3.Connection, ios_id: int, android_id: int) -> None:
    """A user's pairing from Settings. Any previous partner of either app is released
    back to undecided, so auto-pairing may still match it elsewhere."""
    apps = {a.id: a for a in db.list_apps(conn)}
    ios, android = apps.get(ios_id), apps.get(android_id)
    if ios is None or android is None or ios.platform != "ios" or android.platform != "android":
        raise ValueError("pairing needs one iOS app and one Android app")
    key = f"user:{ios_id}-{android_id}"
    with db.transaction(conn):
        for app in (ios, android):
            if app.pair_key and app.pair_key != key:
                for other in apps.values():
                    if other.pair_key == app.pair_key and other.id not in (ios_id, android_id):
                        conn.execute(
                            "UPDATE apps SET pair_key = NULL, pair_source = NULL WHERE id = ?",
                            (other.id,),
                        )
        db.set_app_pair(conn, ios_id, key, "user")
        db.set_app_pair(conn, android_id, key, "user")


def unpair(conn: sqlite3.Connection, app_id: int) -> None:
    """A user's un-pairing from Settings: both apps stay unpaired, and auto-pairing
    won't re-pair them (a deliberate non-pairing, ``pair_source = 'user'``)."""
    apps = db.list_apps(conn)
    target = next((a for a in apps if a.id == app_id), None)
    if target is None:
        raise ValueError(f"no app {app_id}")
    members = [a for a in apps if target.pair_key and a.pair_key == target.pair_key] or [target]
    with db.transaction(conn):
        for app in members:
            db.set_app_pair(conn, app.id, None, "user")
