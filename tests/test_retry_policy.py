"""Tests for brain/retry_policy.py — the pure retry-at-earliest-reset decision.

No scheduler, no clock, no I/O: every input is explicit and the only nondeterminism
(jitter) is pinned via ``jitter_seconds``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from brain.retry_policy import (
    BrainFailure,
    decide_retry,
    GUARD_MINUTES,
    MAX_RETRIES_PER_DAY,
)


_NOW = datetime(2026, 7, 20, 22, 50, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _reset_in(hours: float) -> str:
    return _iso(_NOW + timedelta(hours=hours))


def _far_next_run() -> datetime:
    # Next cron ~next day — always leaves room for a retry.
    return _NOW + timedelta(days=1)


class TestDecideRetry:
    def test_happy_path_fires_at_reset_plus_jitter(self):
        reset = _reset_in(2)
        r = decide_retry(BrainFailure(True, reset), _NOW, 0, _far_next_run(),
                         jitter_seconds=300)
        assert r == datetime.fromisoformat(reset) + timedelta(seconds=300)

    def test_not_all_cooling_returns_none(self):
        # A non-all-cooling failure must never schedule a retry.
        assert decide_retry(BrainFailure(False, _reset_in(2)), _NOW, 0, _far_next_run(),
                            jitter_seconds=300) is None

    def test_unknown_reset_returns_none(self):
        assert decide_retry(BrainFailure(True, ""), _NOW, 0, _far_next_run(),
                            jitter_seconds=300) is None
        assert decide_retry(BrainFailure(True, None), _NOW, 0, _far_next_run(),
                            jitter_seconds=300) is None
        assert decide_retry(BrainFailure(True, "not-a-timestamp"), _NOW, 0, _far_next_run(),
                            jitter_seconds=300) is None

    def test_cap_at_max_retries(self):
        reset = _reset_in(2)
        # count 0 and 1 fire; count == MAX does not.
        assert decide_retry(BrainFailure(True, reset), _NOW, MAX_RETRIES_PER_DAY - 1,
                            _far_next_run(), jitter_seconds=300) is not None
        assert decide_retry(BrainFailure(True, reset), _NOW, MAX_RETRIES_PER_DAY,
                            _far_next_run(), jitter_seconds=300) is None
        assert decide_retry(BrainFailure(True, reset), _NOW, MAX_RETRIES_PER_DAY + 5,
                            _far_next_run(), jitter_seconds=300) is None

    def test_guard_blocks_retry_too_close_to_next_run(self):
        reset = _reset_in(2)
        retry_at = datetime.fromisoformat(reset) + timedelta(seconds=300)
        # next run only 20 min after the retry → within the 30-min guard → blocked.
        nsr = retry_at + timedelta(minutes=GUARD_MINUTES - 10)
        assert decide_retry(BrainFailure(True, reset), _NOW, 0, nsr, jitter_seconds=300) is None

    def test_guard_allows_retry_with_room(self):
        reset = _reset_in(2)
        retry_at = datetime.fromisoformat(reset) + timedelta(seconds=300)
        # next run 45 min after the retry → outside the 30-min guard → allowed.
        nsr = retry_at + timedelta(minutes=GUARD_MINUTES + 15)
        r = decide_retry(BrainFailure(True, reset), _NOW, 0, nsr, jitter_seconds=300)
        assert r == retry_at

    def test_no_next_run_bound_still_fires(self):
        # next_scheduled_run=None → guard skipped, retry still bounded by reset+jitter.
        reset = _reset_in(2)
        r = decide_retry(BrainFailure(True, reset), _NOW, 0, None, jitter_seconds=300)
        assert r == datetime.fromisoformat(reset) + timedelta(seconds=300)

    def test_past_reset_runs_now_plus_jitter(self):
        # If the reset already passed, run soon (now + jitter), not in the past.
        reset = _iso(_NOW - timedelta(hours=1))
        r = decide_retry(BrainFailure(True, reset), _NOW, 0, _far_next_run(),
                         jitter_seconds=300)
        assert r == _NOW + timedelta(seconds=300)

    def test_jitter_within_5_to_10_min_when_unpinned(self):
        reset = _reset_in(2)
        base = datetime.fromisoformat(reset)
        for _ in range(50):
            r = decide_retry(BrainFailure(True, reset), _NOW, 0, _far_next_run())
            assert r is not None
            delta = (r - base).total_seconds()
            assert 5 * 60 <= delta <= 10 * 60

    def test_naive_datetimes_treated_as_utc(self):
        # A naive now / next_run must not raise (treated as UTC).
        reset = _reset_in(2)
        naive_now = _NOW.replace(tzinfo=None)
        naive_nsr = (_NOW + timedelta(days=1)).replace(tzinfo=None)
        r = decide_retry(BrainFailure(True, reset), naive_now, 0, naive_nsr,
                         jitter_seconds=300)
        assert r is not None

    def test_z_suffixed_reset_parses(self):
        # earliest_reset with a trailing 'Z' must parse.
        reset_z = (_NOW + timedelta(hours=2)).isoformat(timespec="seconds").replace("+00:00", "Z")
        r = decide_retry(BrainFailure(True, reset_z), _NOW, 0, _far_next_run(),
                         jitter_seconds=300)
        assert r is not None

    def test_custom_max_and_guard(self):
        reset = _reset_in(2)
        # custom max_retries=1: count 0 fires, count 1 does not.
        assert decide_retry(BrainFailure(True, reset), _NOW, 0, _far_next_run(),
                            max_retries=1, jitter_seconds=300) is not None
        assert decide_retry(BrainFailure(True, reset), _NOW, 1, _far_next_run(),
                            max_retries=1, jitter_seconds=300) is None
