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


def _heavyweight_job():
    """The concentrated Opus-Brain book: studies Flagship's book and presses its best ideas. Runs
    AFTER flagship + autonomous so it constrains against a fresh Flagship book."""
    from bot.heavyweight import run_heavyweight
    run_heavyweight()


def _china_job():
    """The free-form China A-share Opus-Brain book: researches the China desks + rebalances itself
    once per Asia trading day, after the mainland A-share close (~07:00 UTC)."""
    from bot.china import run_china
    run_china()


def _hk_job():
    """The free-form Hong-Kong Opus-Brain book (HK listings only, HKD): researches the China desks +
    rebalances itself once per Asia trading day, after the HK close (~08:00 UTC)."""
    from bot.hk import run_hk
    run_hk()


def _etf_job():
    """The free-form ETF Opus-Brain book: rotates across US-listed ETFs (index/sector/factor/duration/
    cash) under an ETF-adapted doctrine + risk guardrails, once per US trading day after the close."""
    from bot.etf import run_etf
    run_etf()


def _snapshot_job():
    """Publish a static snapshot of the dashboard to the public Macro Dashboard (GitHub Pages).
    Writes site/mastermind/mastermind_snapshot.json into the macro repo (via the vendor/macro
    symlink) and pushes it to origin/main. Resilient — never raises into the scheduler."""
    from scripts.export_macro_snapshot import run as export_snapshot
    export_snapshot()


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
    h_hour = int(os.environ.get("HEAVYWEIGHT_DAILY_UTC_HOUR", "23"))
    # China book fires on Asia's clock: the A-share close is 15:00 CST = 07:00 UTC, so build a bit
    # after (08:00 UTC ≈ 16:00 CST). Separate from the US books' evening cadence.
    cn_hour = int(os.environ.get("CHINA_DAILY_UTC_HOUR", "8"))
    hk_hour = int(os.environ.get("HK_DAILY_UTC_HOUR", "9"))
    _DB.parent.mkdir(parents=True, exist_ok=True)
    sch = BackgroundScheduler(jobstores={"default": SQLAlchemyJobStore(url=f"sqlite:///{_DB}")},
                              timezone="UTC")
    sch.add_job(_job, CronTrigger(hour=hour, minute=40), id="daily_loop",
                replace_existing=True, misfire_grace_time=3600, coalesce=True)
    # Mon–Fri only (no Sat/Sun) — the autonomous book refreshes once per trading day after close.
    sch.add_job(_autonomous_job, CronTrigger(day_of_week="mon-fri", hour=a_hour, minute=10),
                id="autonomous_daily", replace_existing=True, misfire_grace_time=3600, coalesce=True)
    # Heavyweight runs LAST (23:25 by default) — after flagship's 22:40 build (so it constrains
    # against a fresh Flagship book) and after autonomous's 23:10 (so the two Brain runs don't
    # hammer the subscription/price feeds at once; they touch disjoint data dirs — no state race).
    sch.add_job(_heavyweight_job, CronTrigger(day_of_week="mon-fri", hour=h_hour, minute=25),
                id="heavyweight_daily", replace_existing=True, misfire_grace_time=3600, coalesce=True)
    # All-China book on Asia's clock (Mon–Fri after the A-share close). Touches a disjoint data dir
    # (data/portfolios/china) and a different feed window from the US books — no state race.
    sch.add_job(_china_job, CronTrigger(day_of_week="mon-fri", hour=cn_hour, minute=0),
                id="china_daily", replace_existing=True, misfire_grace_time=3600, coalesce=True)
    # HK book on Asia's clock (Mon–Fri after the HK close, ~09:00 UTC). Disjoint data dir
    # (data/portfolios/hk) — no state race with the A-share china book.
    sch.add_job(_hk_job, CronTrigger(day_of_week="mon-fri", hour=hk_hour, minute=0),
                id="hk_daily", replace_existing=True, misfire_grace_time=3600, coalesce=True)
    # ETF book on the US evening cadence (Mon–Fri after the close), staggered 5 min after the
    # autonomous book so the two US Brain runs don't hammer the subscription/price feeds at once;
    # disjoint data dir (data/portfolios/etf) — no state race.
    sch.add_job(_etf_job, CronTrigger(day_of_week="mon-fri", hour=a_hour, minute=15),
                id="etf_daily", replace_existing=True, misfire_grace_time=3600, coalesce=True)
    # Publish the dashboard snapshot to the public Macro Dashboard (GitHub Pages) TWICE a day:
    #   • ~12:25 UTC — a morning refresh that picks up the overnight China book (08:00) and the
    #     prior night's autonomous/heavyweight Brain books (23:xx).
    #   • ~22:25 UTC — a post-close push, after the 22:00 flagship book and BEFORE the macro
    #     daily build (22:40 UTC), so the evening deploy carries a fresh snapshot.
    # Hours are configurable via MACRO_SNAPSHOT_UTC_HOURS (comma-separated, default "12,22").
    # Runs every day (the macro site refreshes daily); touches only the macro repo's
    # site/mastermind/ path and pushes to its origin/main.
    snap_hours = (os.environ.get("MACRO_SNAPSHOT_UTC_HOURS", "12,22").strip() or "12,22")
    sch.add_job(_snapshot_job, CronTrigger(hour=snap_hours, minute=25),
                id="publish_macro_snapshot", replace_existing=True,
                misfire_grace_time=3600, coalesce=True)
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


def maybe_first_heavyweight_run() -> bool:
    """On first turn-on, build the Heavyweight book right away (instead of waiting for the next
    close), but ONLY once Flagship has published a non-empty book to constrain against. No-op once
    Heavyweight has a NAV track record. Gated on the Claude layer being available + the Flagship
    universe being non-empty + HEAVYWEIGHT_FIRST_RUN != '0'. Runs in a daemon thread."""
    if os.environ.get("HEAVYWEIGHT_FIRST_RUN", "1") == "0":
        return False
    try:
        from portfolio import registry
        nav_path = registry.data_dir("heavyweight") / "nav_history.jsonl"
        if nav_path.exists() and nav_path.read_text().strip():
            return False                       # already tracking — the cron owns it now
    except Exception:
        pass
    try:
        from bot.heavyweight import _flagship_universe
        if not _flagship_universe():
            return False                       # nothing to constrain against yet — wait for Flagship
    except Exception:
        return False
    try:
        from brain import cli_bridge
        if not cli_bridge.available():
            return False                       # no subscription/CLI → don't fire a doomed armed run
    except Exception:
        return False
    import threading

    def _go():
        try:
            from bot.heavyweight import run_heavyweight
            run_heavyweight()
        except Exception:
            pass

    threading.Thread(target=_go, name="heavyweight-first-run", daemon=True).start()
    return True


def maybe_first_china_run() -> bool:
    """On first turn-on, immediately build the all-China book so it can buy right away — instead of
    waiting for the next Asia close. No-op once it has a NAV track record. Gated on the Claude layer
    being available + CHINA_FIRST_RUN != '0'. Runs in a daemon thread (never blocks startup)."""
    if os.environ.get("CHINA_FIRST_RUN", "1") == "0":
        return False
    try:
        from portfolio import registry
        nav_path = registry.data_dir("china") / "nav_history.jsonl"
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
            from bot.china import run_china
            run_china()
        except Exception:
            pass

    threading.Thread(target=_go, name="china-first-run", daemon=True).start()
    return True


def maybe_first_hk_run() -> bool:
    """On first turn-on, immediately build the HK book so it can buy right away. No-op once it has a
    NAV track record. Gated on the Claude layer being available + HK_FIRST_RUN != '0'. Daemon thread."""
    if os.environ.get("HK_FIRST_RUN", "1") == "0":
        return False
    try:
        from portfolio import registry
        nav_path = registry.data_dir("hk") / "nav_history.jsonl"
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
            from bot.hk import run_hk
            run_hk()
        except Exception:
            pass

    threading.Thread(target=_go, name="hk-first-run", daemon=True).start()
    return True


def maybe_first_etf_run() -> bool:
    """On first turn-on, immediately build the ETF book so it can rotate right away — instead of
    waiting for the next US close. No-op once it has a NAV track record. Gated on the Claude layer
    being available + ETF_FIRST_RUN != '0'. Runs in a daemon thread (never blocks startup)."""
    if os.environ.get("ETF_FIRST_RUN", "1") == "0":
        return False
    try:
        from portfolio import registry
        nav_path = registry.data_dir("etf") / "nav_history.jsonl"
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
            from bot.etf import run_etf
            run_etf()
        except Exception:
            pass

    threading.Thread(target=_go, name="etf-first-run", daemon=True).start()
    return True
