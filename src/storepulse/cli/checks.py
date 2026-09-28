"""Credential checks shared by `init` (before saving) and `doctor` (any time)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal

from storepulse.core import discovery
from storepulse.core.sources import apple_sales, play_installs, play_vitals
from storepulse.core.sources.apple_client import (
    AppleAuthError,
    AppleClient,
    AppleError,
    AppleVendorError,
)
from storepulse.core.sources.google_client import GoogleAuthError, GoogleClient, GoogleError

# Old enough that the report should exist, recent enough that Apple still keeps it.
APPLE_PROBE_DAYS_AGO = 3

Level = Literal["ok", "warn", "fail"]


@dataclass(frozen=True)
class Check:
    level: Level
    text: str
    pending_permission: bool = False


@dataclass
class CheckResult:
    checks: list[Check] = field(default_factory=list)
    apps: list[dict[str, str]] = field(default_factory=list)

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.level == "fail"]


def check_apple(client: AppleClient, vendor_number: str, today: date) -> CheckResult:
    result = CheckResult()
    # 1. Does the key work at all? (401 → wrong key, key ID or issuer ID.)
    try:
        result.apps = discovery.list_apple_apps(client)
        result.checks.append(Check("ok", f"Apple key accepted; {len(result.apps)} app(s) visible."))
    except AppleAuthError as exc:
        if exc.status != 403:
            result.checks.append(Check("fail", str(exc)))
            return result
        result.checks.append(
            Check(
                "warn",
                "Apple key accepted, but it can't list apps (GET /v1/apps returned 403). "
                "Apps will be added from sales reports as they appear.",
            )
        )
    except AppleError as exc:
        result.checks.append(Check("fail", str(exc)))
        return result

    # 2. Does it have Sales and Reports, and is the vendor number right?
    probe = today - timedelta(days=APPLE_PROBE_DAYS_AGO)
    try:
        fetched = apple_sales.fetch_day(client, vendor_number, probe, today=today)
    except AppleVendorError as exc:
        result.checks.append(Check("fail", str(exc)))
        return result
    except AppleAuthError as exc:
        if exc.status == 403:
            text = (
                "the key was accepted but can't download sales reports (HTTP 403): it lacks "
                "the Sales and Reports role. Edit the key in App Store Connect > Users and "
                "Access > Integrations. See README, 'Apple API key'."
            )
        else:
            text = str(exc)
        result.checks.append(Check("fail", text))
        return result
    except AppleError as exc:
        result.checks.append(Check("fail", str(exc)))
        return result
    if fetched.status == "ok":
        text = f"Sales report for {probe} downloaded; vendor number confirmed."
        result.checks.append(Check("ok", text))
    elif fetched.status == "no_sales":
        text = f"No sales on {probe}, but the report request was accepted."
        result.checks.append(Check("ok", text))
    elif fetched.status == "not_ready":
        text = f"Report for {probe} isn't ready yet; request was accepted."
        result.checks.append(Check("ok", text))
    else:
        result.checks.append(
            Check(
                "warn",
                "couldn't fully confirm the vendor number; the first backfill will tell. "
                f"(Apple returned 404 for {probe} without saying why.)",
            )
        )
    return result


def _google_fail(exc: GoogleError) -> Check:
    return Check(
        "fail",
        str(exc),
        pending_permission=isinstance(exc, GoogleAuthError) and exc.pending_permission,
    )


def check_google(
    client: GoogleClient, bucket: str, *, packages: list[str] | None = None
) -> CheckResult:
    """Token, bucket, app listing; with ``packages``, also vitals freshness per app."""
    result = CheckResult()
    try:
        client.check_token()
        result.checks.append(Check("ok", f"Google key accepted for {client.sa.client_email}."))
    except GoogleError as exc:
        result.checks.append(_google_fail(exc))
        return result
    try:
        objects = client.list_objects(bucket, play_installs.PREFIX)
        result.checks.append(
            Check("ok", f"Bucket gs://{bucket} readable ({len(objects)} installs files).")
        )
    except GoogleError as exc:
        result.checks.append(_google_fail(exc))
    try:
        result.apps = client.search_apps()
        result.checks.append(
            Check("ok", f"Play Developer Reporting API: {len(result.apps)} app(s) visible.")
        )
    except GoogleError as exc:
        result.checks.append(_google_fail(exc))
    for package in packages or []:
        for metric_set in play_vitals.METRIC_SETS:
            try:
                latest = client.latest_daily_date(package, metric_set)
            except GoogleError as exc:
                result.checks.append(_google_fail(exc))
                continue
            if latest is None:
                text = f"{package} {metric_set}: no daily data reported yet."
                result.checks.append(Check("warn", text))
            else:
                text = f"{package} {metric_set}: data through {latest}."
                result.checks.append(Check("ok", text))
    return result
