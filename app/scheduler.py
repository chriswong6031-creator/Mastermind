"""APScheduler wiring — fire the daily loops on a cron cadence (the "loop").

Single in-process scheduler on a SQLite jobstore so the schedule survives restarts. 18 jobs:
  * 'macro_refresh'       — pull vendored macro data every 3 h (belt-and-suspenders freshness).
  * 'daily_mark'          — mark all paper books to NAV daily before the flagship build.
  * 'daily_loop'          — gated flagship book (bot.daily.run_daily, Mon–Fri after close).
  * 'autonomous_daily'    — free-form Opus-Brain US book (bot.autonomous, Mon–Fri).
  * 'heavyweight_daily'   — heavyweight book (bot.heavyweight, Mon–Fri).
  * 'china_daily'         — CN Brain book (bot.china_daily, Mon–Fri).
  * 'hk_daily'            — HK Brain book (bot.hk_daily, Mon–Fri).
  * 'etf_daily'           — ETF Brain book (bot.etf_daily, Mon–Fri).
  * 'settle_pending'      — settle Self-Directed pending orders at the US open (Mon–Fri).
  * 'settle_brain_asia'   — settle asia Brain pending orders at the HK/CN open (Mon–Fri).
  * 'watch_us'            — intraday watchlist review for US books (Mon–Fri).
  * 'watch_asia'          — intraday watchlist review for Asia books (Mon–Fri).
  * 'derisk_us'           — fast de-risk tripwire for US books (Mon–Fri; armed via MASTERMIND_FAST_DERISK).
  * 'snapshot'            — portfolio snapshot capture at configured hours (Mon–Fri).
  * 'cio_weekly'          — weekly CIO review (Mon only).
  * 'improvement_agenda'  — weekly improvement-agenda refresh.
  * 'loop_maintenance'    — periodic ledger + experiment maintenance.
  * 'experiment_maturity' — experiment maturity sweep.
Started from app.main on startup; the flagship is also exposed via POST /daily and the
autonomous book via POST /api/autonomous/run. Configure the hours with BOT_DAILY_UTC_HOUR /
AUTONOMOUS_DAILY_UTC_HOUR.

WEEKEND HYGIENE NOTE (MW1 investigation)
-----------------------------------------
daily_loop fires the flagship book build; a session requires market data (which only exists on
trading days) — no documented intent to run on weekends. Changed to mon-fri.

publish_macro_snapshot pushes a snapshot of the portfolio/regime state.  Its content is
date-stamped market data only (no weekend quotes, no book rebuild on weekends).  A weekend
push would only ship a stale carry from Friday.  No documented external freshness requirement
for weekend stamps.  Changed to mon-fri.  To restore weekend behaviour, set
MACRO_SNAPSHOT_UTC_HOURS and ensure the macro repo accepts weekend pushes.

GOVERNANCE WIRING (MW1)
------------------------
Every job is wrapped with control_plane.run_ledger (start_run/end_run, trigger="cron").
Per-book jobs additionally hold a per-book advisory file lock (control_plane.locks) via
acquire_or_log; if the lock is held by a concurrent run, the job SKIPS and emits a
run_skipped event (status="lock_held").  The global _loop_maintenance_job holds
global:loop_maintenance; all concurrent callers skip + log.

Swallowed exceptions in _loop_maintenance_job, _settle_pending_job, _derisk_us_job, and
_watch_us_job now each append a run_events record (kind="step_failed", severity=ADVISORY_ONLY)
before continuing, so silent failures become queryable records.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import bot  # noqa: F401

log = logging.getLogger(__name__)

_DB = Path(__file__).resolve().parent.parent / "data" / "scheduler.sqlite"
_scheduler = None

# ---------------------------------------------------------------------------
# governance helpers
# ---------------------------------------------------------------------------

def _ledger_start(job: str, book: str | None = None, trigger: str = "cron"):
    """Start a run-ledger record.  Never raises."""
    try:
        from control_plane import run_ledger
        return run_ledger.start_run(job, book=book, trigger=trigger)
    except Exception:  # noqa: BLE001
        return None


def _ledger_end(handle, status: str, *, severity: str | None = None):
    """End a run-ledger record.  Never raises."""
    if handle is None:
        return
    try:
        from control_plane import run_ledger
        run_ledger.end_run(handle, status, severity=severity)
    except Exception:  # noqa: BLE001
        pass


def _step_failed_event(job: str, book: str, step: str, exc: BaseException):
    """Append a step_failed run_event for a swallowed exception.  Never raises."""
    try:
        from control_plane import run_events
        run_events.append({
            "kind": "step_failed",
            "job": job,
            "book": book,
            "step": step,
            "status": "error",
            "severity": "ADVISORY_ONLY",
            "err": exc,
            "actor": "system",
        })
    except Exception:  # noqa: BLE001
        pass


def _skip_event(job: str, book: str):
    """Append a run_skipped event when a lock is held.  Never raises."""
    try:
        from control_plane import run_events
        run_events.append({
            "kind": "run_skipped",
            "job": job,
            "book": book,
            "step": "acquire",
            "status": "lock_held",
            "severity": "ADVISORY_ONLY",
            "actor": "system",
        })
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# per-book book-id lookup (used by lock names)
# ---------------------------------------------------------------------------
#
# Per-book builds hold "book:<id>" locks; jobs that touch multiple books or no
# specific book hold "global:<op>" locks.

# ---------------------------------------------------------------------------
# job implementations
# ---------------------------------------------------------------------------

def _job():
    """Flagship daily loop: gated book build (Mon–Fri after close)."""
    handle = _ledger_start("daily_loop", book="flagship", trigger="cron")
    try:
        from control_plane import locks
        lock = locks.acquire_or_log("book:flagship", job="daily_loop", book="flagship")
        if lock is None:
            _skip_event("daily_loop", "flagship")
            _ledger_end(handle, "skip", severity="ADVISORY_ONLY")
            return
        with lock:
            from bot.daily import run_daily
            run_daily()
        _ledger_end(handle, "ok")
    except Exception as exc:  # noqa: BLE001
        _ledger_end(handle, "error")
        log.warning("daily_loop failed: %s", exc)


def _autonomous_job():
    """The free-form Opus-Brain book: researches + rebalances itself once per trading day."""
    handle = _ledger_start("autonomous_daily", book="autonomous", trigger="cron")
    try:
        from control_plane import locks
        lock = locks.acquire_or_log("book:autonomous", job="autonomous_daily", book="autonomous")
        if lock is None:
            _skip_event("autonomous_daily", "autonomous")
            _ledger_end(handle, "skip", severity="ADVISORY_ONLY")
            return
        with lock:
            from bot.autonomous import run_autonomous
            run_autonomous()
        _ledger_end(handle, "ok")
    except Exception as exc:  # noqa: BLE001
        _ledger_end(handle, "error")
        log.warning("autonomous_daily failed: %s", exc)


def _heavyweight_job():
    """The concentrated Opus-Brain book: studies Flagship's book and presses its best ideas. Runs
    AFTER flagship + autonomous so it constrains against a fresh Flagship book."""
    handle = _ledger_start("heavyweight_daily", book="heavyweight", trigger="cron")
    try:
        from control_plane import locks
        lock = locks.acquire_or_log("book:heavyweight", job="heavyweight_daily", book="heavyweight")
        if lock is None:
            _skip_event("heavyweight_daily", "heavyweight")
            _ledger_end(handle, "skip", severity="ADVISORY_ONLY")
            return
        with lock:
            from bot.heavyweight import run_heavyweight
            run_heavyweight()
        _ledger_end(handle, "ok")
    except Exception as exc:  # noqa: BLE001
        _ledger_end(handle, "error")
        log.warning("heavyweight_daily failed: %s", exc)


def _china_job():
    """The free-form China A-share Opus-Brain book: researches the China desks + rebalances itself
    once per Asia trading day, after the mainland A-share close (~07:00 UTC)."""
    handle = _ledger_start("china_daily", book="china", trigger="cron")
    try:
        from control_plane import locks
        lock = locks.acquire_or_log("book:china", job="china_daily", book="china")
        if lock is None:
            _skip_event("china_daily", "china")
            _ledger_end(handle, "skip", severity="ADVISORY_ONLY")
            return
        with lock:
            from bot.china import run_china
            run_china()
        _ledger_end(handle, "ok")
    except Exception as exc:  # noqa: BLE001
        _ledger_end(handle, "error")
        log.warning("china_daily failed: %s", exc)


def _hk_job():
    """The free-form Hong-Kong Opus-Brain book (HK listings only, HKD): researches the China desks +
    rebalances itself once per Asia trading day, after the HK close (~08:00 UTC)."""
    handle = _ledger_start("hk_daily", book="hk", trigger="cron")
    try:
        from control_plane import locks
        lock = locks.acquire_or_log("book:hk", job="hk_daily", book="hk")
        if lock is None:
            _skip_event("hk_daily", "hk")
            _ledger_end(handle, "skip", severity="ADVISORY_ONLY")
            return
        with lock:
            from bot.hk import run_hk
            run_hk()
        _ledger_end(handle, "ok")
    except Exception as exc:  # noqa: BLE001
        _ledger_end(handle, "error")
        log.warning("hk_daily failed: %s", exc)


def _etf_job():
    """The free-form ETF Opus-Brain book: rotates across US-listed ETFs (index/sector/factor/duration/
    cash) under an ETF-adapted doctrine + risk guardrails, once per US trading day after the close."""
    handle = _ledger_start("etf_daily", book="etf", trigger="cron")
    try:
        from control_plane import locks
        lock = locks.acquire_or_log("book:etf", job="etf_daily", book="etf")
        if lock is None:
            _skip_event("etf_daily", "etf")
            _ledger_end(handle, "skip", severity="ADVISORY_ONLY")
            return
        with lock:
            from bot.etf import run_etf
            run_etf()
        _ledger_end(handle, "ok")
    except Exception as exc:  # noqa: BLE001
        _ledger_end(handle, "error")
        log.warning("etf_daily failed: %s", exc)


def _snapshot_job():
    """Publish a static snapshot of the dashboard to the public Macro Dashboard (GitHub Pages).
    Writes site/mastermind/mastermind_snapshot.json into the macro repo (via the vendor/macro
    symlink) and pushes it to origin/main. Resilient — never raises into the scheduler."""
    handle = _ledger_start("publish_macro_snapshot", trigger="cron")
    try:
        from scripts.export_macro_snapshot import run as export_snapshot
        export_snapshot()
        _ledger_end(handle, "ok")
    except Exception as exc:  # noqa: BLE001
        _ledger_end(handle, "error")
        log.warning("publish_macro_snapshot failed: %s", exc)


def _vps_state_sync_job():
    """Push the live paper-trading state (data/) to the serve-only VPS mirror so the public
    dashboard (bot.mastermind-x.com, /opt/mastermind/data/) tracks the Mac — the SINGLE canonical
    writer — within one cron tick.

    WHY THIS LIVES IN THE SCHEDULER (not a launchd job): the box reads the data/ the Brain writes
    under ~/Documents, and launchd agents on this Mac are TCC-denied from reading ~/Documents (every
    other lane runs from a ~/…-ops-wt worktree for exactly this reason). The scheduler runs inside
    the always-on Brain process, which HAS ~/Documents access and is the sole writer — so the push
    fires from the one context that can read the data, right where it is produced. History: the
    com.mastermind.vpssync LaunchAgent (every 15 min) could never work and was disabled 2026-06-28;
    the box then only refreshed on manual deploys and silently froze for ~5 days (last push
    2026-07-02) until this job replaced it. NEVER runs on the box: MASTERMIND_SERVE_ONLY=1 disables
    the scheduler entirely (app.main), and the sync script itself no-ops under that flag.

    Best-effort: a push miss is recorded in the run ledger (queryable via /api/scheduler) so a
    stalled sync is surfaced, not silent — and can never kill the scheduler. Cheap no-op when
    nothing changed (rsync is additive)."""
    handle = _ledger_start("vps_state_sync", trigger="cron")
    try:
        import subprocess
        script = Path(__file__).resolve().parent.parent / "scripts" / "sync_state_to_vps.sh"
        if not script.exists():
            _step_failed_event("vps_state_sync", "", "missing_script", FileNotFoundError(str(script)))
            _ledger_end(handle, "error")
            return
        proc = subprocess.run(["/bin/bash", str(script)], capture_output=True, text=True, timeout=180)
        if proc.returncode == 0:
            _ledger_end(handle, "ok")
        else:
            _step_failed_event("vps_state_sync", "", "rsync",
                               RuntimeError((proc.stderr or proc.stdout or "rsync failed").strip()[:300]))
            _ledger_end(handle, "error")
    except Exception as exc:  # noqa: BLE001 — a sync miss must never kill the scheduler
        _ledger_end(handle, "error")
        log.warning("vps_state_sync failed: %s", exc)


def _settle_pending_job():
    """Settle the US books' queued orders at the OPEN, during market hours.

    All books DECIDE after their close (the flagship at 22:40 UTC; the US Brain books at 23:10/23:15)
    and, while the market is shut, only QUEUE their target — they never book an off-hours fill. This
    morning sweep settles them at the real session open: the flagship's queued buy orders
    (queue_orders → fill_pending) and the US Brain books' queued target (autonomous + etf →
    paper_account.settle_target, a full rebalance to the decided book at the open mark), then
    republishes so the dashboard renders the freshly-filled positions. Idempotent + never raises.

    LOCKING (MW1 fix): settle_us touches both the autonomous AND etf books' account state — the
    same state mutated by _autonomous_job and _etf_job.  We must hold BOTH locks before running;
    if either is busy (a concurrent build is in progress), skip the entire settle and log.
    Acquisition order is alphabetical (book:autonomous < book:etf) — settle is the only multi-lock
    holder and single-book jobs hold exactly one lock each, so this fixed order is deadlock-safe."""
    handle = _ledger_start("settle_pending", trigger="cron")
    try:
        from control_plane import locks
        # Acquire both locks in alphabetical order (deadlock-safe — see docstring).
        lock_auto = locks.acquire_or_log("book:autonomous", job="settle_pending", book="autonomous")
        if lock_auto is None:
            _skip_event("settle_pending", "autonomous+etf")
            _ledger_end(handle, "skip", severity="ADVISORY_ONLY")
            return
        lock_etf = locks.acquire_or_log("book:etf", job="settle_pending", book="etf")
        if lock_etf is None:
            lock_auto.release()
            _skip_event("settle_pending", "autonomous+etf")
            _ledger_end(handle, "skip", severity="ADVISORY_ONLY")
            return
        try:
            from scripts.fill_pending_now import settle
            try:
                settle("flagship", require_open=True)
            except Exception as exc:  # noqa: BLE001 — a settle miss must never kill the scheduler
                _step_failed_event("settle_pending", "flagship", "fill_pending_flagship", exc)
            try:
                from bot import settle as _settle
                _settle.settle_us()  # autonomous + etf: settle the queued target at the US open
            except Exception as exc:  # noqa: BLE001
                _step_failed_event("settle_pending", "autonomous+etf", "settle_us", exc)
        finally:
            lock_etf.release()
            lock_auto.release()
        _ledger_end(handle, "ok")
    except Exception as exc:  # noqa: BLE001
        _ledger_end(handle, "error")
        log.warning("settle_pending outer failed: %s", exc)


def _settle_brain_asia_job():
    """Settle the Greater-China Brain books' queued targets at the A-share OPEN (~01:30 UTC). The
    china/hk books decide after their close and queue; this fills the queued target at the next open
    via a full rebalance, then republishes. No-op when the market is shut or nothing is queued.

    LOCKING (MW1 fix): settle_asia touches both the china AND hk books' account state — the same
    state mutated by _china_job and _hk_job.  We must hold BOTH locks before running; if either is
    busy, skip the entire settle and log.
    Acquisition order is alphabetical (book:china < book:hk) — deadlock-safe for the same reason
    as _settle_pending_job (settle is the only multi-lock holder; single-book jobs hold one lock)."""
    handle = _ledger_start("settle_brain_asia", trigger="cron")
    try:
        from control_plane import locks
        # Acquire both locks in alphabetical order (deadlock-safe — see docstring).
        lock_china = locks.acquire_or_log("book:china", job="settle_brain_asia", book="china")
        if lock_china is None:
            _skip_event("settle_brain_asia", "china+hk")
            _ledger_end(handle, "skip", severity="ADVISORY_ONLY")
            return
        lock_hk = locks.acquire_or_log("book:hk", job="settle_brain_asia", book="hk")
        if lock_hk is None:
            lock_china.release()
            _skip_event("settle_brain_asia", "china+hk")
            _ledger_end(handle, "skip", severity="ADVISORY_ONLY")
            return
        try:
            from bot import settle as _settle
            _settle.settle_asia()  # china + hk
        finally:
            lock_hk.release()
            lock_china.release()
        _ledger_end(handle, "ok")
    except Exception as exc:  # noqa: BLE001
        _ledger_end(handle, "error")
        log.warning("settle_brain_asia failed: %s", exc)


def _watch_us_job():
    """Overnight watch for the US Brain books: between the US close and the next open, re-read the live
    overnight tape; on a MATERIAL move (deterministic tripwire — free, no LLM) re-prompt the Brain to
    revise its queued target (which settles at the open). Cheap on a calm tape; never raises."""
    handle = _ledger_start("watch_us_overnight", trigger="cron")
    try:
        from bot import overnight
        try:
            overnight.watch_us()
        except Exception as exc:  # noqa: BLE001 — a watch miss must never kill the scheduler
            _step_failed_event("watch_us_overnight", "", "watch_us", exc)
        _ledger_end(handle, "ok")
    except Exception as exc:  # noqa: BLE001
        _ledger_end(handle, "error")
        log.warning("watch_us_overnight outer failed: %s", exc)


def _watch_asia_job():
    """Overnight watch for the Greater-China Brain books (china + hk) between their close and the next
    A-share open. Same tripwire→refine discipline as the US watch. Never raises."""
    handle = _ledger_start("watch_asia_overnight", trigger="cron")
    try:
        from bot import overnight
        overnight.watch_asia()
        _ledger_end(handle, "ok")
    except Exception as exc:  # noqa: BLE001
        _ledger_end(handle, "error")
        log.warning("watch_asia_overnight failed: %s", exc)


def _derisk_us_job():
    """FAST DE-RISK sweep for the US books DURING the session — the reflex the desk lacked on
    2026-06-23. A deterministic tripwire (macro RISK-OFF state / SPY gamma flip / credit gap / −X% theme
    day — free, no LLM) auto-cuts the held Flagship book to the gross cap and revises the US Brain books'
    queued targets. Flag-gated (MASTERMIND_FAST_DERISK); a no-op when disarmed or no unwind is confirmed.
    Never raises."""
    handle = _ledger_start("derisk_us_intraday", trigger="cron")
    try:
        from bot import derisk
        try:
            derisk.sweep_us()
        except Exception as exc:  # noqa: BLE001 — a de-risk miss must never kill the scheduler
            _step_failed_event("derisk_us_intraday", "", "sweep_us", exc)
        _ledger_end(handle, "ok")
    except Exception as exc:  # noqa: BLE001
        _ledger_end(handle, "error")
        log.warning("derisk_us_intraday outer failed: %s", exc)


def _macro_refresh_job():
    """Keep the vendored macro analyzer data fresh (origin/main == the live site) + run the
    staleness tripwire. The book once bought NVDA off a days-stale read; never raises."""
    handle = _ledger_start("macro_refresh", trigger="cron")
    try:
        from data_layer import macro_refresh
        macro_refresh.refresh_and_check()
        _ledger_end(handle, "ok")
    except Exception as exc:  # noqa: BLE001 — a refresh miss must never kill the scheduler
        _ledger_end(handle, "error")
        log.warning("macro_refresh failed: %s", exc)


def _cio_weekly_job():
    """CIO / Meta-PM weekly accountability review (W-L / L3 reads all-7 books). Reads per-role
    calibration multipliers + each seat's graded KPIs + all-7-book NAV-vs-benchmark + the shadow
    leaderboard, and WRITES the 'what is working / who is miscalibrated' note to
    data/brain/cio/<isoweek>.{json,md}. RECOMMENDS ONLY — never trades, flips a flag, or mutates a
    seat. The Improvement Agenda that fuses over this note runs as its OWN dedicated job
    (``_improvement_agenda_job``) 30 min later, so this job passes ``with_agenda=False`` to avoid a
    double-write. Lazy import + try/except so a review miss never kills the scheduler.

    After the US review, write_regional() is called best-effort so data/lifecycle/regional/ accrues
    weekly snapshots of the china/hk grades. A miss is logged but never aborts the CIO review."""
    handle = _ledger_start("cio_weekly", trigger="cron")
    try:
        from scripts.run_cio import run as run_cio
        run_cio(with_agenda=False)  # the dedicated agenda job owns the scheduled agenda write
        _ledger_end(handle, "ok")
    except Exception as exc:  # noqa: BLE001 — a CIO miss must never kill the scheduler
        _ledger_end(handle, "error")
        log.warning("cio_weekly failed: %s", exc)
    # regional lifecycle review — best-effort, never aborts the CIO run even on error
    try:
        from brain.book_lifecycle import write_regional
        rr = write_regional()
        _step_failed_event("cio_weekly", "regional", "write_regional",
                           RuntimeError("ok=False")) if not rr.get("ok") else None
        _re_append_regional(rr)
    except Exception as exc:  # noqa: BLE001
        log.warning("cio_weekly write_regional failed: %s", exc)


def _re_append_regional(rr: dict) -> None:
    """Append a run_event for the regional lifecycle write.  Never raises."""
    try:
        from control_plane import run_events as _re
        _re.append({
            "kind": "write_regional_lifecycle",
            "job": "cio_weekly",
            "book": "regional",
            "step": "write_regional",
            "status": "ok" if rr.get("ok") else "error",
            "as_of": rr.get("as_of"),
            "n_recommendations": rr.get("n_recommendations"),
            "json_path": rr.get("json_path"),
            "actor": "system",
        })
    except Exception:  # noqa: BLE001
        pass


def _improvement_agenda_job():
    """W-L / L6: weekly improvement agenda build.

    Fuses every accountability artifact (calibration, journal lesson clusters, shadow-vs-live gaps,
    benchmark-ledger gaps, validation verdicts, experiment-registry maturities, deploy-lag, student
    drift) into a RANKED list of concrete improvement items and writes it to:
      • data/agenda/<date>.json  (the machine artifact)
      • data/agenda/AGENDA.md    (the human briefing — what any maintenance session opens cold)

    This is the answer to 'what should we tell the AI to fix': a scheduled Opus session (or Fable)
    opens AGENDA.md and the top items are pre-argued with evidence. Display + advisory ONLY — it never
    trades, never flips a flag, never changes a seat's behavior. Runs 30 minutes after the CIO review
    so it can consume the fresh CIO artifact. Never raises."""
    handle = _ledger_start("improvement_agenda_weekly", trigger="cron")
    try:
        from brain import improvement_agenda
        improvement_agenda.write()
        _ledger_end(handle, "ok")
    except Exception as exc:  # noqa: BLE001 — an agenda miss must never kill the scheduler
        _ledger_end(handle, "error")
        log.warning("improvement_agenda_weekly failed: %s", exc)


def _experiment_maturity_job():
    """W-L / L6: daily experiment-registry maturity check.

    Promotes any OPEN experiment whose comeback_date has been reached to MATURED, persisting the
    status change in data/experiments/registry.json so the next agenda build surfaces it at the top.
    Cheap, deterministic, LLM-free. Never raises into the scheduler.

    MW2: emits a ``experiment_matured`` governance event for each experiment promoted, via the
    governance emitter (b).  The emit happens IN THIS WRAPPER — never inside experiment_registry
    (lane B owns that file)."""
    handle = _ledger_start("experiment_maturity", trigger="cron")
    try:
        from brain import experiment_registry
        matured_ids = experiment_registry.matured()  # side-effect: promotes date-reached items → matured
        _ledger_end(handle, "ok")
        # MW2 emitter (b): one governance event per matured experiment
        _emit_experiment_matured(matured_ids or [])
    except Exception as exc:  # noqa: BLE001 — a maturity check miss must never kill the scheduler
        _ledger_end(handle, "error")
        log.warning("experiment_maturity failed: %s", exc)


_MATURED_EMITTED = Path(__file__).resolve().parent.parent / "data" / "governance" / "experiment_matured_emitted.json"


def _emit_experiment_matured(matured_items: list) -> None:
    """MW2 emitter (b): emit ``experiment_matured`` governance events for newly promoted
    experiments. Runs INSIDE the scheduler job wrapper — never inside experiment_registry
    (lane B owns that file). Never raises.

    Two production realities this handles:
    - ``matured()`` returns list[dict] (whole experiment records), not ids — extract the id.
    - ``matured()`` returns ALL matured-but-unjudged items on EVERY call (the job is a
      mon-fri cron), so without dedup the ledger fills with a duplicate event per weekday
      until a human judges the item. A sidecar records already-emitted ids; each id emits
      exactly once, at its maturation transition."""
    try:
        import json as _json
        from control_plane import governance as _gov
        ids: list[str] = []
        for it in (matured_items or []):
            exp_id = it.get("id") if isinstance(it, dict) else it
            if exp_id:
                ids.append(str(exp_id))
        if not ids:
            return
        emitted: set[str] = set()
        try:
            if _MATURED_EMITTED.exists():
                emitted = set(_json.loads(_MATURED_EMITTED.read_text()))
        except Exception:  # noqa: BLE001 — unreadable sidecar degrades to re-emit, never to silence
            emitted = set()
        new = [i for i in ids if i not in emitted]
        for exp_id in new:
            _gov.append({
                "event_type": "experiment_matured",
                "target": exp_id,
                "actor": "experiment_maturity_job",
                "reason": "comeback_date reached; experiment promoted to MATURED",
                "after": "matured",
                "rollback": "manually set experiment status back to open in data/experiments/registry.json",
                "source_artifact": "app.scheduler._experiment_maturity_job",
            })
        if new:
            _MATURED_EMITTED.parent.mkdir(parents=True, exist_ok=True)
            _MATURED_EMITTED.write_text(_json.dumps(sorted(emitted | set(ids))))
    except Exception:  # noqa: BLE001 — governance emit must never kill the scheduler
        pass


def _loop_maintenance_job():
    """Advance the FORWARD-LEARNING substrate every trading day — independent of the flagship's
    material-change gate.

    The flagship build (bot.phase2) hosts the whole accountability/learning loop: the parallel
    forward SHADOW A/B books, the desk-lever A/B, the universe-wide PREDICTION log, the OUTCOME-LEDGER
    resolution, and the track-record + empirical-CALIBRATION refresh. But all of that lives AFTER
    phase2's material-change gate — so on a carried-forward day it never runs and the forward clocks
    freeze (observed: the shadow books advanced on 3 of 6 sessions while the live book advanced
    daily). That starves the very flywheel the system is meant to grow over months.

    This job re-runs the gate-INDEPENDENT, prod-ISOLATED, degrade-safe pieces after the evening builds
    so matured theses resolve ON TIME and the A/B NAV curves tick every session. It NEVER trades and
    never touches prod book/cash/position state. Best-effort per step (one failure can't sink the
    others) and never raises into the scheduler. Runs Mon–Fri at 23:45 UTC, after the flagship
    (22:40) + autonomous (23:10) + heavyweight (23:25) builds: on a rebuild day it picks up today's
    fresh decision inputs; on a carried day the shadow/desk-A/B runs HOLD + re-mark (empty-inputs
    guard) instead of liquidating.

    GOVERNANCE (MW1): global:loop_maintenance lock prevents concurrent runs (e.g. HTTP-triggered
    manual run overlapping the nightly cron).  Swallowed sub-step exceptions each write a
    step_failed run_event (ADVISORY_ONLY) before the next step continues — so every silent failure
    is now queryable."""
    handle = _ledger_start("loop_maintenance", trigger="cron")
    try:
        from control_plane import locks
        lock = locks.acquire_or_log("global:loop_maintenance", job="loop_maintenance", book="")
        if lock is None:
            _skip_event("loop_maintenance", "")
            _ledger_end(handle, "skip", severity="ADVISORY_ONLY")
            return
        with lock:
            _run_loop_maintenance_steps()
        _ledger_end(handle, "ok")
    except Exception as exc:  # noqa: BLE001
        _ledger_end(handle, "error")
        log.warning("loop_maintenance outer failed: %s", exc)


def _run_loop_maintenance_steps():
    """Execute the ~12 maintenance sub-steps; each failure emits a step_failed event and continues."""
    from datetime import date
    asof = date.today().isoformat()
    asof_d = date.today()

    # 1. universe-wide forward prediction log + off-policy REJECTION log — both read fresh and only
    #    ADD/label/grade (never liquidate), so they are always safe to run. rejections.record() with no
    #    new items just forward-grades the open rejected names (a carried day still resolves matured ones).
    try:
        from portfolio import predictions
        predictions.record(asof)
    except Exception as exc:  # noqa: BLE001
        _step_failed_event("loop_maintenance", "", "predictions.record", exc)
    try:
        from portfolio import rejections
        rejections.record(asof)
    except Exception as exc:  # noqa: BLE001
        _step_failed_event("loop_maintenance", "", "rejections.record", exc)
    # 1c. retrain the fast statistical STUDENT (CatBoost) on the resolved universe log (#3) — nightly,
    #     cheap, LLM-free, walk-forward OOS, degrade-safe (no-op without catboost / enough resolved rows).
    #     Its calibrated read feeds the Brain prompts (flag-gated MASTERMIND_STUDENT).
    try:
        from brain import student
        student.train(asof)
    except Exception as exc:  # noqa: BLE001
        _step_failed_event("loop_maintenance", "", "student.train", exc)
    # 1d. retrain the DISTILLED-OPUS classifier (#3 v2) — mimics Opus's buy decisions so easy calls can
    #     be routed cheaply (don't-waste-Opus). LLM-free, degrade-safe, 'building' until Opus accrues
    #     months of decisions. No-op if catboost absent / too few buys.
    try:
        from brain import distill
        distill.train(asof)
    except Exception as exc:  # noqa: BLE001
        _step_failed_event("loop_maintenance", "", "distill.train", exc)
    # 1e. INTERIM MARKS (#11) — log day-5/day-10 trajectory checkpoints for open conviction theses
    #     (early-warning for the risk layer weeks before the 21-bday grade). Evidence only, never the
    #     label; idempotent keep-first; degrade-safe.
    try:
        from brain import interim_marks
        interim_marks.record(asof)
    except Exception as exc:  # noqa: BLE001
        _step_failed_event("loop_maintenance", "", "interim_marks.record", exc)

    # 2. parallel forward shadow books + desk-lever A/B — re-derive (or HOLD on a carried day) + mark
    #    forward. The empty-inputs guard inside run() prevents a no-decision day from liquidating them.
    try:
        from portfolio import shadow_books
        shadow_books.run(asof)
    except Exception as exc:  # noqa: BLE001
        _step_failed_event("loop_maintenance", "", "shadow_books.run", exc)
    try:
        from portfolio import desk_ab
        desk_ab.run(asof)
    except Exception as exc:  # noqa: BLE001
        _step_failed_event("loop_maintenance", "", "desk_ab.run", exc)

    # 3. grade matured theses ONCE via the entry→horizon path-replay grader, then fan the result into
    #    (a) the OUTCOME LEDGER (reliability + lens-edge substrate), (b) the Brier TRACK RECORD + prod
    #    ledger close, and (c) the empirical CALIBRATION refresh — so the perception→outcome loop
    #    advances every trading day, not only on a flagship rebuild. Each step is idempotent.
    realized: dict = {}
    try:
        from brain import outcomes as _outcomes
        realized = _outcomes.realized_returns(asof_d)
    except Exception as exc:  # noqa: BLE001
        _step_failed_event("loop_maintenance", "", "outcomes.realized_returns", exc)
    try:
        from brain import outcome_ledger
        outcome_ledger.resolve(asof, realized=realized)  # {} → no-op; shares the same grader
    except Exception as exc:  # noqa: BLE001
        _step_failed_event("loop_maintenance", "", "outcome_ledger.resolve", exc)
    if realized:
        try:
            from brain import scorer as _scorer, ledger as _ledger
            from data_layer import store as _store
            tr = _scorer.track_record(asof_d, realized=realized)
            con = _store.connect()
            _store.save_track_record(con, asof, tr)
            _by_id = {t["id"]: t for t in _ledger.all_theses()}
            for _tid, _rr in realized.items():
                _th = _by_id.get(_tid)
                if _th and _th.get("status", "open") == "open":
                    try:
                        _ledger.close(_th["subject"], "resolved", realized=_rr)
                    except Exception:  # noqa: BLE001
                        pass
        except Exception as exc:  # noqa: BLE001
            _step_failed_event("loop_maintenance", "", "track_record+ledger_close", exc)
    try:
        from brain import calibration as _calibration
        _calibration.persist(asof_d)
    except Exception as exc:  # noqa: BLE001
        _step_failed_event("loop_maintenance", "", "calibration.persist", exc)
    # 3b. per-seat Brinson ATTRIBUTION rollup — decompose every RESOLVED name's active return across
    #     the seats that touched it and update data/brain/attribution/<asof>.json + _rollup.json. This
    #     is the substrate brain/reputation.py reads to warm the seat weights off the reputation floor.
    #     Pure (no LLM), reads only on-disk seat artifacts, idempotent per asof, never liquidates.
    try:
        from brain import attribution as _attribution
        _attribution.persist(asof_d)
    except Exception as exc:  # noqa: BLE001
        _step_failed_event("loop_maintenance", "", "attribution.persist", exc)


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
    handle = _ledger_start("daily_mark", trigger="cron")
    try:
        from portfolio import paper_account, registry
    except Exception as exc:  # noqa: BLE001
        _ledger_end(handle, "error")
        log.warning("daily_mark: import failed: %s", exc)
        return
    asof = _today_iso()
    # ── W-L / L1: mark through the ONE marking layer (portfolio.marks) ──
    # Build a SINGLE union prices dict for the whole sweep (every book's held names + every book's
    # benchmark + the defensive basket) in ONE pass, logged source-by-source (polygon-EOD →
    # yahoo-parquet → last-good-carry, never avg_cost). Per-book USD→base-ccy conversion still
    # happens below. One snapshot per run = no out-of-order dup rows, one price per symbol (P7).
    union_usd: dict = {}
    try:
        from portfolio import marks
        from brain.benchmark_ledger import DEFENSIVE_BASKET
        want: set = set(DEFENSIVE_BASKET)
        for _pid in registry.ids():
            try:
                _st = paper_account._load_account(_pid) if _pid != "self_directed" else {}
                want |= set(_st.get("positions", {}).keys())
                want.add(paper_account._benchmark_for(_pid))
            except Exception:  # noqa: BLE001
                pass
        # US symbols only for the marking layer (yahoo/polygon are USD feeds); *.HK/*.SS/*.SZ keep
        # the legacy per-book accessor path below (Tushare/Yahoo-local + FX).
        us_want = {t for t in want if t and "." not in t}
        union_usd = marks.prices_for(us_want, asof)
    except Exception:  # noqa: BLE001
        union_usd = {}
    # ── the defensive-benchmark ledger (P6 — the book that beats us is a named daily input) ──
    try:
        _build_benchmark_ledger(asof, union_usd)
    except Exception:  # noqa: BLE001
        pass
    for pid in _MARK_BOOK_IDS:
        try:
            # LOCKING (MW1 fix): take each book's lock briefly while we mark it to avoid racing a
            # concurrent build (e.g. flagship at 22:40 vs daily_mark at 22:35).  This job is read-
            # only w.r.t. positions/cash — it only appends a nav_history row — but mark() writes
            # to the account file, so it shares the same mutation surface.  If the lock is held
            # (a build started early), skip this book and log; the build itself will mark.
            from control_plane import locks as _locks
            _book_lock = _locks.acquire_or_log(f"book:{pid}", job="daily_mark", book=pid)
            if _book_lock is None:
                _step_failed_event("daily_mark", pid, f"lock_held:{pid}", RuntimeError("lock held — skipping mark"))
                continue
            with _book_lock:
                # cash sweep first: idle cash earns ~4%/yr (money-market), idempotent per date, so the
                # NAV we mark below already reflects today's accrued cash. Best-effort (never raises).
                paper_account.accrue_cash_yield(_today_iso(), portfolio_id=pid)
                state = paper_account._load_account(pid)
                bench = paper_account._benchmark_for(pid)
                ccy = registry.currency(pid)
                tickers = set(state.get("positions", {}).keys()) | {bench}
                # batch-warm the US live quotes in ONE request so the per-name loop below hits a warm
                # cache instead of firing a separate yfinance download per holding.
                try:
                    from data_layer import yahoo_feed
                    yahoo_feed.warm([t for t in tickers if t and "." not in t])
                except Exception:  # noqa: BLE001
                    pass
                prices: dict = {}
                for t in tickers:
                    # prefer the ONE marking layer's union mark (L1); fall back to the legacy accessor
                    # for names it couldn't price (esp. *.HK/*.SS/*.SZ, which route through Tushare/Yahoo
                    # -local below via _current_price). ALWAYS in USD at this point.
                    px = union_usd.get((t or "").upper()) or paper_account._current_price(t)
                    if not (px and px > 0):
                        continue
                    # A non-USD book is priced end-to-end in its BASE currency (cash, avg_cost, AND its
                    # benchmark inception price), so the USD mark must be converted before it hits
                    # mark()/NAV — exactly as bot/settle._price does (it converts EVERY symbol, benchmark
                    # included). WITHOUT this the daily sweep books a CNY/HKD position at its USD value
                    # (~÷7) against base-currency cash, so a china/hk book it merely re-marks (didn't
                    # rebuild) shows a phantom crash in nav_history (the 2026-06-23 china/hk cliff). The
                    # benchmark is converted too: its inception price was stored in base currency, so
                    # leaving the live mark in USD would crater the spy_nav line by the same factor.
                    if ccy != "USD":
                        try:
                            from portfolio import fx
                            px = fx.usd_to(px, ccy)
                        except Exception:  # noqa: BLE001
                            continue
                    if px and px > 0:
                        prices[t] = px
                if prices:
                    paper_account.mark(prices, asof, portfolio_id=pid, benchmark=bench)
        except Exception:  # noqa: BLE001 — one book's mark miss must never kill the sweep
            continue
    # ── W-L / L1: mark the Self-Directed book too (its own engine, its own nav_history) ──
    # It is NOT a paper_account book, so it marks through its own mark seam. Install the ONE marking
    # layer as its injected resolver for this sweep so it reads the same price every other book does
    # (fixing the phantom-zero-return bug), then snapshot its NAV.
    try:
        from portfolio import self_directed, marks
        _sd_state = self_directed._load_account()
        # only advance a NAV history once the hand-driven book actually HOLDS something (an empty
        # book has nothing to mark; this also keeps the empty-books contract of the daily sweep).
        if _sd_state.get("positions"):
            self_directed.set_price_resolver(lambda t: marks.mark_one(t, asof))
            try:
                self_directed.mark(prices=union_usd, asof=asof)
                # W6/T3 — PUBLISH the self-directed book to data/portfolios/self_directed/latest.json
                # so it becomes a first-class published book: visible to firm_exposure.summary() as the
                # named-yardstick row and joinable to Heavyweight's firm-union universe. Best-effort;
                # publish() never raises and firm_exposure EXCLUDES it from all clamp/headroom math, so
                # this only ADDS the display-only yardstick — it can never shape the books it measures.
                self_directed.publish(prices=union_usd, asof=asof)
            finally:
                self_directed.set_price_resolver(None)  # never leave the seam installed
    except Exception:  # noqa: BLE001
        pass
    _ledger_end(handle, "ok")


def _build_benchmark_ledger(asof: str, union_usd: dict) -> None:
    """Build the four-bogey benchmark ledger for `asof` from a rolling mark history of SPY + the
    defensive basket. We accumulate today's marks into data/benchmark/_series.json (a small
    {ticker:{date:px}} store) so the renorm has a real window; the ledger then renorms every bogey
    to growth-of-$1 and ranks them.  After the US build, build the two regional ledgers (china / hk)
    with the same series store (FXI is the proxy for both).  Best-effort throughout; never raises.
    Regime read is the live risk frame (degrades to plain-SPY / plain-proxy if absent)."""
    import json as _json
    from brain import benchmark_ledger
    from control_plane import run_events as _re
    # Use the benchmark_ledger module's _BENCH_DIR so monkeypatching in tests redirects writes
    # to the sandbox (rather than the live data/benchmark/ tree).
    series_path = benchmark_ledger._BENCH_DIR / "_series.json"
    # US build: accumulate SPY + defensive basket + FXI (also needed by regional bogeys)
    want = [benchmark_ledger.SPY, *benchmark_ledger.DEFENSIVE_BASKET,
            *benchmark_ledger.CN_BOGEY, *benchmark_ledger.HK_BOGEY]
    want = list(dict.fromkeys(want))  # deduplicate, preserve order
    try:
        series = _json.loads(series_path.read_text()) if series_path.exists() else {}
    except Exception:  # noqa: BLE001
        series = {}
    for t in want:
        px = union_usd.get(t)
        if px and px > 0:
            series.setdefault(t, {})[asof] = round(float(px), 6)
    try:
        series_path.parent.mkdir(parents=True, exist_ok=True)
        series_path.write_text(_json.dumps(series, indent=2, sort_keys=True))
    except Exception:  # noqa: BLE001
        pass
    regime = None
    try:
        from brain import macro_risk
        regime = macro_risk.latest() if hasattr(macro_risk, "latest") else None
    except Exception:  # noqa: BLE001
        regime = None
    benchmark_ledger.build(series, asof=asof, regime=regime)

    # Regional bogeys (china + hk) — best-effort; a miss MUST NOT abort the US build.
    # Each bogey row carries bogey_is_proxy=True + proxy_reason so any lifecycle rec that cites
    # this bogey is honest about the instrument (FXI ≠ CSI300 / Hang Seng).
    # proxy_meta is passed INTO build_regional so the flags are stamped BEFORE the artifact is
    # persisted to disk — the on-disk JSON is the source of truth, not just the in-memory return.
    _proxy_reasons = {
        "china": "FXI (iShares China Large-Cap) is a USD-listed proxy for CSI300/A-shares; "
                 "000300.SS and MCHI/ASHR are not in the yahoo parquet store as of 2026-07-03.",
        "hk":    "FXI is the only China-region ETF in the yahoo parquet store; "
                 "2800.HK (Tracker Fund of HK) is the canonical HK proxy but is not yet priced.",
    }
    for _book_id in benchmark_ledger.BOOK_BOGEY_OVERRIDES:
        try:
            _proxy_reason = _proxy_reasons.get(
                _book_id,
                f"constituent list {benchmark_ledger.BOOK_BOGEY_OVERRIDES[_book_id]} is a proxy; "
                "update BOOK_BOGEY_OVERRIDES when a canonical instrument is available.",
            )
            result = benchmark_ledger.build_regional(
                series, _book_id, asof=asof,
                proxy_meta={"bogey_is_proxy": True, "proxy_reason": _proxy_reason},
            )
            _bogey = (result.get("bogeys") or {}).get("regional") or {}
            _re.append({
                "kind": "build_regional_benchmark",
                "job": "daily_mark",
                "book": _book_id,
                "step": "build_regional",
                "status": "ok",
                "bogey_is_proxy": True,
                "proxy_reason": _proxy_reason,
                "n_points": _bogey.get("n_points"),
                "actor": "system",
            })
        except Exception as _exc:  # noqa: BLE001 — regional miss never aborts US build
            try:
                _re.append({
                    "kind": "step_failed",
                    "job": "daily_mark",
                    "book": _book_id,
                    "step": "build_regional_benchmark",
                    "status": "error",
                    "severity": "ADVISORY_ONLY",
                    "err": _exc,
                    "actor": "system",
                })
            except Exception:  # noqa: BLE001
                pass


def _today_iso() -> str:
    from datetime import date
    return date.today().isoformat()


def _portfolio_risk_compose_job():
    """Portfolio held-risk RTH intraday compose (Mon-Fri, every 30 min, 13:00-20:30 UTC).

    Sequence: (b) fetch positions, (c) compose lanes + roles, (d) alert governor,
    (e) write_state, (g) VPS push (best-effort). Skips macro_refresh (--skip-refresh)
    on intraday runs; daily job at 15:05 UTC handles the full refresh.

    NOTE: absent on the VPS (MASTERMIND_SERVE_ONLY=1 disables the scheduler entirely).
    VPS reads data/portfolio_watch/ from rsync'd state. Never raises into the scheduler.
    """
    handle = _ledger_start("portfolio_risk_compose", trigger="cron")
    try:
        import subprocess as _sp
        import sys as _sys
        from pathlib import Path as _Path
        _script = _Path(__file__).resolve().parent.parent / "scripts" / "run_portfolio_risk.py"
        result = _sp.run(
            [_sys.executable, str(_script), "--skip-refresh"],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            log.warning("portfolio_risk_compose failed (rc=%s): %s",
                        result.returncode, result.stderr[:500])
            _ledger_end(handle, "error")
        else:
            _ledger_end(handle, "ok")
    except Exception as exc:  # noqa: BLE001
        _ledger_end(handle, "error")
        log.warning("portfolio_risk_compose failed: %s", exc)


def _portfolio_risk_daily_job():
    """Portfolio held-risk daily compose with macro refresh (Mon-Fri, 15:05 UTC = 07:05 PT).

    Runs once daily after the nightly macro data is fresh. Includes macro_refresh,
    compose, alerts, outcome-ledger append, and VPS push. The --skip-alerts flag is
    NOT passed here so the daily run fires transition alerts.

    NOTE: absent on VPS (serve-only disables scheduler). Never raises.
    """
    handle = _ledger_start("portfolio_risk_daily", trigger="cron")
    try:
        import subprocess as _sp
        import sys as _sys
        from pathlib import Path as _Path
        _script = _Path(__file__).resolve().parent.parent / "scripts" / "run_portfolio_risk.py"
        result = _sp.run(
            [_sys.executable, str(_script)],  # full sequence: macro_refresh + outcomes + vps
            capture_output=True, text=True, timeout=600,
        )
        if result.returncode != 0:
            log.warning("portfolio_risk_daily failed (rc=%s): %s",
                        result.returncode, result.stderr[:500])
            _ledger_end(handle, "error")
        else:
            _ledger_end(handle, "ok")
    except Exception as exc:  # noqa: BLE001
        _ledger_end(handle, "error")
        log.warning("portfolio_risk_daily failed: %s", exc)


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
    # Jobstore choice (incident 2026-07-06 ×2): the sqlite jobstore flipped to
    # "attempt to write a readonly database" hours into a run (macOS provenance on
    # files created by an agent-session process tree) and the scheduler thread died
    # silently. Persistence buys almost nothing here — every boot re-registers all
    # jobs with replace_existing=True — so production runs MASTERMIND_JOBSTORE=memory
    # (set in .env); sqlite remains the default for cross-restart misfire catch-up
    # in environments where the file is trustworthy.
    if os.environ.get("MASTERMIND_JOBSTORE", "sqlite").strip().lower() == "memory":
        sch = BackgroundScheduler(timezone="UTC")  # default MemoryJobStore
    else:
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
    # FLAGSHIP DAILY LOOP — Mon–Fri only.  A weekend run finds no market data and a no-op build
    # would confuse the dashboard's "last built" display.  day_of_week added in MW1.
    sch.add_job(_job, CronTrigger(day_of_week="mon-fri", hour=hour, minute=40, timezone="UTC"),
                id="daily_loop",
                replace_existing=True, misfire_grace_time=3600, coalesce=True)
    # Mon–Fri only (no Sat/Sun) — the autonomous book refreshes once per trading day after close.
    sch.add_job(_autonomous_job, CronTrigger(day_of_week="mon-fri", hour=a_hour, minute=10, timezone="UTC"),
                id="autonomous_daily", replace_existing=True, misfire_grace_time=3600, coalesce=True)
    # Heavyweight runs LAST (23:25 by default) — after flagship's 22:40 build (so it constrains
    # against a fresh Flagship book) and after autonomous's 23:10 (so the two Brain runs don't
    # hammer the subscription/price feeds at once; they touch disjoint data dirs — no state race).
    sch.add_job(_heavyweight_job, CronTrigger(day_of_week="mon-fri", hour=h_hour, minute=25, timezone="UTC"),
                id="heavyweight_daily", replace_existing=True, misfire_grace_time=3600, coalesce=True)
    # All-China book on Asia's clock (Mon–Fri after the A-share close). Touches a disjoint data dir
    # (data/portfolios/china) and a different feed window from the US books — no state race.
    sch.add_job(_china_job, CronTrigger(day_of_week="mon-fri", hour=cn_hour, minute=0, timezone="UTC"),
                id="china_daily", replace_existing=True, misfire_grace_time=3600, coalesce=True)
    # HK book on Asia's clock (Mon–Fri after the HK close, ~09:00 UTC). Disjoint data dir
    # (data/portfolios/hk) — no state race with the A-share china book.
    sch.add_job(_hk_job, CronTrigger(day_of_week="mon-fri", hour=hk_hour, minute=0, timezone="UTC"),
                id="hk_daily", replace_existing=True, misfire_grace_time=3600, coalesce=True)
    # ETF book on the US evening cadence (Mon–Fri after the close), staggered 5 min after the
    # autonomous book so the two US Brain runs don't hammer the subscription/price feeds at once;
    # disjoint data dir (data/portfolios/etf) — no state race.
    sch.add_job(_etf_job, CronTrigger(day_of_week="mon-fri", hour=a_hour, minute=15, timezone="UTC"),
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
    # FAST DE-RISK — an INTRADAY US-session sweep so a confirmed unwind is cut off-schedule, not at the
    # once-daily post-close run (the 2026-06-23 gap). Every ~30 min through the US cash session; the job
    # itself is free + a no-op unless MASTERMIND_FAST_DERISK is armed AND the deterministic tripwire
    # fires. UTC-pinned. The overnight watch jobs already carry the Brain pending-target de-risk.
    derisk_hours = (os.environ.get("DERISK_US_UTC_HOURS", "14-20").strip() or "14-20")
    sch.add_job(_derisk_us_job,
                CronTrigger(day_of_week="mon-fri", hour=derisk_hours, minute="0,30", timezone="UTC"),
                id="derisk_us_intraday", replace_existing=True, misfire_grace_time=1800, coalesce=True)
    # MACRO SNAPSHOT PUSH — Mon–Fri only.  Content is date-stamped market data; a weekend push
    # would only ship a stale Friday carry.  No external freshness requirement for weekend stamps
    # was documented.  day_of_week added in MW1.  To restore weekend behaviour, set
    # MACRO_SNAPSHOT_UTC_HOURS and ensure the macro repo accepts weekend pushes.
    snap_hours = (os.environ.get("MACRO_SNAPSHOT_UTC_HOURS", "12,22").strip() or "12,22")
    sch.add_job(_snapshot_job,
                CronTrigger(day_of_week="mon-fri", hour=snap_hours, minute=25, timezone="UTC"),
                id="publish_macro_snapshot", replace_existing=True,
                misfire_grace_time=3600, coalesce=True)
    # VPS STATE SYNC — push data/ to the serve-only box (bot.mastermind-x.com) every 15 min so the
    # public dashboard tracks the Mac. REPLACES the disabled com.mastermind.vpssync LaunchAgent,
    # which could never work: launchd is TCC-blocked from reading the data under ~/Documents on this
    # Mac (fund/liveflow/optionshub all run from ~/…-ops-wt for the same reason). Runs 24/7 from the
    # always-on Brain process (the sole ~/Documents-capable writer) so every write — builds, settles,
    # marks, snapshots — reaches the box within a tick; a cheap no-op when nothing changed. Never
    # runs on the box (MASTERMIND_SERVE_ONLY disables the scheduler). Cadence via VPS_STATE_SYNC_MINUTE.
    vps_sync_minute = (os.environ.get("VPS_STATE_SYNC_MINUTE", "*/15").strip() or "*/15")
    sch.add_job(_vps_state_sync_job, CronTrigger(minute=vps_sync_minute, timezone="UTC"),
                id="vps_state_sync", replace_existing=True, misfire_grace_time=3600, coalesce=True)

    # CIO / Meta-PM weekly accountability review (additive, read-only — recommends, never trades).
    # Default Sunday 10:00 UTC; configurable via CIO_WEEKLY_DAY / CIO_WEEKLY_UTC_HOUR.
    cio_dow = os.environ.get("CIO_WEEKLY_DAY", "sun")
    cio_hour = int(os.environ.get("CIO_WEEKLY_UTC_HOUR", "10"))
    sch.add_job(_cio_weekly_job,
                CronTrigger(day_of_week=cio_dow, hour=cio_hour, minute=0, timezone="UTC"),
                id="cio_weekly", replace_existing=True, misfire_grace_time=7200, coalesce=True)
    # IMPROVEMENT AGENDA (W-L / L6) — weekly fusion of all accountability artifacts into a ranked
    # AGENDA.md (human) + data/agenda/<date>.json (machine). Runs 30 min after CIO so it can consume
    # the fresh CIO note. Configurable via AGENDA_WEEKLY_DAY / AGENDA_WEEKLY_UTC_HOUR.
    agenda_dow = os.environ.get("AGENDA_WEEKLY_DAY", cio_dow)
    agenda_hour = int(os.environ.get("AGENDA_WEEKLY_UTC_HOUR", str(cio_hour)))
    sch.add_job(_improvement_agenda_job,
                CronTrigger(day_of_week=agenda_dow, hour=agenda_hour, minute=30, timezone="UTC"),
                id="improvement_agenda_weekly", replace_existing=True,
                misfire_grace_time=7200, coalesce=True)
    # FORWARD-LEARNING MAINTENANCE — advance the accountability/learning substrate EVERY trading day,
    # independent of the flagship's material-change gate. The shadow A/B books, the desk-lever A/B, the
    # universe prediction log, the outcome-ledger resolution and the track-record/calibration refresh
    # all live AFTER phase2's gate, so on a carried-forward day they never run and the forward clocks
    # freeze. This job re-runs the gate-independent, prod-ISOLATED, degrade-safe pieces after the
    # evening builds (Mon–Fri 23:45 UTC, after flagship 22:40 + autonomous 23:10 + heavyweight 23:25),
    # so matured theses resolve on time and the A/B NAV curves tick every session. UTC-pinned for the
    # same reason as settle_pending (a bare trigger would inherit the host tz). Configurable hour.
    lm_hour = int(os.environ.get("LOOP_MAINT_UTC_HOUR", "23"))
    sch.add_job(_loop_maintenance_job,
                CronTrigger(day_of_week="mon-fri", hour=lm_hour, minute=45, timezone="UTC"),
                id="loop_maintenance", replace_existing=True, misfire_grace_time=3600, coalesce=True)
    # EXPERIMENT MATURITY CHECK (W-L / L6) — daily sweep that promotes any OPEN experiment whose
    # comeback_date has been reached to MATURED, persisting the status change in
    # data/experiments/registry.json so the next agenda build surfaces it at the top (nothing rots).
    # Runs at <loop-maint hour>:50, just AFTER loop_maintenance (23:45), so the fresh resolved-thesis
    # count feeds the maturity check. LLM-free; deterministic; never raises into the scheduler.
    sch.add_job(_experiment_maturity_job,
                CronTrigger(day_of_week="mon-fri", hour=lm_hour, minute=50, timezone="UTC"),
                id="experiment_maturity", replace_existing=True,
                misfire_grace_time=3600, coalesce=True)
    # PORTFOLIO RISK DESK — RTH intraday compose (Mon-Fri, every 30 min during US session).
    # UTC 13:00-20:30 = roughly 06:00-13:30 PT (RTH 06:30-13:00 PT); job runs at :00 and :30.
    # NOTE: absent on the VPS (MASTERMIND_SERVE_ONLY=1 disables the scheduler entirely).
    # VPS reads data/portfolio_watch/ from the rsync'd state pushed by scripts/push_portfolio_watch_to_vps.sh.
    sch.add_job(_portfolio_risk_compose_job,
                CronTrigger(day_of_week="mon-fri", hour="13-20", minute="0,30", timezone="UTC"),
                id="portfolio_risk_compose", replace_existing=True,
                misfire_grace_time=1800, coalesce=True)
    # PORTFOLIO RISK DESK — once-daily post-nightly compose (Mon-Fri, 15:05 UTC = 07:05 PT).
    # Includes macro_refresh + outcome-ledger append + VPS push.
    # NOTE: absent on VPS (serve-only disables scheduler).
    sch.add_job(_portfolio_risk_daily_job,
                CronTrigger(day_of_week="mon-fri", hour=15, minute=5, timezone="UTC"),
                id="portfolio_risk_daily", replace_existing=True,
                misfire_grace_time=3600, coalesce=True)
    sch.start()
    _scheduler = sch
    return sch


# ---------------------------------------------------------------------------
# /api/scheduler health query
# ---------------------------------------------------------------------------

def scheduler_health() -> list[dict]:
    """Return per-job health records for the /api/scheduler endpoint.

    Each record:
      id, next_run_time (ISO or null), last_started (ISO or null),
      last_finished (ISO or null), last_skipped (ISO or null),
      last_status (str or null), last_severity (str or null).

    Reads the tail of data/governance/run_events.jsonl.  Never raises.
    """
    import json as _json

    # ── read tail of run_events.jsonl (last 2000 lines is plenty) ──
    events_by_job: dict[str, list[dict]] = {}
    try:
        from control_plane.run_events import _ledger_path
        p = _ledger_path()
        if p.exists():
            lines = p.read_text().splitlines()
            for line in lines[-2000:]:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = _json.loads(line)
                    job = ev.get("job") or ""
                    if job:
                        events_by_job.setdefault(job, []).append(ev)
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        pass

    # ── build per-job last-event lookup ──
    def _last(job: str, kind: str) -> dict | None:
        evs = events_by_job.get(job, [])
        for ev in reversed(evs):
            if ev.get("kind") == kind:
                return ev
        return None

    # ── known job ids (from scheduler) ──
    job_ids = [
        "macro_refresh", "daily_mark", "daily_loop",
        "autonomous_daily", "heavyweight_daily", "china_daily", "hk_daily", "etf_daily",
        "settle_pending", "settle_brain_asia",
        "watch_us_overnight", "watch_asia_overnight",
        "derisk_us_intraday",
        "publish_macro_snapshot",
        "cio_weekly", "improvement_agenda_weekly",
        "loop_maintenance", "experiment_maturity",
        "portfolio_risk_compose", "portfolio_risk_daily",
        "vps_state_sync",
    ]

    # ── next_run_time from APScheduler ──
    next_run: dict[str, str | None] = {}
    try:
        global _scheduler
        if _scheduler is not None:
            for job in _scheduler.get_jobs():
                nrt = job.next_run_time
                next_run[job.id] = nrt.isoformat() if nrt else None
    except Exception:  # noqa: BLE001
        pass

    records = []
    for jid in job_ids:
        started_ev = _last(jid, "run_started")
        finished_ev = _last(jid, "run_finished")
        skipped_ev = _last(jid, "run_skipped")

        last_status = finished_ev.get("status") if finished_ev else None
        last_severity = finished_ev.get("severity") if finished_ev else None

        records.append({
            "id": jid,
            "next_run_time": next_run.get(jid),
            "last_started": started_ev.get("ts") if started_ev else None,
            "last_finished": finished_ev.get("ts") if finished_ev else None,
            "last_skipped": skipped_ev.get("ts") if skipped_ev else None,
            "last_status": last_status,
            "last_severity": last_severity,
        })
    return records


# ---------------------------------------------------------------------------
# first-run daemon threads (startup)
# ---------------------------------------------------------------------------

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
            return False  # already has a track record — the cron owns it now
    except Exception:
        pass
    try:
        from brain import cli_bridge
        if not cli_bridge.available():
            return False  # no subscription/CLI → don't fire a doomed armed run
    except Exception:
        return False
    import threading

    def _go():
        handle = _ledger_start("autonomous_daily", book="autonomous", trigger="first_run")
        try:
            from control_plane import locks
            lock = locks.acquire_or_log("book:autonomous", job="autonomous_daily", book="autonomous")
            if lock is None:
                _skip_event("autonomous_daily", "autonomous")
                _ledger_end(handle, "skip", severity="ADVISORY_ONLY")
                return
            with lock:
                from bot.autonomous import run_autonomous
                run_autonomous()
            _ledger_end(handle, "ok")
        except Exception as exc:  # noqa: BLE001
            _ledger_end(handle, "error")
            log.warning("autonomous first-run failed: %s", exc)

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
            return False  # already tracking — the cron owns it now
    except Exception:
        pass
    try:
        from bot.heavyweight import _flagship_universe
        if not _flagship_universe():
            return False  # nothing to constrain against yet — wait for Flagship
    except Exception:
        return False
    try:
        from brain import cli_bridge
        if not cli_bridge.available():
            return False  # no subscription/CLI → don't fire a doomed armed run
    except Exception:
        return False
    import threading

    def _go():
        handle = _ledger_start("heavyweight_daily", book="heavyweight", trigger="first_run")
        try:
            from control_plane import locks
            lock = locks.acquire_or_log("book:heavyweight", job="heavyweight_daily", book="heavyweight")
            if lock is None:
                _skip_event("heavyweight_daily", "heavyweight")
                _ledger_end(handle, "skip", severity="ADVISORY_ONLY")
                return
            with lock:
                from bot.heavyweight import run_heavyweight
                run_heavyweight()
            _ledger_end(handle, "ok")
        except Exception as exc:  # noqa: BLE001
            _ledger_end(handle, "error")
            log.warning("heavyweight first-run failed: %s", exc)

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
            return False  # already has a track record — the cron owns it now
    except Exception:
        pass
    try:
        from brain import cli_bridge
        if not cli_bridge.available():
            return False  # no subscription/CLI → don't fire a doomed armed run
    except Exception:
        return False
    import threading

    def _go():
        handle = _ledger_start("china_daily", book="china", trigger="first_run")
        try:
            from control_plane import locks
            lock = locks.acquire_or_log("book:china", job="china_daily", book="china")
            if lock is None:
                _skip_event("china_daily", "china")
                _ledger_end(handle, "skip", severity="ADVISORY_ONLY")
                return
            with lock:
                from bot.china import run_china
                run_china()
            _ledger_end(handle, "ok")
        except Exception as exc:  # noqa: BLE001
            _ledger_end(handle, "error")
            log.warning("china first-run failed: %s", exc)

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
            return False  # already has a track record — the cron owns it now
    except Exception:
        pass
    try:
        from brain import cli_bridge
        if not cli_bridge.available():
            return False  # no subscription/CLI → don't fire a doomed armed run
    except Exception:
        return False
    import threading

    def _go():
        handle = _ledger_start("hk_daily", book="hk", trigger="first_run")
        try:
            from control_plane import locks
            lock = locks.acquire_or_log("book:hk", job="hk_daily", book="hk")
            if lock is None:
                _skip_event("hk_daily", "hk")
                _ledger_end(handle, "skip", severity="ADVISORY_ONLY")
                return
            with lock:
                from bot.hk import run_hk
                run_hk()
            _ledger_end(handle, "ok")
        except Exception as exc:  # noqa: BLE001
            _ledger_end(handle, "error")
            log.warning("hk first-run failed: %s", exc)

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
            return False  # already has a track record — the cron owns it now
    except Exception:
        pass
    try:
        from brain import cli_bridge
        if not cli_bridge.available():
            return False  # no subscription/CLI → don't fire a doomed armed run
    except Exception:
        return False
    import threading

    def _go():
        handle = _ledger_start("etf_daily", book="etf", trigger="first_run")
        try:
            from control_plane import locks
            lock = locks.acquire_or_log("book:etf", job="etf_daily", book="etf")
            if lock is None:
                _skip_event("etf_daily", "etf")
                _ledger_end(handle, "skip", severity="ADVISORY_ONLY")
                return
            with lock:
                from bot.etf import run_etf
                run_etf()
            _ledger_end(handle, "ok")
        except Exception as exc:  # noqa: BLE001
            _ledger_end(handle, "error")
            log.warning("etf first-run failed: %s", exc)

    threading.Thread(target=_go, name="etf-first-run", daemon=True).start()
    return True
