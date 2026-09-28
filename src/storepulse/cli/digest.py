"""`storepulse digest --dry-run`: print the digest, optionally write its HTML, never send it."""

from __future__ import annotations

import argparse
from pathlib import Path

from storepulse.cli.common import Env
from storepulse.core import config, db
from storepulse.core import digest as digest_core


def cmd_digest(args: argparse.Namespace, env: Env) -> int:
    cfg = config.load()
    conn = db.connect(config.db_path())
    try:
        built = digest_core.build_digest(conn, cfg, today=env.pacific_today())
    finally:
        conn.close()
    env.say(built.text)
    if args.html:
        Path(args.html).write_text(built.html, encoding="utf-8")
        env.say(f"HTML written to {args.html}")
    return 0
