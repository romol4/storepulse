"""The hosted app's in-process daily scheduler (docs/SPEC.md, Scheduling and
reliability: "an in-process scheduler (APScheduler) in the container")."""

from __future__ import annotations

import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from storepulse.web.jobs import Jobs

log = logging.getLogger(__name__)

JOB_ID = "daily-run"
DEFAULT_TIMEZONE = "UTC"


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("unknown time zone %r; using %s", name, DEFAULT_TIMEZONE)
        return ZoneInfo(DEFAULT_TIMEZONE)


def trigger(run_time: str, timezone: str) -> CronTrigger:
    hour, minute = (int(part) for part in run_time.split(":"))
    return CronTrigger(hour=hour, minute=minute, timezone=zone(timezone))


class Scheduler:
    def __init__(self, jobs: Jobs) -> None:
        self._jobs = jobs
        self._scheduler = BackgroundScheduler()

    def start(self, run_time: str, timezone: str) -> None:
        self._scheduler.add_job(
            self._fire,
            trigger(run_time, timezone),
            id=JOB_ID,
            replace_existing=True,
            misfire_grace_time=6 * 3600,  # a late wake-up still runs (7-day re-pull)
            coalesce=True,
        )
        self._scheduler.start()
        log.info("daily run scheduled at %s (%s)", run_time, timezone or DEFAULT_TIMEZONE)

    def reschedule(self, run_time: str, timezone: str) -> None:
        if self._scheduler.running:
            self._scheduler.reschedule_job(JOB_ID, trigger=trigger(run_time, timezone))

    def next_run(self) -> str | None:
        job = self._scheduler.get_job(JOB_ID) if self._scheduler.running else None
        fire = getattr(job, "next_run_time", None) if job is not None else None
        return fire.isoformat(timespec="minutes") if fire is not None else None

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    def _fire(self) -> None:
        if self._jobs.run("Daily run") is None:
            log.warning("skipped the scheduled run: another job is still running")
