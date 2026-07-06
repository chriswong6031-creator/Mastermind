"""control_plane.run_ledger — job-level run records for Mastermind.

DISTINCTION FROM brain/runlog.py
---------------------------------
``brain/runlog.py`` is the LLM-SESSION trace: every individual call, result,
reasoning chunk, and trade step within a single bot session is appended to a
per-session JSONL file.  It is deep, granular, and session-scoped.

``control_plane.run_ledger`` is the JOB-LEVEL layer: one ``run_started`` event
and one ``run_finished`` event bracket each nightly or intraday job (loop_maintenance,
settle, derisk, watch, clamp, publish_macro_snapshot, etc.).  Records carry:
  - run_id (uuid + ts)
  - job name and book id
  - trigger type (cron / http / manual / test)
  - git short-SHA at invocation (captured once at module import, cached)
  - flags snapshot (MASTERMIND_* env vars at invocation, from control_plane.flags)
  - status and severity at completion
  - artifact paths produced/consumed

This layer is queryable without loading individual session JSONL blobs and
is the permanent audit trail for "did this job run, when, and did it succeed?"
Both layers may coexist for the same job; they serve different query patterns.

NEVER-RAISE CONTRACT
--------------------
``start_run`` and ``end_run`` never propagate exceptions; errors are logged
as warnings and a null/best-effort result is returned.  A run-ledger failure
must never abort the job being instrumented.
"""
from __future__ import annotations

import logging
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# git SHA — captured once at module import, cached for the process lifetime.
# Using subprocess here is intentional: importlib metadata is unreliable in
# editable installs and does not give HEAD SHA.
# ---------------------------------------------------------------------------

def _get_git_sha() -> str:
    """Return current git short-SHA, or 'unknown' on failure."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:  # noqa: BLE001
        pass
    return "unknown"


_GIT_SHA: str = _get_git_sha()


# ---------------------------------------------------------------------------
# RunHandle
# ---------------------------------------------------------------------------

@dataclass
class RunHandle:
    """Opaque handle returned by ``start_run``; pass to ``end_run``."""
    run_id:    str
    job:       str
    book:      str
    trigger:   str
    _started:  float = field(default_factory=time.monotonic, repr=False)

    def elapsed(self) -> float:
        """Elapsed wall-clock seconds since ``start_run`` was called."""
        return time.monotonic() - self._started


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def start_run(
    job: str,
    *,
    book: str | None = None,
    trigger: str = "cron",
    extra: dict[str, Any] | None = None,
    root: str = None,  # type: ignore[assignment]
) -> RunHandle:
    """Record a run_started event and return a RunHandle.

    Parameters
    ----------
    job : str
        Job name, e.g. "loop_maintenance", "settle", "derisk".
    book : str | None
        Portfolio book id, e.g. "flagship".  None for global/multi-book jobs.
    trigger : str
        How the job was invoked: "cron" | "http" | "manual" | "test".
    extra : dict | None
        Extra fields to embed in the run_started event.
    root : str | None
        Project root override for tests.

    Returns
    -------
    RunHandle
        Must be passed to ``end_run`` when the job completes.
        NEVER raises — on error returns a minimal handle with run_id="error".
    """
    try:
        from control_plane import flags as _flags  # local — avoids import-time cost
        from control_plane import run_events

        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        run_id = f"{ts[:19].replace(':', '-').replace('T', '_')}_{uuid.uuid4().hex[:8]}"
        handle = RunHandle(run_id=run_id, job=job, book=book or "", trigger=trigger)

        event: dict[str, Any] = {
            "kind":    "run_started",
            "job":     job,
            "book":    book or "",
            "step":    "start",
            "status":  "ok",
            "actor":   "system",
            "extra": {
                "run_id":       run_id,
                "trigger":      trigger,
                "git_sha":      _GIT_SHA,
                "flags":        _flags.enumerate_flags(),
                **(extra or {}),
            },
        }
        run_events.append(event, root=root)
        return handle

    except Exception as exc:  # noqa: BLE001
        log.warning("control_plane.run_ledger.start_run(%r) failed: %s", job, exc)
        return RunHandle(run_id="error", job=job, book=book or "", trigger=trigger)


def end_run(
    handle: RunHandle,
    status: str,
    *,
    severity: str | None = None,
    artifacts: list[str] | None = None,
    extra: dict[str, Any] | None = None,
    root: str = None,  # type: ignore[assignment]
) -> None:
    """Record a run_finished event.

    Parameters
    ----------
    handle : RunHandle
        The handle returned by ``start_run``.
    status : str
        "ok" | "error" | "skip" | "warn".
    severity : str | None
        Severity enum name (e.g. "HARD_STOP", "FREEZE") if an R3 guardrail fired.
    artifacts : list[str] | None
        Output artifact paths written by this job run.
    extra : dict | None
        Extra fields to embed.

    NEVER raises.
    """
    # Captured OUTSIDE the try so the except handler cannot itself raise on a
    # None/stub handle — the never-raise contract must hold on the failure path.
    job = getattr(handle, "job", "?")
    try:
        from control_plane import run_events

        elapsed = round(handle.elapsed(), 3)
        event: dict[str, Any] = {
            "kind":     "run_finished",
            "job":      handle.job,
            "book":     handle.book,
            "step":     "end",
            "status":   status,
            "severity": severity,
            "actor":    "system",
            "extra": {
                "run_id":    handle.run_id,
                "trigger":   handle.trigger,
                "git_sha":   _GIT_SHA,
                "elapsed_s": elapsed,
                "artifacts": artifacts or [],
                **(extra or {}),
            },
        }
        run_events.append(event, root=root)

    except Exception as exc:  # noqa: BLE001
        log.warning("control_plane.run_ledger.end_run(%r) failed: %s", job, exc)
