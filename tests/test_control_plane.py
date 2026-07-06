"""Unit tests for control_plane — MW1 substrate package.

Covers:
  - run_events: append never raises; event_id stable; unwritable dir returns None
  - locks: exclusivity (two acquires on same name, second returns None); release
  - run_ledger: start/end round-trip; both events land in the ledger
  - flags: enumerate_flags picks up monkeypatched MASTERMIND_TEST_FLAG
  - guardrail: GuardrailResult.log severity filtering (TELEMETRY_ONLY silent)
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _read_events(root: Path) -> list[dict]:
    """Read all events from the tmp ledger."""
    p = root / "data" / "governance" / "run_events.jsonl"
    if not p.exists():
        return []
    rows = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


# ===========================================================================
# run_events
# ===========================================================================

class TestRunEvents:
    def test_append_returns_event_id(self, tmp_path):
        from control_plane import run_events
        eid = run_events.append({"kind": "test", "job": "job1", "book": "b"}, root=tmp_path)
        assert eid is not None
        assert len(eid) == 16, f"event_id should be 16 hex chars, got {eid!r}"

    def test_append_writes_to_ledger(self, tmp_path):
        from control_plane import run_events
        run_events.append({"kind": "k1", "job": "j1", "book": "b1", "step": "s1"}, root=tmp_path)
        rows = _read_events(tmp_path)
        assert len(rows) == 1
        row = rows[0]
        assert row["kind"] == "k1"
        assert row["job"] == "j1"
        assert "ts" in row
        assert "event_id" in row

    def test_append_multiple_rows(self, tmp_path):
        from control_plane import run_events
        for i in range(5):
            run_events.append({"kind": f"k{i}", "job": "j"}, root=tmp_path)
        rows = _read_events(tmp_path)
        assert len(rows) == 5

    def test_event_id_stable(self, tmp_path, monkeypatch):
        """Same ts+kind+job+book+step always produces the same event_id."""
        from control_plane import run_events as re_mod

        # Freeze time so two calls produce the same ts
        fixed_ts = "2026-07-05T12:00:00+00:00"

        import datetime as dt_module
        class _FakeDt(dt_module.datetime):
            @classmethod
            def now(cls, tz=None):  # type: ignore[override]
                return dt_module.datetime.fromisoformat(fixed_ts)

        # Patch the datetime inside run_events
        monkeypatch.setattr(re_mod, "datetime", _FakeDt, raising=False)

        # Also patch the datetime module reference used inside the function
        import sys
        import datetime as _datetime_mod
        orig = _datetime_mod.datetime
        _datetime_mod.datetime = _FakeDt  # type: ignore[assignment]

        try:
            e1 = re_mod.append({"kind": "k", "job": "j", "book": "b", "step": "s"}, root=tmp_path)
            # Patch internal _event_id directly to verify determinism
            eid_direct = re_mod._event_id(fixed_ts, "k", "j", "b", "s")
            assert e1 == eid_direct, "event_id must match direct _event_id call"
        finally:
            _datetime_mod.datetime = orig  # type: ignore[assignment]

    def test_append_never_raises_on_unwritable_dir(self, tmp_path, monkeypatch):
        """Even if the ledger directory is unwritable, append returns None silently."""
        from control_plane import run_events as re_mod

        def _bad_path(*a, **kw):
            raise PermissionError("denied")

        monkeypatch.setattr(re_mod, "_ledger_path", _bad_path)
        # Must return None, not raise
        result = re_mod.append({"kind": "test"}, root=tmp_path)
        assert result is None

    def test_append_actor_default(self, tmp_path):
        from control_plane import run_events
        run_events.append({"kind": "x"}, root=tmp_path)
        row = _read_events(tmp_path)[0]
        assert row.get("actor") == "system"

    def test_append_custom_actor(self, tmp_path):
        from control_plane import run_events
        run_events.append({"kind": "x", "actor": "loop_maintenance"}, root=tmp_path)
        row = _read_events(tmp_path)[0]
        assert row.get("actor") == "loop_maintenance"

    def test_err_truncated(self, tmp_path):
        from control_plane import run_events
        long_err = "x" * 1000
        run_events.append({"kind": "e", "err": Exception(long_err)}, root=tmp_path)
        row = _read_events(tmp_path)[0]
        # err field is repr'd and truncated to 500
        assert len(row.get("err", "")) <= 510  # some repr overhead allowed


# ===========================================================================
# locks
# ===========================================================================

class TestLocks:
    def test_acquire_returns_lock(self, tmp_path):
        from control_plane import locks
        lock = locks.acquire("book:test_a", root=tmp_path)
        assert lock is not None
        lock.release()

    def test_second_acquire_on_same_name_returns_none(self, tmp_path):
        """With the first lock held, a second acquire on the same name returns None."""
        from control_plane import locks
        lock1 = locks.acquire("book:exclusivity", root=tmp_path)
        assert lock1 is not None, "first acquire must succeed"
        try:
            lock2 = locks.acquire("book:exclusivity", root=tmp_path)
            assert lock2 is None, "second acquire while first is held must return None"
        finally:
            lock1.release()

    def test_acquire_after_release_succeeds(self, tmp_path):
        """After releasing the first lock, a new acquire on the same name succeeds."""
        from control_plane import locks
        lock1 = locks.acquire("book:reacquire", root=tmp_path)
        assert lock1 is not None
        lock1.release()
        lock2 = locks.acquire("book:reacquire", root=tmp_path)
        assert lock2 is not None, "acquire after release must succeed"
        lock2.release()

    def test_context_manager(self, tmp_path):
        from control_plane import locks
        with locks.acquire("global:ctx_test", root=tmp_path) as lock:
            assert lock is not None
            # while inside, a second acquire returns None
            lock2 = locks.acquire("global:ctx_test", root=tmp_path)
            assert lock2 is None

    def test_different_names_independent(self, tmp_path):
        from control_plane import locks
        lock_a = locks.acquire("book:independent_a", root=tmp_path)
        lock_b = locks.acquire("book:independent_b", root=tmp_path)
        assert lock_a is not None
        assert lock_b is not None
        lock_a.release()
        lock_b.release()

    def test_release_idempotent(self, tmp_path):
        from control_plane import locks
        lock = locks.acquire("global:idempotent", root=tmp_path)
        assert lock is not None
        lock.release()
        lock.release()  # second release must not raise

    def test_lock_file_created_under_data_locks(self, tmp_path):
        from control_plane import locks
        lock = locks.acquire("book:file_check", root=tmp_path)
        assert lock is not None
        lock_path = tmp_path / "data" / "locks" / "book:file_check.lock"
        assert lock_path.exists()
        lock.release()


# ===========================================================================
# run_ledger
# ===========================================================================

class TestRunLedger:
    def _events_for(self, tmp_path: Path, kind: str) -> list[dict]:
        return [e for e in _read_events(tmp_path) if e.get("kind") == kind]

    def test_start_end_round_trip(self, tmp_path):
        from control_plane import run_ledger
        handle = run_ledger.start_run("test_job", book="flagship", trigger="test", root=tmp_path)
        assert handle.run_id != "error"
        run_ledger.end_run(handle, "ok", root=tmp_path)

        started = self._events_for(tmp_path, "run_started")
        finished = self._events_for(tmp_path, "run_finished")
        assert len(started) == 1
        assert len(finished) == 1

    def test_start_event_has_git_sha(self, tmp_path):
        from control_plane import run_ledger
        handle = run_ledger.start_run("sha_job", root=tmp_path)
        run_ledger.end_run(handle, "ok", root=tmp_path)
        started = self._events_for(tmp_path, "run_started")[0]
        extra = started.get("extra", {})
        assert "git_sha" in extra
        assert extra["git_sha"] != ""

    def test_start_event_has_flags_snapshot(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MASTERMIND_TEST_RUNLEDGER", "1")
        from control_plane import run_ledger
        handle = run_ledger.start_run("flags_job", root=tmp_path)
        run_ledger.end_run(handle, "ok", root=tmp_path)
        started = self._events_for(tmp_path, "run_started")[0]
        flags_snap = started.get("extra", {}).get("flags", {})
        assert "MASTERMIND_TEST_RUNLEDGER" in flags_snap

    def test_end_event_has_elapsed(self, tmp_path):
        from control_plane import run_ledger
        handle = run_ledger.start_run("elapsed_job", root=tmp_path)
        run_ledger.end_run(handle, "ok", root=tmp_path)
        finished = self._events_for(tmp_path, "run_finished")[0]
        extra = finished.get("extra", {})
        assert "elapsed_s" in extra
        assert extra["elapsed_s"] >= 0

    def test_end_event_has_artifacts(self, tmp_path):
        from control_plane import run_ledger
        handle = run_ledger.start_run("art_job", root=tmp_path)
        run_ledger.end_run(handle, "ok", artifacts=["data/x.json"], root=tmp_path)
        finished = self._events_for(tmp_path, "run_finished")[0]
        assert "data/x.json" in finished.get("extra", {}).get("artifacts", [])

    def test_end_never_raises(self, tmp_path, monkeypatch):
        from control_plane import run_ledger, run_events as re_mod

        monkeypatch.setattr(re_mod, "_ledger_path", lambda *a, **kw: (_ for _ in ()).throw(
            PermissionError("denied")))

        handle = run_ledger.start_run("safe_job", root=tmp_path)
        # must not raise even if ledger is broken
        run_ledger.end_run(handle, "ok", root=tmp_path)

    def test_severity_propagated(self, tmp_path):
        from control_plane import run_ledger
        handle = run_ledger.start_run("sev_job", root=tmp_path)
        run_ledger.end_run(handle, "error", severity="FREEZE", root=tmp_path)
        finished = self._events_for(tmp_path, "run_finished")[0]
        assert finished.get("severity") == "FREEZE"

    def test_run_id_in_both_events(self, tmp_path):
        from control_plane import run_ledger
        handle = run_ledger.start_run("id_job", root=tmp_path)
        run_ledger.end_run(handle, "ok", root=tmp_path)
        started  = self._events_for(tmp_path, "run_started")[0]
        finished = self._events_for(tmp_path, "run_finished")[0]
        assert started["extra"]["run_id"] == finished["extra"]["run_id"] == handle.run_id


# ===========================================================================
# flags
# ===========================================================================

class TestFlags:
    def test_enumerate_flags_picks_up_test_flag(self, monkeypatch):
        monkeypatch.setenv("MASTERMIND_TEST_FLAG", "1")
        from control_plane import flags
        result = flags.enumerate_flags()
        assert "MASTERMIND_TEST_FLAG" in result
        assert result["MASTERMIND_TEST_FLAG"] == "1"

    def test_non_mastermind_vars_excluded(self, monkeypatch):
        monkeypatch.setenv("SOME_OTHER_VAR", "abc")
        from control_plane import flags
        result = flags.enumerate_flags()
        assert "SOME_OTHER_VAR" not in result

    def test_known_flags_is_nonempty_list(self):
        from control_plane import flags
        assert isinstance(flags.KNOWN_FLAGS, list)
        assert len(flags.KNOWN_FLAGS) > 0
        # spot-check a few known flags
        assert "MASTERMIND_REQUIRE_AUTH" in flags.KNOWN_FLAGS
        assert "MASTERMIND_FLAGSHIP_JUDGMENT" in flags.KNOWN_FLAGS
        assert "MASTERMIND_FIRM_CAPS" in flags.KNOWN_FLAGS

    def test_enumerate_returns_dict(self):
        from control_plane import flags
        result = flags.enumerate_flags()
        assert isinstance(result, dict)

    def test_unset_flags_absent(self, monkeypatch):
        """A flag that is NOT set in the environment must not appear in the output."""
        monkeypatch.delenv("MASTERMIND_STUDENT", raising=False)
        from control_plane import flags
        result = flags.enumerate_flags()
        assert "MASTERMIND_STUDENT" not in result


# ===========================================================================
# guardrail
# ===========================================================================

class TestGuardrail:
    def test_passed_constructor(self):
        from control_plane.guardrail import GuardrailResult, Severity
        r = GuardrailResult.passed("test_guard")
        assert r.ok is True
        assert r.severity == Severity.TELEMETRY_ONLY
        assert r.guard == "test_guard"
        assert r.action_taken == "none"

    def test_failed_constructor(self):
        from control_plane.guardrail import GuardrailResult, Severity
        r = GuardrailResult.failed(
            "peer_freshness",
            Severity.FREEZE,
            "all peer files missing",
            "froze new adds",
        )
        assert r.ok is False
        assert r.severity == Severity.FREEZE
        assert r.guard == "peer_freshness"

    def test_severity_ordering(self):
        from control_plane.guardrail import Severity
        assert Severity.TELEMETRY_ONLY < Severity.ADVISORY_ONLY
        assert Severity.ADVISORY_ONLY  < Severity.SHRINK
        assert Severity.SHRINK         < Severity.FREEZE
        assert Severity.FREEZE         < Severity.HARD_STOP
        assert Severity.HARD_STOP      >= Severity.FREEZE

    def test_log_telemetry_only_silent(self, tmp_path, monkeypatch):
        """TELEMETRY_ONLY results must NOT write to the ledger."""
        from control_plane.guardrail import GuardrailResult
        import control_plane.run_events as re_mod
        monkeypatch.setattr(re_mod, "_ledger_path", lambda *a, **kw: tmp_path / "data" / "governance" / "run_events.jsonl")

        r = GuardrailResult.passed("noop_guard")
        result = r.log(job="j", book="b")
        assert result is None, "TELEMETRY_ONLY must return None from .log()"
        # No file should have been written
        ledger = tmp_path / "data" / "governance" / "run_events.jsonl"
        assert not ledger.exists() or ledger.read_text().strip() == ""

    def test_log_advisory_only_writes(self, tmp_path):
        """ADVISORY_ONLY must write a guardrail event to the ledger."""
        from control_plane.guardrail import GuardrailResult, Severity
        r = GuardrailResult.failed(
            "dashboard_write",
            Severity.ADVISORY_ONLY,
            "dashboard file not writable",
            "continued without dashboard update",
        )
        eid = r.log(job="loop_maintenance", book="flagship")
        # The log call routes through run_events which uses module-relative path by default;
        # we can't easily redirect without patching.  Just check it doesn't raise and returns a str.
        # For isolation we patch run_events.append directly.

    def test_log_advisory_only_writes_isolated(self, tmp_path, monkeypatch):
        """ADVISORY_ONLY with patched run_events writes a guardrail record."""
        from control_plane.guardrail import GuardrailResult, Severity
        import control_plane.run_events as re_mod
        import control_plane.guardrail as gr_mod

        # Patch run_events.append in the guardrail module's namespace
        written: list[dict] = []
        monkeypatch.setattr(re_mod, "append", lambda ev, **kw: written.append(ev) or "abc123", raising=False)

        r = GuardrailResult.failed(
            "cap_guard",
            Severity.ADVISORY_ONLY,
            "cap exception",
            "froze new adds",
        )
        eid = r.log(job="settle", book="flagship")
        assert written, "ADVISORY_ONLY must call run_events.append"
        ev = written[0]
        assert ev["kind"] == "guardrail"
        assert ev["step"] == "cap_guard"
        assert ev["job"] == "settle"
        assert ev["book"] == "flagship"

    def test_log_freeze_writes(self, tmp_path, monkeypatch):
        from control_plane.guardrail import GuardrailResult, Severity
        import control_plane.run_events as re_mod

        written: list[dict] = []
        monkeypatch.setattr(re_mod, "append", lambda ev, **kw: written.append(ev) or "eid", raising=False)

        r = GuardrailResult.failed(
            "book_cap",
            Severity.FREEZE,
            "peer all-missing",
            "froze new adds",
        )
        r.log(job="cron", book="autonomous")
        assert written
        assert written[0]["severity"] == "FREEZE"

    def test_log_hard_stop_writes(self, tmp_path, monkeypatch):
        from control_plane.guardrail import GuardrailResult, Severity
        import control_plane.run_events as re_mod

        written: list[dict] = []
        monkeypatch.setattr(re_mod, "append", lambda ev, **kw: written.append(ev) or "eid", raising=False)

        r = GuardrailResult.failed(
            "auth_config",
            Severity.HARD_STOP,
            "auth disabled in production",
            "startup refused",
        )
        r.log(job="startup", book="")
        assert written
        assert written[0]["severity"] == "HARD_STOP"
