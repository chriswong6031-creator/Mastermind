"""LLM factor-hypothesis translator + frozen-judge pre-screen (loop.llm_candidates, #8 proposer side).

Pins: only implemented kinds + sane bounded params translate to a FactorCandidate; bad hypotheses are
dropped; the seed library is all valid; and validate() degrades safely when the single-name panel can't
load (no raise). The judge itself is frozen/untouched — these tests only exercise the proposer."""
from __future__ import annotations

import pandas as pd

from loop import llm_candidates as L


def _mem():
    return pd.DataFrame({"ticker": ["AAA", "BBB"]})   # a non-None PIT table is all the ctor requires


def test_valid_spec_accepts_good_rejects_bad():
    assert L._valid_spec({"name": "m", "kind": "momentum", "lookback": 252, "skip": 21})
    assert not L._valid_spec({"name": "x", "kind": "skew", "lookback": 252})        # kind not implemented
    assert not L._valid_spec({"name": "x", "kind": "momentum", "lookback": 9999})   # lookback out of bounds
    assert not L._valid_spec({"name": "x", "kind": "momentum", "lookback": 60, "frac": 0.9})  # frac too big
    assert not L._valid_spec({"name": "x", "kind": "momentum", "lookback": 60, "side": "neutral"})
    assert not L._valid_spec({"name": "", "kind": "lowvol", "lookback": 0, "vol_win": 60})     # no name
    assert not L._valid_spec("notadict")


def test_materialize_builds_candidate_or_none():
    c = L.materialize_hypothesis({"name": "mom", "kind": "momentum", "lookback": 252, "skip": 21}, _mem())
    assert c is not None and c.spec["kind"] == "momentum" and c.name == "mom"
    assert len(c.spec_hash) == 64                                  # deterministic sha256 identity
    assert L.materialize_hypothesis({"name": "bad", "kind": "skew", "lookback": 10}, _mem()) is None


def test_generate_drops_invalid():
    specs = [{"name": "ok", "kind": "lowvol", "lookback": 0, "vol_win": 60},
             {"name": "bad", "kind": "telepathy", "lookback": 5}]
    cands = L.generate(specs, _mem())
    assert [c.name for c in cands] == ["ok"]


def test_seed_hypotheses_are_all_valid():
    assert L.SEED_HYPOTHESES and all(L._valid_spec(s) for s in L.SEED_HYPOTHESES)


def test_validate_degrades_when_panel_unavailable(monkeypatch):
    # no sp1500 panel in this env → validate must NOT raise; it returns a degraded status
    import loop.single_name_panel as P
    monkeypatch.setattr(P, "load_panel", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no panel")))
    out = L.validate(L.SEED_HYPOTHESES)
    assert out["status"] in ("unavailable", "no_valid_candidates", "scored")
    assert out["n_specs"] == len(L.SEED_HYPOTHESES) and "candidates" in out
