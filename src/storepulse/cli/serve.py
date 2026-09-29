"""`storepulse serve`: run the hosted web app (docs/SPEC.md, hosted mode)."""

from __future__ import annotations

import argparse
import importlib.util
import logging

from storepulse.cli.common import CliError, Env
from storepulse.core import config

WEB_MODULES = ("fastapi", "uvicorn", "jinja2", "multipart", "argon2", "pyotp", "apscheduler")


def missing_web_modules() -> list[str]:
    return [name for name in WEB_MODULES if importlib.util.find_spec(name) is None]


def cmd_serve(args: argparse.Namespace, env: Env) -> int:
    missing = missing_web_modules()
    if missing:
        raise CliError(
            "the hosted web app needs the optional web dependencies "
            f"({', '.join(missing)} missing): install `storepulse[web]`, or run the Docker "
            "image (README, 'Hosted mode')"
        )
    # `serve` always means hosted mode: settings and secrets live in the database, so any
    # CLI command this process runs (and the scheduler's runs) uses them too.
    config.enter_hosted_mode()
    # A long-running server logs its runs at info level (`docker compose logs`); main()
    # already installed the redacting handler on this logger.
    logging.getLogger("storepulse").setLevel(logging.INFO)

    import uvicorn

    from storepulse.web.app import create_app

    app = create_app()  # reads and checks the master key before binding the port
    env.say(f"Storepulse on http://{args.host}:{args.port} (data: {config.data_dir()})")
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        proxy_headers=True,
        forwarded_allow_ips=config.trusted_proxies(),
        log_level="info",
        server_header=False,
    )
    return 0
