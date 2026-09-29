"""Non-secret settings (``config.toml``) and local-mode paths."""

from __future__ import annotations

import dataclasses
import os
import sqlite3
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import platformdirs

from storepulse.core import db
from storepulse.core.secrets import STORE_DB

APP_NAME = "storepulse"
CONFIG_FILENAME = "config.toml"
DB_FILENAME = "storepulse.db"

SECURITY_STARTTLS = "starttls"
SECURITY_SSL = "ssl"
EMAIL_SECURITY_MODES = (SECURITY_STARTTLS, SECURITY_SSL)

# docs/SPEC.md, Outputs: Google Play's own bad-behavior thresholds.
DEFAULT_CRASH_THRESHOLD = 0.0109
DEFAULT_ANR_THRESHOLD = 0.0047
DEFAULT_STALE_DAYS = 4
DEFAULT_RUN_TIME = "10:30"
DEFAULT_SCHEDULE_DAYS = 7

# Overrides for tests and unusual setups; they point at directories, never at secrets.
CONFIG_DIR_ENV = "STOREPULSE_CONFIG_DIR"
DATA_DIR_ENV = "STOREPULSE_DATA_DIR"


def config_dir() -> Path:
    override = os.environ.get(CONFIG_DIR_ENV)
    return (
        Path(override)
        if override
        else Path(platformdirs.user_config_dir(APP_NAME, appauthor=False))
    )


def data_dir() -> Path:
    override = os.environ.get(DATA_DIR_ENV)
    return (
        Path(override) if override else Path(platformdirs.user_data_dir(APP_NAME, appauthor=False))
    )


def db_path() -> Path:
    return data_dir() / DB_FILENAME


@dataclass
class AppleConfig:
    issuer_id: str
    key_id: str
    vendor_number: str


@dataclass
class GoogleConfig:
    bucket_uri: str
    service_account_email: str


@dataclass
class EmailConfig:
    host: str
    port: int
    security: str  # "starttls" or "ssl"
    username: str
    from_addr: str
    to_addrs: list[str]


@dataclass
class DigestConfig:
    crash_threshold: float = DEFAULT_CRASH_THRESHOLD
    anr_threshold: float = DEFAULT_ANR_THRESHOLD
    # Only the daily-cadence overdue rule (apple_sales, play_vitals) is configurable this
    # way; the monthly and earnings cadence day-counts are fixed (docs/SPEC.md, Scheduling
    # and reliability), since they follow Play's publishing mechanics, not a preference.
    stale_days: int = DEFAULT_STALE_DAYS
    # Optional fixed rates for a converted total; no outbound calls, no daily reference rate.
    rates: dict[str, float] = field(default_factory=dict)
    display_currency: str = ""


@dataclass
class ScheduleConfig:
    run_time: str = DEFAULT_RUN_TIME
    days: int = DEFAULT_SCHEDULE_DAYS


@dataclass
class Config:
    secret_store: str = ""
    apple: AppleConfig | None = None
    google: GoogleConfig | None = None
    email: EmailConfig | None = None
    digest: DigestConfig = field(default_factory=DigestConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    extra: dict[str, object] = field(default_factory=dict)


CONFIG_SECTIONS = ("apple", "google", "email", "digest", "schedule")
MODE_ENV = "STOREPULSE_MODE"


class ConfigError(Exception):
    pass


def hosted_mode() -> bool:
    """True inside the hosted container (``STOREPULSE_MODE=hosted``, set by the
    Dockerfile), where settings live in the database's ``settings`` table and secrets in
    ``secrets``, not in ``config.toml`` and the OS keychain."""
    return os.environ.get(MODE_ENV, "").strip().lower() == "hosted"


def load_hosted(conn: sqlite3.Connection) -> Config:
    """Hosted mode's Config, from the settings table. Its secret store is always the
    database (docs/SPEC.md, Storage)."""
    cfg = from_settings(db.get_settings(conn))
    cfg.secret_store = STORE_DB
    return cfg


def save_hosted(conn: sqlite3.Connection, cfg: Config) -> None:
    """Write every Config field to the settings table; sections set to None are removed."""
    values = to_settings(cfg)
    stale = [
        key
        for key in db.get_settings(conn)
        if key.partition(".")[0] in CONFIG_SECTIONS and key not in values
    ]
    db.set_settings(conn, values, remove=stale)


def load(path: Path | None = None) -> Config:
    if path is None and hosted_mode():
        conn = db.connect(db_path())
        try:
            return load_hosted(conn)
        finally:
            conn.close()
    path = path or config_dir() / CONFIG_FILENAME
    if not path.exists():
        return Config()
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"could not read {path}: {exc}") from None
    return from_mapping(raw, str(path))


def from_mapping(raw: dict[str, Any], path: str = "settings") -> Config:
    """Build a Config from the nested ``{section: {field: value}}`` shape that both
    ``config.toml`` and the hosted ``settings`` table (after ``unflatten``) produce.
    ``path`` names the source in error messages."""
    raw = dict(raw)
    cfg = Config(secret_store=str(raw.pop("secret_store", "")))
    apple = raw.pop("apple", None)
    if isinstance(apple, dict):
        try:
            cfg.apple = AppleConfig(
                issuer_id=str(apple["issuer_id"]),
                key_id=str(apple["key_id"]),
                vendor_number=str(apple["vendor_number"]),
            )
        except KeyError as exc:
            raise ConfigError(f"{path}: [apple] is missing {exc.args[0]!r}") from None
    google = raw.pop("google", None)
    if isinstance(google, dict):
        try:
            cfg.google = GoogleConfig(
                bucket_uri=str(google["bucket_uri"]),
                service_account_email=str(google["service_account_email"]),
            )
        except KeyError as exc:
            raise ConfigError(f"{path}: [google] is missing {exc.args[0]!r}") from None
    email = raw.pop("email", None)
    if isinstance(email, dict):
        try:
            to_addrs = email["to_addrs"]
            if not isinstance(to_addrs, list):
                raise ConfigError(f"{path}: [email] to_addrs must be a list")
            cfg.email = EmailConfig(
                host=str(email["host"]),
                port=int(email["port"]),
                security=str(email["security"]),
                username=str(email["username"]),
                from_addr=str(email["from_addr"]),
                to_addrs=[str(a) for a in to_addrs],
            )
        except KeyError as exc:
            raise ConfigError(f"{path}: [email] is missing {exc.args[0]!r}") from None
        if cfg.email.security not in EMAIL_SECURITY_MODES:
            raise ConfigError(
                f"{path}: [email] security must be one of {EMAIL_SECURITY_MODES}, "
                f"got {cfg.email.security!r}"
            )
    digest = raw.pop("digest", None)
    if isinstance(digest, dict):
        rates = digest.get("rates", {})
        if not isinstance(rates, dict):
            raise ConfigError(f"{path}: [digest] rates must be a table of currency = rate")
        cfg.digest = DigestConfig(
            crash_threshold=float(digest.get("crash_threshold", DEFAULT_CRASH_THRESHOLD)),
            anr_threshold=float(digest.get("anr_threshold", DEFAULT_ANR_THRESHOLD)),
            stale_days=int(digest.get("stale_days", DEFAULT_STALE_DAYS)),
            rates={str(k): float(v) for k, v in rates.items()},
            display_currency=str(digest.get("display_currency", "")),
        )
    schedule = raw.pop("schedule", None)
    if isinstance(schedule, dict):
        cfg.schedule = ScheduleConfig(
            run_time=str(schedule.get("run_time", DEFAULT_RUN_TIME)),
            days=int(schedule.get("days", DEFAULT_SCHEDULE_DAYS)),
        )
    cfg.extra = raw
    return cfg


def _toml_str(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return f'"{escaped}"'


def _toml_list(values: Iterable[str]) -> str:
    return "[" + ", ".join(_toml_str(v) for v in values) + "]"


def _toml_float(value: float) -> str:
    return repr(float(value))


def dumps(cfg: Config) -> str:
    lines = ["# Storepulse settings. No secrets here; they live in the secret store.", ""]
    if cfg.secret_store:
        lines.append(f"secret_store = {_toml_str(cfg.secret_store)}")
    if cfg.apple is not None:
        lines += [
            "",
            "[apple]",
            f"issuer_id = {_toml_str(cfg.apple.issuer_id)}",
            f"key_id = {_toml_str(cfg.apple.key_id)}",
            f"vendor_number = {_toml_str(cfg.apple.vendor_number)}",
        ]
    if cfg.google is not None:
        lines += [
            "",
            "[google]",
            f"bucket_uri = {_toml_str(cfg.google.bucket_uri)}",
            f"service_account_email = {_toml_str(cfg.google.service_account_email)}",
        ]
    if cfg.email is not None:
        lines += [
            "",
            "[email]",
            f"host = {_toml_str(cfg.email.host)}",
            f"port = {cfg.email.port}",
            f"security = {_toml_str(cfg.email.security)}",
            f"username = {_toml_str(cfg.email.username)}",
            f"from_addr = {_toml_str(cfg.email.from_addr)}",
            f"to_addrs = {_toml_list(cfg.email.to_addrs)}",
        ]
    lines += [
        "",
        "[digest]",
        f"crash_threshold = {_toml_float(cfg.digest.crash_threshold)}",
        f"anr_threshold = {_toml_float(cfg.digest.anr_threshold)}",
        f"stale_days = {cfg.digest.stale_days}",
    ]
    if cfg.digest.display_currency:
        lines.append(f"display_currency = {_toml_str(cfg.digest.display_currency)}")
    if cfg.digest.rates:
        lines += ["", "[digest.rates]"]
        lines += [
            f"{_toml_str(currency)} = {_toml_float(rate)}"
            for currency, rate in sorted(cfg.digest.rates.items())
        ]
    lines += [
        "",
        "[schedule]",
        f"run_time = {_toml_str(cfg.schedule.run_time)}",
        f"days = {cfg.schedule.days}",
    ]
    return "\n".join(lines) + "\n"


def to_mapping(cfg: Config) -> dict[str, dict[str, Any]]:
    """The nested ``{section: {field: value}}`` form of a Config, without ``secret_store``
    and ``extra`` (the hosted settings table stores neither; docs/SPEC.md, Storage)."""
    sections: dict[str, dict[str, Any]] = {}
    if cfg.apple is not None:
        sections["apple"] = dataclasses.asdict(cfg.apple)
    if cfg.google is not None:
        sections["google"] = dataclasses.asdict(cfg.google)
    if cfg.email is not None:
        sections["email"] = dataclasses.asdict(cfg.email)
    sections["digest"] = dataclasses.asdict(cfg.digest)
    sections["schedule"] = dataclasses.asdict(cfg.schedule)
    return sections


def to_settings(cfg: Config) -> dict[str, Any]:
    """One entry per dotted key (``apple.vendor_number``, ``digest.rates``, …), the shape
    of the hosted ``settings`` table."""
    return {
        f"{section}.{name}": value
        for section, fields in to_mapping(cfg).items()
        for name, value in fields.items()
    }


def from_settings(rows: dict[str, Any]) -> Config:
    """The inverse of ``to_settings``. Keys outside Config's sections (hosted-only
    settings such as ``hosted.base_url``) are ignored here."""
    nested: dict[str, dict[str, Any]] = {}
    for key, value in rows.items():
        section, _, name = key.partition(".")
        if section in CONFIG_SECTIONS and name:
            nested.setdefault(section, {})[name] = value
    return from_mapping(nested)


def save(cfg: Config, path: Path | None = None) -> Path:
    if path is None and hosted_mode():
        raise ConfigError(
            "in hosted mode, settings are changed in the web app's Settings page, not with "
            "this command"
        )
    if cfg.extra:
        raise ConfigError("unknown config sections would be lost on save")
    path = path or config_dir() / CONFIG_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(dumps(cfg), encoding="utf-8")
    os.replace(tmp, path)
    return path
