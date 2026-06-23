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


def _settle_pending_job():
    """Settle the US books' queued orders at the OPEN, during market hours.

    All books DECIDE after their close (the flagship at 22:40 UTC; the US Brain books at 23:10/23:15)
    and, while the market is shut, only QUEUE their target — they never book an off-hours fill. This
    morning sweep settles them at the real session open: the flagship's queued buy orders
    (queue_orders → fill_pending) and the US Brain books' queued target (autonomous + etf →
    paper_account.settle_target, a full rebalance to the decided book at the open mark), then
    republishes so the dashboard renders the freshly-filled positions. Idempotent + never raises."""
    try:
        from scripts.fill_pending_now import settle
        settle("flagship", require_open=True)
    except Exception:  # noqa: BLE001 — a settle miss must never kill the scheduler
        pass
    try:
        from bot import settle as _settle
        _settle.settle_us()                      # autonomous + etf: settle the queued target at the US open
    except Exception:  # noqa: BLE001
        pass


def _settle_brain_asia_job():
    """Settle the Greater-China Brain books' queued targets at the A-share OPEN (~01:30 UTC). The
    china/hk books decide after their close and queue; this fills the queued target at the next open
    via a full rebalance, then republishes. No-op when the market is shut or nothing is queued."""
    try:
        from bot import settle as _settle
        _settle.settle_asia()                    # china + hk
    except Exception:  # noqa: BLE001
        pass


def _watch_us_job():
    """Overnight watch for the US Brain books: between the US close and the next open, re-read the live
    overnight tape; on a MATERIAL move (deterministic tripwire — free, no LLM) re-prompt the Brain to
    revise its queued target (which settles at the open). Cheap on a calm tape; never raises."""
    try:
        from bot import overnight
        overnight.watch_us()
    except Exception:  # noqa: BLE001 — a watch miss must never kill the scheduler
        pass


def _watch_asia_job():
    """Overnight watch for the Greater-China Brain books (china + hk) between their close and the next
    A-share open. Same tripwire→refine discipline as the US watch. Never raises."""
    try:
        from bot import overnight
        overnight.watch_asia()
    except Exception:  # noqa: BLE001
        pass


def _macro_refresh_job():
    """Keep the vendored macro analyzer data fresh (origin/main == the live site) + run the
    staleness tripwire. The book once bought NVDA off a days-stale read; never raises."""
    try:
        from data_layer import macro_refresh
        macro_refresh.refresh_and_check()
    except Exception:  # noqa: BLE001 — a refresh miss must never kill the scheduler
        pass


def _cio_weekly_job():
    """CIO / Meta-PM weekly accountability review: reads per-role calibration multipliers + each seat's
    graded KPIs + book NAV-vs-benchmark + the shadow leaderboard, and WRITES a 'what is working / who is
    miscalibrated' note + non-binding tuning recommendations to data/brain/cio/<isoweek>.{json,md}.
    It RECOMMENDS ONLY — it never trades, never flips a flag, never changes a seat's behavior. Lazy
    import + try/except so a review miss never kills the scheduler."""
    try:
        from scripts.run_cio import run as run_cio
        run_cio()
    except Exception:  # noqa: BLE001 — a CIO miss must never kill the scheduler
        pass


# The books that the deterministic/Brain builders mark only on their OWN run day. flagship marks
# on its build (and on its carried-forward sweep), each Brain book on its run — but a book that
# does NOT rebuild on a given day never advances its nav_history, so it can't be graded forward.
# self_directed is excluded: it is NOT a paper_account book (its own engine owns its NAV).
_MARK_BOOK_IDS = ["flagship", "autonomous", "heavyweight", "china", "hk", "etf"]


def _daily_mark_job():
    """Mark EVERY paper book to market once per trading day, regardless of whether it rebuilt.

    The blocker this closes: a book built once is never re-marked on non-rebuild days (flagship's
    own mark only fires when phase2.run executes; each Brain book marks only on its own run), so
    nav_history never advances and held positions are never re-priced — nothing can be graded
    forward. This read-only sweep loads each book, gathers a live mark for every held ticker plus
    that book's benchmark, and appends an idempotent-per-date nav_history row. It NEVER trades, never
    touches cash/positions, and is best-effort per book — one book failing cannot abort the others.

    Runs Mon–Fri shortly BEFORE the flagship build (22:35 UTC) so a fresh daily mark is in place
    before the evening builds; mark() is idempotent per date, so a later same-day build just
    replaces the row. Never raises into the scheduler."""
    try:
        from portfolio import paper_account, registry
    except Exception:  # noqa: BLE001
        return
    for pid in _MARK_BOOK_IDS:
        try:
            # cash sweep first: idle cash earns ~4%/yr (money-market), idempotent per date, so the
            # NAV we mark below already reflects today's accrued cash. Best-effort (never raises).
            paper_account.accrue_cash_yield(_today_iso(), portfolio_id=pid)
            state = paper_account._load_account(pid)
            bench = paper_account._benchmark_for(pid)
            tickers = set(state.get("positions", {}).keys()) | {bench}
            prices: dict = {}
            for t in tickers:
                px = paper_account._current_price(t)
                if px and px > 0:
                    prices[t] = px
            if prices:
                paper_account.mark(prices, _today_iso(), portfolio_id=pid, benchmark=bench)
        except Exception:  # noqa: BLE001 — one book's mark miss must never kill the sweep
            continue


def _today_iso() -> str:
    from datetime import date
    return date.today().isoformat()


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
    # Settle flagship's overnight-queued orders the morning AFTER they were queued — during the US
    # session so they fill at the real open. 15:00 UTC is safely post-open year-round (9:30 ET =
    # 13:30 UTC under EDT / 14:30 UTC under EST); the job itself re-checks market_calendar.is_open().
    settle_hour = int(os.environ.get("SETTLE_PENDING_UTC_HOUR", "15"))
    _DB.parent.mkdir(parents=True, exist_ok=True)
    sch = BackgroundScheduler(jobstores={"default": SQLAlchemyJobStore(url=f"sqlite:///{_DB}")},
                              timezone="UTC")
    # FRESHNESS FOUNDATION: pull the vendored macro analyzer data (origin/main == the live site)
    # every 3h so no book ever decides on a stale read (the NVDA stale-"Constructive"-vs-live-"avoid"
    # bug). The staleness tripwire warns, or refuses to trade via MACRO_STALE_BLOCK=1. run_daily also
    # refreshes inline as a belt-and-suspenders guard right before the flagship build reads.
    sch.add_job(_macro_refresh_job, CronTrigger(hour="*/3", minute=30), id="macro_refresh",
                replace_existing=True, misfire_grace_time=3600, coalesce=True)
    # DAILY MARK-TO-MARKET: re-price EVERY paper book once per trading day, even when it does not
    # rebuild — otherwise a book built once never advances its nav_history and can't be graded
    # forward. Fires Mon–Fri at <flagship hour>:35, just BEFORE the 22:40 flagship build, so a fresh
    # daily mark is in place before the evening builds (mark() is idempotent per date, so a later
    # same-day build merely replaces the row). UTC pinned for the same reason as settle_pending below.
    sch.add_job(_daily_mark_job,
                CronTrigger(day_of_week="mon-fri", hour=hour, minute=35, timezone="UTC"),
                id="daily_mark", replace_existing=True, misfire_grace_time=3600, coalesce=True)
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
    # Settle flagship's queued PENDING orders at the open (Mon–Fri, 15:00 UTC ≈ 10–11am ET — mid US
    # session year-round). Closes the gap left by the post-close-only build, which queues overnight
    # buys but never reaches fill_pending — so without this the gated book never actually trades.
    # NOTE: timezone is pinned to UTC explicitly. A bare CronTrigger(hour=…) inherits the MACHINE's
    # local tz (APScheduler ignores the scheduler's timezone for an already-tz'd trigger), which
    # would drift this off the US session on a non-UTC host — fatal here, since the is_open() guard
    # would then skip every run. Cheap + idempotent (no-op when nothing's queued or the market's shut).
    sch.add_job(_settle_pending_job,
                CronTrigger(day_of_week="mon-fri", hour=settle_hour, minute=0, timezone="UTC"),
                id="settle_pending", replace_existing=True, misfire_grace_time=7200, coalesce=True)
    # Settle the Greater-China Brain books' queued targets at the A-share OPEN (09:30 CST = 01:30
    # UTC). The china/hk builds run after their close (08:00/09:00 UTC) and only QUEUE; this fills
    # the queued target at the next open. UTC-pinned (same reason as settle_pending). Idempotent +
    # no-op when the market's shut — the settle re-checks china_calendar.is_open() per book.
    asia_settle_hour = int(os.environ.get("ASIA_SETTLE_UTC_HOUR", "1"))
    sch.add_job(_settle_brain_asia_job,
                CronTrigger(day_of_week="mon-fri", hour=asia_settle_hour, minute=35, timezone="UTC"),
                id="settle_brain_asia", replace_existing=True, misfire_grace_time=7200, coalesce=True)
    # OVERNIGHT WATCH — let the Brain books re-decide on the LIVE overnight tape before the open. A
    # deterministic tripwire (data_layer.overnight: futures/intl/vol risk read) gates the Opus refine,
    # so most ticks are free; the Brain only re-prompts on a material overnight move, revising its
    # queued target (which settles at the open). US books: a few ticks between the US close and open
    # (~02/06/11 UTC). Asia books: between their close and the next A-share open (~14/20/00 UTC).
    us_watch_hours = (os.environ.get("US_WATCH_UTC_HOURS", "2,6,11").strip() or "2,6,11")
    asia_watch_hours = (os.environ.get("ASIA_WATCH_UTC_HOURS", "14,20,0").strip() or "14,20,0")
    sch.add_job(_watch_us_job, CronTrigger(day_of_week="mon-fri", hour=us_watch_hours, minute=20, timezone="UTC"),
                id="watch_us_overnight", replace_existing=True, misfire_grace_time=3600, coalesce=True)
    sch.add_job(_watch_asia_job, CronTrigger(day_of_week="mon-fri", hour=asia_watch_hours, minute=20, timezone="UTC"),
                id="watch_asia_overnight", replace_existing=True, misfire_grace_time=3600, coalesce=True)
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

    # CIO / Meta-PM weekly accountability review (additive, read-only — recommends, never trades).
    # Default Sunday 10:00 UTC; configurable via CIO_WEEKLY_DAY / CIO_WEEKLY_UTC_HOUR.
    cio_dow = os.environ.get("CIO_WEEKLY_DAY", "sun")
    cio_hour = int(os.environ.get("CIO_WEEKLY_UTC_HOUR", "10"))
    sch.add_job(_cio_weekly_job,
                CronTrigger(day_of_week=cio_dow, hour=cio_hour, minute=0, timezone="UTC"),
                id="cio_weekly", replace_existing=True, misfire_grace_time=7200, coalesce=True)
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
