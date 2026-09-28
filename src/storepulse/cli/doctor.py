"""`storepulse doctor`: test every configured credential without pulling data."""

from __future__ import annotations

import argparse

from storepulse.cli.checks import Check, check_apple, check_google, check_vitals
from storepulse.cli.common import (
    APPLE_P8_SECRET,
    GOOGLE_SA_SECRET,
    CliError,
    Env,
    apple_client,
    google_client,
    store_for_run,
)
from storepulse.cli.setup import report
from storepulse.core import config, db, discovery
from storepulse.core.secrets import SecretStoreError
from storepulse.core.sources.google_auth import GoogleCredentialError, load_service_account
from storepulse.core.sources.google_client import parse_bucket_uri


def cmd_doctor(args: argparse.Namespace, env: Env) -> int:
    cfg = config.load()
    if cfg.apple is None and cfg.google is None:
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

    env.say("SMTP: not configured yet (arrives with the daily email).")
    env.say("(doctor reads metadata only; the Apple check requests one day's sales report.)")
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
