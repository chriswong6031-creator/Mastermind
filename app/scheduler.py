"""APScheduler wiring — fire the daily loops on a cron cadence (the "loop").

Single in-process scheduler on a SQLite jobstore so the schedule survives restarts. Two jobs:
  * 'daily_loop'       — the gated flagship book (bot.daily.run_daily, every day after close).
  * 'autonomous_daily' — the free-form Opus-Brain book (bot.autonomous.run_autonomous,
                          Mon–Fri only, after close).
Started from app.main on startup; the flagship is also exposed via POST /daily and the
autonomous book via POST /api/autonomous/run. Configure the hours with BOT_DAILY_UTC_HOUR /
AUTONOMOUS_DAILY_UTC_HOUR.
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


def _autonomous_job():
    """The free-form Opus-Brain book: researches + rebalances itself once per trading day."""
    from bot.autonomous import run_autonomous
    run_autonomous()


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
    a_hour = int(os.environ.get("AUTONOMOUS_DAILY_UTC_HOUR", "23"))
    _DB.parent.mkdir(parents=True, exist_ok=True)
    sch = BackgroundScheduler(jobstores={"default": SQLAlchemyJobStore(url=f"sqlite:///{_DB}")},
                              timezone="UTC")
    sch.add_job(_job, CronTrigger(hour=hour, minute=40), id="daily_loop",
                replace_existing=True, misfire_grace_time=3600, coalesce=True)
    # Mon–Fri only (no Sat/Sun) — the autonomous book refreshes once per trading day after close.
    sch.add_job(_autonomous_job, CronTrigger(day_of_week="mon-fri", hour=a_hour, minute=10),
                id="autonomous_daily", replace_existing=True, misfire_grace_time=3600, coalesce=True)
    sch.start()
    _scheduler = sch
    return sch


def maybe_first_autonomous_run() -> bool:
    """On first turn-on, immediately build the autonomous book so it can buy right away —
    instead of waiting for the next scheduled close. No-op once it has a NAV track record.

    Runs in a daemon thread so FastAPI startup never blocks on the (long) Brain call. Gated on
    the Claude reasoning layer being available (no point arming the Brain otherwise) and on
    AUTONOMOUS_FIRST_RUN != '0'. Returns True if a first run was kicked off.
    """
    if os.environ.get("AUTONOMOUS_FIRST_RUN", "1") == "0":
        return False
    try:
        from portfolio import registry
        nav_path = registry.data_dir("autonomous") / "nav_history.jsonl"
        if nav_path.exists() and nav_path.read_text().strip():
            return False                       # already has a track record — the cron owns it now
    except Exception:
        pass
    try:
        from brain import cli_bridge
        if not cli_bridge.available():
            return False                       # no subscription/CLI → don't fire a doomed armed run
    except Exception:
        return False
    import threading

    def _go():
        try:
            from bot.autonomous import run_autonomous
            run_autonomous()
        except Exception:
            pass

    threading.Thread(target=_go, name="autonomous-first-run", daemon=True).start()
    return True
