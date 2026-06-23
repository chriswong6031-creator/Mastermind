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
def _isolate_positions_ledger(tmp_path, monkeypatch):
    """Isolate the positions ledger + fills blotter so the real book build tests (phase2 /
    tracking) don't write phantom ADD/TRIM history into the LIVE data/portfolio/*.json — which
    otherwise pollutes the dashboard activity feed on every `pytest` run."""
    try:
        import portfolio.position_log as pl
        monkeypatch.setattr(pl, "_LEDGER_PATH", tmp_path / "_positions_ledger.json", raising=False)
    except Exception:
        pass
    try:
        import portfolio.trade_history as th
        monkeypatch.setattr(th, "_FILLS_PATH", tmp_path / "_fills.jsonl", raising=False)
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _isolate_research_papers(tmp_path, monkeypatch):
    """Force the deterministic research-paper path (no armed Claude session during pytest) and
    redirect the papers/feed-note writes into a tmp dir, so the book build's research gate never
    hits the network nor pollutes the live research feed."""
    monkeypatch.setenv("MASTERMIND_RESEARCH_LLM", "0")
    # Never let the FastAPI startup hook fire a real (armed) Brain run during pytest, even when a
    # test mounts the app via TestClient on a machine with the Claude CLI present (which would
    # otherwise spawn a live Opus session + write a real book). Covers every Brain book.
    monkeypatch.setenv("AUTONOMOUS_FIRST_RUN", "0")
    monkeypatch.setenv("CHINA_FIRST_RUN", "0")
    monkeypatch.setenv("ETF_FIRST_RUN", "0")
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


@pytest.fixture(autouse=True)
def _clear_fx_cache():
    """The China book's FX rate memo (portfolio.fx) is a process-global; clear it around every
    test so an FX-dependent test is order-independent (a real rate read in one test can't leak a
    non-fallback rate into another)."""
    try:
        from portfolio import fx
        fx.clear_cache()
        yield
        fx.clear_cache()
    except Exception:
        yield
