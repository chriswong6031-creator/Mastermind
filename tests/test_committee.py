"""Guards for the blind FORGE→SENTINEL→NEXUS committee (brain/committee).

The safety-critical property is that the committee is SUBTRACT-ONLY: it can confirm, trim, or
drop a buy FORGE already confirmed, but can NEVER escalate size or rescue a name FORGE blocked.
NEXUS is a pure function, so that invariant is tested exhaustively. We also prove SENTINEL is
blind (its input never contains FORGE's verdict), that no-LLM degrades cleanly, and that
artifacts are written.
"""
from __future__ import annotations

import json

import pytest

import bot  # noqa: F401
from brain import committee as C


def _bd(confirmed=True, combined=78):
    return {"confirmed": confirmed, "combined": combined, "engine_score": 80,
            "research_score": 76, "viability": "compelling", "size_mult": 1.0}


def test_nexus_never_rescues_a_blocked_name():
    # FORGE did not confirm → committee cannot turn it into a buy, even on SENTINEL SUPPORT
    for st in ("SUPPORT", "CONDITIONAL", "OPPOSE"):
        d = C.nexus(_bd(confirmed=False), {"stance": st, "confidence": 0.9})
        assert d["action"] == "drop" and d["scale"] == 0.0


def test_nexus_is_subtract_only_for_all_stances():
    for st in ("SUPPORT", "CONDITIONAL", "OPPOSE"):
        for conf in (0.2, 0.5, 0.75, 1.0):
            d = C.nexus(_bd(confirmed=True), {"stance": st, "confidence": conf})
            assert d["action"] in ("confirm", "trim", "drop")
            assert 0.0 <= d["scale"] <= 1.0          # never escalates above engine size


def test_nexus_stance_mapping():
    assert C.nexus(_bd(), {"stance": "SUPPORT", "confidence": 0.9})["action"] == "confirm"
    assert C.nexus(_bd(), {"stance": "SUPPORT", "confidence": 0.9})["scale"] == 1.0
    assert C.nexus(_bd(), {"stance": "CONDITIONAL", "confidence": 0.5})["action"] == "trim"
    assert C.nexus(_bd(), {"stance": "OPPOSE", "confidence": 0.4})["action"] == "trim"     # weak
    assert C.nexus(_bd(), {"stance": "OPPOSE", "confidence": 0.8})["action"] == "drop"     # strong


def test_nexus_no_adversary_passes_engine_decision():
    d = C.nexus(_bd(confirmed=True), None)
    assert d["action"] == "confirm" and d["scale"] == 1.0


def test_sentinel_input_is_blind_to_forge():
    """SENTINEL must never see FORGE's verdict/score/recommendation/paper."""
    engine_full = {"synthesis": {"size_authority": "up", "confluence": 0.3, "vetoes": [], "quad": "Q1"},
                   "rows": [{"lens": "valuation", "status": "validated", "note": "cheap"}]}
    payload = C._sentinel_input("NVDA", engine_full, {"quad": "Q1"}, {"held_conviction": ["AME"]})
    blob = json.dumps(payload).lower()
    for leaked in ("combined", "research_score", "confirmed", "recommend", "report_md",
                   "viability", "fair_value", "size_mult"):
        assert leaked not in blob, f"SENTINEL input leaked FORGE field: {leaked}"
    assert payload["ticker"] == "NVDA" and "engine_decision_matrix" in payload


def test_assess_no_llm_is_graceful(monkeypatch):
    monkeypatch.setattr(C.client, "available", lambda: False)
    monkeypatch.setattr(C, "_ARTIFACTS", C.Path("/tmp/_mm_committee_test"))
    out = C.assess("NVDA", "2026-06-20", engine_full={}, breakdown=_bd(confirmed=True), regime={})
    assert out["ran"] is False and out["action"] == "confirm" and out["scale"] == 1.0
    assert out["sentinel"] is None


def test_enabled_flag_off(monkeypatch):
    monkeypatch.setenv("MASTERMIND_COMMITTEE", "0")
    monkeypatch.setattr(C.client, "available", lambda: True)
    assert C.enabled() is False


def test_assess_with_mocked_oppose_drops(monkeypatch, tmp_path):
    from brain import calibration as _cal
    monkeypatch.setattr(_cal, "multiplier", lambda agent: 1.0)  # isolate from on-disk calibration
    monkeypatch.setattr(C, "_ARTIFACTS", tmp_path / "committee")
    monkeypatch.setattr(C.client, "available", lambda: True)
    monkeypatch.setenv("MASTERMIND_COMMITTEE", "1")
    # mock the model to return a high-conviction OPPOSE
    monkeypatch.setattr(C.client, "call_model", lambda s, u, **k: (
        json.dumps({"stance": "OPPOSE", "confidence": 0.85, "strongest_bear": "late, crowded, macro-hostile"}),
        {}))
    out = C.assess("NVDA", "2026-06-20", engine_full={"synthesis": {}, "rows": []},
                   breakdown=_bd(confirmed=True), regime={"quad": "Q1"})
    assert out["ran"] is True and out["sentinel"]["stance"] == "OPPOSE"
    assert out["action"] == "drop" and out["scale"] == 0.0
    # artifacts written
    d = tmp_path / "committee" / "2026-06-20" / "NVDA"
    assert (d / "sentinel.json").exists() and (d / "nexus.json").exists() and (d / "debate.md").exists()


def test_sentinel_assess_normalises_bad_json(monkeypatch):
    monkeypatch.setattr(C.client, "available", lambda: True)
    monkeypatch.setattr(C.client, "call_model", lambda s, u, **k: ("not json at all", {}))
    assert C.sentinel_assess("X", {}, {}, {}) is None
    # unknown stance + out-of-range confidence are clamped
    monkeypatch.setattr(C.client, "call_model", lambda s, u, **k: (
        json.dumps({"stance": "MAYBE", "confidence": 5}), {}))
    v = C.sentinel_assess("X", {}, {}, {})
    assert v["stance"] == "CONDITIONAL" and 0.0 <= v["confidence"] <= 1.0
