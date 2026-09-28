"""Shared helpers for Play Console bulk report files."""

from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")


class ReportParseError(ValueError):
    pass


def pacific_today() -> date:
    return datetime.now(PACIFIC).date()


# -- text and numbers ------------------------------------------------------------------


def decode_csv(content: bytes) -> str:
    """Decode a Play CSV: UTF-16 (with or without BOM) or UTF-8."""
    if content.startswith((b"\xff\xfe", b"\xfe\xff")):
        codec = "utf-16"
    elif len(content) >= 2 and content[1] == 0 and content[0] != 0:
        codec = "utf-16-le"
    elif len(content) >= 2 and content[0] == 0 and content[1] != 0:
        codec = "utf-16-be"
    else:
        codec = "utf-8-sig"
    try:
        return content.decode(codec)
    except UnicodeDecodeError:
        raise ReportParseError(f"report is not valid {codec} text") from None


def read_csv(
    text: str, required: tuple[str, ...], what: str, *, any_of: tuple[str, ...] = ()
) -> list[dict[str, str]]:
    """Parse CSV text into rows.

    Raises ReportParseError if a ``required`` column is missing, or if ``any_of`` is
    given and none of those columns is present.
    """
    if not text.strip():
        raise ReportParseError(f"{what} is empty (no header row)")
    reader = csv.DictReader(io.StringIO(text))
    header = [h.strip() for h in (reader.fieldnames or [])]
    reader.fieldnames = header
    missing = [c for c in required if c not in header]
    if missing:
        raise ReportParseError(f"{what} is missing columns: {', '.join(missing)}")
    if any_of and not any(c in header for c in any_of):
        raise ReportParseError(f"{what} has none of the expected columns: {', '.join(any_of)}")
    rows: list[dict[str, str]] = []
    for line, raw in enumerate(reader, start=2):
        if None in raw:
            raise ReportParseError(f"{what} line {line}: too many fields")
        if not any((v or "").strip() for v in raw.values()):
            continue
        rows.append({k: (v or "").strip() for k, v in raw.items()})
    return rows


def parse_number(value: str, column: str, what: str) -> Decimal | None:
    """Parse a number like ``1,234.50``; an empty cell is missing (None), not zero."""
    text = value.strip().replace(",", "")
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        raise ReportParseError(f"{what}: {column} is not a number: {value!r}") from None


# Explicit English month names so parsing never depends on the system locale.
_MONTHS = {
    name: number
    for number, names in enumerate(
        [
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ],
        start=1,
    )
    for name in names
}
_ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_NAMED_RE = re.compile(r"^([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})$")


def parse_play_date(value: str) -> date:
    """Parse ``2026-09-01`` or ``Sep 1, 2026`` / ``September 1, 2026`` without strptime."""
    text = value.strip()
    iso = _ISO_RE.match(text)
    try:
        if iso:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        named = _NAMED_RE.match(text)
        if named and named.group(1).lower() in _MONTHS:
            return date(int(named.group(3)), _MONTHS[named.group(1).lower()], int(named.group(2)))
    except ValueError:
        pass
    raise ReportParseError(f"unrecognized date {value!r}")


# -- zip files -------------------------------------------------------------------------


def read_zip_csv(content: bytes, what: str) -> str:
    """Return the decoded text of the single CSV inside a Play report zip."""
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = [n for n in archive.namelist() if n.lower().endswith(".csv")]
            if len(names) != 1:
                raise ReportParseError(f"{what} should contain one CSV, found {len(names)}")
            return decode_csv(archive.read(names[0]))
    except zipfile.BadZipFile:
        raise ReportParseError(f"{what} is not a valid zip file") from None


# -- months ----------------------------------------------------------------------------


@dataclass(frozen=True, order=True)
class Month:
    year: int
    month: int

    @classmethod
    def of(cls, day: date) -> Month:
        return cls(day.year, day.month)

    @classmethod
    def parse(cls, text: str) -> Month:
        """``2026-09`` or ``202609``."""
        match = re.fullmatch(r"(\d{4})-?(\d{2})", text.strip())
        if not match or not 1 <= int(match.group(2)) <= 12:
            raise ValueError(f"expected YYYY-MM, got {text!r}")
        return cls(int(match.group(1)), int(match.group(2)))

    @property
    def key(self) -> str:
        return f"{self.year:04d}{self.month:02d}"

    @property
    def first_day(self) -> date:
        return date(self.year, self.month, 1)

    @property
    def last_day(self) -> date:
        return date.fromordinal(self.next().first_day.toordinal() - 1)

    def next(self) -> Month:
        return Month(self.year + self.month // 12, self.month % 12 + 1)

    def prev(self) -> Month:
        return Month(self.year - (self.month == 1), 12 if self.month == 1 else self.month - 1)

    def __str__(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"


def month_range(start: Month, end: Month) -> list[Month]:
    months: list[Month] = []
    current = start
    while current <= end:
        months.append(current)
        current = current.next()
    return months


def last_months(count: int, today: date) -> list[Month]:
    end = Month.of(today)
    start = end
    for _ in range(count - 1):
        start = start.prev()
    return month_range(start, end)


_MONTH_IN_NAME = re.compile(r"_(\d{6})(?:[_.]|$)")


def month_of_object(name: str) -> Month | None:
    match = _MONTH_IN_NAME.search(name.rsplit("/", 1)[-1])
    return Month.parse(match.group(1)) if match else None


MonthPlan = Literal["collect", "skip", "not_ready", "no_file"]


def plan_month(
    month: Month,
    available: set[Month],
    today: date,
    *,
    published_after_month_end: bool = False,
    ready_day: int = 15,
) -> MonthPlan:
    """Decide what to do with a month given which months have files.

    - ``skip``: nothing to collect (before the first file, or a month whose monthly
      report can't exist yet). Nothing is logged.
    - ``not_ready``: expected soon: the current month for daily-updated files, or last
      month's monthly report before ``ready_day`` of this month.
    - ``no_file``: a past month with no file (e.g. no sales that month).
    """
    if month in available:
        return "collect"
    current = Month.of(today)
    if month > current or not available or month < min(available):
        return "skip"
    if published_after_month_end:
        if month == current:
            return "skip"
        if month == current.prev() and today.day < ready_day:
            return "not_ready"
        return "no_file"
    return "not_ready" if month == current else "no_file"
