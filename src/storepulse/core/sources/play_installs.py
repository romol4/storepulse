"""Play Console installs reports (``stats/installs/``): daily installs, uninstalls, devices."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from storepulse.core import db
from storepulse.core.sources.play_common import (
    Month,
    month_of_object,
    parse_number,
    parse_play_date,
    read_csv,
)

SOURCE = "play_installs"
PREFIX = "stats/installs/installs_"

# User-based columns, to match Play Console's default "User acquisitions" / "User losses".
COLUMNS = {
    "Daily User Installs": "installs",
    "Daily User Uninstalls": "uninstalls",
    "Active Device Installs": "active_devices",
}
UNKNOWN_COUNTRY = "ZZ"  # user-assigned ISO code for rows with no country


@dataclass(frozen=True)
class InstallsRow:
    day: str
    country: str
    values: dict[str, Decimal] = field(default_factory=dict)


def object_prefix(package: str) -> str:
    return f"{PREFIX}{package}_"


def month_files(names: list[str], package: str) -> dict[Month, dict[str, str]]:
    """Map month → {"overview": name, "country": name} for one package."""
    prefix = object_prefix(package)
    files: dict[Month, dict[str, str]] = {}
    for name in names:
        if not name.startswith(prefix):
            continue
        month = month_of_object(name)
        if month is None:
            continue
        for kind in ("overview", "country"):
            if name.endswith(f"_{kind}.csv"):
                files.setdefault(month, {})[kind] = name
    return files


def parse_installs(text: str, *, by_country: bool, what: str) -> list[InstallsRow]:
    """Parse an overview or country installs CSV.

    An empty cell means the value is missing, not zero: no metric is produced for it.
    """
    required = ("Date", "Country") if by_country else ("Date",)
    rows = read_csv(text, required, what, any_of=tuple(COLUMNS))
    parsed: list[InstallsRow] = []
    for raw in rows:
        day = parse_play_date(raw["Date"]).isoformat()
        country = "ALL"
        if by_country:
            country = raw["Country"].upper() or UNKNOWN_COUNTRY
        values: dict[str, Decimal] = {}
        for column, metric in COLUMNS.items():
            number = parse_number(raw.get(column, ""), column, what)
            if number is not None:
                values[metric] = number
        parsed.append(InstallsRow(day, country, values))
    return parsed


def to_metric_rows(
    app_id: int, overview: list[InstallsRow], country: list[InstallsRow]
) -> list[db.MetricRow]:
    """Combine both files into one row set: ALL rows from the overview, plus per-country
    rows. Readers use the ALL row when present and only sum countries otherwise."""
    totals: dict[tuple[str, str, str], Decimal] = {}
    for row in [*overview, *country]:
        for metric, value in row.values.items():
            key = (row.day, row.country, metric)
            totals[key] = totals.get(key, Decimal(0)) + value
    return [
        db.MetricRow(day, app_id, country_code, metric, "", float(value))
        for (day, country_code, metric), value in sorted(totals.items())
    ]
