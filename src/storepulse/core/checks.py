"""Credential checks shared by `init`, `doctor` and the hosted setup flow (before saving,
and any time after)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal

from storepulse.core import discovery
from storepulse.core.sources import (
    apple_sales,
    play_earnings,
    play_installs,
    play_sales,
    play_vitals,
)
from storepulse.core.sources.apple_client import (
    AppleAgreementError,
    AppleAuthError,
    AppleClient,
    AppleError,
    AppleVendorError,
)
from storepulse.core.sources.google_client import (
    FINANCIAL_PERMISSION,
    GoogleAuthError,
    GoogleClient,
    GoogleError,
)
from storepulse.core.sources.revenuecat import (
    RevenueCatClient,
    RevenueCatError,
    RevenueCatParseError,
    parse_overview,
)

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
        # A plain 403 may just mean the role can't list apps; an agreement 403 blocks
        # everything, so it fails here with Apple's reason.
        if exc.status != 403 or isinstance(exc, AppleAgreementError):
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
        # The exception already names the fix (role or agreement) and Apple's reason.
        result.checks.append(Check("fail", str(exc)))
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


def check_google(client: GoogleClient, bucket: str) -> CheckResult:
    """Token, bucket (installs, plus the optional financial prefixes), and app listing.

    Reads object metadata only; no report file is downloaded.
    """
    result = CheckResult()
    try:
        client.check_token()
        result.checks.append(Check("ok", f"Google key accepted for {client.sa.client_email}."))
    except GoogleError as exc:
        result.checks.append(_google_fail(exc))
        return result
    try:
        if client.list_objects(bucket, play_installs.PREFIX, max_results=1):
            result.checks.append(Check("ok", f"Bucket gs://{bucket}: installs reports visible."))
        else:
            result.checks.append(
                Check(
                    "warn",
                    f"Bucket gs://{bucket} is readable but has no installs reports yet. New "
                    "accounts and apps take a day or two; otherwise check the bucket URI.",
                )
            )
    except GoogleError as exc:
        result.checks.append(_google_fail(exc))
    for prefix, what in ((play_sales.PREFIX, "sales"), (play_earnings.PREFIX, "earnings")):
        try:
            visible = bool(client.list_objects(bucket, prefix, max_results=1))
        except GoogleAuthError as exc:
            if not exc.pending_permission:
                result.checks.append(_google_fail(exc))
                continue
            visible = False
        except GoogleError as exc:
            result.checks.append(_google_fail(exc))
            continue
        if visible:
            result.checks.append(Check("ok", f"Android {what} reports visible."))
        else:
            # Optional: without it everything else works, but Android revenue is missing.
            result.checks.append(
                Check(
                    "warn",
                    f"No Android {what} reports visible. If the account has no Android "
                    "revenue yet, ignore this. Otherwise Android revenue won't be collected "
                    f"until the service account has the optional '{FINANCIAL_PERMISSION}' "
                    "permission (global) in Play Console > Users and permissions.",
                )
            )
    try:
        result.apps = client.search_apps()
        level: Level = "ok" if result.apps else "warn"
        result.checks.append(
            Check(level, f"Play Developer Reporting API: {len(result.apps)} app(s) visible.")
        )
    except GoogleError as exc:
        result.checks.append(_google_fail(exc))
    return result


def check_revenuecat(client: RevenueCatClient) -> CheckResult:
    """Fetch the project metrics overview once, live, before anything is saved (same
    pattern as check_apple/check_google: a validated credential, or a clear failure)."""
    result = CheckResult()
    try:
        metrics = parse_overview(client.fetch_overview())
    except (RevenueCatError, RevenueCatParseError) as exc:
        result.checks.append(Check("fail", str(exc)))
        return result
    result.checks.append(Check("ok", f"RevenueCat key accepted; {len(metrics)} metric(s) found."))
    return result


def check_vitals(client: GoogleClient, packages: list[str]) -> list[Check]:
    """Freshness of both vitals metric sets for each package (metadata only)."""
    checks: list[Check] = []
    for package in packages:
        for metric_set in play_vitals.METRIC_SETS:
            try:
                latest = client.latest_daily_date(package, metric_set)
            except GoogleError as exc:
                checks.append(_google_fail(exc))
                continue
            if latest is None:
                checks.append(Check("warn", f"{package} {metric_set}: no daily data reported yet."))
            else:
                checks.append(Check("ok", f"{package} {metric_set}: data through {latest}."))
    return checks
