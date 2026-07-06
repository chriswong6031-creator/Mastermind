"""Guards for the MACRO STRATEGIST seat (brain/strategist).

Offline only: no vendor engine, no network. We monkeypatch the dashboard `_load` helper to feed
synthetic flow/etf_pulse/emergence dicts, and `client.call_model` to return a canned verdict —
no real LLM is ever called. We prove:
  * enabled() is False when the flag is OFF (byte-identical guard for the gate),
  * the verdict dict has the right shape, stage/stance enums are normalised, leadership clamped,
  * a non-JSON LLM reply degrades to None (never raises).
"""
from __future__ import annotations

import json

import pytest

import bot  # noqa: F401
from brain import client, strategist as S


def _fake_load(rel):
    if rel == "site/basketdata/flow.json":
        return {"baskets": [
            {"id": "ai_infra", "name": "AI Infrastructure", "stage": "confirmed",
             "breadth": 0.7, "leadership": 0.8, "perf_20d_rel": 0.04},
            {"id": "onshoring", "name": "Onshoring", "stage": "emerging",
             "breadth": 0.5, "leadership": 0.6, "ret_20d_rel": 0.02},
            {"id": "junk", "name": "Forming Junk", "stage": "forming", "breadth": 0.1},
        ]}
    if rel == "site/basketdata/etf_pulse.json":
        return {"leaders": ["SMH", "XLK"], "laggards": ["XLU"],
                "sector": {"tech": {"mom_20d": 0.05, "pctile_252d": 90, "above_200d": True}},
                "risk": {"label_en": "risk-on"},
                "style": [{"pair": "growth/value", "lead_en": "growth", "tilt": 0.3}]}
    if rel == "site/basketdata/narrative_emergence.json":
        return {"clusters": [
            {"name_en": "Power Grid", "score": 72, "score_label": {"en": "early"},
             "momentum": "up", "basket_overlap": 0.2, "caveats_en": "thin breadth"}]}
    if rel == "site/allocationdata/macro_narrative.json":
        return {"dominant_themes": [{"theme": "disinflation", "count": 5}]}
    if rel == "site/basketdata/vol_sentiment.json":
        return {"vix": 14.0, "vix_pctile": 20, "sentiment_en": "complacent"}
    return None


def test_enabled_false_when_flag_off(monkeypatch):
    monkeypatch.delenv("MASTERMIND_FLAGSHIP_JUDGMENT", raising=False)
    assert S.enabled() is False
    monkeypatch.setenv("MASTERMIND_FLAGSHIP_JUDGMENT", "0")
    assert S.enabled() is False


def test_input_keeps_only_confirmed_emerging(monkeypatch):
    monkeypatch.setattr(S, "_load", _fake_load)
    payload = S._strategist_input({"quad": 1, "quad_name": "Goldilocks"}, "2026-06-22")
    stages = {b["stage"] for b in payload["basket_flow"]}
    assert stages == {"confirmed", "emerging"}          # 'forming' junk filtered out
    assert payload["sector_pulse"]["leaders"] == ["SMH", "XLK"]
    assert payload["sector_pulse"]["risk_label"] == "risk-on"
    assert payload["emergence"][0]["score_label"] == "early"   # dict score_label flattened


def test_assess_shape_and_normalisation(monkeypatch):
    monkeypatch.setattr(S, "_load", _fake_load)
    monkeypatch.setattr(client, "available", lambda: True)
    canned = json.dumps({
        "confirmed_themes": [
            {"theme": "AI Infrastructure", "stage": "CONFIRMED", "leadership": 1.7,
             "names": ["nvda", "smh"], "why": "breadth + leading sector"},
            {"theme": "Mystery", "stage": "forming", "leadership": -3, "names": [], "why": "x"}],
        "backdrop_stance": "RISK_ON", "supportive": True,
        "watch_emerging": ["Power Grid"], "crowding_flags": ["complacent VIX"],
        "rationale": "Confirmed AI leadership; backdrop supportive."})
    monkeypatch.setattr(client, "call_model", lambda *a, **k: (canned, {}))
    v = S.strategist_assess("2026-06-22", {"quad": 1})
    assert v is not None
    assert v["agent"] == "strategist"
    assert v["backdrop_stance"] == "risk_on"            # enum normalised
    th0, th1 = v["confirmed_themes"]
    assert th0["stage"] == "confirmed" and th0["leadership"] == 1.0   # clamped to [0,1]
    assert th0["names"] == ["NVDA", "SMH"]              # upper-cased
    assert th1["stage"] == "emerging"                  # unknown 'forming' -> emerging fallback
    assert th1["leadership"] == 0.0                     # clamped up from -3
    assert "calibration_multiplier" in v


def test_assess_none_on_bad_json(monkeypatch):
    monkeypatch.setattr(S, "_load", _fake_load)
    monkeypatch.setattr(client, "available", lambda: True)
    monkeypatch.setattr(client, "call_model", lambda *a, **k: ("not json at all", {}))
    assert S.strategist_assess("2026-06-22", {"quad": 1}) is None


def test_assess_none_when_no_llm(monkeypatch):
    monkeypatch.setattr(client, "available", lambda: False)
    assert S.strategist_assess("2026-06-22", {"quad": 1}) is None


# ---------------------------------------------------------------------------
# W-NW.1 strategist — neural_web_context key must be compact (market_plane, not context())
# ---------------------------------------------------------------------------

def test_neural_web_context_key_is_market_plane_not_raw_artifact(monkeypatch):
    """With MASTERMIND_NW_CONTEXT=1, neural_web_context in _strategist_input must come from
    market_plane() — a compact ~7-field dict — NOT the full raw context() artifact.

    Distinguishing invariant: market_plane() never contains 'candidate_context'; the full
    context() artifact always does (it is the main body of the artifact).  We inject a fake
    market_plane() that returns a known compact sentinel dict and a fake context() that returns
    a dict with 'candidate_context' (the raw-artifact shape), then assert only the market_plane
    shape reaches the payload.
    """
    import brain.neural_web_context as NWC

    # Sentinel that market_plane() would return — compact, no candidate_context
    _MARKET_PLANE_SENTINEL = {
        "verdict": {"direction": "risk_on"},
        "regime": {"quad": 1},
        "vol": {},
        "breadth": {},
        "contradiction_count": 0,
        "contradiction_summary": {},
        "asof": "2026-07-05",
        "stale": False,
    }

    # What the raw context() artifact looks like — has candidate_context
    _RAW_CONTEXT_SENTINEL = {
        "schema": "v1",
        "is_context_only": True,
        "as_of": "2026-07-05",
        "candidate_context": {"AAPL": {"lobe_signals": {}}},
        "lobes": {"market": {}, "contradictions": {}},
    }

    monkeypatch.setenv("MASTERMIND_NW_CONTEXT", "1")
    monkeypatch.setattr(NWC, "market_plane", lambda: _MARKET_PLANE_SENTINEL)
    monkeypatch.setattr(NWC, "context", lambda: _RAW_CONTEXT_SENTINEL)
    monkeypatch.setattr(S, "_load", _fake_load)

    payload = S._strategist_input({"quad": 1, "quad_name": "Goldilocks"}, "2026-07-05")
    nw = payload["neural_web_context"]

    # Must be the compact market_plane shape, not the raw artifact
    assert "candidate_context" not in nw, (
        "neural_web_context key must NOT contain candidate_context (raw artifact leak)"
    )
    assert nw.get("asof") == "2026-07-05", "market_plane sentinel asof should be present"
    assert "stale" in nw, "market_plane shape must have stale key"


def test_neural_web_context_key_empty_when_flag_off(monkeypatch):
    """With MASTERMIND_NW_CONTEXT unset (OFF), neural_web_context key must be {}."""
    monkeypatch.delenv("MASTERMIND_NW_CONTEXT", raising=False)
    monkeypatch.setattr(S, "_load", _fake_load)

    payload = S._strategist_input({"quad": 1, "quad_name": "Goldilocks"}, "2026-07-05")
    assert payload["neural_web_context"] == {}, (
        "neural_web_context must be {} when MASTERMIND_NW_CONTEXT is unset"
    )
