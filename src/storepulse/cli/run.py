"""`storepulse run`: collect every configured source, then send the digest."""

from __future__ import annotations

import argparse
import contextlib

from storepulse.cli.common import (
    APPLE_P8_SECRET,
    GOOGLE_SA_SECRET,
    SMTP_PASSWORD_SECRET,
    CliError,
    Env,
    apple_client,
    google_client,
    store_for_run,
)
from storepulse.core import config, db, runner
from storepulse.core.sources.apple_client import AppleClient
from storepulse.core.sources.google_auth import GoogleCredentialError, load_service_account
from storepulse.core.sources.google_client import GoogleClient


def cmd_run(args: argparse.Namespace, env: Env) -> int:
    cfg = config.load()
    if cfg.apple is None and cfg.google is None:
        raise CliError("nothing is set up yet; run `storepulse init` first")
    store = store_for_run(env, cfg)

    apple: AppleClient | None = None
    if cfg.apple is not None:
        p8 = store.get(APPLE_P8_SECRET)
        if p8 is None:
            raise CliError(f"no Apple key found in {store.describe()}; run `storepulse init` again")
        apple = apple_client(env, cfg.apple, p8)

    google: GoogleClient | None = None
    if cfg.google is not None:
        raw = store.get(GOOGLE_SA_SECRET)
        if raw is None:
            raise CliError(
                f"no Google service account found in {store.describe()}; "
                "run `storepulse init` again"
            )
        try:
            sa = load_service_account(raw)
        except GoogleCredentialError as exc:
            raise CliError(str(exc)) from None
        google = google_client(env, sa)

    smtp_password = store.get(SMTP_PASSWORD_SECRET) if cfg.email is not None else None

    def progress(label: str, outcome: str) -> None:
        env.say(f"  {label}  {outcome}")

    conn = db.connect(config.db_path())
    try:
        with contextlib.ExitStack() as stack:
            if apple is not None:
                stack.enter_context(apple)
            if google is not None:
                stack.enter_context(google)
            result = runner.run_all(
                conn,
                cfg,
                apple_client=apple,
                google_client=google,
                smtp_password=smtp_password,
                days=args.days,
                send_email=not args.no_email,
                today=env.pacific_today(),
                smtp_factory=env.smtp_factory,
                progress=progress,
            )
    finally:
        conn.close()

    if result.errors:
        env.warn(f"{len(result.errors)} source(s) had problems:")
        for error in result.errors:
            env.warn(f"  {error}")
    if result.email_error:
        env.warn(f"email: {result.email_error}")
    elif result.email_sent:
        env.say("Digest sent.")
    elif args.no_email:
        env.say("Digest built (--no-email).")
    else:
        env.say("Digest not sent.")
    return 0 if result.ok else 1
