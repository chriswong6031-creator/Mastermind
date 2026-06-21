"""Self-calibrating gate: outcome_ledger.lens_weights turns realized hit-rates into per-lens vote
weights, and lenses.synthesize uses them — but is IDENTICAL to equal-voting until a track record
accrues. Offline."""
from __future__ import annotations

import json
from unittest import mock

import bot  # noqa: F401


# ---------------------------------------------------------------------------
# lens_weights — realized hit-rate -> reliability multiplier (shrunk by sample size)
# ---------------------------------------------------------------------------

def test_lens_weights_rewards_predictive_lens(tmp_path, monkeypatch):
    from brain import outcome_ledger as ol
    monkeypatch.setattr(ol, "_PATH", tmp_path / "ol.jsonl", raising=False)
    recs = []
    for i in range(30):                                   # trend bull hit 21/30 = 0.70
        recs.append({"outcome": 1 if i < 21 else 0, "prob_correct": 0.6, "lens_dirs": {"trend": "bull"}})
    for i in range(20):                                   # valuation bull hit 10/20 = 0.50 (coin flip)
        recs.append({"outcome": 1 if i < 10 else 0, "prob_correct": 0.6, "lens_dirs": {"valuation": "bull"}})
    for i in range(8):                                    # narrative bull, n=8 < min_n -> omitted
        recs.append({"outcome": 1 if i < 6 else 0, "prob_correct": 0.6, "lens_dirs": {"narrative": "bull"}})
    (tmp_path / "ol.jsonl").write_text("\n".join(json.dumps(r) for r in recs) + "\n")

    w = ol.lens_weights(min_n=20)
    assert w["trend"] > 1.0, "a lens beating coin-flip is up-weighted"
    assert abs(w["trend"] - 1.24) < 1e-6, "0.70 hit, n=30, shrunk to 0.62 -> 1.0 + 2*(0.62-0.5) = 1.24"
    assert abs(w["valuation"] - 1.0) < 1e-9, "a 50% lens is neutral"
    assert "narrative" not in w, "below min_n -> omitted (keeps the 1.0 default = equal vote)"


def test_lens_weights_empty_until_track_record():
    from brain import outcome_ledger as ol
    with mock.patch.object(ol, "_read", return_value=[]):
        assert ol.lens_weights() == {}


def test_lens_weights_damps_a_losing_lens(tmp_path, monkeypatch):
    from brain import outcome_ledger as ol
    monkeypatch.setattr(ol, "_PATH", tmp_path / "ol.jsonl", raising=False)
    recs = [{"outcome": 1 if i < 6 else 0, "prob_correct": 0.6, "lens_dirs": {"extension": "bull"}}
            for i in range(40)]                            # 6/40 = 0.15 hit
    (tmp_path / "ol.jsonl").write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    w = ol.lens_weights(min_n=20)
    assert w["extension"] < 1.0, "a lens that consistently misses is damped below 1.0"
    assert w["extension"] >= 0.5, "but floored at 0.5"


# ---------------------------------------------------------------------------
# synthesize — reliability-weighted confluence, equal until data accrues
# ---------------------------------------------------------------------------

def test_synthesize_equal_vote_when_no_edge_data(monkeypatch):
    from portfolio import lenses
    monkeypatch.setattr(lenses, "_lens_reliability", lambda: {})
    rows = [{"lens": "sector_rs", "direction": "bull", "value": {}},
            {"lens": "trend", "direction": "bull", "value": {}},
            {"lens": "flows_13f", "direction": "bear", "value": {}}]
    s = lenses.synthesize({"rows": rows})
    assert s["confluence"] == 0.333, "with no edge data, confluence == (bull-bear)/n exactly as before"
    assert s["bull"] == 2 and s["bear"] == 1


def test_synthesize_weights_a_reliable_lens(monkeypatch):
    from portfolio import lenses
    monkeypatch.setattr(lenses, "_lens_reliability", lambda: {"sector_rs": 1.5})
    rows = [{"lens": "sector_rs", "direction": "bull", "value": {}},   # weight 1.5
            {"lens": "trend", "direction": "bear", "value": {}}]       # weight 1.0 (default)
    s = lenses.synthesize({"rows": rows})
    # equal vote would be 0.0 (1 bull, 1 bear); weighted: (1*1.5 - 1*1.0)/(1.5+1.0) = 0.2
    assert s["confluence"] == 0.2, "a proven lens tips an otherwise-even read"
    assert s["bull"] == 1 and s["bear"] == 1, "raw counts unchanged (for display); only confluence is weighted"
