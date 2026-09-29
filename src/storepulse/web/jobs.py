"""Background work for the hosted app: the daily run, Run now, and backfills.

One job at a time, on a single worker thread; runner.run_all's lock file also guards
against a `docker compose exec … storepulse run` started by hand at the same moment.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, timedelta

import httpx

from storepulse.core import config, db, discovery, mailer, runner
from storepulse.core.secrets import DbEncryptedStore, redact
from storepulse.core.sources import apple_sales
from storepulse.core.sources.google_client import parse_bucket_uri
from storepulse.core.sources.play_common import last_months

log = logging.getLogger(__name__)


@dataclass
class JobStatus:
    running: str | None = None  # what's running now, e.g. "Run now"
    last_label: str | None = None
    last_finished: str | None = None
    last_errors: list[str] = field(default_factory=list)
    last_email: str | None = None  # "sent", or why not


@dataclass
class JobDeps:
    transport: httpx.BaseTransport | None = None
    sleep: Callable[[float], None] | None = None
    smtp_factory: mailer.SMTPFactory | None = None
    today: Callable[[], date] | None = None
    master_key: str | None = None


class Jobs:
    def __init__(self, deps: JobDeps) -> None:
        self.deps = deps
        self.status = JobStatus()
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="storepulse-job")

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _today(self) -> date:
        return self.deps.today() if self.deps.today else apple_sales.pacific_today()

    def _submit(self, label: str, work: Callable[[], None]) -> Future[None] | None:
        """Start ``work`` unless a job is already running; returns None if busy."""
        with self._lock:
            if self.status.running is not None:
                return None
            self.status.running = label

        def wrapped() -> None:
            errors: list[str] = []
            try:
                work()
            except Exception as exc:  # never let a job kill the worker thread
                message = redact(f"{type(exc).__name__}: {exc}")
                log.error("%s failed: %s", label, message)
                errors.append(message)
            finally:
                with self._lock:
                    self.status.running = None
                    self.status.last_label = label
                    self.status.last_finished = db.utc_now()
                    if errors:
                        self.status.last_errors = errors

        return self._pool.submit(wrapped)

    def run(self, label: str = "Daily run") -> Future[None] | None:
        return self._submit(label, self._run)

    def backfill(self, apple_days: int, play_months: int) -> Future[None] | None:
        return self._submit(
            "Backfill", lambda: self._backfill(apple_days=apple_days, play_months=play_months)
        )

    # -- work ---------------------------------------------------------------------------

    def _run(self) -> None:
        conn = db.connect(config.db_path())
        clients: runner.RunClients | None = None
        try:
            cfg = config.load_hosted(conn)
            store = DbEncryptedStore(conn, self.deps.master_key)
            clients = runner.clients_from_store(
                store, cfg, transport=self.deps.transport, sleep=self.deps.sleep
            )
            base_url = db.get_settings(conn).get("hosted.base_url") or None
            # Re-evaluate pairing before this run's own digest is built, so a pair that's
            # already discovered but not yet paired (e.g. a store rename) is merged in
            # today's email too, not just from tomorrow's run onward. Collection can still
            # discover brand-new apps below, so pair again after for those.
            discovery.pair_apps(conn)
            result = runner.run_all(
                conn,
                cfg,
                apple_client=clients.apple,
                google_client=clients.google,
                smtp_password=clients.smtp_password,
                send_email=cfg.email is not None,
                today=self._today(),
                smtp_factory=self.deps.smtp_factory,
                dashboard_url=str(base_url) if base_url else None,
                sleep=self.deps.sleep,
            )
            discovery.pair_apps(conn)
            self.status.last_errors = clients.errors + result.errors
            if result.email_sent:
                self.status.last_email = "sent"
            else:
                self.status.last_email = result.email_error or "not sent"
        finally:
            if clients is not None:
                clients.close()
            conn.close()

    def _backfill(self, *, apple_days: int, play_months: int) -> None:
        conn = db.connect(config.db_path())
        clients: runner.RunClients | None = None
        errors: list[str] = []
        try:
            cfg = config.load_hosted(conn)
            store = DbEncryptedStore(conn, self.deps.master_key)
            clients = runner.clients_from_store(
                store, cfg, transport=self.deps.transport, sleep=self.deps.sleep
            )
            errors += clients.errors
            today = self._today()
            yesterday = today - timedelta(days=1)
            if clients.apple is not None and cfg.apple is not None and apple_days > 0:
                span = runner.apple_sales_range(
                    yesterday - timedelta(days=apple_days - 1), yesterday, today
                )
                summary = runner.collect_apple_sales(
                    conn,
                    clients.apple,
                    cfg.apple.vendor_number,
                    span.dates,
                    sleep=self.deps.sleep,
                    today=today,
                )
                errors += runner.group_errors(summary.errors)
            if clients.google is not None and cfg.google is not None and play_months > 0:
                bucket = parse_bucket_uri(cfg.google.bucket_uri)
                discovery.refresh_play_apps(clients.google, conn, bucket)
                months = last_months(play_months, today)
                for collect in (
                    runner.collect_play_installs,
                    runner.collect_play_sales,
                    runner.collect_play_earnings,
                ):
                    play = collect(conn, clients.google, bucket, months, today=today)
                    errors += [
                        f"{play.source}: {line}" for line in runner.group_errors(play.errors)
                    ]
                vitals_days = [yesterday - timedelta(days=i) for i in reversed(range(30))]
                vitals = runner.collect_play_vitals(conn, clients.google, vitals_days)
                errors += [
                    f"{vitals.source}: {line}" for line in runner.group_errors(vitals.errors)
                ]
            discovery.pair_apps(conn)
            self.status.last_errors = errors
        finally:
            if clients is not None:
                clients.close()
            conn.close()
