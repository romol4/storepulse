"""`storepulse` command line."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence

from storepulse import __version__
from storepulse.cli.collect import SOURCES, cmd_backfill
from storepulse.cli.common import CliError, Env
from storepulse.cli.digest import cmd_digest
from storepulse.cli.doctor import cmd_doctor
from storepulse.cli.run import cmd_run
from storepulse.cli.schedule import cmd_schedule_install, cmd_schedule_remove, cmd_schedule_show
from storepulse.cli.setup import cmd_init
from storepulse.core import config
from storepulse.core.mailer import MailerError
from storepulse.core.runner import LockedError
from storepulse.core.secrets import RedactingFilter, SecretStoreError, redact
from storepulse.core.sources.apple_client import AppleError
from storepulse.core.sources.google_client import GoogleError

__all__ = ["Env", "build_parser", "main"]


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

    backfill = sub.add_parser(
        "backfill",
        help="load historical data for a source",
        description=(
            "apple_sales and play_vitals load by day (--days N, or --from YYYY-MM-DD "
            "[--to YYYY-MM-DD]); play_installs, play_sales and play_earnings load by month "
            "(--months N, or --from YYYY-MM [--to YYYY-MM])."
        ),
    )
    backfill.add_argument("--source", required=True, choices=SOURCES)
    when = backfill.add_mutually_exclusive_group(required=True)
    when.add_argument("--days", type=int, help="the last N days, ending yesterday")
    when.add_argument("--months", type=int, help="the last N months, including this one")
    when.add_argument("--from", dest="from_date", help="YYYY-MM-DD, or YYYY-MM for monthly sources")
    backfill.add_argument("--to", dest="to_date", help="end of the --from range (inclusive)")
    backfill.set_defaults(func=cmd_backfill)

    doctor = sub.add_parser("doctor", help="test every saved credential without pulling data")
    doctor.set_defaults(func=cmd_doctor)

    run = sub.add_parser("run", help="collect every configured source, then send the digest")
    run.add_argument("--days", type=int, help="days to re-pull (default: [schedule].days)")
    run.add_argument(
        "--no-email", action="store_true", help="collect and build the digest, don't send it"
    )
    run.set_defaults(func=cmd_run)

    digest = sub.add_parser("digest", help="print the digest; never sends it")
    digest.add_argument(
        "--dry-run", action="store_true", help="print the plain-text digest (this is always true)"
    )
    digest.add_argument("--html", metavar="FILE", help="also write the HTML digest to FILE")
    digest.set_defaults(func=cmd_digest)

    schedule = sub.add_parser("schedule", help="manage the OS-native daily schedule")
    schedule_sub = schedule.add_subparsers(dest="schedule_command", required=True)

    install = schedule_sub.add_parser("install", help="install the daily schedule")
    install.add_argument("--time", metavar="HH:MM", help="run time (default: [schedule].run_time)")
    install.set_defaults(func=cmd_schedule_install)

    remove = schedule_sub.add_parser("remove", help="remove the daily schedule")
    remove.set_defaults(func=cmd_schedule_remove)

    show = schedule_sub.add_parser("show", help="show the current schedule status")
    show.set_defaults(func=cmd_schedule_show)

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
    except (
        CliError,
        AppleError,
        GoogleError,
        SecretStoreError,
        config.ConfigError,
        MailerError,
        LockedError,
    ) as exc:
        print(f"error: {redact(str(exc))}", file=env.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=env.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
