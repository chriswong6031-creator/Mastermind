"""Guards for the GATE OFFICER seat (brain/gate_officer).

Offline only: we stub ``client.call_model`` to return a canned JSON string (NO real LLM), and
force ``enabled() -> True``. Per the P1 package-attribute lesson, the seat lazy-does
``from portfolio import lenses`` INSIDE ``_gate_input`` / ``gate_assess`` — so we patch BOTH the
``portfolio`` package attribute AND ``sys.modules`` (belt-and-suspenders) so a combined pytest run
never reaches the real lenses (which would touch the vendor tree). We prove:
  * the subtract-only INVARIANT: a decision naming a non-proposed ticker is dropped,
  * veto/withhold zero the name and (via apply_gate) park it to the watchlist,
  * trim scales the weight and tags the row,
  * approve is a no-op,
  * an LLM/parse failure degrades to an empty decisions list (never raises),
  * the never-add guarantee in the pure apply_gate reshaper.
"""
from __future__ import annotations

import sys
import types

import pytest

import brain as _brain_pkg
import portfolio as _pf_pkg
from brain import gate_officer as G


def _fake_lenses():
    m = types.ModuleType("portfolio.lenses")
    m._load = lambda rel: {"sector": "Technology"}
    m.full = lambda t, kind="name": {"synthesis": {"confluence": 0.3}}
    m._g = lambda d, path, default=None: default
    return m


def _arm(monkeypatch, canned_json: str):
    """Force the seat ON, stub the LLM with a canned reply, and patch lenses offline."""
    monkeypatch.setattr(G, "enabled", lambda: True)

    def _call_model(system, user, *, role="pm", max_tokens=1500, seat=None, record_book=None):
        return canned_json, {}
    monkeypatch.setattr(G.client, "call_model", _call_model)

    fake_lenses = _fake_lenses()
    monkeypatch.setattr(_pf_pkg, "lenses", fake_lenses, raising=False)
    monkeypatch.setitem(sys.modules, "portfolio.lenses", fake_lenses)


def _book():
    return [
        {"ticker": "NVDA", "weight": 0.07, "confluence": 0.4, "divergences": [],
         "bull": "ai", "bear": "rich", "committee": {"action": "confirm", "sentinel_stance": "SUPPORT"}},
        {"ticker": "AME", "weight": 0.05, "confluence": 0.3, "divergences": [],
         "bull": "onshore", "bear": "late", "committee": {"action": "confirm", "sentinel_stance": "SUPPORT"}},
    ]


# ───────────────────────── gate_assess (seat executor) ─────────────────────────
def test_assess_veto_only_proposed(monkeypatch, tmp_path):
    monkeypatch.setattr(G, "_ARTIFACTS", tmp_path / "gate")
    _arm(monkeypatch, '{"decisions":[{"ticker":"AME","action":"veto","reason":"sector crowded"}],'
                      '"book_view":"too tech-heavy","rationale":"trim concentration"}')
    res = G.gate_assess(_book(), "2026-06-22", regime={"quad": 1}, portfolio_ctx={})
    assert res["ran"] is True
    decs = {d["ticker"]: d for d in res["decisions"]}
    assert decs["AME"]["action"] == "veto" and decs["AME"]["scale"] == 0.0


def test_assess_cannot_add_non_proposed(monkeypatch, tmp_path):
    monkeypatch.setattr(G, "_ARTIFACTS", tmp_path / "gate")
    # canned decision names TSLA, which is NOT in the proposed book → must be dropped.
    _arm(monkeypatch, '{"decisions":[{"ticker":"TSLA","action":"approve","reason":"add it"}],'
                      '"book_view":"","rationale":""}')
    res = G.gate_assess(_book(), "2026-06-22", regime={}, portfolio_ctx={})
    assert all(d["ticker"] in {"NVDA", "AME"} for d in res["decisions"])
    assert "TSLA" not in {d["ticker"] for d in res["decisions"]}


def test_assess_llm_failure_degrades(monkeypatch, tmp_path):
    monkeypatch.setattr(G, "_ARTIFACTS", tmp_path / "gate")
    monkeypatch.setattr(G, "enabled", lambda: True)

    def _boom(*a, **k):
        raise RuntimeError("llm down")
    monkeypatch.setattr(G.client, "call_model", _boom)
    fake_lenses = _fake_lenses()
    monkeypatch.setattr(_pf_pkg, "lenses", fake_lenses, raising=False)
    monkeypatch.setitem(sys.modules, "portfolio.lenses", fake_lenses)

    res = G.gate_assess(_book(), "2026-06-22", regime={}, portfolio_ctx={})
    assert res["decisions"] == [] and res["ran"] is False


def test_assess_off_no_decisions(monkeypatch, tmp_path):
    monkeypatch.setattr(G, "_ARTIFACTS", tmp_path / "gate")
    monkeypatch.setattr(G, "enabled", lambda: False)
    fake_lenses = _fake_lenses()
    monkeypatch.setattr(_pf_pkg, "lenses", fake_lenses, raising=False)
    monkeypatch.setitem(sys.modules, "portfolio.lenses", fake_lenses)
    res = G.gate_assess(_book(), "2026-06-22", regime={}, portfolio_ctx={})
    assert res["decisions"] == [] and res["ran"] is False


# ───────────────────────── apply_gate (pure reshaper) ─────────────────────────
def test_apply_veto_parks_to_watchlist():
    parked = []
    wl = types.SimpleNamespace(
        append=lambda t, asof, reason=None, tech=None, combined=None: parked.append((t, reason)))
    decisions = [{"ticker": "AME", "action": "veto", "scale": 0.0, "reason": "crowded"}]
    out = G.apply_gate(_book(), decisions, asof="2026-06-22", watchlist=wl)
    assert {r["ticker"] for r in out} == {"NVDA"}          # AME dropped
    assert parked and parked[0][0] == "AME"
    assert parked[0][1].startswith("gate_officer:")


def test_apply_withhold_drops_and_parks():
    parked = []
    wl = types.SimpleNamespace(
        append=lambda t, asof, reason=None, tech=None, combined=None: parked.append(t))
    decisions = [{"ticker": "AME", "action": "withhold", "scale": 0.0, "reason": "wait"}]
    out = G.apply_gate(_book(), decisions, asof="2026-06-22", watchlist=wl)
    assert {r["ticker"] for r in out} == {"NVDA"} and parked == ["AME"]


def test_apply_trim_scales_weight():
    decisions = [{"ticker": "NVDA", "action": "trim", "scale": 0.5, "reason": "oversized"}]
    out = G.apply_gate(_book(), decisions, asof="2026-06-22")
    nvda = [r for r in out if r["ticker"] == "NVDA"][0]
    assert nvda["weight"] == pytest.approx(0.035)         # 0.07 * 0.5
    assert nvda["gate"]["action"] == "trim" and nvda["gate"]["scale"] == 0.5


def test_apply_approve_is_noop():
    decisions = [{"ticker": "NVDA", "action": "approve", "scale": 1.0, "reason": ""}]
    out = G.apply_gate(_book(), decisions, asof="2026-06-22")
    assert {r["ticker"] for r in out} == {"NVDA", "AME"}
    assert [r for r in out if r["ticker"] == "NVDA"][0]["weight"] == pytest.approx(0.07)


def test_apply_cannot_add_non_proposed():
    # a decision for a name NOT in the book is simply ignored — apply_gate walks `book`, not
    # `decisions`, so it can never inject a row.
    decisions = [{"ticker": "TSLA", "action": "approve", "scale": 1.0, "reason": "add"}]
    out = G.apply_gate(_book(), decisions, asof="2026-06-22")
    assert {r["ticker"] for r in out} == {"NVDA", "AME"}


def test_apply_veto_all_empties_book():
    decisions = [{"ticker": "NVDA", "action": "veto", "scale": 0.0, "reason": "x"},
                 {"ticker": "AME", "action": "veto", "scale": 0.0, "reason": "y"}]
    out = G.apply_gate(_book(), decisions, asof="2026-06-22")
    assert out == []                                       # caller degrades to engine path
