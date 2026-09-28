"""Play Console earnings reports (``earnings/earnings_YYYYMM_*.zip``): final proceeds.

Published once, early in the following month. Amounts are in the merchant (payout)
currency and include Google's fee and tax lines, so the sum is net proceeds. Loading a
month replaces that month's provisional ``play_sales`` rows in the same transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from storepulse.core import db
from storepulse.core.sources.play_common import (
    Month,
    month_of_object,
    parse_number,
    parse_play_date,
    read_csv,
)

SOURCE = "play_earnings"
PREFIX = "earnings/earnings_"
REQUIRED = (
    "Transaction Date",
    "Product id",
    "Buyer Country",
    "Merchant Currency",
    "Amount (Merchant Currency)",
)


def done_key(month: Month) -> str:
    return f"play_earnings_done:{month.key}"


@dataclass
class MappedMonth:
    rows: list[db.MetricRow]
    unmapped_rows: int = 0
    out_of_month_rows: int = 0

    def note(self) -> str | None:
        parts = []
        if self.unmapped_rows:
            parts.append(f"{self.unmapped_rows} rows without a known package")
        if self.out_of_month_rows:
            parts.append(f"{self.out_of_month_rows} rows dated outside the month")
        return "; ".join(parts) or None


def month_files(names: list[str]) -> dict[Month, list[str]]:
    files: dict[Month, list[str]] = {}
    for name in names:
        month = month_of_object(name)
        if month is not None and name.startswith(PREFIX) and name.endswith(".zip"):
            files.setdefault(month, []).append(name)
    return files


def map_month(month: Month, texts: list[str], packages: dict[str, int], what: str) -> MappedMonth:
    result = MappedMonth(rows=[])
    totals: dict[tuple[str, int, str, str], Decimal] = {}
    for text in texts:
        for raw in read_csv(text, REQUIRED, what):
            amount = parse_number(
                raw["Amount (Merchant Currency)"], "Amount (Merchant Currency)", what
            )
            if amount is None:
                continue
            day = parse_play_date(raw["Transaction Date"])
            if Month.of(day) != month:
                result.out_of_month_rows += 1
                continue
            app_id = packages.get(raw["Product id"])
            if app_id is None:
                result.unmapped_rows += 1
                continue
            country = raw["Buyer Country"].upper() or "ZZ"
            key = (day.isoformat(), app_id, country, raw["Merchant Currency"].upper())
            totals[key] = totals.get(key, Decimal(0)) + amount
    result.rows = [
        db.MetricRow(day, app_id, country, "proceeds", currency, float(value))
        for (day, app_id, country, currency), value in sorted(totals.items())
        if value != 0
    ]
    return result
