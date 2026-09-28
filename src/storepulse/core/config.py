"""Non-secret settings (``config.toml``) and local-mode paths."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import platformdirs

APP_NAME = "storepulse"
CONFIG_FILENAME = "config.toml"
DB_FILENAME = "storepulse.db"

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
class Config:
    secret_store: str = ""
    apple: AppleConfig | None = None
    google: GoogleConfig | None = None
    extra: dict[str, object] = field(default_factory=dict)


class ConfigError(Exception):
    pass


def load(path: Path | None = None) -> Config:
    path = path or config_dir() / CONFIG_FILENAME
    if not path.exists():
        return Config()
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"could not read {path}: {exc}") from None
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
    cfg.extra = raw
    return cfg


def _toml_str(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return f'"{escaped}"'


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
    return "\n".join(lines) + "\n"


def save(cfg: Config, path: Path | None = None) -> Path:
    if cfg.extra:
        raise ConfigError("unknown config sections would be lost on save")
    path = path or config_dir() / CONFIG_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(dumps(cfg), encoding="utf-8")
    os.replace(tmp, path)
    return path
