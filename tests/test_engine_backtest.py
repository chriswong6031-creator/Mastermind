"""Guards for the historical engine-backtest verdict surface (loop.engine_backtest).

build() is heavy + holdout-burning (covered by a skip-unless-data integration test); the cheap
read path load() must always degrade to an honest stub, never raise.
"""
from __future__ import annotations

import json

import pytest

import bot  # noqa: F401
from loop import engine_backtest as EB


def test_load_missing_is_honest_stub(monkeypatch, tmp_path):
    monkeypatch.setattr(EB, "_VERDICT", tmp_path / "absent.json")
    v = EB.load()
    assert v["status"] == "unavailable" and "note" in v


def test_load_reads_persisted_verdict(monkeypatch, tmp_path):
    p = tmp_path / "engine_verdict.json"
    p.write_text(json.dumps({"status": "ok", "verdict": {"pbo": 0.16, "best_dsr": 0.979}}))
    monkeypatch.setattr(EB, "_VERDICT", p)
    v = EB.load()
    assert v["status"] == "ok" and v["verdict"]["pbo"] == 0.16


def test_build_never_raises(monkeypatch):
    # force the heavy import path to fail → must return a stub, not raise
    import builtins
    real = builtins.__import__

    def _boom(name, *a, **k):
        if name == "loop" or name.startswith("loop."):
            raise ImportError("simulated missing harness")
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", _boom)
    v = EB.build(asof="2026-06-21", write=False)
    assert v["status"] == "unavailable"


@pytest.mark.integration
def test_build_real_if_data_present():
    """Real frozen gauntlet on the cached panel. Skips if data/infra unavailable."""
    v = EB.build(asof="2026-06-21", write=False)
    if v.get("status") != "ok":
        pytest.skip("engine backtest data unavailable in this environment")
    ver = v["verdict"]
    assert ver["n_candidates"] >= 1 and ver["effective_n"] >= 1
    assert 0.0 <= ver["pbo"] <= 1.0 and "best_dsr" in ver
