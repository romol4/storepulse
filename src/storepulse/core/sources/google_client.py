"""Google Cloud Storage (Play bulk reports) and Play Developer Reporting API client."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any
from urllib.parse import quote

import httpx

from storepulse.core.secrets import redact
from storepulse.core.sources.google_auth import (
    DOCS_HINT,
    ServiceAccount,
    TokenError,
    TokenSource,
)

log = logging.getLogger(__name__)

GCS_BASE = "https://storage.googleapis.com/storage/v1"
REPORTING_BASE = "https://playdeveloperreporting.googleapis.com/v1beta1"
REPORTING_TZ = "America/Los_Angeles"

BULK_PERMISSION = "View app information and download bulk reports"
FINANCIAL_PERMISSION = "View financial data, orders, and cancellation survey responses"
# Bucket prefixes that need the financial permission (granted globally).
FINANCIAL_PREFIXES = ("sales/", "earnings/")
# 403 reasons that are Cloud project problems, not Play permissions.
HARD_403_REASONS = frozenset(
    {"SERVICE_DISABLED", "ACCESS_TOKEN_SCOPE_INSUFFICIENT", "accessNotConfigured"}
)


def permission_for(object_name: str) -> str:
    return FINANCIAL_PERMISSION if object_name.startswith(FINANCIAL_PREFIXES) else BULK_PERMISSION


MAX_ATTEMPTS = 3


class GoogleError(Exception):
    """A failed Google API call. Never carries credential material."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


class GoogleAuthError(GoogleError):
    """401/403 or a failed token exchange. Not retried.

    ``pending_permission`` is set for a 403 after a successful token exchange: the key is
    valid, but Play Console hasn't granted access (yet). New service accounts can take
    a day or two before Play permissions take effect.
    """

    def __init__(self, message: str, status: int, pending_permission: bool = False) -> None:
        super().__init__(message, status)
        self.pending_permission = pending_permission


def parse_bucket_uri(uri: str) -> str:
    """``gs://pubsite_prod_123/`` → ``pubsite_prod_123``."""
    text = uri.strip()
    if not text.startswith("gs://"):
        raise ValueError(
            "the bucket URI should look like gs://pubsite_prod_…; copy it from Play Console → "
            f"Download reports → Copy Cloud Storage URI. {DOCS_HINT}"
        )
    bucket = text[len("gs://") :].strip("/").split("/", 1)[0]
    if not bucket:
        raise ValueError(f"the bucket URI has no bucket name. {DOCS_HINT}")
    return bucket


@dataclass(frozen=True)
class GcsObject:
    name: str
    size: int


def _date_json(day: date) -> dict[str, Any]:
    return {"year": day.year, "month": day.month, "day": day.day, "timeZone": {"id": REPORTING_TZ}}


def _json_date(value: Mapping[str, Any]) -> date:
    return date(int(value["year"]), int(value["month"]), int(value["day"]))


class GoogleClient:
    def __init__(
        self,
        sa: ServiceAccount,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.sa = sa
        self._http = httpx.Client(transport=transport, timeout=timeout)
        self._sleep = sleep or time.sleep
        self.tokens = TokenSource(sa, self._http, clock)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> GoogleClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def check_token(self) -> None:
        """Exchange the key for a token; raises GoogleAuthError on failure."""
        self._token()

    def _token(self, force: bool = False) -> str:
        try:
            return self.tokens.token(force=force)
        except TokenError as exc:
            raise GoogleAuthError(str(exc), exc.status) from None

    def _denied(
        self, status: int, purpose: str, response: httpx.Response, permission: str
    ) -> GoogleAuthError:
        """Map 401/403 to the right fix, always keeping Google's own message."""
        email = self.sa.client_email
        said = redact(_error_text(response))
        said_text = f" Google said: {_as_sentence(said)}" if said else ""
        if status == 401:
            return GoogleAuthError(
                f"Google rejected the access token for {purpose} (HTTP 401). "
                f"Check the service account key.{said_text} {DOCS_HINT}",
                status,
            )
        reasons = _error_reasons(response)
        if reasons & HARD_403_REASONS:
            # Not a Play permission: e.g. the Reporting API isn't enabled in the Cloud
            # project. Google's message says exactly what to do (and links to it).
            return GoogleAuthError(
                f"Google denied {purpose} (HTTP 403) for {email}: "
                f"{_as_sentence(said or 'access denied')} "
                "This is a Google Cloud project setting, not a Play Console permission. "
                f"{DOCS_HINT}",
                status,
            )
        return GoogleAuthError(
            f"Google denied {purpose} (HTTP 403) for {email}. The key is valid, so this is a "
            f"Play Console permission: in Users and permissions, give this email "
            f"'{permission}'. If you just did, new service accounts can take 24-48 hours "
            f"before Play permissions take effect.{said_text} {DOCS_HINT}",
            status,
            pending_permission=True,
        )

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        json: Mapping[str, Any] | None = None,
        purpose: str = "this request",
        permission: str = BULK_PERMISSION,
    ) -> httpx.Response:
        reauthed = False
        attempt = 0
        while True:
            attempt += 1
            headers = {"Authorization": f"Bearer {self._token()}"}
            try:
                response = self._http.request(
                    method, url, params=params, json=json, headers=headers
                )
            except httpx.TransportError as exc:
                if attempt >= MAX_ATTEMPTS:
                    raise GoogleError(
                        f"could not reach Google for {purpose}: {type(exc).__name__}", 0
                    ) from None
                self._backoff(attempt, f"network error ({type(exc).__name__})")
                continue
            status = response.status_code
            if status < 400:
                return response
            if status == 401 and not reauthed:
                reauthed = True
                self._token(force=True)
                attempt -= 1
                continue
            if status in (401, 403):
                raise self._denied(status, purpose, response, permission)
            if (status == 429 or status >= 500) and attempt < MAX_ATTEMPTS:
                self._backoff(attempt, f"HTTP {status}")
                continue
            raise GoogleError(
                f"Google returned HTTP {status} for {purpose}: {redact(_error_text(response))}",
                status,
            )

    def _backoff(self, attempt: int, why: str) -> None:
        delay = 2.0**attempt
        log.warning("Google %s; retrying in %.0fs", why, delay)
        self._sleep(delay)

    # -- Cloud Storage -----------------------------------------------------------------

    def list_objects(
        self, bucket: str, prefix: str, *, max_results: int | None = None
    ) -> list[GcsObject]:
        """List object metadata under a prefix. ``max_results`` stops after one page of at
        most that many objects (a cheap "can I see anything here?" probe)."""
        url = f"{GCS_BASE}/b/{quote(bucket, safe='')}/o"
        params: dict[str, str] = {"prefix": prefix, "fields": "items(name,size),nextPageToken"}
        if max_results is not None:
            params["maxResults"] = str(max_results)
        found: list[GcsObject] = []
        while True:
            body = self.request(
                "GET",
                url,
                params=params,
                purpose=f"listing gs://{bucket}/{prefix}",
                permission=permission_for(prefix),
            ).json()
            for item in body.get("items") or []:
                found.append(GcsObject(str(item["name"]), int(item.get("size", 0))))
            token = body.get("nextPageToken")
            if not token or max_results is not None:
                return found
            params = {**params, "pageToken": str(token)}

    def download(self, bucket: str, name: str) -> bytes:
        url = f"{GCS_BASE}/b/{quote(bucket, safe='')}/o/{quote(name, safe='')}"
        return self.request(
            "GET",
            url,
            params={"alt": "media"},
            purpose=f"downloading gs://{bucket}/{name}",
            permission=permission_for(name),
        ).content

    # -- Play Developer Reporting API ----------------------------------------------------

    def search_apps(self) -> list[dict[str, str]]:
        url = f"{REPORTING_BASE}/apps:search"
        params: dict[str, str] = {"pageSize": "1000"}
        apps: list[dict[str, str]] = []
        while True:
            body = self.request("GET", url, params=params, purpose="listing Play apps").json()
            for app in body.get("apps") or []:
                apps.append(
                    {
                        "package": str(app.get("packageName", "")),
                        "name": str(app.get("displayName", "")),
                    }
                )
            token = body.get("nextPageToken")
            if not token:
                return apps
            params = {**params, "pageToken": str(token)}

    def latest_daily_date(self, package: str, metric_set: str) -> date | None:
        """Last complete day with data for a metric set (DAILY freshness), if any.

        ``latestEndTime`` is the exclusive end of the newest aggregation period, so the
        last complete day is the day before it.
        """
        url = f"{REPORTING_BASE}/apps/{package}/{metric_set}"
        body = self.request("GET", url, purpose=f"{metric_set} freshness for {package}").json()
        for entry in (body.get("freshnessInfo") or {}).get("freshnesses") or []:
            if entry.get("aggregationPeriod") == "DAILY" and entry.get("latestEndTime"):
                return _json_date(entry["latestEndTime"]) - timedelta(days=1)
        return None

    def query_metric_set(
        self, package: str, metric_set: str, metrics: list[str], start: date, end: date
    ) -> dict[date, dict[str, float]]:
        """DAILY values for ``start``..``end`` inclusive, keyed by day then metric name."""
        url = f"{REPORTING_BASE}/apps/{package}/{metric_set}:query"
        body: dict[str, Any] = {
            "timelineSpec": {
                "aggregationPeriod": "DAILY",
                "startTime": _date_json(start),
                "endTime": _date_json(end + timedelta(days=1)),  # exclusive
            },
            "metrics": metrics,
            "pageSize": 1000,
        }
        values: dict[date, dict[str, float]] = {}
        while True:
            page = self.request(
                "POST", url, json=body, purpose=f"querying {metric_set} for {package}"
            ).json()
            for row in page.get("rows") or []:
                day = _json_date(row["startTime"])
                for metric in row.get("metrics") or []:
                    raw = (metric.get("decimalValue") or {}).get("value")
                    if raw in (None, ""):
                        continue
                    values.setdefault(day, {})[str(metric["metric"])] = float(raw)
            token = page.get("nextPageToken")
            if not token:
                return values
            body = {**body, "pageToken": str(token)}


def _as_sentence(text: str) -> str:
    """End ``text`` with one full stop, so the hint appended after it reads correctly."""
    return text if text.endswith((".", "!", "?")) else f"{text}."


def _error_reasons(response: httpx.Response) -> set[str]:
    """Reasons from ``error.details[].reason`` (google.rpc.ErrorInfo) and GCS's
    ``error.errors[].reason``."""
    try:
        body = response.json()
    except ValueError:
        return set()
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return set()
    reasons: set[str] = set()
    for key in ("details", "errors"):
        for item in error.get(key) or []:
            if isinstance(item, dict) and item.get("reason"):
                reasons.add(str(item["reason"]))
    return reasons


def _error_text(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.reason_phrase
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error.get("status") or "")
    return str(error or response.reason_phrase)
