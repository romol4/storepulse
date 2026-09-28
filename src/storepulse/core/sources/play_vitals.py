"""Play Developer Reporting API vitals: daily and 28-day user-weighted crash/ANR rates."""

from __future__ import annotations

from datetime import date

from storepulse.core import db

SOURCE = "play_vitals"

# API metric → our metric. The 28-day user-weighted rates are what Google's
# bad-behavior thresholds (1.09% crash, 0.47% ANR) apply to; alerts should use them.
METRIC_SETS: dict[str, dict[str, str]] = {
    "crashRateMetricSet": {
        "userPerceivedCrashRate": "crash_rate",
        "userPerceivedCrashRate28dUserWeighted": "crash_rate_28d",
        "distinctUsers": "vitals_users",
    },
    "anrRateMetricSet": {
        "userPerceivedAnrRate": "anr_rate",
        "userPerceivedAnrRate28dUserWeighted": "anr_rate_28d",
    },
}


def to_metric_rows(
    app_id: int, results: dict[str, dict[date, dict[str, float]]]
) -> list[db.MetricRow]:
    """Map per-metric-set query results to rows. Missing values produce no row."""
    rows: list[db.MetricRow] = []
    for metric_set, by_day in results.items():
        mapping = METRIC_SETS[metric_set]
        for day, values in sorted(by_day.items()):
            for api_metric, value in sorted(values.items()):
                metric = mapping.get(api_metric)
                if metric is not None:
                    rows.append(db.MetricRow(day.isoformat(), app_id, "ALL", metric, "", value))
    return rows
