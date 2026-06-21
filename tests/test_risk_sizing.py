"""Tests for portfolio.risk_sizing — vol-managed sizing overlay (graceful, additive)."""
from __future__ import annotations

from portfolio import risk_sizing as rs


def _seed(mult, gross=1.0, state="neutral"):
    rs.reset_cache()
    rs._CACHE["mult"] = dict(mult)
    rs._CACHE["regime"] = {"gross_mult": gross, "state": state}


def test_apply_reweights_by_inverse_vol_and_preserves_budget():
    _seed({"A": 1.6, "B": 0.5, "C": 1.0})
    pos = [{"ticker": "A", "weight": 0.1}, {"ticker": "B", "weight": 0.1},
           {"ticker": "C", "weight": 0.1}]
    rs.apply(pos, budget=0.30, name_cap=0.20)
    w = {p["ticker"]: p["weight"] for p in pos}
    assert w["A"] > w["C"] > w["B"]                       # calm > neutral > wild
    assert abs(sum(w.values()) - 0.30) < 1e-6            # renormalized to budget


def test_lean_out_regime_holds_cash():
    _seed({"A": 1.0, "B": 1.0}, gross=0.75, state="lean_out")
    pos = [{"ticker": "A", "weight": 0.1}, {"ticker": "B", "weight": 0.1}]
    rs.apply(pos, budget=0.30, name_cap=0.5)
    assert abs(sum(p["weight"] for p in pos) - 0.225) < 1e-6    # 0.30 * 0.75


def test_lean_in_does_not_lever_above_budget():
    _seed({"A": 1.0}, gross=1.2, state="lean_in")
    pos = [{"ticker": "A", "weight": 0.1}]
    rs.apply(pos, budget=0.10, name_cap=0.5)
    assert pos[0]["weight"] <= 0.10 + 1e-9               # full budget, never levered


def test_name_cap_respected():
    _seed({"A": 1.6, "B": 0.1})
    pos = [{"ticker": "A", "weight": 0.1}, {"ticker": "B", "weight": 0.1}]
    rs.apply(pos, budget=0.50, name_cap=0.08)
    assert all(p["weight"] <= 0.08 + 1e-9 for p in pos)


def test_missing_field_is_neutral():
    _seed({})                                            # no macro field => everything 1.0
    assert rs.vol_mult("ZZZZ") == 1.0
    assert rs.selection_gross() == 1.0
    pos = [{"ticker": "X", "weight": 0.1}, {"ticker": "Y", "weight": 0.1}]
    rs.apply(pos, budget=0.20, name_cap=0.5)
    assert abs(pos[0]["weight"] - pos[1]["weight"]) < 1e-9    # unchanged relative sizing


def test_empty_or_zero_budget_is_safe():
    _seed({"A": 1.5})
    assert rs.apply([], budget=0.3) == []
    pos = [{"ticker": "A", "weight": 0.1}]
    assert rs.apply(pos, budget=0.0) is pos
