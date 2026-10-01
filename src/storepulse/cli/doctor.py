"""`storepulse doctor`: test every configured credential without pulling data."""

from __future__ import annotations

import argparse

from storepulse.cli.checks import Check, check_apple, check_google, check_revenuecat, check_vitals
from storepulse.cli.common import (
    APPLE_P8_SECRET,
    GOOGLE_SA_SECRET,
    REVENUECAT_KEY_SECRET,
    SMTP_PASSWORD_SECRET,
    CliError,
    Env,
    apple_client,
    google_client,
    revenuecat_client,
    store_for_run,
)
from storepulse.cli.setup import report
from storepulse.core import config, db, discovery, mailer
from storepulse.core.secrets import SecretStoreError
from storepulse.core.sources.google_auth import GoogleCredentialError, load_service_account
from storepulse.core.sources.google_client import parse_bucket_uri


def cmd_doctor(args: argparse.Namespace, env: Env) -> int:
    cfg = config.load()
    if cfg.apple is None and cfg.google is None and cfg.revenuecat is None and cfg.email is None:
        raise CliError("nothing is set up yet; run `storepulse init` first")
    checks: list[Check] = []

    def section(title: str, found: list[Check]) -> None:
        env.say(title)
        for check in found:
            report(env, check)
        checks.extend(found)

    try:
        store = store_for_run(env, cfg)
        section("Secret store", [Check("ok", store.describe())])
    except SecretStoreError as exc:
        section("Secret store", [Check("fail", str(exc))])
        return 1

    if cfg.apple is not None:
        apple = cfg.apple
        p8 = store.get(APPLE_P8_SECRET)
        if p8 is None:
            section("Apple", [Check("fail", "no Apple key saved; run `storepulse init`")])
        else:
            try:
                with apple_client(env, apple, p8) as client:
                    result = check_apple(client, apple.vendor_number, env.pacific_today())
                section("Apple", result.checks)
            except ValueError as exc:
                section("Apple", [Check("fail", str(exc))])

    if cfg.google is not None:
        raw = store.get(GOOGLE_SA_SECRET)
        if raw is None:
            section(
                "Google Play", [Check("fail", "no service account saved; run `storepulse init`")]
            )
        else:
            try:
                sa = load_service_account(raw)
                bucket = parse_bucket_uri(cfg.google.bucket_uri)
            except (GoogleCredentialError, ValueError) as exc:
                section("Google Play", [Check("fail", str(exc))])
            else:
                with google_client(env, sa) as gclient:
                    gresult = check_google(gclient, bucket)
                    conn = db.connect(config.db_path())
                    try:
                        # Register what's visible now, so apps launched after setup are
                        # picked up without re-running init.
                        discovery.register_play_apps(conn, gresult.apps)
                        packages = sorted(db.apps_for(conn, "android"))
                    finally:
                        conn.close()
                    gresult.checks.append(
                        Check(
                            "ok" if packages else "warn",
                            f"{len(gresult.apps)} Android app(s) visible, "
                            f"{len(packages)} registered.",
                        )
                    )
                    gresult.checks += check_vitals(gclient, packages)
                section("Google Play", gresult.checks)

    if cfg.revenuecat is not None:
        key = store.get(REVENUECAT_KEY_SECRET)
        if key is None:
            section("RevenueCat", [Check("fail", "no RevenueCat key saved; run `storepulse init`")])
        else:
            with revenuecat_client(env, cfg.revenuecat, key) as rc_client:
                section("RevenueCat", check_revenuecat(rc_client).checks)

    if cfg.email is None:
        # Not configured is not itself a warning, matching how an unconfigured Apple or
        # Google is silently skipped above rather than counted against the tally.
        env.say("SMTP: not configured yet; run `storepulse init` to add it.")
    else:
        password = store.get(SMTP_PASSWORD_SECRET)
        if password is None:
            section("SMTP", [Check("fail", "no SMTP password saved; run `storepulse init` again")])
        else:
            try:
                mailer.check(cfg.email, password, factory=env.smtp_factory)
            except mailer.MailerError as exc:
                section("SMTP", [Check("fail", str(exc))])
            else:
                section("SMTP", [Check("ok", f"login and NOOP succeeded for {cfg.email.host}")])

    env.say(
        "(doctor reads metadata only; the Apple check requests one day's sales report, and "
        "the RevenueCat check fetches the project metrics overview.)"
    )
    failed = [c for c in checks if c.level == "fail"]
    env.say()
    if failed:
        env.say(f"{len(failed)} check(s) failed.")
        return 1
    warned = [c for c in checks if c.level == "warn"]
    if warned:
        env.say(
            f"No failures, but {len(warned)} warning(s): some data may not be collected "
            "until they're resolved."
        )
    else:
        env.say("All checks passed.")
    return 0
