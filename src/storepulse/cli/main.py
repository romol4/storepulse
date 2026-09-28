"""`storepulse` command line: init wizard and backfill (Phase 1)."""

from __future__ import annotations

import argparse
import getpass
import logging
import sqlite3
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import TextIO

import httpx

from storepulse import __version__
from storepulse.core import config, db, discovery, runner
from storepulse.core.secrets import (
    SECRETS_FILENAME,
    RedactingFilter,
    SecretStore,
    SecretStoreError,
    default_store,
    open_store,
    redact,
)
from storepulse.core.sources import apple_sales
from storepulse.core.sources.apple_client import (
    AppleAgreementError,
    AppleAuthError,
    AppleClient,
    AppleError,
    AppleVendorError,
)

APPLE_P8_SECRET = "apple_p8"  # noqa: S105 (secret name, not a value)
DEFAULT_BACKFILL_DAYS = 90
# Checked during init: old enough that the report should exist, recent enough to be kept.
INIT_CHECK_DAYS_AGO = 3


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


def _ask(env: Env, prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        value = env.input(f"{prompt}{suffix}: ").strip()
        if value or default:
            return value or default
        env.say("  A value is required.")


def _confirm(env: Env, prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    value = env.input(f"{prompt} [{hint}]: ").strip().lower()
    return default if not value else value.startswith("y")


def _existing_passphrase(env: Env) -> Callable[[], str]:
    """Interactive prompt only; the store itself prefers STOREPULSE_PASSPHRASE."""

    def prompt() -> str:
        return env.getpass("Secrets file passphrase: ")

    return prompt


def _new_passphrase(env: Env, path: Path) -> Callable[[], str]:
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


def _make_client(env: Env, apple: config.AppleConfig, p8: str) -> AppleClient:
    return AppleClient(apple.issuer_id, apple.key_id, p8, transport=env.transport, sleep=env.sleep)


# -- init ----------------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace, env: Env) -> int:
    cfg = config.load()
    prev = cfg.apple
    env.say("Storepulse setup: Apple App Store Connect")
    env.say("Create a key in App Store Connect > Users and Access > Integrations with the")
    env.say("Sales and Reports role (README, 'Apple API key').")
    env.say()
    apple = config.AppleConfig(
        issuer_id=_ask(env, "Issuer ID", prev.issuer_id if prev else ""),
        key_id=_ask(env, "Key ID", prev.key_id if prev else ""),
        vendor_number=_ask(env, "Vendor number", prev.vendor_number if prev else ""),
    )
    p8_path = Path(_ask(env, "Path to the .p8 key file")).expanduser()
    try:
        p8 = p8_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise CliError(f"could not read the .p8 file at {p8_path}") from None

    env.say()
    env.say("Checking the key with App Store Connect...")
    try:
        client = _make_client(env, apple, p8)
    except ValueError as exc:
        raise CliError(str(exc)) from None
    with client:
        # 1. Does the key work at all? (401 → wrong key, key ID or issuer ID.)
        apps: list[dict[str, str]] = []
        try:
            apps = discovery.list_apple_apps(client)
            env.say(f"  OK Key accepted; {len(apps)} app(s) visible.")
        except AppleAuthError as exc:
            # A plain 403 may just mean the role can't list apps; an agreement 403 blocks
            # everything, so it fails here with Apple's reason.
            if exc.status != 403 or isinstance(exc, AppleAgreementError):
                raise
            env.say("  OK Key accepted, but it can't list apps (GET /v1/apps returned 403).")
            env.say("    Apps will be added from sales reports as they appear.")

        # 2. Does it have Sales and Reports, and is the vendor number right?
        check_day = env.pacific_today() - timedelta(days=INIT_CHECK_DAYS_AGO)
        try:
            result = apple_sales.fetch_day(
                client, apple.vendor_number, check_day, today=env.pacific_today()
            )
        except AppleVendorError as exc:
            raise CliError(str(exc)) from None
        except AppleAuthError as exc:
            raise CliError(str(exc)) from None
        if result.status == "ok":
            env.say(f"  OK Sales report for {check_day} downloaded; vendor number confirmed.")
        elif result.status == "no_sales":
            env.say(f"  OK No sales on {check_day}, but the report request was accepted.")
        elif result.status == "not_ready":
            env.say(f"  OK Report for {check_day} isn't ready yet; request was accepted.")
        else:
            env.warn(
                "couldn't fully confirm the vendor number; the first backfill will tell. "
                f"(Apple returned 404 for {check_day} without saying why.)"
            )

    # 3. Everything checked out: save.
    cdir = config.config_dir()
    passphrase = _new_passphrase(env, cdir / SECRETS_FILENAME)
    if env.store_factory is not None:
        store = env.store_factory(cdir, passphrase)
    else:
        store = default_store(cdir, passphrase=passphrase)
    store.set(APPLE_P8_SECRET, p8)
    cfg.secret_store = store.kind
    cfg.apple = apple
    cfg_path = config.save(cfg)
    env.say()
    env.say(f"Secrets stored in: {store.describe()}")
    env.say(f"  .p8 fingerprint {store.fingerprint(APPLE_P8_SECRET)}")
    env.say(f"Settings saved to: {cfg_path}")
    env.say(f"You can now delete {p8_path}; Storepulse keeps its own copy.")

    conn = db.connect(config.db_path())
    try:
        registered = discovery.register_apple_apps(conn, apps)
        if registered:
            env.say()
            env.say("Apps found:")
            for store_id, name in registered:
                env.say(f"  - {name} ({store_id})")
        env.say()
        if args.no_backfill or not _confirm(env, "Load historical sales now?"):
            env.say("Done. Run `storepulse backfill --source apple_sales --days 90` any time.")
            return 0
        answer = _ask(env, "How many days back", str(DEFAULT_BACKFILL_DAYS))
        if not answer.isdigit() or int(answer) < 1:
            raise CliError(f"expected a number of days, got {answer!r}")
        days = int(answer)
        end = env.pacific_today() - timedelta(days=1)
        start = end - timedelta(days=days - 1)
        return _backfill_apple(env, conn, apple, p8, start, end)
    finally:
        conn.close()


# -- backfill ------------------------------------------------------------------------------


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {value!r}") from None


def cmd_backfill(args: argparse.Namespace, env: Env) -> int:
    cfg = config.load()
    if cfg.apple is None or not cfg.secret_store:
        raise CliError("Apple isn't set up yet; run `storepulse init` first")
    store = open_store(cfg.secret_store, config.config_dir(), _existing_passphrase(env))
    p8 = store.get(APPLE_P8_SECRET)
    if p8 is None:
        raise CliError(f"no Apple key found in {store.describe()}; run `storepulse init` again")
    yesterday = env.pacific_today() - timedelta(days=1)
    if args.days is not None:
        if args.days < 1:
            raise CliError("--days must be at least 1")
        end = yesterday
        start = end - timedelta(days=args.days - 1)
    else:
        start = args.from_date
        end = args.to_date or yesterday
    conn = db.connect(config.db_path())
    try:
        return _backfill_apple(env, conn, cfg.apple, p8, start, end)
    finally:
        conn.close()


def _backfill_apple(
    env: Env,
    conn: sqlite3.Connection,
    apple: config.AppleConfig,
    p8: str,
    start: date,
    end: date,
) -> int:
    try:
        span = runner.apple_sales_range(start, end, today=env.pacific_today())
    except ValueError as exc:
        raise CliError(str(exc)) from None
    if span.skipped:
        first, last = span.skipped
        env.warn(
            "Apple keeps daily sales reports for about a year; "
            f"skipped {first} to {last} (not requested, nothing recorded)."
        )
    if not span.dates:
        env.say("Nothing to load.")
        return 0
    env.say(f"Loading apple_sales {span.dates[0]} > {span.dates[-1]} ({len(span.dates)} days)...")
    with _make_client(env, apple, p8) as client:
        summary = runner.collect_apple_sales(
            conn,
            client,
            apple.vendor_number,
            span.dates,
            today=env.pacific_today(),
            sleep=env.sleep,
            progress=lambda d, outcome: env.say(f"  {d}  {outcome}"),
        )
    env.say()
    env.say(
        f"Done: {summary.ok} days loaded ({summary.rows} rows), "
        f"{summary.no_sales + summary.inferred_no_sales} with no sales, "
        f"{len(summary.not_ready)} not ready, {len(summary.errors)} errors."
    )
    if summary.new_apps:
        env.say(f"New apps from reports: {', '.join(sorted(set(summary.new_apps)))}")
    if summary.kept_rows:
        env.warn(
            f"{len(summary.kept_rows)} day(s) returned an unexplained 404; stored data was kept: "
            + ", ".join(d.isoformat() for d in summary.kept_rows)
        )
    if summary.unknown_types:
        codes = ", ".join(f"{c} ({n} rows)" for c, n in sorted(summary.unknown_types.items()))
        env.warn(
            f"unrecognized product types (only proceeds counted): {codes}. "
            "Please report them so they can be mapped."
        )
    if summary.remapped:
        env.say(
            f"Re-mapped in-app purchases for {len(summary.remapped)} day(s) once their "
            "parent app appeared in later reports."
        )
    if summary.unmapped_rows:
        env.warn(f"{summary.unmapped_rows} report rows matched no known app and were skipped.")
    if summary.unmapped_child_rows:
        env.warn(
            f"{summary.unmapped_child_rows} in-app purchase rows belong to an app Storepulse "
            "hasn't seen yet. Re-run this backfill after the app has a download in the "
            "range (or once the key can list apps) to pick them up."
        )
    for day, message in summary.errors:
        env.warn(f"{day}: {message}")
    return 1 if summary.errors else 0


# -- entry point ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="storepulse", description="App Store Connect and Google Play stats collector."
    )
    parser.add_argument("--version", action="version", version=f"storepulse {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="show info-level logs")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="guided setup: validate and save credentials")
    init.add_argument("--no-backfill", action="store_true", help="skip the first backfill")
    init.set_defaults(func=cmd_init)

    backfill = sub.add_parser("backfill", help="load historical data for a source")
    backfill.add_argument("--source", required=True, choices=[apple_sales.SOURCE])
    when = backfill.add_mutually_exclusive_group(required=True)
    when.add_argument("--days", type=int, help="the last N days, ending yesterday")
    when.add_argument("--from", dest="from_date", type=_parse_date, help="YYYY-MM-DD")
    backfill.add_argument(
        "--to", dest="to_date", type=_parse_date, help="YYYY-MM-DD (default: yesterday)"
    )
    backfill.set_defaults(func=cmd_backfill)
    return parser


def _setup_logging(env: Env, verbose: bool) -> None:
    handler = logging.StreamHandler(env.stderr)
    handler.addFilter(RedactingFilter())
    handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    root = logging.getLogger("storepulse")
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO if verbose else logging.WARNING)
    root.propagate = False


def main(argv: Sequence[str] | None = None, env: Env | None = None) -> int:
    env = env or Env()
    args = build_parser().parse_args(argv)
    _setup_logging(env, args.verbose)
    try:
        return int(args.func(args, env))
    except (CliError, AppleError, SecretStoreError, config.ConfigError) as exc:
        print(f"error: {redact(str(exc))}", file=env.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=env.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
