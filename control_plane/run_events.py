"""control_plane.run_events — append-only event log for Mastermind jobs.

Writes to data/governance/run_events.jsonl (one JSON object per line).

NEVER-RAISE CONTRACT
--------------------
``append()`` catches all exceptions, logs a warning, and returns None.
A logging failure must never abort the job that generated the event.
This mirrors the never-raise contract in
``vendor/macro_src/engine/neuralweb/governance.py``.

EVENT-ID CONTRACT
-----------------
``event_id`` is the first 16 hex digits of SHA-256 over the canonical-JSON
representation of (ts, kind, job, book, step) — deterministic given those
five fields.  The full payload is NOT included so callers can reconstruct
the id without the full event dict.  It is a CONTENT HASH, NOT a primary
key: two same-second events with an identical key tuple collide.  Both rows
are still written (append-only); consumers must never dedupe by event_id.

Standard fields (all optional except ts and event_id):
    ts          (str)  UTC ISO-8601 isoformat(timespec="seconds") — set automatically
    event_id    (str)  SHA-256[:16] of canonical key tuple — set automatically
    kind        (str)  event kind, e.g. "run_started", "run_finished", "guardrail",
                       "lock_conflict", "exception_swallowed"
    job         (str)  name of the nightly/intraday job, e.g. "loop_maintenance"
    book        (str)  portfolio book id, e.g. "flagship"
    step        (str)  sub-step within the job, e.g. "settle"
    status      (str)  "ok" | "error" | "skip" | "warn"
    severity    (str)  Severity enum value (see guardrail.py), or None
    err         (str)  repr(exc) truncated to 500 chars, or None
    actor       (str)  writer identity, default "system"
    extra       (dict) free-form additional fields

Append is atomic at the single-line level: ``open(..., "a")`` + one write.
The OS guarantees that a single write() of a complete line is not interleaved
with other writers on POSIX (Advisory: do not exceed ~4 KB per event to stay
inside typical pipe-buffer atomicity).
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# path resolution
# ---------------------------------------------------------------------------

def _ledger_path(root: str | Path | None = None) -> Path:
    """Resolve data/governance/run_events.jsonl, creating parent dirs as needed."""
    if root is None:
        base = Path(__file__).resolve().parent.parent / "data"
    else:
        base = Path(root) / "data"
    p = base / "governance" / "run_events.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------------------
# event-id
# ---------------------------------------------------------------------------

def _event_id(ts: str, kind: str, job: str, book: str, step: str) -> str:
    """SHA-256[:16] of canonical (ts|kind|job|book|step) — deterministic."""
    raw = f"{ts}|{kind}|{job}|{book}|{step}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def append(
    event: dict[str, Any],
    *,
    root: str | Path | None = None,
) -> str | None:
    """Append one event to data/governance/run_events.jsonl.

    Parameters
    ----------
    event : dict
        Caller-supplied payload.  ``ts`` and ``event_id`` are injected
        automatically; any caller-supplied ``ts`` is overridden.
        Standard keys: kind, job, book, step, status, severity, err, actor, extra.
    root : str | Path | None
        Project root override (None = resolve relative to this file).
        Used in tests to redirect writes to a tmp dir.

    Returns
    -------
    str | None
        The ``event_id`` of the appended record, or None on any error.
        NEVER raises.
    """
    try:
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        kind  = str(event.get("kind",  "") or "")
        job   = str(event.get("job",   "") or "")
        book  = str(event.get("book",  "") or "")
        step  = str(event.get("step",  "") or "")
        eid   = _event_id(ts, kind, job, book, step)

        row: dict[str, Any] = {
            "ts":       ts,
            "event_id": eid,
            "kind":     kind,
            "job":      job,
            "book":     book,
            "step":     step,
            "status":   event.get("status") or None,
            "severity": event.get("severity") or None,
            "err":      (repr(event["err"])[:500] if event.get("err") else None),
            "actor":    event.get("actor") or "system",
            "extra":    event.get("extra") or {},
        }
        # remove explicit None values to keep lines compact
        row = {k: v for k, v in row.items() if v is not None and v != {}}
        # always keep ts and event_id
        row["ts"] = ts
        row["event_id"] = eid

        p = _ledger_path(root)
        line = json.dumps(row, separators=(",", ":"), default=str)
        with p.open("a") as fh:
            fh.write(line + "\n")
        return eid

    except Exception as exc:  # noqa: BLE001
        log.warning("control_plane.run_events.append failed: %s", exc)
        return None
