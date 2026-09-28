"""`storepulse init`: guided, validated setup for Apple and Google Play."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Generic, TypeVar

from storepulse.cli import collect
from storepulse.cli.checks import Check, CheckResult, check_apple, check_google
from storepulse.cli.common import (
    APPLE_P8_SECRET,
    GOOGLE_SA_SECRET,
    CliError,
    Env,
    apple_client,
    ask,
    ask_positive_int,
    confirm,
    google_client,
    store_for_run,
    store_for_setup,
)
from storepulse.core import config, db, discovery
from storepulse.core.sources.google_auth import GoogleCredentialError, load_service_account
from storepulse.core.sources.google_client import (
    BULK_PERMISSION,
    FINANCIAL_PERMISSION,
    parse_bucket_uri,
)
from storepulse.core.sources.play_common import last_months

C = TypeVar("C")

DEFAULT_APPLE_DAYS = 90
DEFAULT_PLAY_MONTHS = 3
DEFAULT_VITALS_DAYS = 30


def report(env: Env, check: Check) -> None:
    label = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL"}[check.level]
    env.say(f"  {label} {check.text}")


def _read(path_text: str, what: str) -> tuple[Path, str]:
    path = Path(path_text).expanduser()
    try:
        return path, path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise CliError(f"could not read the {what} at {path}") from None


@dataclass
class Setup(Generic[C]):
    config: C
    secret: str
    path: Path | None  # None when the stored key was reused
    result: CheckResult
    clean: bool  # every check passed (not "saved anyway")


def _key_source(
    env: Env, cfg: config.Config, prompt: str, secret_name: str, what: str
) -> tuple[Path | None, str]:
    """Read a key file, or reuse the stored key when the user leaves the path blank."""
    stored_ok = bool(cfg.secret_store) and (
        (secret_name == APPLE_P8_SECRET and cfg.apple is not None)
        or (secret_name == GOOGLE_SA_SECRET and cfg.google is not None)
    )
    if not stored_ok:
        return _read(ask(env, prompt), what)
    answer = env.input(f"{prompt} (Enter to keep the saved key): ").strip()
    if answer:
        return _read(answer, what)
    stored = store_for_run(env, cfg).get(secret_name)
    if stored is None:
        raise CliError(f"no saved {what} found; give the path to the file")
    return None, stored


def _setup_apple(env: Env, cfg: config.Config) -> Setup[config.AppleConfig]:
    prev = cfg.apple
    env.say()
    env.say("Apple App Store Connect")
    env.say("Create a key in App Store Connect > Users and Access > Integrations with the")
    env.say("Sales and Reports role (README, 'Apple API key').")
    apple = config.AppleConfig(
        issuer_id=ask(env, "Issuer ID", prev.issuer_id if prev else ""),
        key_id=ask(env, "Key ID", prev.key_id if prev else ""),
        vendor_number=ask(env, "Vendor number", prev.vendor_number if prev else ""),
    )
    p8_path, p8 = _key_source(env, cfg, "Path to the .p8 key file", APPLE_P8_SECRET, ".p8 file")
    env.say("Checking the key with App Store Connect...")
    try:
        client = apple_client(env, apple, p8)
    except ValueError as exc:
        raise CliError(str(exc)) from None
    with client:
        result = check_apple(client, apple.vendor_number, env.pacific_today())
    for check in result.checks:
        if check.level != "fail":
            report(env, check)
    if result.failed:
        raise CliError(result.failed[0].text)
    return Setup(apple, p8, p8_path, result, clean=True)


def _setup_google(env: Env, cfg: config.Config) -> Setup[config.GoogleConfig]:
    prev = cfg.google
    env.say()
    env.say("Google Play")
    env.say("You need a service account JSON key invited in Play Console with")
    env.say(f"'{BULK_PERMISSION}'. Android revenue also needs the optional")
    env.say(f"'{FINANCIAL_PERMISSION}' (README, 'Google service account').")
    sa_path, raw = _key_source(
        env, cfg, "Path to the service account JSON key", GOOGLE_SA_SECRET, "service account key"
    )
    try:
        sa = load_service_account(raw)
    except GoogleCredentialError as exc:
        raise CliError(str(exc)) from None
    uri = ask(env, "Cloud Storage URI (gs://pubsite_prod_...)", prev.bucket_uri if prev else "")
    try:
        bucket = parse_bucket_uri(uri)
    except ValueError as exc:
        raise CliError(str(exc)) from None
    env.say("Checking the service account with Google...")
    with google_client(env, sa) as client:
        result = check_google(client, bucket)
    for check in result.checks:
        report(env, check)
    failed = result.failed
    clean = not failed and bool(result.apps)
    if failed:
        if all(c.pending_permission for c in failed):
            env.say(
                "The key works, but Play Console hasn't granted access yet. New service "
                "accounts can take 24-48 hours; `storepulse doctor` will tell you when it's ready."
            )
            if not confirm(env, "Save the Google settings anyway?", default=False):
                raise CliError("Google setup not saved")
        else:
            raise CliError(failed[0].text)
    google = config.GoogleConfig(bucket_uri=uri.strip(), service_account_email=sa.client_email)
    return Setup(google, raw, sa_path, result, clean=clean)


def cmd_init(args: argparse.Namespace, env: Env) -> int:
    cfg = config.load()
    env.say("Storepulse setup. Each platform is optional; set up at least one.")
    apple = google = None
    # Default to "no" for a platform that's already set up, so re-running init to add the
    # other one doesn't walk back through (and ask for the key file of) this one.
    if confirm(env, "Set up Apple App Store Connect?", default=cfg.apple is None):
        apple = _setup_apple(env, cfg)
    if confirm(env, "Set up Google Play?", default=cfg.google is None):
        google = _setup_google(env, cfg)
    if apple is None and google is None:
        if cfg.apple is None and cfg.google is None:
            raise CliError("nothing was set up; answer yes for Apple or Google Play")
        env.say("Nothing changed.")
        return 0

    # Every check passed: save.
    store = store_for_setup(env, cfg)
    if apple is not None:
        store.set(APPLE_P8_SECRET, apple.secret)
        cfg.apple = apple.config
    if google is not None:
        store.set(GOOGLE_SA_SECRET, google.secret)
        cfg.google = google.config
    cfg.secret_store = store.kind
    cfg_path = config.save(cfg)
    env.say()
    env.say(f"Secrets stored in: {store.describe()}")
    if apple is not None:
        env.say(f"  .p8 fingerprint {store.fingerprint(APPLE_P8_SECRET)}")
    if google is not None:
        env.say(f"  service account fingerprint {store.fingerprint(GOOGLE_SA_SECRET)}")
    env.say(f"Settings saved to: {cfg_path}")
    for saved in (apple, google):
        # Only once setup fully worked; after "save anyway" the file may still be needed.
        if saved is not None and saved.path is not None and saved.clean:
            env.say(f"You can now delete {saved.path}; Storepulse keeps its own copy.")
        elif saved is not None and saved.path is not None:
            env.say(f"Keep {saved.path} until `storepulse doctor` passes.")

    conn = db.connect(config.db_path())
    try:
        registered: list[tuple[str, str]] = []
        if apple is not None:
            registered += discovery.register_apple_apps(conn, apple.result.apps)
        if google is not None:
            registered += discovery.register_play_apps(conn, google.result.apps)
        if registered:
            env.say()
            env.say("Apps found:")
            for store_id, name in registered:
                env.say(f"  - {name} ({store_id})")
        env.say()
        if args.no_backfill or not confirm(env, "Load historical data now?"):
            env.say("Done. Run `storepulse backfill --help` to load history any time.")
            return 0
        status = 0
        yesterday = env.pacific_today() - timedelta(days=1)
        if apple is not None:
            days = ask_positive_int(env, "Apple: how many days back", DEFAULT_APPLE_DAYS)
            status |= collect.backfill_apple(
                env, conn, cfg, yesterday - timedelta(days=days - 1), yesterday
            )
        if google is not None:
            months = ask_positive_int(env, "Google Play: how many months back", DEFAULT_PLAY_MONTHS)
            span = last_months(months, env.pacific_today())
            status |= collect.backfill_play(env, conn, cfg, list(collect.PLAY_MONTHLY), months=span)
            vitals_days = [
                yesterday - timedelta(days=i) for i in reversed(range(DEFAULT_VITALS_DAYS))
            ]
            status |= collect.backfill_play(env, conn, cfg, ["play_vitals"], dates=vitals_days)
        return status
    finally:
        conn.close()
