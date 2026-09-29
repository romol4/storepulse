"""SQLite schema, migrations and upserts. All database access goes through here."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Collection, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

# Fixed metric vocabulary (docs/SPEC.md, Storage). Unknown metrics are rejected.
METRICS: frozenset[str] = frozenset(
    {
        "installs",
        "redownloads",
        "uninstalls",
        "active_devices",
        "proceeds",
        "sales_gross",
        "iap_units",
        "impressions",
        "page_views",
        "crash_rate",
        "anr_rate",
        "crash_rate_28d",
        "anr_rate_28d",
        "vitals_users",
        "active_subscriptions",
        "active_trials",
        "subscription_churn",
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
    """Open the database, enable WAL and foreign keys, and apply pending migrations.

    The database can hold plaintext sales data and, in hosted mode, encrypted secrets
    and session token hashes, so the file and its directory are locked to the owner
    only on every connect — not just on first creation, so permissions self-heal.
    """
    in_memory = str(path) == ":memory:"
    if not in_memory:
        directory = Path(path).parent
        directory.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            os.chmod(directory, 0o700)
    conn = sqlite3.connect(str(path), isolation_level=None)
    if not in_memory and os.name == "posix":
        os.chmod(path, 0o600)
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
        _insert_owned(conn, source, batch, now)
    return len(batch)


class SourceCollisionError(ValueError):
    """A row's key is already owned by another source; nothing was written."""


_UPSERT = (
    "INSERT INTO daily_metrics "
    "(date, app_id, country, metric, currency, value, source, updated_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT (date, app_id, country, metric, currency) DO UPDATE SET "
    "value = excluded.value, updated_at = excluded.updated_at "
    # Only a row's own source may overwrite it; a collision changes nothing and is
    # detected below instead of silently transferring ownership.
    "WHERE daily_metrics.source = excluded.source"
)


def _insert_owned(conn: sqlite3.Connection, source: str, batch: list[MetricRow], now: str) -> None:
    """Upsert rows for ``source``; raise SourceCollisionError if another source owns a key.

    Must run inside a transaction so a collision rolls back the whole replacement.
    """
    before = conn.total_changes
    conn.executemany(
        _UPSERT,
        [(r.date, r.app_id, r.country, r.metric, r.currency, r.value, source, now) for r in batch],
    )
    if conn.total_changes - before == len(batch):
        return
    for r in batch:
        owner = conn.execute(
            "SELECT source FROM daily_metrics "
            "WHERE date = ? AND app_id = ? AND country = ? AND metric = ? AND currency = ?",
            (r.date, r.app_id, r.country, r.metric, r.currency),
        ).fetchone()
        if owner is not None and owner["source"] != source:
            raise SourceCollisionError(
                f"{source} tried to write {r.metric} for app {r.app_id} {r.country} {r.date}, "
                f"but that row belongs to {owner['source']}; nothing was written"
            )
    raise SourceCollisionError(f"{source}: fewer rows were written than expected")


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
        _insert_owned(conn, source, batch, now)
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
    app_id: int | None = None,
) -> None:
    """Record the outcome of one source/date pull. ``note`` goes in the ``error`` column.

    ``app_id`` scopes the attempt to one app, for a per-app source (play_installs,
    play_vitals) that logs one row per app under the same report_date; leave it unset
    for an account-wide source (apple_sales, play_sales, play_earnings).
    """
    if status not in INGEST_STATUSES:
        raise ValueError(f"unknown ingest status {status!r}")
    conn.execute(
        "INSERT INTO ingest_log (source, report_date, started_at, finished_at, status, rows, "
        "error, app_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (source, report_date, started_at, utc_now(), status, rows, note, app_id),
    )


# -- hosted mode (docs/SPEC.md, Storage → Hosted-mode additions) ---------------------------


@dataclass(frozen=True)
class SecretRow:
    name: str
    ciphertext: bytes
    nonce: bytes
    fingerprint: str
    updated_at: str


def get_secret_row(conn: sqlite3.Connection, name: str) -> SecretRow | None:
    row = conn.execute(
        "SELECT name, ciphertext, nonce, fingerprint, updated_at FROM secrets WHERE name = ?",
        (name,),
    ).fetchone()
    if row is None:
        return None
    return SecretRow(
        str(row["name"]),
        bytes(row["ciphertext"]),
        bytes(row["nonce"]),
        str(row["fingerprint"]),
        str(row["updated_at"]),
    )


def put_secret_row(
    conn: sqlite3.Connection, name: str, ciphertext: bytes, nonce: bytes, fingerprint: str
) -> None:
    conn.execute(
        "INSERT INTO secrets (name, ciphertext, nonce, fingerprint, updated_at) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT (name) DO UPDATE SET "
        "ciphertext = excluded.ciphertext, nonce = excluded.nonce, "
        "fingerprint = excluded.fingerprint, updated_at = excluded.updated_at",
        (name, ciphertext, nonce, fingerprint, utc_now()),
    )


def delete_secret_row(conn: sqlite3.Connection, name: str) -> None:
    conn.execute("DELETE FROM secrets WHERE name = ?", (name,))


def secret_names(conn: sqlite3.Connection) -> list[str]:
    return [str(r["name"]) for r in conn.execute("SELECT name FROM secrets ORDER BY name")]


def get_settings(conn: sqlite3.Connection) -> dict[str, Any]:
    """Every settings row, JSON-decoded."""
    return {
        str(r["key"]): json.loads(r["value"])
        for r in conn.execute("SELECT key, value FROM settings")
    }


def set_settings(
    conn: sqlite3.Connection, values: dict[str, Any], *, remove: Collection[str] = ()
) -> None:
    """Upsert ``values`` (JSON-encoded) and delete ``remove``, in one transaction."""
    now = utc_now()
    with transaction(conn):
        for key, value in values.items():
            conn.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (key, json.dumps(value, sort_keys=True), now),
            )
        for key in remove:
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))


@dataclass(frozen=True)
class User:
    id: int
    email: str
    password_hash: str
    totp_secret_enc: bytes | None


def _user(row: sqlite3.Row | None) -> User | None:
    if row is None:
        return None
    totp = row["totp_secret_enc"]
    return User(
        int(row["id"]),
        str(row["email"]),
        str(row["password_hash"]),
        bytes(totp) if totp is not None else None,
    )


def user_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"])


def create_user(
    conn: sqlite3.Connection, email: str, password_hash: str, totp_secret_enc: bytes | None
) -> int:
    row = conn.execute(
        "INSERT INTO users (email, password_hash, totp_secret_enc) VALUES (?, ?, ?) RETURNING id",
        (email, password_hash, totp_secret_enc),
    ).fetchone()
    return int(row["id"])


def get_user_by_email(conn: sqlite3.Connection, email: str) -> User | None:
    return _user(conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone())


def get_user(conn: sqlite3.Connection, user_id: int) -> User | None:
    return _user(conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())


def set_user_password_hash(conn: sqlite3.Connection, user_id: int, password_hash: str) -> None:
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id))


def claim_totp_step(conn: sqlite3.Connection, user_id: int, step: int) -> bool:
    """Record ``step`` as the user's last accepted TOTP time step, only if it is later than
    the one already recorded. False means that code (or an earlier one) was already used.
    One statement, so two requests racing with the same code can't both succeed."""
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at "
            "WHERE CAST(settings.value AS INTEGER) < CAST(excluded.value AS INTEGER)",
            (f"hosted.totp_last_step.{user_id}", json.dumps(step), utc_now()),
        )
    return cur.rowcount == 1


def set_user_totp(conn: sqlite3.Connection, user_id: int, totp_secret_enc: bytes | None) -> None:
    conn.execute("UPDATE users SET totp_secret_enc = ? WHERE id = ?", (totp_secret_enc, user_id))


@dataclass(frozen=True)
class Session:
    token_hash: str
    user_id: int
    csrf_token: str
    totp_pending: bool
    expires_at: str


def create_session(
    conn: sqlite3.Connection,
    token_hash: str,
    user_id: int,
    csrf_token: str,
    expires_at: str,
    *,
    totp_pending: bool,
) -> None:
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, csrf_token, totp_pending, created_at, "
        "expires_at) VALUES (?, ?, ?, ?, ?, ?)",
        (token_hash, user_id, csrf_token, int(totp_pending), utc_now(), expires_at),
    )


def get_session(conn: sqlite3.Connection, token_hash: str, now: str) -> Session | None:
    row = conn.execute(
        "SELECT * FROM sessions WHERE token_hash = ? AND expires_at > ?", (token_hash, now)
    ).fetchone()
    if row is None:
        return None
    return Session(
        str(row["token_hash"]),
        int(row["user_id"]),
        str(row["csrf_token"]),
        bool(row["totp_pending"]),
        str(row["expires_at"]),
    )


def delete_session(conn: sqlite3.Connection, token_hash: str) -> None:
    conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))


def delete_user_sessions(conn: sqlite3.Connection, user_id: int) -> None:
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def delete_expired_sessions(conn: sqlite3.Connection, now: str) -> None:
    conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))


@dataclass(frozen=True)
class AppInfo:
    id: int
    platform: str
    store_id: str
    name: str  # as the store reports it
    display_name: str | None
    pair_key: str | None
    pair_source: str | None
    hidden: bool

    @property
    def label(self) -> str:
        return self.display_name or self.name


def list_apps(conn: sqlite3.Connection) -> list[AppInfo]:
    rows = conn.execute(
        "SELECT id, platform, store_id, name, display_name, pair_key, pair_source, hidden "
        "FROM apps ORDER BY name, platform"
    )
    return [
        AppInfo(
            int(r["id"]),
            str(r["platform"]),
            str(r["store_id"]),
            str(r["name"]),
            r["display_name"],
            r["pair_key"],
            r["pair_source"],
            bool(r["hidden"]),
        )
        for r in rows
    ]


def set_app_pair(
    conn: sqlite3.Connection, app_id: int, pair_key: str | None, pair_source: str
) -> None:
    if pair_source not in ("auto", "user"):
        raise ValueError(f"unknown pair_source {pair_source!r}")
    conn.execute(
        "UPDATE apps SET pair_key = ?, pair_source = ? WHERE id = ?",
        (pair_key, pair_source, app_id),
    )


def set_app_display(
    conn: sqlite3.Connection, app_id: int, *, display_name: str | None, hidden: bool
) -> None:
    """Rename or hide an app. Never touches pairing (docs/SPEC.md, Storage)."""
    conn.execute(
        "UPDATE apps SET display_name = ?, hidden = ? WHERE id = ?",
        (display_name or None, int(hidden), app_id),
    )
