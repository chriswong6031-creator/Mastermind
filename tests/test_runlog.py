"""tests/test_runlog.py — offline round-trip tests for brain/runlog.

No network, no LLM, no db. Uses a fresh temporary directory for the runs store
so tests don't pollute or depend on data/brain/runs/.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest


# ── helpers ──────────────────────────────────────────────────────────────────

def _patch_runs_dir(tmp_path: Path, monkeypatch):
    """Redirect the runlog module's _RUNS_DIR and _INDEX to tmp_path."""
    import brain.runlog as rl
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(rl, "_RUNS_DIR", runs_dir)
    monkeypatch.setattr(rl, "_INDEX", runs_dir / "index.jsonl")
    return rl


# ── tests ────────────────────────────────────────────────────────────────────

def test_start_run_returns_string(tmp_path, monkeypatch):
    rl = _patch_runs_dir(tmp_path, monkeypatch)
    run_id = rl.start_run("book", title="test run")
    assert isinstance(run_id, str) and len(run_id) > 8


def test_log_step_appends(tmp_path, monkeypatch):
    rl = _patch_runs_dir(tmp_path, monkeypatch)
    run_id = rl.start_run("book")
    rl.log_step(run_id, "book_step", "regime read",
                "quad=risk-on top_sector=SMH", quad="risk-on")
    rl.log_step(run_id, "trade", "sized AVGO leadership",
                "ticker=AVGO weight=0.125", ticker="AVGO", weight=0.125)

    path = rl._run_path(run_id)
    assert path.exists()
    lines = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    # first line is meta, then our 2 steps
    steps = [l for l in lines if "_meta" not in l]
    assert len(steps) == 2
    assert steps[0]["type"] == "book_step"
    assert steps[1]["type"] == "trade"
    assert steps[1]["ticker"] == "AVGO"


def test_end_run_updates_index(tmp_path, monkeypatch):
    rl = _patch_runs_dir(tmp_path, monkeypatch)
    run_id = rl.start_run("daily", title="daily test")
    rl.log_step(run_id, "reasoning", "prompt", "hello")
    rl.end_run(run_id, summary="test summary", cost_usd=0.002)

    idx = rl._INDEX
    assert idx.exists()
    rows = [json.loads(l) for l in idx.read_text().splitlines() if l.strip()]
    assert any(r["run_id"] == run_id for r in rows)
    entry = next(r for r in rows if r["run_id"] == run_id)
    assert entry["summary"] == "test summary"
    assert entry["cost_usd"] == pytest.approx(0.002)
    assert entry["n_steps"] == 1


def test_list_runs_newest_first(tmp_path, monkeypatch):
    rl = _patch_runs_dir(tmp_path, monkeypatch)
    ids = []
    for i in range(3):
        rid = rl.start_run("book", title=f"run {i}")
        rl.log_step(rid, "book_step", f"step {i}", "detail")
        rl.end_run(rid, summary=f"summary {i}")
        ids.append(rid)
        # small sleep to ensure distinct timestamps
        time.sleep(0.02)

    listed = rl.list_runs()
    assert len(listed) == 3
    # newest first
    assert listed[0]["run_id"] == ids[-1]
    assert listed[-1]["run_id"] == ids[0]


def test_read_run_by_id(tmp_path, monkeypatch):
    rl = _patch_runs_dir(tmp_path, monkeypatch)
    run_id = rl.start_run("research", title="research run")
    rl.log_step(run_id, "tool_call", "call WebSearch",
                '{"query": "NVDA earnings"}', tool="WebSearch",
                args={"query": "NVDA earnings"})
    rl.log_step(run_id, "tool_result", "tool result",
                "NVDA beat by $0.10", result="NVDA beat by $0.10")
    rl.log_step(run_id, "reasoning", "assistant text",
                "Based on the search results...")
    rl.end_run(run_id, summary="research done", cost_usd=0.05)

    result = rl.read_run(run_id)
    assert result["run_id"] == run_id
    assert result["kind"] == "research"
    assert len(result["steps"]) == 3
    assert result["steps"][0]["type"] == "tool_call"
    assert result["steps"][0]["tool"] == "WebSearch"
    assert result["steps"][1]["type"] == "tool_result"
    assert result["steps"][2]["type"] == "reasoning"


def test_read_run_none_returns_latest(tmp_path, monkeypatch):
    rl = _patch_runs_dir(tmp_path, monkeypatch)
    for i in range(2):
        rid = rl.start_run("book", title=f"run {i}")
        rl.log_step(rid, "book_step", "step", "x")
        rl.end_run(rid, summary=f"s{i}")
        time.sleep(0.02)

    latest = rl.list_runs()[0]["run_id"]
    result = rl.read_run(None)
    assert result["run_id"] == latest


def test_read_run_missing_id_returns_error(tmp_path, monkeypatch):
    rl = _patch_runs_dir(tmp_path, monkeypatch)
    result = rl.read_run("nonexistent-run-id")
    assert result["steps"] == []
    assert "error" in result


def test_list_runs_empty_dir(tmp_path, monkeypatch):
    rl = _patch_runs_dir(tmp_path, monkeypatch)
    assert rl.list_runs() == []


def test_log_step_never_raises_on_bad_run_id(tmp_path, monkeypatch):
    rl = _patch_runs_dir(tmp_path, monkeypatch)
    # should silently swallow the error (run file doesn't exist but we called open(..., 'a'))
    rl.log_step("totally-bad-id", "book_step", "x", "y")  # must not raise


def test_end_run_twice_deduplicates(tmp_path, monkeypatch):
    rl = _patch_runs_dir(tmp_path, monkeypatch)
    run_id = rl.start_run("book")
    rl.log_step(run_id, "book_step", "x", "y")
    rl.end_run(run_id, summary="first")
    rl.end_run(run_id, summary="second")  # should deduplicate

    rows = [json.loads(l) for l in rl._INDEX.read_text().splitlines() if l.strip()]
    matching = [r for r in rows if r["run_id"] == run_id]
    assert len(matching) == 1
    assert matching[0]["summary"] == "second"


def test_import_app_main():
    """Confirm the whole FastAPI app can be imported (routes registered etc.)."""
    from app.main import app  # noqa: F401
    assert app is not None
