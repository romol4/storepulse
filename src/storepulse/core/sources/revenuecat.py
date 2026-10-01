"""RevenueCat API v2: the project metrics overview, pulled once per run.

docs/SPEC.md, RevenueCat (Phase 6b). Unlike every other source in this package, this one
has no report_date to re-pull: the metrics overview always reflects the current moment,
so one GET per run (core/runner.py's ``collect_revenuecat``) is the whole collection,
stored as one dated snapshot in the ``snapshots`` table (schema already in place since
Phase 1, unused until this phase) rather than a dated row in ``daily_metrics``.

The endpoint path, response shape, and the assumption that money metrics are reported in
USD below are a best-effort reading of RevenueCat's public v2 API docs
(https://www.revenuecat.com/docs/api-v2) -- not verified against a live project from this
environment, the same caveat every source module in this codebase carries (see
apple_analytics.py's docstring). Verify against a real project before relying on it. The
four metric ids collected ("mrr", "active_subscriptions", "active_trials", "revenue") are
the ones docs/SPEC.md's Phase 6b scope names; any other metric id the overview returns
(e.g. "new_customers", "active_users") is simply not collected, not an error -- there is
no fixed, exhaustive vocabulary to account for here the way apple_sales must account for
every product type.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from storepulse.core import db
from storepulse.core.secrets import redact, register_secret

log = logging.getLogger(__name__)

SOURCE = "revenuecat"
BASE_URL = "https://api.revenuecat.com"
MAX_ATTEMPTS = 3
DOCS_HINT = "See README, 'RevenueCat'."

MONEY_METRICS = frozenset({"mrr", "revenue"})
COUNT_METRICS = frozenset({"active_subscriptions", "active_trials"})
METRIC_IDS = MONEY_METRICS | COUNT_METRICS
# RevenueCat's own dashboard reports money metrics in the project's display currency,
# which defaults to USD; the overview response isn't confirmed to carry its own currency
# code, so a per-item "currency" field is used when present, and USD is assumed otherwise.
DEFAULT_CURRENCY = "USD"


class RevenueCatError(Exception):
    """A non-2xx response, or an unusable one, from RevenueCat. Never carries the key."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


class RevenueCatAuthError(RevenueCatError):
    """401/403/404: the key or project ID is wrong, or the key was revoked. Not retried."""


class RevenueCatParseError(ValueError):
    pass


class RevenueCatClient:
    def __init__(
        self,
        api_key: str,
        project_id: str,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] | None = None,
        base_url: str = BASE_URL,
        timeout: float = 30.0,
    ) -> None:
        register_secret(api_key)
        self._project_id = project_id
        self._sleep = sleep or time.sleep
        self._http = httpx.Client(
            base_url=base_url,
            transport=transport,
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> RevenueCatClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def fetch_overview(self) -> dict[str, Any]:
        """GET the project's metrics overview; retries 429/5xx/network errors with
        backoff, like the Apple and Google clients, and raises RevenueCatAuthError for a
        rejected key or an unknown project without retrying."""
        purpose = "fetching the RevenueCat project metrics overview"
        path = f"/v2/projects/{self._project_id}/metrics/overview"
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self._http.get(path)
            except httpx.TransportError as exc:
                if attempt >= MAX_ATTEMPTS:
                    raise RevenueCatError(
                        f"could not reach RevenueCat for {purpose}: "
                        f"{redact(type(exc).__name__ + ': ' + str(exc))}",
                        0,
                    ) from None
                self._backoff(attempt, f"network error ({type(exc).__name__})")
                continue

            status = response.status_code
            if status < 400:
                body = response.json()
                if not isinstance(body, dict):
                    raise RevenueCatError(f"unexpected response for {purpose}", status)
                return body
            if status in (401, 403):
                raise RevenueCatAuthError(
                    f"RevenueCat rejected the API key (HTTP {status}) for {purpose}. Check "
                    f"that the secret v2 API key is current and hasn't been revoked, in "
                    f"RevenueCat > Project settings > API keys. {DOCS_HINT}",
                    status,
                )
            if status == 404:
                raise RevenueCatAuthError(
                    f"RevenueCat returned HTTP 404 for {purpose}. Check the project ID. "
                    f"{DOCS_HINT}",
                    status,
                )
            if (status == 429 or status >= 500) and attempt < MAX_ATTEMPTS:
                self._backoff(attempt, f"HTTP {status}")
                continue
            raise RevenueCatError(
                f"RevenueCat returned HTTP {status} for {purpose}: {redact(response.text)}",
                status,
            )

    def _backoff(self, attempt: int, why: str) -> None:
        delay = 2.0**attempt
        log.warning("RevenueCat %s; retrying in %.0fs", why, delay)
        self._sleep(delay)


@dataclass(frozen=True)
class OverviewMetric:
    id: str
    value: float
    currency: str


def parse_overview(body: dict[str, Any]) -> list[OverviewMetric]:
    """The overview's items, filtered to the metric ids docs/SPEC.md's Phase 6b scope
    names. An item for any other metric id is silently skipped (see module docstring)."""
    items = body.get("items")
    if not isinstance(items, list):
        raise RevenueCatParseError("metrics overview response has no 'items' list")
    metrics: list[OverviewMetric] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        metric_id = str(item.get("id", ""))
        if metric_id not in METRIC_IDS:
            continue
        value = item.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RevenueCatParseError(f"metric {metric_id!r} has a non-numeric value: {value!r}")
        currency = str(item.get("currency") or DEFAULT_CURRENCY).upper()
        metrics.append(OverviewMetric(metric_id, float(value), currency))
    return metrics


def map_overview(metrics: list[OverviewMetric], taken_at: str) -> list[db.SnapshotRow]:
    """One project-wide snapshot row per metric (``app_id`` is NULL: this overview is
    account-wide, not per-app, docs/SPEC.md). Money metrics carry their currency; count
    metrics carry '' (daily_metrics' own convention for a non-money metric)."""
    return [
        db.SnapshotRow(
            taken_at=taken_at,
            app_id=None,
            metric=m.id,
            currency=m.currency if m.id in MONEY_METRICS else "",
            value=m.value,
        )
        for m in sorted(metrics, key=lambda m: m.id)
    ]
