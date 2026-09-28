"""Play Console sales reports (``sales/salesreport_YYYYMM.zip``): provisional proceeds.

Updated daily, so the current month has data. Amounts are item prices (excluding tax)
in the buyer's currency, before Google's fee: provisional until the month's earnings
report replaces them (see play_earnings).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal

from storepulse.core import db
from storepulse.core.sources.play_common import (
    Month,
    month_of_object,
    parse_number,
    parse_play_date,
    read_csv,
    read_zip_csv,
)

SOURCE = "play_sales"
PREFIX = "sales/salesreport_"
REQUIRED = (
    "Order Charged Date",
    "Financial Status",
    "Product ID",
    "Currency of Sale",
    "Item Price",
    "Country of Buyer",
)


@dataclass
class MappedMonth:
    rows: list[db.MetricRow]
    unmapped_rows: int = 0
    out_of_month_rows: int = 0
    ignored_statuses: Counter[str] = field(default_factory=Counter)

    def note(self) -> str | None:
        parts = []
        if self.unmapped_rows:
            parts.append(f"{self.unmapped_rows} rows for unknown packages")
        if self.out_of_month_rows:
            parts.append(f"{self.out_of_month_rows} rows dated outside the month")
        if self.ignored_statuses:
            statuses = ", ".join(f"{s} x{n}" for s, n in sorted(self.ignored_statuses.items()))
            parts.append(f"ignored statuses: {statuses}")
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
            status = raw["Financial Status"]
            price = parse_number(raw["Item Price"], "Item Price", what)
            if price is None:
                continue
            lowered = status.lower()
            if "refund" in lowered:
                # Subtract the magnitude, whichever sign Google records refunds with.
                amount = -abs(price)
            elif lowered == "charged":
                amount = price
            else:
                result.ignored_statuses[status or "(empty)"] += 1
                continue
            day = parse_play_date(raw["Order Charged Date"])
            if Month.of(day) != month:
                result.out_of_month_rows += 1
                continue
            app_id = packages.get(raw["Product ID"])
            if app_id is None:
                result.unmapped_rows += 1
                continue
            country = raw["Country of Buyer"].upper() or "ZZ"
            key = (day.isoformat(), app_id, country, raw["Currency of Sale"].upper())
            totals[key] = totals.get(key, Decimal(0)) + amount
    result.rows = [
        db.MetricRow(day, app_id, country, "proceeds", currency, float(value))
        for (day, app_id, country, currency), value in sorted(totals.items())
        if value != 0
    ]
    return result


def read_texts(contents: list[bytes], what: str) -> list[str]:
    return [read_zip_csv(content, what) for content in contents]
