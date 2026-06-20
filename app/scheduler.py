"""APScheduler wiring — fire the daily loop on a cron cadence (the "loop").

Single in-process scheduler on a SQLite jobstore so the schedule survives restarts. The
daily job runs bot.daily.run_daily after the US close. Started from app.main on startup;
also exposed via POST /daily for a manual trigger. Configure the hour with BOT_DAILY_UTC_HOUR.
"""
from __future__ import annotations

import os
from pathlib import Path

import bot  # noqa: F401

_DB = Path(__file__).resolve().parent.parent / "data" / "scheduler.sqlite"
_scheduler = None


def _job():
    from bot.daily import run_daily
    run_daily()


def start():
    """Start the daily-loop scheduler (idempotent). Returns the scheduler or None."""
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        return None
    hour = int(os.environ.get("BOT_DAILY_UTC_HOUR", "22"))
    _DB.parent.mkdir(parents=True, exist_ok=True)
    sch = BackgroundScheduler(jobstores={"default": SQLAlchemyJobStore(url=f"sqlite:///{_DB}")},
                              timezone="UTC")
    sch.add_job(_job, CronTrigger(hour=hour, minute=40), id="daily_loop",
                replace_existing=True, misfire_grace_time=3600, coalesce=True)
    sch.start()
    _scheduler = sch
    return sch
