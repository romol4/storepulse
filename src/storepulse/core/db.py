"""SQLite schema, migrations and upserts. All database access goes through here."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Collection, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

# Fixed metric vocabulary (docs/SPEC.md, Storage). Unknown metrics are rejected.
METRICS: frozenset[str] = frozenset(
    {
        "installs",
        "redownloads",
        "uninstalls",
        "active_devices",
        "proceeds",
        "iap_units",
        "impressions",
        "page_views",
        "crash_rate",
        "anr_rate",
        "crash_rate_28d",
        "anr_rate_28d",
        "vitals_users",
    }
)

INGEST_STATUSES: frozenset[str] = frozenset({"ok", "not_ready", "error"})

_MIGRATION_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class UnknownMetricError(ValueError):
    pass


@dataclass(frozen=True)
class MetricRow:
    date: str
    app_id: int
    country: str
    metric: str
    currency: str
    value: float


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _migrations() -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for entry in resources.files("storepulse.core.migrations").iterdir():
        match = _MIGRATION_RE.match(entry.name)
        if match:
            found.append((int(match.group(1)), entry.read_text(encoding="utf-8")))
    found.sort()
    return found


def connect(path: Path | str) -> sqlite3.Connection:
    """Open the database, enable WAL and foreign keys, and apply pending migrations."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    migrate(conn)
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'kv'"
    ).fetchone()
    if not exists:
        return 0
    value = get_kv(conn, "schema_version")
    return int(value) if value else 0


def migrate(conn: sqlite3.Connection) -> int:
    """Apply numbered migrations newer than the stored schema version. Returns the version."""
    current = schema_version(conn)
    for number, sql in _migrations():
        if number <= current:
            continue
        with transaction(conn):
            for statement in _split_sql(sql):
                conn.execute(statement)
            set_kv(conn, "schema_version", str(number))
        current = number
    return current


def _split_sql(sql: str) -> list[str]:
    lines = [line for line in sql.splitlines() if not line.lstrip().startswith("--")]
    return [stmt.strip() for stmt in "\n".join(lines).split(";") if stmt.strip()]


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def get_kv(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row["value"])


def set_kv(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO kv (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def upsert_app(conn: sqlite3.Connection, platform: str, store_id: str, name: str) -> int:
    """Insert or rename an app, returning its id."""
    row = conn.execute(
        "INSERT INTO apps (name, platform, store_id) VALUES (?, ?, ?) "
        "ON CONFLICT (platform, store_id) DO UPDATE SET name = excluded.name "
        "RETURNING id",
        (name, platform, store_id),
    ).fetchone()
    return int(row["id"])


def find_app(conn: sqlite3.Connection, platform: str, store_id: str) -> int | None:
    row = conn.execute(
        "SELECT id FROM apps WHERE platform = ? AND store_id = ?", (platform, store_id)
    ).fetchone()
    return None if row is None else int(row["id"])


def _validate(row: MetricRow) -> None:
    if row.metric not in METRICS:
        raise UnknownMetricError(f"unknown metric {row.metric!r}")
    if not _DATE_RE.match(row.date):
        raise ValueError(f"date must be YYYY-MM-DD, got {row.date!r}")


def count_source_day(conn: sqlite3.Connection, source: str, date: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM daily_metrics WHERE source = ? AND date = ?", (source, date)
    ).fetchone()
    return int(row["n"])


def replace_source_day(
    conn: sqlite3.Connection, source: str, date: str, rows: Iterable[MetricRow]
) -> int:
    """Replace one source's rows for one date in a single transaction.

    Deleting first means countries that dropped to zero don't linger; the upsert means
    duplicate keys within a batch overwrite instead of failing. Returns rows written.
    """
    batch = list(rows)
    for row in batch:
        _validate(row)
        if row.date != date:
            raise ValueError(f"row date {row.date} does not match {date}")
    now = utc_now()
    with transaction(conn):
        conn.execute("DELETE FROM daily_metrics WHERE source = ? AND date = ?", (source, date))
        conn.executemany(
            "INSERT INTO daily_metrics "
            "(date, app_id, country, metric, currency, value, source, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (date, app_id, country, metric, currency) DO UPDATE SET "
            "value = excluded.value, source = excluded.source, "
            "updated_at = excluded.updated_at",
            [
                (r.date, r.app_id, r.country, r.metric, r.currency, r.value, source, now)
                for r in batch
            ],
        )
    return len(batch)


def replace_source_range(
    conn: sqlite3.Connection,
    source: str,
    start: str,
    end: str,
    rows: Iterable[MetricRow],
    *,
    app_ids: Collection[int] | None = None,
    clear_sources: Collection[str] = (),
    kv: dict[str, str] | None = None,
) -> int:
    """Replace a source's rows for ``start``..``end`` (inclusive) in one transaction.

    ``app_ids`` limits the delete to those apps (for per-app files). ``clear_sources``
    also deletes other sources' rows in the range, e.g. provisional ``play_sales`` rows
    once the final ``play_earnings`` report arrives. ``kv`` entries are set in the same
    transaction. Returns rows written.
    """
    batch = list(rows)
    for row in batch:
        _validate(row)
        if not start <= row.date <= end:
            raise ValueError(f"row date {row.date} is outside {start}..{end}")
        if app_ids is not None and row.app_id not in app_ids:
            raise ValueError(f"row for app {row.app_id} is outside the replaced apps")
    sources = [source, *clear_sources]
    where = f"source IN ({', '.join('?' for _ in sources)}) AND date BETWEEN ? AND ?"
    params: list[object] = [*sources, start, end]
    if app_ids is not None:
        ids = list(app_ids)
        where += f" AND app_id IN ({', '.join('?' for _ in ids)})" if ids else " AND 0"
        params += ids
    now = utc_now()
    with transaction(conn):
        conn.execute(f"DELETE FROM daily_metrics WHERE {where}", params)  # noqa: S608
        conn.executemany(
            "INSERT INTO daily_metrics "
            "(date, app_id, country, metric, currency, value, source, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (date, app_id, country, metric, currency) DO UPDATE SET "
            "value = excluded.value, source = excluded.source, "
            "updated_at = excluded.updated_at",
            [
                (r.date, r.app_id, r.country, r.metric, r.currency, r.value, source, now)
                for r in batch
            ],
        )
        for key, value in (kv or {}).items():
            set_kv(conn, key, value)
    return len(batch)


def count_rows(
    conn: sqlite3.Connection,
    source: str,
    start: str,
    end: str,
    app_ids: Collection[int] | None = None,
) -> int:
    sql = "SELECT COUNT(*) AS n FROM daily_metrics WHERE source = ? AND date BETWEEN ? AND ?"
    params: list[object] = [source, start, end]
    if app_ids is not None:
        ids = list(app_ids) or [-1]
        sql += f" AND app_id IN ({', '.join('?' for _ in ids)})"
        params += ids
    return int(conn.execute(sql, params).fetchone()["n"])


def apps_for(conn: sqlite3.Connection, platform: str) -> dict[str, int]:
    """store_id → app id for one platform."""
    rows = conn.execute("SELECT id, store_id FROM apps WHERE platform = ?", (platform,))
    return {str(r["store_id"]): int(r["id"]) for r in rows}


def log_ingest(
    conn: sqlite3.Connection,
    *,
    source: str,
    report_date: str,
    started_at: str,
    status: str,
    rows: int | None = None,
    note: str | None = None,
) -> None:
    """Record the outcome of one source/date pull. ``note`` goes in the ``error`` column."""
    if status not in INGEST_STATUSES:
        raise ValueError(f"unknown ingest status {status!r}")
    conn.execute(
        "INSERT INTO ingest_log (source, report_date, started_at, finished_at, status, rows, error)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (source, report_date, started_at, utc_now(), status, rows, note),
    )
