"""Contract tests for the typed, point-in-time decision_context.v2 perception seam."""
from __future__ import annotations

import json
from pathlib import Path

from brain import decision_context as DC


def _regime() -> dict:
    return {
        "date": "2026-07-29",
        "quad": "Q1",
        "quad_name": "Goldilocks",
        "confidence": 0.183,
        "growth_score": -0.133,
        "growth_confidence": 0.067,
        "inflation_score": 0.4,
        "inflation_confidence": 0.3,
        "liquidity_overlay": "expanding",
        "liquidity_quality": {
            "asof": "2026-07-29",
            "label": "stress-expansion",
            "quantity_roc_bn": 26.8,
        },
        "cycle_tag": "mid",
        "transition_state": "TRANSITIONING",
        "flip_margin": 0.0,
        "contradicting": ["growth_us2y_direction", "growth_breadth_direction"],
        "quad_vector": {
            "p": {"Q1": 0.0004, "Q2": 0.5084, "Q3": 0.4897, "Q4": 0.0015},
            "hard_label_agrees": False,
            "confidence": 0.318,
            "source": "causal filtered HMM",
            "transition_momentum": {
                "gaining": "Q3",
                "gaining_rate": 0.0734,
                "losing": "Q2",
                "losing_rate": -0.0737,
                "window_sessions": 5,
            },
        },
        "risk_state": {"state": "caution", "score": 41.9, "gross_factor": 0.9},
        "market_drivers": {
            "primary": "ai_semis",
            "primary_label": "AI / semis",
            "direction": "AI/semis unwind — tech-led de-rating",
            "confidence": "high",
            "strength": 3.06,
            "evidence": ["semis RS -3.6σ"],
        },
    }


def _plane(
    direction: str | None,
    *,
    status: str = "advisory",
    stale: bool = False,
    present: bool = True,
    magnitude: float | None = None,
    signal_active: bool | None = None,
) -> dict:
    raw = {
        "present": present,
        "artifact_present": present,
        "signal_active": signal_active if signal_active is not None else direction != "neutral",
    }
    return {
        "reading": "fixture",
        "direction": direction,
        "magnitude": magnitude,
        "freshness": {
            "asof": "2026-07-29" if present else None,
            "age_sessions": 0 if present else None,
            "stale": stale,
        },
        "confidence": 0.7,
        "status": status,
        "source_contract": "fixture.contract",
        "raw": raw,
    }


def _view() -> dict:
    return {
        "schema_version": "market_view.v1",
        "asof": "2026-07-29",
        "planes": {
            "risk_radar": _plane("risk_off", status="validated", magnitude=0.16),
            "cycles": _plane("risk_on", status="validated", magnitude=0.0),
            "turning_point": _plane(
                "neutral", signal_active=False, magnitude=None
            ),
            "rrg": _plane(None, stale=True, present=False),
        },
        "net_posture_tilt": {
            "tilt": 0.0,
            "direction": "neutral",
            "n_validated": 2,
            "contributors": ["risk_radar", "cycles"],
        },
        "label_vs_planes": {
            "label_direction": "risk_on",
            "plane_consensus_direction": "neutral",
            "conflict": False,
            "relationship": "unconfirmed",
            "confirmed": False,
            "dissenting_planes": ["risk_radar"],
        },
        "assembly": {
            "decision_total": 3,
            "decision_coverage": 0.6667,
            "degraded": False,
        },
    }


def test_contract_preserves_regime_vector_trajectory_and_quality() -> None:
    out = DC.assemble(
        _regime(), _view(), neural_web={}, built_at="2026-07-30T00:00:00+00:00"
    )
    assert out["schema_version"] == "decision_context.v2"
    assert out["governor"]["relationship"] == "unconfirmed"
    assert out["governor"]["confirmed"] is False
    assert out["regime"]["hard_label"]["name"] == "Goldilocks"
    assert out["regime"]["probabilistic_state"]["hard_label_agrees"] is False
    assert out["regime"]["probabilistic_state"]["probabilities"]["Q3"] == 0.4897
    assert out["regime"]["trajectory"]["gaining_quad"] == "Q3"
    assert out["regime"]["trajectory"]["flip_margin"] == 0.0
    assert out["regime"]["liquidity"]["quality"]["label"] == "stress-expansion"


def test_signal_units_authority_and_availability_are_explicit() -> None:
    out = DC.assemble(_regime(), _view(), neural_web={})
    rows = {row["name"]: row for row in out["signals"]}
    assert rows["risk_radar"]["layer"] == "governor"
    assert rows["risk_radar"]["allowed_effect"] == "governor_vote"
    assert rows["risk_radar"]["value"] == {
        "value": 0.16,
        "unit": "probability",
        "horizon": "21_sessions",
    }
    assert rows["turning_point"]["availability"]["artifact_present"] is True
    assert rows["turning_point"]["availability"]["signal_active"] is False
    assert rows["rrg"]["availability"]["artifact_present"] is False
    assert rows["rrg"]["layer"] == "context"


def test_stale_validated_record_cannot_become_governor() -> None:
    view = _view()
    view["planes"]["risk_radar"] = _plane(
        "risk_off", status="advisory", stale=True, magnitude=0.16
    )
    out = DC.assemble(_regime(), view, neural_web={})
    row = next(row for row in out["signals"] if row["name"] == "risk_radar")
    assert row["layer"] == "context"
    assert row["allowed_effect"] == "shrink_only"
    assert row["freshness"]["stale"] is True


def test_source_after_decision_asof_is_quarantined() -> None:
    view = _view()
    view["planes"]["risk_radar"]["freshness"]["asof"] = "2026-07-30"
    out = DC.assemble(_regime(), view, neural_web={})
    row = next(row for row in out["signals"] if row["name"] == "risk_radar")
    assert row["freshness"]["future_dated"] is True
    assert row["freshness"]["stale"] is True
    assert row["layer"] == "context"


def test_neural_web_lobes_are_freshness_gated_and_cortex_excluded() -> None:
    neural = {
        "as_of": "2026-07-29",
        "freshness": {
            "theme_rotation": {"as_of": "2026-07-29", "stale": False},
            "market_structure": {"as_of": "2026-07-10", "stale": True},
            "cortex": {"as_of": "2026-07-29", "stale": False},
        },
        "lobes": {
            "theme_rotation": {
                "as_of": "2026-07-29",
                "leadership_state": "rotating",
                "migration_absorbing": ["Health Care"],
            },
            "market_structure": {
                "asof": "2026-07-10",
                "gamma_regime": "short",
            },
            "cortex": {"asof": "2026-07-29", "memo": "NEVER_INCLUDE"},
        },
    }
    out = DC.assemble(_regime(), _view(), neural_web=neural)
    contexts = out["neural_web"]["contexts"]
    assert contexts["theme_rotation"]["leadership_state"] == "rotating"
    assert "market_structure" not in contexts
    assert "cortex" not in contexts
    assert "NEVER_INCLUDE" not in json.dumps(out)


def test_neural_lobe_after_decision_asof_is_excluded() -> None:
    neural = {
        "as_of": "2026-07-29",
        "freshness": {
            "theme_rotation": {"as_of": "2026-07-30", "stale": False},
        },
        "lobes": {
            "theme_rotation": {
                "as_of": "2026-07-30",
                "leadership_state": "rotating",
            },
        },
    }
    out = DC.assemble(_regime(), _view(), neural_web=neural)
    assert out["neural_web"]["lobe_health"]["theme_rotation"]["future_dated"] is True
    assert "theme_rotation" not in out["neural_web"]["contexts"]


def test_future_dated_cycle_is_quarantined() -> None:
    regime = _regime()
    regime["business_cycle"] = {
        "asof": "2026-07-31",
        "available": True,
        "phase": {"label": "recovery"},
    }
    out = DC.assemble(regime, _view(), neural_web={})
    cycle = out["regime"]["cycle"]["business_cycle"]
    assert cycle["admitted"] is False
    assert cycle["future_dated"] is True
    assert "phase" not in cycle
    assert out["data_quality"]["temporal_anomalies"] == [
        "business_cycle_asof_after_regime_market_asof"
    ]


def test_prompt_summary_retains_precision_without_raw_signal_matrix() -> None:
    out = DC.assemble(_regime(), _view(), neural_web={})
    summary = DC.prompt_summary(out)
    assert summary["probabilistic_state"]["hard_label_agrees"] is False
    assert summary["trajectory"]["gaining_quad"] == "Q3"
    assert "signals" not in summary


def test_pm_prompt_renders_probability_transition_and_liquidity_quality() -> None:
    from brain import pm_conviction as PM

    ctx = DC.assemble(_regime(), _view(), neural_web={})
    payload = {
        "asof": "2026-07-29",
        "regime": {
            "quad": "Q1",
            "quad_name": "Goldilocks",
            "decision_context": DC.prompt_summary(ctx),
        },
        "engine_candidates": [],
        "leadership_legs": [],
        "defensive_candidates": [],
        "engine_rejected": [],
        "forge_summaries": [],
        "engine_proposed_weights_ADVISORY": {},
    }
    prompt = PM._build_prompt(payload)
    assert "Canonical decision context v2" in prompt
    assert "hard_label_agrees=False" in prompt
    assert "gaining=Q3@0.0734" in prompt
    assert "quality=stress-expansion" in prompt


def test_build_writes_latest_and_dated_atomically(monkeypatch, tmp_path: Path) -> None:
    artifact_dir = tmp_path / "decision_context"
    monkeypatch.setattr(DC, "_ARTIFACT_DIR", artifact_dir)
    monkeypatch.setattr(DC, "_LATEST_PATH", artifact_dir / "latest.json")
    out = DC.build(
        regime=_regime(),
        market_view=_view(),
        neural_web={},
        write=True,
    )
    assert out["schema_version"] == "decision_context.v2"
    assert (artifact_dir / "latest.json").exists()
    assert (artifact_dir / "2026-07-29.json").exists()
    assert not list(artifact_dir.glob("*.tmp"))


def test_phase2_market_view_inputs_wire_all_existing_planes(monkeypatch) -> None:
    from bot import phase2
    from brain import regime_nowcast
    from portfolio import distribution_tells

    monkeypatch.setattr(
        distribution_tells,
        "score",
        lambda held, prices_fn=None: {
            "asof": "2026-07-29",
            "hot": False,
            "holdings": held,
        },
    )
    monkeypatch.setattr(
        regime_nowcast,
        "nowcast",
        lambda **kwargs: {
            "stance": "doubt",
            "applies": True,
            "legs": {"asof": kwargs["asof"], "n_doubt": 2},
        },
    )
    regime = _regime()
    out = phase2._market_view_inputs(
        regime,
        "2026-07-29",
        holdings=[{"ticker": "AAPL", "current_weight": 0.1}],
    )
    assert out["distribution_tells"]["holdings"][0]["ticker"] == "AAPL"
    assert out["liquidity_quality"]["label"] == "stress-expansion"
    assert out["regime_nowcast"]["stance"] == "doubt"
