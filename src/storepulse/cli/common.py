"""Shared CLI plumbing: injectable I/O, prompts, secret store and client factories."""

from __future__ import annotations

import getpass
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import TextIO

import httpx

from storepulse.core import config
from storepulse.core.secrets import (
    SECRETS_FILENAME,
    SecretStore,
    default_store,
    open_store,
)
from storepulse.core.sources import apple_sales
from storepulse.core.sources.apple_client import AppleClient
from storepulse.core.sources.google_auth import ServiceAccount
from storepulse.core.sources.google_client import GoogleClient

APPLE_P8_SECRET = "apple_p8"  # noqa: S105 (secret name, not a value)
GOOGLE_SA_SECRET = "google_sa"  # noqa: S105 (secret name, not a value)


@dataclass
class Env:
    """Injected I/O so the wizard is testable without a terminal or network."""

    stdout: TextIO = field(default_factory=lambda: sys.stdout)
    stderr: TextIO = field(default_factory=lambda: sys.stderr)
    input: Callable[[str], str] = input
    getpass: Callable[[str], str] = getpass.getpass
    transport: httpx.BaseTransport | None = None
    sleep: Callable[[float], None] | None = None
    today: date | None = None
    store_factory: Callable[[Path, Callable[[], str]], SecretStore] | None = None

    def say(self, text: str = "") -> None:
        print(text, file=self.stdout)

    def warn(self, text: str) -> None:
        print(f"warning: {text}", file=self.stderr)

    def pacific_today(self) -> date:
        return self.today or apple_sales.pacific_today()


class CliError(Exception):
    pass


def ask(env: Env, prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        value = env.input(f"{prompt}{suffix}: ").strip()
        if value or default:
            return value or default
        env.say("  A value is required.")


def confirm(env: Env, prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    value = env.input(f"{prompt} [{hint}]: ").strip().lower()
    return default if not value else value.startswith("y")


def ask_positive_int(env: Env, prompt: str, default: int) -> int:
    answer = ask(env, prompt, str(default))
    if not answer.isdigit() or int(answer) < 1:
        raise CliError(f"expected a positive number, got {answer!r}")
    return int(answer)


def existing_passphrase(env: Env) -> Callable[[], str]:
    """Interactive prompt only; the store itself prefers STOREPULSE_PASSPHRASE."""

    def prompt() -> str:
        return env.getpass("Secrets file passphrase: ")

    return prompt


def new_passphrase(env: Env, path: Path) -> Callable[[], str]:
    def prompt() -> str:
        if path.exists():
            return env.getpass("Secrets file passphrase: ")
        env.say("No OS keychain is available, so secrets go in an encrypted file.")
        while True:
            first = env.getpass("Choose a passphrase for the secrets file: ")
            if len(first) < 8:
                env.say("  Use at least 8 characters.")
                continue
            if env.getpass("Repeat the passphrase: ") == first:
                return first
            env.say("  Passphrases didn't match; try again.")

    return prompt


def store_for_setup(env: Env, cfg: config.Config) -> SecretStore:
    """The store init saves into: the one already recorded, else a freshly detected one."""
    cdir = config.config_dir()
    if cfg.secret_store:
        return open_store(cfg.secret_store, cdir, new_passphrase(env, cdir / SECRETS_FILENAME))
    passphrase = new_passphrase(env, cdir / SECRETS_FILENAME)
    if env.store_factory is not None:
        return env.store_factory(cdir, passphrase)
    return default_store(cdir, passphrase=passphrase)


def store_for_run(env: Env, cfg: config.Config) -> SecretStore:
    """The store recorded at setup; never re-detected."""
    if not cfg.secret_store:
        raise CliError("nothing is set up yet; run `storepulse init` first")
    return open_store(cfg.secret_store, config.config_dir(), existing_passphrase(env))


def apple_client(env: Env, apple: config.AppleConfig, p8: str) -> AppleClient:
    return AppleClient(apple.issuer_id, apple.key_id, p8, transport=env.transport, sleep=env.sleep)


def google_client(env: Env, sa: ServiceAccount) -> GoogleClient:
    return GoogleClient(sa, transport=env.transport, sleep=env.sleep)
