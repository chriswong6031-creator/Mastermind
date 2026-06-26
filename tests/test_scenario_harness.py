"""Scenario harness (brain.scenario_harness, #9) — leakage-control utilities for honest LLM stress-tests.

Pins the post-cutoff gate, the lookahead-propensity (contamination) score, the counterfactual
constructors, and that grade() discards high-LAP scenarios and is LLM-free without an injected caller.
"""
from __future__ import annotations

from brain import scenario_harness as S


def test_post_cutoff_gate_and_filter():
    assert S.is_post_cutoff("2026-06-21") and not S.is_post_cutoff("2025-12-01")
    assert not S.is_post_cutoff("garbage")
    assert S.filter_post_cutoff([{"asof": "2026-06-21"}, {"asof": "2025-06-01"}]) == [{"asof": "2026-06-21"}]


def test_lookahead_propensity_scores():
    assert S.lookahead_propensity("ZZZQ", "2026-06-21") == 0.0     # post-cutoff, obscure → clean
    assert S.lookahead_propensity("NVDA", "2026-06-21") == 0.25    # famous mega-cap, post-cutoff
    assert S.lookahead_propensity("ZZZQ", "2025-06-01") == 0.6     # pre-cutoff → outcome memorized
    assert S.lookahead_propensity("AAPL", "2020-03-15") == 1.0     # pre-cutoff + famous + crash (capped)


def test_counterfactuals_lower_contamination():
    scen = S.counterfactuals("NVDA", "2026-06-21")
    assert {s["type"] for s in scen} == set(S._SCENARIO_TYPES)
    by = {s["type"]: s for s in scen}
    assert by["anonymized"]["subject"] == "Company A"
    # perturbations that remove the famous-name / known-outcome leak carry LOWER LAP than the raw catalyst case
    assert by["anonymized"]["lap"] <= by["catalyst_removed"]["lap"]
    assert by["pricepath_swapped"]["lap"] <= by["catalyst_removed"]["lap"]


def test_grade_is_llm_free_by_default_and_discards_high_lap():
    scen = [{"type": "a", "lap": 0.9}, {"type": "b", "lap": 0.1}]
    g0 = S.grade(scen)                                              # no caller → no LLM
    assert g0["status"] == "no_caller" and g0["n_clean"] == 1 and g0["discarded_high_lap"] == 1
    g1 = S.grade(scen, caller=lambda s: "bullish")                 # injected cheap-tier caller
    assert g1["status"] == "graded" and len(g1["graded"]) == 1 and g1["graded"][0]["reaction"] == "bullish"
