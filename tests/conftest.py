"""Shared test fixtures.

Several tests exercise the real book build (bot.phase2.run) and armed-session
paths, which call brain.runlog.start_run(...) and would otherwise append real
run-log entries into data/brain/runs/ on every `pytest` invocation — cluttering
the live "Brain Activity" feed. Isolate the run-log to a tmp dir for all tests.
"""
import pytest


@pytest.fixture(autouse=True)
def _isolate_runlog(tmp_path, monkeypatch):
    try:
        import brain.runlog as rl
        runs_dir = tmp_path / "_runlog"
        runs_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(rl, "_RUNS_DIR", runs_dir, raising=False)
        monkeypatch.setattr(rl, "_INDEX", runs_dir / "index.jsonl", raising=False)
    except Exception:
        pass
