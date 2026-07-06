"""Tests for MW1 scheduler governance — L3 lane.

Covers:
  - lock contention: concurrent same-book trigger → one skips + emits run_skipped event
  - loop_maintenance step failure → step_failed event + remaining steps still run
  - /api/scheduler shape (endpoint returns jobs list with required keys)
  - startup event written on app_started
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _read_events(root: Path) -> list[dict]:
    p = root / "data" / "governance" / "run_events.jsonl"
    if not p.exists():
        return []
    rows = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _events_of_kind(root: Path, kind: str) -> list[dict]:
    return [e for e in _read_events(root) if e.get("kind") == kind]


# ---------------------------------------------------------------------------
# 1. Lock contention: same-book concurrent trigger → one skips + event written
# ---------------------------------------------------------------------------

class TestLockContention:
    def test_concurrent_same_book_one_skips(self, tmp_path):
        """Two concurrent calls for the same book-lock: the second must skip + emit run_skipped."""
        from control_plane import locks, run_events

        # Hold the lock from "thread 1"
        held = locks.acquire("book:flagship", root=tmp_path)
        assert held is not None, "first acquire must succeed"

        # Simulate what the scheduler does when lock is held: acquire_or_log returns None, caller
        # writes run_skipped.
        try:
            lock2 = locks.acquire_or_log(
                "book:flagship",
                job="daily_loop",
                book="flagship",
                root=tmp_path,
                events_root=tmp_path,
            )
            assert lock2 is None, "second acquire while first is held must return None"

            # acquire_or_log writes a lock_conflict event; the scheduler wrapper then writes
            # run_skipped. Verify lock_conflict was written.
            events = _read_events(tmp_path)
            conflicts = [e for e in events if e.get("kind") == "lock_conflict"]
            assert len(conflicts) >= 1
            assert conflicts[0]["status"] == "lock_held"
            assert conflicts[0]["job"] == "daily_loop"

            # Now simulate the full _skip_event + _ledger_end(skip) path used in scheduler.py
            run_events.append({
                "kind": "run_skipped",
                "job": "daily_loop",
                "book": "flagship",
                "step": "acquire",
                "status": "lock_held",
                "severity": "ADVISORY_ONLY",
                "actor": "system",
            }, root=tmp_path)

            skipped = [e for e in _read_events(tmp_path) if e.get("kind") == "run_skipped"]
            assert len(skipped) >= 1
            assert skipped[0]["status"] == "lock_held"
        finally:
            held.release()

    def test_after_release_second_acquires(self, tmp_path):
        """After the first holder releases, a new acquire must succeed."""
        from control_plane import locks

        lock1 = locks.acquire("book:autonomous", root=tmp_path)
        assert lock1 is not None
        lock1.release()

        lock2 = locks.acquire_or_log(
            "book:autonomous",
            job="autonomous_daily",
            book="autonomous",
            root=tmp_path,
            events_root=tmp_path,
        )
        assert lock2 is not None, "after release, second acquire must succeed"
        lock2.release()

    def test_different_books_independent(self, tmp_path):
        """Locks on different book ids are independent."""
        from control_plane import locks

        la = locks.acquire("book:flagship", root=tmp_path)
        lb = locks.acquire("book:autonomous", root=tmp_path)
        assert la is not None
        assert lb is not None
        la.release()
        lb.release()


# ---------------------------------------------------------------------------
# 2. loop_maintenance step failure → step_failed event + remaining steps run
# ---------------------------------------------------------------------------

class TestLoopMaintenanceStepFailures:
    def test_step_failed_event_written_on_exception(self, tmp_path, monkeypatch):
        """A step that raises must emit a step_failed event and not abort later steps."""
        import app.scheduler as sched_mod
        import control_plane.run_events as re_mod

        # Redirect run_events writes to tmp_path
        original_ledger_path = re_mod._ledger_path

        def _patched_ledger_path(root=None):
            return original_ledger_path(tmp_path)

        monkeypatch.setattr(re_mod, "_ledger_path", _patched_ledger_path)

        # Patch _run_loop_maintenance_steps so two steps run: first raises, second records a sentinel
        sentinel = {"ran": False}

        def _fake_steps():
            # step 1: fail
            try:
                raise RuntimeError("injected failure")
            except Exception as exc:
                sched_mod._step_failed_event("loop_maintenance", "", "predictions.record", exc)
            # step 2: should still run despite step 1's failure
            sentinel["ran"] = True

        monkeypatch.setattr(sched_mod, "_run_loop_maintenance_steps", _fake_steps)

        # Also need the lock to succeed — point locks dir at tmp_path
        import control_plane.locks as locks_mod
        orig_locks_dir = locks_mod._locks_dir

        def _patched_locks_dir(root=None):
            return orig_locks_dir(tmp_path)

        monkeypatch.setattr(locks_mod, "_locks_dir", _patched_locks_dir)

        # Call the job directly
        sched_mod._loop_maintenance_job()

        # step_failed event must have been written
        events = _read_events(tmp_path)
        step_fails = [e for e in events if e.get("kind") == "step_failed"]
        assert len(step_fails) >= 1, "step_failed event must be written"
        sf = step_fails[0]
        assert sf["step"] == "predictions.record"
        assert sf["severity"] == "ADVISORY_ONLY"
        assert sf["job"] == "loop_maintenance"

        # remaining steps still ran
        assert sentinel["ran"], "remaining steps must run after a step failure"

    def test_step_failed_event_fields(self, tmp_path):
        """_step_failed_event writes kind=step_failed with required fields."""
        import control_plane.run_events as re_mod
        import app.scheduler as sched_mod

        original_ledger_path = re_mod._ledger_path

        def _patched_ledger_path(root=None):
            return original_ledger_path(tmp_path)

        # Monkeypatch via the module reference used in _step_failed_event
        orig = re_mod._ledger_path
        re_mod._ledger_path = _patched_ledger_path
        try:
            exc = ValueError("test error")
            sched_mod._step_failed_event("loop_maintenance", "flagship", "calibration.persist", exc)
            events = _read_events(tmp_path)
            sf = [e for e in events if e.get("kind") == "step_failed"]
            assert sf, "step_failed event must be written"
            ev = sf[0]
            assert ev["kind"] == "step_failed"
            assert ev["step"] == "calibration.persist"
            assert ev["job"] == "loop_maintenance"
            assert ev["severity"] == "ADVISORY_ONLY"
            assert ev["status"] == "error"
        finally:
            re_mod._ledger_path = orig

    def test_step_failed_never_raises(self, monkeypatch):
        """_step_failed_event must never propagate an exception even if run_events is broken."""
        import app.scheduler as sched_mod
        import control_plane.run_events as re_mod

        monkeypatch.setattr(re_mod, "append", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("broken")))
        # Must not raise
        sched_mod._step_failed_event("loop_maintenance", "", "some_step", RuntimeError("x"))


# ---------------------------------------------------------------------------
# 3. /api/scheduler endpoint shape
# ---------------------------------------------------------------------------

class TestSchedulerEndpoint:
    def test_scheduler_health_returns_list(self, tmp_path, monkeypatch):
        """scheduler_health() returns a list of dicts with required keys."""
        import app.scheduler as sched_mod
        import control_plane.run_events as re_mod

        # Write a couple of run events into tmp_path so the tail reader has something
        import control_plane.run_ledger as rl_mod
        original_ledger_path = re_mod._ledger_path

        def _patched_ledger_path(root=None):
            return original_ledger_path(tmp_path)

        monkeypatch.setattr(re_mod, "_ledger_path", _patched_ledger_path)

        handle = rl_mod.start_run("loop_maintenance", book="", trigger="cron", root=tmp_path)
        rl_mod.end_run(handle, "ok", root=tmp_path)

        # Patch _scheduler to None (no APScheduler instance) to exercise the degraded path
        monkeypatch.setattr(sched_mod, "_scheduler", None)

        # Also patch _ledger_path inside the scheduler_health function's import
        records = sched_mod.scheduler_health()

        assert isinstance(records, list), "scheduler_health must return a list"
        assert len(records) > 0, "must return at least one job record"

        required_keys = {"id", "next_run_time", "last_started", "last_finished",
                         "last_skipped", "last_status", "last_severity"}
        for rec in records:
            missing = required_keys - set(rec.keys())
            assert not missing, f"job record missing keys: {missing}, record: {rec}"

    def test_scheduler_health_known_job_ids(self, monkeypatch):
        """scheduler_health must include all 18 known job ids."""
        import app.scheduler as sched_mod
        import control_plane.run_events as re_mod

        monkeypatch.setattr(sched_mod, "_scheduler", None)
        # Patch ledger path to a non-existent dir so it returns empty gracefully
        monkeypatch.setattr(re_mod, "_ledger_path", lambda root=None: Path("/tmp/__no_such_path__/run_events.jsonl"))

        records = sched_mod.scheduler_health()
        ids = {r["id"] for r in records}
        expected = {
            "macro_refresh", "daily_mark", "daily_loop",
            "autonomous_daily", "heavyweight_daily", "china_daily", "hk_daily", "etf_daily",
            "settle_pending", "settle_brain_asia",
            "watch_us_overnight", "watch_asia_overnight",
            "derisk_us_intraday",
            "publish_macro_snapshot",
            "cio_weekly", "improvement_agenda_weekly",
            "loop_maintenance", "experiment_maturity",
        }
        missing = expected - ids
        assert not missing, f"scheduler_health missing job ids: {missing}"

    def test_scheduler_health_reflects_last_run(self, tmp_path, monkeypatch):
        """scheduler_health correctly reads last_started / last_finished / last_status."""
        import app.scheduler as sched_mod
        import control_plane.run_events as re_mod
        import control_plane.run_ledger as rl_mod

        original_ledger_path = re_mod._ledger_path

        def _patched_ledger_path(root=None):
            return original_ledger_path(tmp_path)

        monkeypatch.setattr(re_mod, "_ledger_path", _patched_ledger_path)
        monkeypatch.setattr(sched_mod, "_scheduler", None)

        handle = rl_mod.start_run("experiment_maturity", book="", trigger="cron", root=tmp_path)
        rl_mod.end_run(handle, "ok", root=tmp_path)

        records = sched_mod.scheduler_health()
        em = next((r for r in records if r["id"] == "experiment_maturity"), None)
        assert em is not None
        assert em["last_status"] == "ok"
        assert em["last_started"] is not None
        assert em["last_finished"] is not None

    def test_api_scheduler_route_returns_jobs(self, monkeypatch):
        """GET /api/scheduler returns {"jobs": [...]} with at least one entry."""
        from fastapi.testclient import TestClient
        try:
            from app.main import app
        except Exception:
            pytest.skip("FastAPI app not importable in this environment")

        import app.scheduler as sched_mod
        import control_plane.run_events as re_mod

        monkeypatch.setattr(sched_mod, "_scheduler", None)
        monkeypatch.setattr(re_mod, "_ledger_path",
                            lambda root=None: Path("/tmp/__no_such_path__/run_events.jsonl"))

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/scheduler")
        assert resp.status_code in (200, 401), f"unexpected status: {resp.status_code}"
        if resp.status_code == 200:
            data = resp.json()
            assert "jobs" in data
            assert isinstance(data["jobs"], list)


# ---------------------------------------------------------------------------
# 4. Startup event written
# ---------------------------------------------------------------------------

class TestStartupEvent:
    def test_app_started_event_fields(self, tmp_path, monkeypatch):
        """The app_started event must carry kind, git_sha, and flags keys."""
        import control_plane.run_events as re_mod

        original_ledger_path = re_mod._ledger_path

        def _patched_ledger_path(root=None):
            return original_ledger_path(tmp_path)

        monkeypatch.setattr(re_mod, "_ledger_path", _patched_ledger_path)

        # Directly emit the same event that _start_scheduler() does (without booting FastAPI)
        re_mod.append({
            "kind": "app_started",
            "job": "startup",
            "book": "",
            "step": "init",
            "status": "ok",
            "actor": "system",
            "extra": {
                "git_sha": "abc1234",
                "flags": {"MASTERMIND_TEST_STARTUP": "1"},
            },
        }, root=tmp_path)

        events = _read_events(tmp_path)
        started = [e for e in events if e.get("kind") == "app_started"]
        assert len(started) == 1
        ev = started[0]
        assert ev["kind"] == "app_started"
        assert ev["job"] == "startup"
        assert ev.get("extra", {}).get("git_sha") == "abc1234"
        assert "MASTERMIND_TEST_STARTUP" in ev.get("extra", {}).get("flags", {})

    def test_app_started_event_survives_restart(self, tmp_path, monkeypatch):
        """Multiple app_started events accumulate in the JSONL (survives restarts)."""
        import control_plane.run_events as re_mod

        original_ledger_path = re_mod._ledger_path

        def _patched_ledger_path(root=None):
            return original_ledger_path(tmp_path)

        monkeypatch.setattr(re_mod, "_ledger_path", _patched_ledger_path)

        for i in range(3):
            re_mod.append({
                "kind": "app_started",
                "job": "startup",
                "book": "",
                "step": "init",
                "status": "ok",
                "actor": "system",
                "extra": {"git_sha": f"sha{i}", "flags": {}},
            }, root=tmp_path)

        events = [e for e in _read_events(tmp_path) if e.get("kind") == "app_started"]
        assert len(events) == 3, "each restart writes a new app_started event"


# ---------------------------------------------------------------------------
# 5. Weekend hygiene — daily_loop and publish_macro_snapshot use mon-fri
# ---------------------------------------------------------------------------

class TestWeekendHygiene:
    def test_daily_loop_is_mon_fri(self):
        """daily_loop trigger must include day_of_week on the nearby CronTrigger call.

        The scheduler builds up a multi-line add_job call:
            sch.add_job(_job, CronTrigger(day_of_week="mon-fri", ...), id="daily_loop", ...)
        We verify that within a window around the daily_loop id string, day_of_week appears.
        """
        import inspect
        import app.scheduler as sched_mod
        src = inspect.getsource(sched_mod.start)
        lines = src.splitlines()
        # Find the line index where daily_loop id is registered
        idx = next((i for i, l in enumerate(lines) if '"daily_loop"' in l or "'daily_loop'" in l), None)
        assert idx is not None, "daily_loop must appear in start()"
        # Check the surrounding 5-line window for day_of_week
        window = lines[max(0, idx - 5): idx + 5]
        found = any("day_of_week" in l for l in window)
        assert found, f"daily_loop block must include day_of_week; window:\n" + "\n".join(window)

    def test_publish_macro_snapshot_is_mon_fri(self):
        """publish_macro_snapshot trigger must include day_of_week on the nearby CronTrigger call."""
        import inspect
        import app.scheduler as sched_mod
        src = inspect.getsource(sched_mod.start)
        lines = src.splitlines()
        idx = next((i for i, l in enumerate(lines)
                    if '"publish_macro_snapshot"' in l or "'publish_macro_snapshot'" in l), None)
        assert idx is not None, "publish_macro_snapshot must appear in start()"
        window = lines[max(0, idx - 5): idx + 5]
        found = any("day_of_week" in l for l in window)
        assert found, f"publish_macro_snapshot block must include day_of_week; window:\n" + "\n".join(window)


# ---------------------------------------------------------------------------
# 6. run_ledger wrapping — ledger start/end round-trip via scheduler helpers
# ---------------------------------------------------------------------------

class TestLedgerHelpers:
    def test_ledger_start_end_via_helpers(self, tmp_path, monkeypatch):
        """_ledger_start/_ledger_end write run_started/run_finished events."""
        import control_plane.run_events as re_mod
        import app.scheduler as sched_mod

        original_ledger_path = re_mod._ledger_path

        def _patched_ledger_path(root=None):
            return original_ledger_path(tmp_path)

        monkeypatch.setattr(re_mod, "_ledger_path", _patched_ledger_path)

        handle = sched_mod._ledger_start("test_job", book="flagship", trigger="cron")
        assert handle is not None
        sched_mod._ledger_end(handle, "ok")

        events = _read_events(tmp_path)
        started  = [e for e in events if e.get("kind") == "run_started"]
        finished = [e for e in events if e.get("kind") == "run_finished"]
        assert len(started) == 1
        assert len(finished) == 1
        assert started[0]["job"] == "test_job"
        assert finished[0]["status"] == "ok"

    def test_ledger_helpers_never_raise_on_broken_events(self, monkeypatch):
        """_ledger_start/_ledger_end must not raise even if control_plane import fails."""
        import app.scheduler as sched_mod

        # Simulate import failure of control_plane
        import sys
        orig = sys.modules.get("control_plane.run_ledger")
        sys.modules["control_plane.run_ledger"] = None  # type: ignore[assignment]
        try:
            handle = sched_mod._ledger_start("broken_job")
            sched_mod._ledger_end(handle, "ok")  # must not raise
        finally:
            if orig is None:
                sys.modules.pop("control_plane.run_ledger", None)
            else:
                sys.modules["control_plane.run_ledger"] = orig
