"""Guards for the RISK OFFICER / EXIT MANAGER seat (brain/risk_officer).

Offline only: we stub ``client.call_model`` to return a canned JSON string (NO real LLM), and
force ``enabled() -> True``. Per the P1 package-attribute lesson, the seat lazy-does
``from brain import ledger`` / ``from portfolio import lenses, paper_account`` INSIDE
``_risk_input`` — so we patch BOTH the package attributes AND ``sys.modules`` (belt-and-suspenders)
so a combined pytest run never reaches the real submodules (which would touch the vendor tree /
live account). We prove:
  * subtract-only + held-only: an exit for a name NOT held is ignored,
  * an exit decision surfaces as an exit; a trim scales,
  * the never-blow-to-cash guard caps exits and demotes the surplus to hold,
  * an LLM/parse failure degrades to {decisions:[], ran:False} (never raises),
  * the pure apply_exits guard in isolation.
"""
from __future__ import annotations

import sys
import types

import pytest

import brain as _brain_pkg
import portfolio as _pf_pkg
from brain import risk_officer as R


def _held(*tickers, rel=None):
    rel = rel or {}
    return [{"sleeve": "conviction", "ticker": t, "held_days": 80, "current_weight": 0.06,
             "time_stop_by": "2026-05-01", "thesis_id": f"th_{t}", "entry_price": 100.0,
             "rel_return_since_entry": rel.get(t)} for t in tickers]


def _fake_offline(monkeypatch):
    """Patch ledger / lenses / paper_account offline (package attr + sys.modules)."""
    ledger = types.ModuleType("brain.ledger")
    ledger.all_theses = lambda: []
    lenses = types.ModuleType("portfolio.lenses")
    lenses._load = lambda rel: {}
    lenses._g = lambda d, path, default=None: default
    pa = types.ModuleType("portfolio.paper_account")
    pa._current_price = lambda t: 110.0
    monkeypatch.setattr(_brain_pkg, "ledger", ledger, raising=False)
    monkeypatch.setattr(_pf_pkg, "lenses", lenses, raising=False)
    monkeypatch.setattr(_pf_pkg, "paper_account", pa, raising=False)
    monkeypatch.setitem(sys.modules, "brain.ledger", ledger)
    monkeypatch.setitem(sys.modules, "portfolio.lenses", lenses)
    monkeypatch.setitem(sys.modules, "portfolio.paper_account", pa)


def _arm(monkeypatch, canned_json: str):
    monkeypatch.setattr(R, "enabled", lambda: True)

    def _call_model(system, user, *, role="pm", max_tokens=1500):
        return canned_json, {}
    monkeypatch.setattr(R.client, "call_model", _call_model)
    _fake_offline(monkeypatch)


# ───────────────────────── risk_assess (seat executor) ─────────────────────────
def test_assess_exit_surfaces(monkeypatch, tmp_path):
    monkeypatch.setattr(R, "_ARTIFACTS", tmp_path / "risk")
    _arm(monkeypatch, '{"decisions":[{"ticker":"AME","action":"exit","reason":"guidance cut",'
                      '"falsifier":"missed est"}],"rationale":"thesis broke"}')
    held = _held("NVDA", "AME", "AVGO", "PANW")
    res = R.risk_assess(None, "2026-06-22", regime={"quad": 1}, held_positions=held)
    assert res["ran"] is True
    exits = [d for d in res["decisions"] if d["action"] == "exit"]
    assert [d["ticker"] for d in exits] == ["AME"]


def test_assess_subtract_only_ignores_non_held(monkeypatch, tmp_path):
    monkeypatch.setattr(R, "_ARTIFACTS", tmp_path / "risk")
    # TSLA is NOT held → the exit must be ignored (no re-entry / no add).
    _arm(monkeypatch, '{"decisions":[{"ticker":"TSLA","action":"exit","reason":"x"}],"rationale":""}')
    held = _held("NVDA", "AME", "AVGO", "PANW")
    res = R.risk_assess(None, "2026-06-22", regime={}, held_positions=held)
    assert res["decisions"] == [] and res["ran"] is False


def test_assess_trim_scales(monkeypatch, tmp_path):
    monkeypatch.setattr(R, "_ARTIFACTS", tmp_path / "risk")
    _arm(monkeypatch, '{"decisions":[{"ticker":"NVDA","action":"trim","scale":0.6,"reason":"RS roll"}],'
                      '"rationale":""}')
    held = _held("NVDA", "AME", "AVGO", "PANW")
    res = R.risk_assess(None, "2026-06-22", regime={}, held_positions=held)
    trims = [d for d in res["decisions"] if d["action"] == "trim"]
    assert trims and trims[0]["ticker"] == "NVDA" and trims[0]["scale"] == pytest.approx(0.6)


def test_assess_never_blow_to_cash(monkeypatch, tmp_path):
    monkeypatch.setattr(R, "_ARTIFACTS", tmp_path / "risk")
    # held=5, the LLM wants to exit all 5 → guard caps to _MAX_EXITS_PER_SESSION (=2) and keeps
    # the held count >= _MIN_INVESTED_NAMES (floor_room = 5-3 = 2, so the cap binds, not the floor);
    # surplus demoted to hold (absent from decisions).
    _arm(monkeypatch, '{"decisions":['
                      '{"ticker":"NVDA","action":"exit","reason":"a"},'
                      '{"ticker":"AME","action":"exit","reason":"b"},'
                      '{"ticker":"AVGO","action":"exit","reason":"c"},'
                      '{"ticker":"PANW","action":"exit","reason":"d"},'
                      '{"ticker":"CRWD","action":"exit","reason":"e"}],"rationale":""}')
    held = _held("NVDA", "AME", "AVGO", "PANW", "CRWD",
                 rel={"NVDA": -0.2, "AME": 0.1, "AVGO": -0.05, "PANW": 0.3, "CRWD": -0.3})
    res = R.risk_assess(None, "2026-06-22", regime={}, held_positions=held)
    exits = [d for d in res["decisions"] if d["action"] == "exit"]
    assert len(exits) <= R._MAX_EXITS_PER_SESSION
    assert (len(held) - len(exits)) >= R._MIN_INVESTED_NAMES
    # worst losers exit first: CRWD (-0.3) and NVDA (-0.2) over the rest.
    assert set(d["ticker"] for d in exits) == {"CRWD", "NVDA"}


def test_assess_llm_failure_degrades(monkeypatch, tmp_path):
    monkeypatch.setattr(R, "_ARTIFACTS", tmp_path / "risk")
    monkeypatch.setattr(R, "enabled", lambda: True)

    def _boom(*a, **k):
        raise RuntimeError("llm down")
    monkeypatch.setattr(R.client, "call_model", _boom)
    _fake_offline(monkeypatch)
    res = R.risk_assess(None, "2026-06-22", regime={}, held_positions=_held("NVDA", "AME"))
    assert res["decisions"] == [] and res["ran"] is False


def test_assess_off_no_exits(monkeypatch, tmp_path):
    monkeypatch.setattr(R, "_ARTIFACTS", tmp_path / "risk")
    monkeypatch.setattr(R, "enabled", lambda: False)
    _fake_offline(monkeypatch)
    res = R.risk_assess(None, "2026-06-22", regime={}, held_positions=_held("NVDA", "AME"))
    assert res["decisions"] == [] and res["ran"] is False


# ───────────────────────── apply_exits (pure guard) ─────────────────────────
def test_apply_exits_caps_and_keeps_floor():
    held = _held("A", "B", "C", "D", rel={"A": -0.3, "B": 0.2, "C": -0.1, "D": 0.4})
    decisions = [{"ticker": t, "action": "exit", "scale": 0.0, "rel_return": r}
                 for t, r in (("A", -0.3), ("B", 0.2), ("C", -0.1), ("D", 0.4))]
    out = R.apply_exits(held, decisions, max_exits=2, min_invested=3)
    assert len(out["exits"]) == 1                          # floor_room = 4-3 = 1, capped below max_exits
    assert out["exits"][0]["ticker"] == "A"               # worst loser exits first


def test_apply_exits_max_exits_binds():
    held = _held("A", "B", "C", "D", "E", "F",
                 rel={"A": -0.5, "B": -0.4, "C": -0.3, "D": 0.1, "E": 0.2, "F": 0.3})
    decisions = [{"ticker": t, "action": "exit", "scale": 0.0, "rel_return": r}
                 for t, r in (("A", -0.5), ("B", -0.4), ("C", -0.3))]
    out = R.apply_exits(held, decisions, max_exits=2, min_invested=3)
    assert len(out["exits"]) == 2                          # max_exits binds (floor_room = 3)
    assert [d["ticker"] for d in out["exits"]] == ["A", "B"]


def test_apply_exits_trims_pass_through():
    held = _held("A", "B", "C")
    decisions = [{"ticker": "A", "action": "trim", "scale": 0.5, "rel_return": -0.1},
                 {"ticker": "B", "action": "exit", "scale": 0.0, "rel_return": -0.2}]
    out = R.apply_exits(held, decisions, max_exits=2, min_invested=3)
    assert out["trims"] and out["trims"][0]["ticker"] == "A"
    assert out["exits"] == []                              # floor_room=0 → no exits
