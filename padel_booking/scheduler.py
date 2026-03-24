"""
Scheduling layer — wraps APScheduler.

Two scheduler types:
  ProductionScheduler  — fires every Tuesday at 06:00 Europe/Amsterdam (DST-aware)
  OnceScheduler        — fires once at a specific datetime (for TEST_RUN_AT)

Both call the same booking_job() function, keeping test and production paths
identical.
"""
from __future__ import annotations

import logging
import signal
import sys
from datetime import datetime
from typing import Callable

import pytz
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

log = logging.getLogger(__name__)

TZ = pytz.timezone("Europe/Amsterdam")


class ProductionScheduler:
    """
    Fires every Tuesday at 06:00 Europe/Amsterdam.
    Blocks the calling thread until SIGINT/SIGTERM.
    """

    def __init__(self, job_fn: Callable) -> None:
        self._scheduler = BlockingScheduler(timezone=TZ)
        self._job_fn = job_fn

    def start(self) -> None:
        trigger = CronTrigger(
            day_of_week="tue",
            hour=6,
            minute=0,
            second=0,
            timezone=TZ,
        )
        self._scheduler.add_job(
            self._job_fn,
            trigger=trigger,
            id="weekly_padel_booking",
            name="Weekly Padel Booking",
            max_instances=1,
            misfire_grace_time=3600,  # fire up to 1 h late if the process was down
            coalesce=True,
        )
        _register_signal_handlers(self._scheduler)
        log.info(
            "Production scheduler started — job fires every Tuesday 06:00 Amsterdam. "
            "Next: %s",
            self._next_run(),
        )
        self._scheduler.start()

    def _next_run(self) -> str:
        jobs = self._scheduler.get_jobs()
        if jobs:
            nf = jobs[0].next_run_time
            return str(nf) if nf else "unknown"
        return "unknown"


class OnceScheduler:
    """
    Fires once at *run_at* (a tz-aware datetime) then exits.
    Used for TEST_RUN_AT.
    """

    def __init__(self, job_fn: Callable, run_at: datetime) -> None:
        self._scheduler = BlockingScheduler(timezone=TZ)
        self._job_fn = job_fn
        self._run_at = run_at

    def start(self) -> None:
        self._scheduler.add_job(
            self._job_fn,
            trigger=DateTrigger(run_date=self._run_at, timezone=TZ),
            id="once_padel_booking",
            name="One-off Padel Booking",
            max_instances=1,
        )

        # Auto-shutdown after the job fires
        def _after_job(event):
            log.info("One-off job completed — shutting down scheduler")
            self._scheduler.shutdown(wait=False)

        from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_ERROR
        self._scheduler.add_listener(_after_job, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)

        _register_signal_handlers(self._scheduler)
        log.info("One-off scheduler — will fire at %s", self._run_at)
        self._scheduler.start()


def _register_signal_handlers(scheduler) -> None:
    """Graceful shutdown on SIGINT/SIGTERM."""
    def _stop(sig, frame):
        log.info("Signal %s received — shutting down scheduler", sig)
        scheduler.shutdown(wait=False)
        sys.exit(0)

    try:
        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)
    except (OSError, ValueError):
        # Signal handling not available in some environments (e.g. threads)
        pass
