"""P3 per-role grading — offline unit tests for brain.calibration's four Flagship-seat graders.

No LLM, no vendor price data. We feed synthetic seat artifacts under a tmp_path-rooted tree
(monkeypatch the module path constants) and stub the price/outcome boundary via a DUAL patch
(package attribute + sys.modules) of brain.outcomes.label_thesis — per the P1/P2 mock lesson, a bare
sys.modules patch leaks the already-imported package attribute through to a real fetch.

We prove: cold-start inertia (n<MIN_N → multiplier 1.0), the good/costly veto sign convention, the
risk exit sign convention, the strategist median rule, and leakage (a decision dated >= asof is
excluded because its forward window has not elapsed).
"""
from __future__ import annotations

import json
import sys
import types
from datetime import date

import pytest

import brain
from brain import calibration


def _route_labels(monkeypatch, table):
    """Dual-patch brain.outcomes.label_thesis to return scripted resolved/rel_return per ticker.
    `table` maps TICKER -> rel_return (float) or None (unresolved / no data)."""
    fake = types.ModuleType("brain.outcomes")

    def label_thesis(thesis, asof=None):
        chk = ((thesis.get("falsifier") or {}).get("check")) or {}
        tk = (chk.get("subject_ticker")
              or (thesis.get("entry_levels") or {}).get("ticker") or "").upper()
        rel = table.get(tk)
        if rel is None:
            return {"id": thesis.get("id"), "ticker": tk, "status": "unresolved",
                    "resolved": False, "rel_return": None}
        return {"id": thesis.get("id"), "ticker": tk, "status": "resolved",
                "resolved": True, "rel_return": float(rel)}

    fake.label_thesis = label_thesis
    monkeypatch.setattr(brain, "outcomes", fake, raising=False)
    monkeypatch.setitem(sys.modules, "brain.outcomes", fake)
    return fake


def _root(monkeypatch, tmp_path):
    committee = tmp_path / "committee"
    gate = tmp_path / "gate_officer"
    risk = tmp_path / "risk_officer"
    monkeypatch.setattr(calibration, "_COMMITTEE", committee)
    monkeypatch.setattr(calibration, "_GATE_OFFICER", gate)
    monkeypatch.setattr(calibration, "_RISK_OFFICER", risk)
    return committee, gate, risk


def _write_decisions(root, agent, d, decisions):
    """Write (or APPEND to) <root>/<d>/decisions.json. The grader reads one decisions.json per date
    holding a list, so repeated calls for the same date accumulate rather than clobber."""
    p = root / d
    p.mkdir(parents=True, exist_ok=True)
    f = p / "decisions.json"
    existing = []
    if f.exists():
        try:
            existing = (json.loads(f.read_text()).get("result") or {}).get("decisions") or []
        except Exception:
            existing = []
    f.write_text(json.dumps(
        {"agent": agent, "result": {"decisions": existing + list(decisions)}}))


def _write_gate(gate_root, d, decisions):
    _write_decisions(gate_root, "gate_officer", d, decisions)


def _write_risk(risk_root, d, decisions):
    _write_decisions(risk_root, "risk_officer", d, decisions)


def _write_strategist(committee_root, d, themes):
    p = committee_root / d / "_FLAGSHIP"
    p.mkdir(parents=True, exist_ok=True)
    (p / "strategist.json").write_text(json.dumps(
        {"agent": "strategist", "verdict": {"confirmed_themes": themes}}))


ASOF = date(2026, 6, 22)


# ───────────────────────────── GATE OFFICER ─────────────────────────────
def test_gate_cold_start_inert(monkeypatch, tmp_path):
    _, gate, _ = _root(monkeypatch, tmp_path)
    _route_labels(monkeypatch, {f"T{i}": -0.05 for i in range(5)})
    for i in range(5):                                   # only 5 < MIN_N=12
        _write_gate(gate, "2026-05-01", [{"ticker": f"T{i}", "action": "veto"}])
    # all on one date but distinct tickers → 5 rows
    out = calibration._gate_reliability(ASOF)
    assert out["n"] == 5
    assert out["status"] == "building"
    assert out["multiplier"] == 1.0                      # cold-start: no adjustment


def test_gate_good_vetoes_multiplier_high(monkeypatch, tmp_path):
    _, gate, _ = _root(monkeypatch, tmp_path)
    tickers = [f"G{i}" for i in range(14)]
    _route_labels(monkeypatch, {t: -0.04 for t in tickers})   # all underperformed → good vetoes
    for t in tickers:
        _write_gate(gate, "2026-04-15", [{"ticker": t, "action": "veto"}])
    out = calibration._gate_reliability(ASOF)
    assert out["n"] == 14 and out["status"] == "scoring"
    assert out["reliability"] == 1.0
    assert out["multiplier"] == 1.0                      # reliability/1.0 clamped to 1.0


def test_gate_costly_vetoes_multiplier_floored(monkeypatch, tmp_path):
    _, gate, _ = _root(monkeypatch, tmp_path)
    tickers = [f"B{i}" for i in range(14)]
    _route_labels(monkeypatch, {t: +0.06 for t in tickers})   # all ripped → costly vetoes
    for t in tickers:
        _write_gate(gate, "2026-04-15", [{"ticker": t, "action": "veto"}])
    out = calibration._gate_reliability(ASOF)
    assert out["reliability"] == 0.0
    assert out["multiplier"] == calibration.FLOOR        # floored at 0.5


def test_gate_leakage_excludes_future_dated(monkeypatch, tmp_path):
    _, gate, _ = _root(monkeypatch, tmp_path)
    _route_labels(monkeypatch, {"X": -0.05})
    _write_gate(gate, ASOF.isoformat(), [{"ticker": "X", "action": "veto"}])   # dated == asof
    _write_gate(gate, "2026-07-01", [{"ticker": "X", "action": "veto"}])       # future
    out = calibration._gate_reliability(ASOF)
    assert out["n"] == 0                                 # both excluded (window not elapsed)


# ───────────────────────────── RISK OFFICER ─────────────────────────────
def test_risk_exit_sign_convention(monkeypatch, tmp_path):
    _, _, risk = _root(monkeypatch, tmp_path)
    good = [f"GE{i}" for i in range(8)]                  # kept falling post-exit → good exit
    bad = [f"BE{i}" for i in range(6)]                   # ripped post-exit → premature
    _route_labels(monkeypatch, {**{t: -0.03 for t in good}, **{t: +0.05 for t in bad}})
    for t in good + bad:
        _write_risk(risk, "2026-04-10", [{"ticker": t, "action": "exit"}])
    out = calibration._risk_reliability(ASOF)
    assert out["n"] == 14 and out["status"] == "scoring"
    assert out["reliability"] == pytest.approx(8 / 14, abs=1e-3)


def test_risk_ignores_non_exit_actions(monkeypatch, tmp_path):
    _, _, risk = _root(monkeypatch, tmp_path)
    _route_labels(monkeypatch, {"H": -0.05, "E": -0.05})
    _write_risk(risk, "2026-04-10", [{"ticker": "H", "action": "hold"},
                                     {"ticker": "E", "action": "exit"}])
    out = calibration._risk_reliability(ASOF)
    assert out["n"] == 1                                 # only the exit graded


# ───────────────────────────── STRATEGIST ─────────────────────────────
def test_strategist_median_rule(monkeypatch, tmp_path):
    committee, _, _ = _root(monkeypatch, tmp_path)
    # theme A: members median > 0 (played out). theme B: median < 0 (failed).
    _route_labels(monkeypatch, {"A1": 0.05, "A2": 0.03, "A3": -0.01,
                                "B1": -0.04, "B2": -0.02, "B3": 0.01})
    _write_strategist(committee, "2026-04-01", [
        {"theme": "semis", "stage": "confirmed", "leadership": 0.8, "names": ["A1", "A2", "A3"]},
        {"theme": "banks", "stage": "confirmed", "leadership": 0.6, "names": ["B1", "B2", "B3"]},
    ])
    out = calibration._strategist_reliability(ASOF)
    assert out["n"] == 2
    assert out["reliability"] == 0.5                     # one played out, one didn't
    assert out["mean_confidence"] == pytest.approx(0.7)  # (0.8 + 0.6)/2


def test_strategist_skips_emerging_stage(monkeypatch, tmp_path):
    committee, _, _ = _root(monkeypatch, tmp_path)
    _route_labels(monkeypatch, {"E1": 0.05})
    _write_strategist(committee, "2026-04-01", [
        {"theme": "x", "stage": "emerging", "leadership": 0.5, "names": ["E1"]}])
    out = calibration._strategist_reliability(ASOF)
    assert out["n"] == 0                                 # emerging stage not graded


# ───────────────────────────── PM-CONVICTION ─────────────────────────────
def test_pm_grades_only_conv_theses(monkeypatch, tmp_path):
    _root(monkeypatch, tmp_path)
    # fake the ledger + outcomes; PM theses end in -conv, FORGE theses don't.
    fake_ledger = types.ModuleType("brain.ledger")
    theses = [
        {"id": "2026-04-01-NVDA-conv", "state_asof": "2026-04-01", "check_by": "2026-05-01",
         "raw_prob_correct": 0.7, "entry_levels": {"ticker": "NVDA"},
         "falsifier": {"check": {"kind": "rel_return", "subject_ticker": "NVDA"}}},
        {"id": "2026-04-01-XYZ-thesis", "state_asof": "2026-04-01", "check_by": "2026-05-01",
         "raw_prob_correct": 0.6, "entry_levels": {"ticker": "XYZ"},
         "falsifier": {"check": {"kind": "rel_return", "subject_ticker": "XYZ"}}},
    ]
    fake_ledger.all_theses = lambda: theses
    monkeypatch.setattr(brain, "ledger", fake_ledger, raising=False)
    monkeypatch.setitem(sys.modules, "brain.ledger", fake_ledger)
    _route_labels(monkeypatch, {"NVDA": 0.05, "XYZ": -0.05})

    pm = calibration._pm_reliability(ASOF)
    assert pm["n"] == 1                                  # only the -conv thesis
    forge = calibration._forge_reliability(ASOF)
    assert forge["n"] == 1                               # only the non-conv thesis


# ───────────────────────────── compute() registration ─────────────────────────────
def test_compute_registers_four_seats_never_raises(monkeypatch, tmp_path):
    _root(monkeypatch, tmp_path)
    # empty ledger so forge/pm/sentinel are inert
    fake_ledger = types.ModuleType("brain.ledger")
    fake_ledger.all_theses = lambda: []
    monkeypatch.setattr(brain, "ledger", fake_ledger, raising=False)
    monkeypatch.setitem(sys.modules, "brain.ledger", fake_ledger)
    _route_labels(monkeypatch, {})
    block = calibration.compute(ASOF)
    for seat in ("forge", "sentinel", "strategist", "pm", "gate", "risk"):
        assert seat in block["agents"]
        assert block["agents"][seat]["multiplier"] == 1.0   # all empty → inert
