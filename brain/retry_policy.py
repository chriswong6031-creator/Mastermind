"""brain/retry_policy.py — the "don't miss decision days" retry decision.

When ALL enabled OAuth keys are cooling/dead, a Brain-armed book job completes with a
no-decision (the book carries forward unchanged — a missed decision day).  If the pool's
earliest reset lands before the job's NEXT scheduled run, we can re-run the SAME job once
the quota window reopens and still get today's decision.

This module holds the PURE decision — no scheduler, no clock, no I/O — so it is unit-testable
in isolation.  ``app/scheduler.py`` owns the side effects (reading the run-event marker,
registering the one-shot job, persisting the per-day counter).

CONTRACT
--------
decide_retry(failure, now, today_count, next_scheduled_run, ...) -> Optional[datetime]

Returns the UTC datetime at which to schedule a one-shot re-run, or None when a retry
should NOT be scheduled.  A retry is scheduled only when ALL hold:

  1. ``failure.all_cooling`` is True (the job failed specifically because the whole pool
     was cooling/dead — not some other error).
  2. ``failure.earliest_reset`` parses to a real UTC ts (we know WHEN the pool reopens).
  3. ``today_count < max_retries`` (default 2 per job per calendar day).
  4. retry_at = earliest_reset + jitter(5..10 min) lands BEFORE
     (next_scheduled_run - guard_minutes), i.e. there is room to slot a retry in before the
     job's own next cron fire (default guard 30 min).  A retry that would collide with (or land
     after) the next scheduled run is pointless — the cron will handle it.

All inputs are explicit; the only nondeterminism (jitter) is injectable via ``jitter_seconds``
so tests can pin it.  Never raises — returns None on any malformed input.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


# Defaults (mirrored as scheduler kwargs; single source of truth here).
MAX_RETRIES_PER_DAY = 2
GUARD_MINUTES = 30          # retry must land >= this long before the next scheduled run
JITTER_MIN_SECONDS = 5 * 60
JITTER_MAX_SECONDS = 10 * 60


@dataclass(frozen=True)
class BrainFailure:
    """The minimal failure info the retry decision needs.

    all_cooling    — True when the job's Brain failed because the whole pool was cooling/dead.
    earliest_reset — ISO-8601 UTC ts of the pool's earliest reset (from all_cooling_info /
                     the cli_bridge marker), or "" / None when unknown.
    """
    all_cooling: bool
    earliest_reset: str | None = None


def _parse_ts(ts: str | None) -> datetime | None:
    """Parse an ISO-8601 UTC ts to an aware datetime, or None.  Never raises."""
    if not ts:
        return None
    try:
        s = ts.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:  # noqa: BLE001
        return None


def decide_retry(
    failure: BrainFailure,
    now: datetime,
    today_count: int,
    next_scheduled_run: datetime | None,
    *,
    max_retries: int = MAX_RETRIES_PER_DAY,
    guard_minutes: int = GUARD_MINUTES,
    jitter_seconds: int | None = None,
) -> datetime | None:
    """Decide whether (and when) to schedule a one-shot re-run.  See module docstring.

    Parameters
    ----------
    failure : BrainFailure
        The classified failure.  A retry is only ever scheduled for an all-cooling failure.
    now : datetime
        Current UTC time (aware or naive-as-UTC).
    today_count : int
        How many retries have ALREADY been scheduled for this job today.
    next_scheduled_run : datetime | None
        The job's next cron fire time.  None → treat as "no bound" (the guard check is skipped;
        the retry is still bounded by earliest_reset being in the future).
    max_retries, guard_minutes, jitter_seconds : see module constants.

    Returns
    -------
    datetime | None
        The one-shot retry time (UTC, aware), or None when no retry should be scheduled.
    """
    try:
        if not isinstance(failure, BrainFailure) or not failure.all_cooling:
            return None
        if today_count >= max_retries:
            return None

        reset = _parse_ts(failure.earliest_reset)
        if reset is None:
            return None  # unknown reset → cannot time a retry

        now_utc = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)

        # Jitter (5..10 min) — injectable for deterministic tests.
        if jitter_seconds is None:
            jit = random.randint(JITTER_MIN_SECONDS, JITTER_MAX_SECONDS)
        else:
            jit = int(jitter_seconds)
        retry_at = reset + timedelta(seconds=jit)

        # The reset may already be in the past (e.g. the window reopened between the failure
        # and this decision); in that case run soon (now + jitter) rather than in the past.
        if retry_at <= now_utc:
            retry_at = now_utc + timedelta(seconds=jit)

        # Guard: the retry must land at least guard_minutes BEFORE the next scheduled run,
        # otherwise the cron will handle it and a retry is pointless.
        if next_scheduled_run is not None:
            nsr = (next_scheduled_run.astimezone(timezone.utc)
                   if next_scheduled_run.tzinfo else next_scheduled_run.replace(tzinfo=timezone.utc))
            if retry_at >= nsr - timedelta(minutes=guard_minutes):
                return None

        return retry_at
    except Exception:  # noqa: BLE001 — a retry-decision miss must never disturb the scheduler
        return None
