"""`storepulse init`: guided, validated setup for Apple and Google Play."""

from __future__ import annotations

import argparse
from datetime import timedelta
from pathlib import Path

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
    store_for_setup,
)
from storepulse.core import config, db, discovery
from storepulse.core.sources.google_auth import GoogleCredentialError, load_service_account
from storepulse.core.sources.google_client import parse_bucket_uri
from storepulse.core.sources.play_common import last_months

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


def _setup_apple(env: Env, cfg: config.Config) -> tuple[config.AppleConfig, str, Path, CheckResult]:
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
    p8_path, p8 = _read(ask(env, "Path to the .p8 key file"), ".p8 file")
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
    return apple, p8, p8_path, result


def _setup_google(
    env: Env, cfg: config.Config
) -> tuple[config.GoogleConfig, str, Path, CheckResult]:
    prev = cfg.google
    env.say()
    env.say("Google Play")
    env.say("You need a service account JSON key invited in Play Console with 'View app")
    env.say("information and download bulk reports' (README, 'Google service account').")
    sa_path, raw = _read(ask(env, "Path to the service account JSON key"), "service account key")
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
    return google, raw, sa_path, result


def cmd_init(args: argparse.Namespace, env: Env) -> int:
    cfg = config.load()
    env.say("Storepulse setup. Each platform is optional; set up at least one.")
    apple = google = None
    if confirm(env, "Set up Apple App Store Connect?", default=True):
        apple = _setup_apple(env, cfg)
    if confirm(env, "Set up Google Play?", default=True):
        google = _setup_google(env, cfg)
    if apple is None and google is None:
        if cfg.apple is None and cfg.google is None:
            raise CliError("nothing was set up; answer yes for Apple or Google Play")
        env.say("Nothing changed.")
        return 0

    # Every check passed: save.
    store = store_for_setup(env, cfg)
    if apple is not None:
        store.set(APPLE_P8_SECRET, apple[1])
        cfg.apple = apple[0]
    if google is not None:
        store.set(GOOGLE_SA_SECRET, google[1])
        cfg.google = google[0]
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
        if saved is not None:
            env.say(f"You can now delete {saved[2]}; Storepulse keeps its own copy.")

    conn = db.connect(config.db_path())
    try:
        registered: list[tuple[str, str]] = []
        if apple is not None:
            registered += discovery.register_apple_apps(conn, apple[3].apps)
        if google is not None:
            registered += discovery.register_play_apps(conn, google[3].apps)
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
