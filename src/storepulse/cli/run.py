"""`storepulse run`: collect every configured source, then send the digest."""

from __future__ import annotations

import argparse
import contextlib

from storepulse.cli.common import CliError, Env, store_for_run
from storepulse.core import config, db, runner


def cmd_run(args: argparse.Namespace, env: Env) -> int:
    cfg = config.load()
    if cfg.apple is None and cfg.google is None:
        raise CliError("nothing is set up yet; run `storepulse init` first")
    store = store_for_run(env, cfg)

    # A missing or broken credential for one platform is reported, not raised, so the
    # other still runs (docs/SPEC.md, "one failing source never blocks the others").
    clients = runner.clients_from_store(store, cfg, transport=env.transport, sleep=env.sleep)
    apple, google, smtp_password = clients.apple, clients.google, clients.smtp_password

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

    result.errors = clients.errors + result.errors

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
