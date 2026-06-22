"""Guards for the empirical calibration loop (brain/calibration) — the safe self-learning layer.

Calibration re-weights each agent's stated confidence to its REALIZED reliability. The safety
properties under test:
  * de-confidence ONLY — the multiplier is bounded to [FLOOR, 1.0]; an underconfident agent is
    never inflated above 1.0, an overconfident one is shrunk toward reality but never below FLOOR;
  * cold-start inert — below MIN_N resolved decisions every multiplier is exactly 1.0;
  * grades the RAW (pre-shrink) confidence so the loop converges to a fixed point instead of
    oscillating;
  * never raises on missing/garbage data;
  * actually applied — a low sentinel multiplier downgrades a high-confidence OPPOSE from a
    hard DROP to a TRIM inside the committee, and the raw confidence is preserved for grading.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

import bot  # noqa: F401 — bootstraps vendor/macro
from brain import calibration as CAL


# ── pure multiplier arithmetic ───────────────────────────────────────────────
def test_mult_cold_start_below_min_n_is_one():
    assert CAL._mult(0.3, 0.9, CAL.MIN_N - 1) == 1.0      # not enough evidence → no adjustment


def test_mult_overconfident_shrinks():
    # reliability 0.5 vs stated 0.8 over enough samples → shrink to 0.5/0.8 = 0.625
    assert CAL._mult(0.5, 0.8, CAL.MIN_N) == 0.625


def test_mult_is_de_confidence_only_capped_at_one():
    # underconfident (reliability 0.9 > stated 0.5) must NOT inflate above 1.0
    assert CAL._mult(0.9, 0.5, CAL.MIN_N) == 1.0


def test_mult_never_below_floor():
    # catastrophically overconfident still floored
    assert CAL._mult(0.01, 0.99, CAL.MIN_N) == CAL.FLOOR


def test_mult_handles_degenerate_inputs():
    assert CAL._mult(None, 0.8, CAL.MIN_N) == 1.0
    assert CAL._mult(0.5, 0.0, CAL.MIN_N) == 1.0          # mean_conf 0 → no division
    assert CAL._mult(0.5, None, CAL.MIN_N) == 1.0


def test_summarize_empty_is_building():
    s = CAL._summarize([])
    assert s["n"] == 0 and s["multiplier"] == 1.0 and s["status"] == "building"


def test_summarize_scoring_when_enough():
    rows = [(1, 0.8)] * 6 + [(0, 0.8)] * 6      # 12 rows, reliability 0.5, mean 0.8
    s = CAL._summarize(rows)
    assert s["n"] == 12 and s["reliability"] == 0.5 and s["mean_confidence"] == 0.8
    assert s["multiplier"] == 0.625 and s["status"] == "scoring"


# ── FORGE reliability from the thesis ledger ─────────────────────────────────
def _thesis(i, raw_pc, rel, *, prob_pc=None, asof="2026-05-01", subj=None):
    subj = subj or f"T{i}"
    return {"id": f"f-{i}", "subject": subj, "state_asof": asof,
            "check_by": "2026-05-20", "raw_prob_correct": raw_pc,
            "prob_correct": prob_pc if prob_pc is not None else raw_pc,
            "entry_levels": {"ticker": subj},
            "falsifier": {"check": {"kind": "rel_return", "subject_ticker": subj,
                                    "vs": "SPY", "op": "<", "threshold": -0.05, "horizon_d": 5}},
            "_rel": rel}


def _patch_labels(monkeypatch, theses):
    monkeypatch.setattr("brain.ledger.all_theses", lambda: theses)

    def _label(t, asof):
        return {"resolved": True, "rel_return": t.get("_rel")}
    monkeypatch.setattr("brain.outcomes.label_thesis", _label)


def test_forge_reliability_overconfident_shrinks(monkeypatch):
    # 12 theses all stated raw 0.8; 6 win (rel +0.10) / 6 lose (rel -0.10 → below -0.05 threshold)
    theses = [_thesis(i, 0.8, 0.10 if i % 2 == 0 else -0.10) for i in range(12)]
    _patch_labels(monkeypatch, theses)
    f = CAL._forge_reliability(dt.date(2026, 6, 1))
    assert f["n"] == 12 and f["reliability"] == 0.5 and f["mean_confidence"] == 0.8
    assert f["multiplier"] == 0.625


def test_forge_grades_raw_not_the_adjusted_value(monkeypatch):
    # raw 0.8 but already-shrunk prob_correct 0.5 stored — grading MUST use raw (mean 0.8 → 0.625),
    # not the adjusted value (which would yield 0.5/0.5 = 1.0 and never converge).
    theses = [_thesis(i, 0.8, 0.10 if i % 2 == 0 else -0.10, prob_pc=0.5) for i in range(12)]
    _patch_labels(monkeypatch, theses)
    f = CAL._forge_reliability(dt.date(2026, 6, 1))
    assert f["mean_confidence"] == 0.8 and f["multiplier"] == 0.625


def test_forge_skips_unresolved_and_not_due(monkeypatch):
    due_resolved = _thesis(0, 0.8, 0.10)
    not_due = {**_thesis(1, 0.8, 0.10), "check_by": "2099-01-01"}
    unresolved = _thesis(2, 0.8, None)
    monkeypatch.setattr("brain.ledger.all_theses", lambda: [due_resolved, not_due, unresolved])

    def _label(t, asof):
        return {"resolved": t["_rel"] is not None, "rel_return": t.get("_rel")}
    monkeypatch.setattr("brain.outcomes.label_thesis", _label)
    f = CAL._forge_reliability(dt.date(2026, 6, 1))
    assert f["n"] == 1                      # only the due + resolved thesis is graded


# ── SENTINEL reliability from committee artifacts ────────────────────────────
def _write_committee(root, date_iso, ticker, stance, raw_conf):
    d = root / date_iso / ticker
    d.mkdir(parents=True, exist_ok=True)
    (d / "sentinel.json").write_text(json.dumps(
        {"agent": "sentinel", "stance": stance, "confidence": raw_conf,
         "raw_confidence": raw_conf}))


def test_sentinel_reliability_grades_directional_stance(monkeypatch, tmp_path):
    root = tmp_path / "committee"
    date_iso = "2026-05-01"
    # OPPOSE on a name that then UNDERperformed (rel<0) = correct; SUPPORT on an UNDERperformer = wrong
    _write_committee(root, date_iso, "WIN", "OPPOSE", 0.8)   # correct (rel<0)
    _write_committee(root, date_iso, "LOSE", "SUPPORT", 0.8)  # wrong  (rel<0)
    monkeypatch.setattr(CAL, "_COMMITTEE", root)
    theses = [{"subject": "WIN", "state_asof": date_iso, "entry_levels": {"ticker": "WIN"}, "_rel": -0.2},
              {"subject": "LOSE", "state_asof": date_iso, "entry_levels": {"ticker": "LOSE"}, "_rel": -0.2}]
    monkeypatch.setattr("brain.ledger.all_theses", lambda: theses)
    monkeypatch.setattr("brain.outcomes.label_thesis",
                        lambda t, asof: {"resolved": True, "rel_return": t["_rel"]})
    s = CAL._sentinel_reliability(dt.date(2026, 6, 1))
    assert s["n"] == 2 and s["reliability"] == 0.5         # 1 right, 1 wrong
    assert s["status"] == "building" and s["multiplier"] == 1.0  # n<MIN_N → inert


def test_sentinel_reliability_no_committee_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(CAL, "_COMMITTEE", tmp_path / "nope")
    s = CAL._sentinel_reliability(dt.date(2026, 6, 1))
    assert s["n"] == 0 and s["multiplier"] == 1.0


# ── compute / persist / load / multiplier ────────────────────────────────────
def test_compute_cold_start_all_ones(monkeypatch, tmp_path):
    monkeypatch.setattr("brain.ledger.all_theses", lambda: [])
    monkeypatch.setattr(CAL, "_COMMITTEE", tmp_path / "none")
    block = CAL.compute(dt.date(2026, 6, 1))
    assert block["agents"]["forge"]["multiplier"] == 1.0
    assert block["agents"]["sentinel"]["multiplier"] == 1.0
    assert block["min_n"] == CAL.MIN_N and block["floor"] == CAL.FLOOR


def test_persist_load_multiplier_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setattr(CAL, "_PATH", tmp_path / "calibration.json")
    theses = [_thesis(i, 0.8, 0.10 if i % 2 == 0 else -0.10) for i in range(12)]
    _patch_labels(monkeypatch, theses)
    monkeypatch.setattr(CAL, "_COMMITTEE", tmp_path / "none")
    block = CAL.persist(dt.date(2026, 6, 1))
    assert (tmp_path / "calibration.json").exists()
    assert block["agents"]["forge"]["multiplier"] == 0.625
    assert CAL.load()["agents"]["forge"]["multiplier"] == 0.625
    assert CAL.multiplier("forge") == 0.625
    assert CAL.multiplier("sentinel") == 1.0      # cold (no artifacts)


def test_multiplier_unknown_agent_and_missing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(CAL, "_PATH", tmp_path / "absent.json")
    assert CAL.multiplier("forge") == 1.0          # no file → safe default
    assert CAL.multiplier("nonsense") == 1.0


def test_compute_never_raises(monkeypatch):
    monkeypatch.setattr("brain.ledger.all_theses", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    block = CAL.compute(dt.date(2026, 6, 1))       # must degrade, not raise
    assert block["agents"]["forge"]["multiplier"] == 1.0


# ── end-to-end: calibration actually de-confidences the committee ────────────
def test_de_confidence_downgrades_committee_drop_to_trim(monkeypatch, tmp_path):
    from brain import committee as C
    monkeypatch.setattr(CAL, "multiplier", lambda agent: 0.5 if agent == "sentinel" else 1.0)
    monkeypatch.setattr(C.client, "available", lambda: True)
    monkeypatch.setattr(C.client, "call_model", lambda s, u, **k: (
        json.dumps({"stance": "OPPOSE", "confidence": 0.8, "strongest_bear": "late + crowded"}), {}))
    v = C.sentinel_assess("NVDA", {}, {}, {})
    # raw 0.8 preserved for grading; effective confidence de-confidenced to 0.4
    assert v["raw_confidence"] == 0.8 and v["confidence"] == 0.4
    # NEXUS now sees 0.4 < 0.6 → OPPOSE becomes a TRIM, not a hard DROP
    bd = {"confirmed": True, "combined": 78, "engine_score": 80, "research_score": 76,
          "viability": "compelling", "size_mult": 1.0}
    d = C.nexus(bd, v)
    assert d["action"] == "trim" and 0.0 < d["scale"] < 1.0
