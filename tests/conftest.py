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


@pytest.fixture(autouse=True)
def _isolate_research_papers(tmp_path, monkeypatch):
    """Force the deterministic research-paper path (no armed Claude session during pytest) and
    redirect the papers/feed-note writes into a tmp dir, so the book build's research gate never
    hits the network nor pollutes the live research feed."""
    monkeypatch.setenv("MASTERMIND_RESEARCH_LLM", "0")
    try:
        import brain.research_paper as rp
        papers = tmp_path / "_papers"
        papers.mkdir(parents=True, exist_ok=True)
        notes = tmp_path / "_papernotes"
        notes.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(rp, "_PAPERS", papers, raising=False)
        monkeypatch.setattr(rp, "_INDEX", papers / "index.jsonl", raising=False)
        monkeypatch.setattr(rp, "_NOTES", notes, raising=False)
    except Exception:
        pass
