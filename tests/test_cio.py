"""Guards for the CIO / Meta-PM weekly review (brain/cio).

Offline only. We feed a fixture calibration.json + nav_history.jsonl into a tmp tree, fake the shadow
leaderboard, the LLM client, and the paper account — patching BOTH the package attribute AND
sys.modules (the combined-run hang lesson: cio lazy-does `from brain import calibration / client` and
`from portfolio import shadow_books` inside functions, so a sys.modules-only patch leaks the real
module through to a live LLM call). We prove:

  * review() returns per-seat reputation for all four seats and never raises,
  * write() persists BOTH <week>.json and <week>.md,
  * empty inputs degrade to an honest stub (no raise),
  * the CIO NEVER trades — a faked paper account sees zero calls,
  * an overconfident seat (low multiplier, scoring) is flagged + gets a recommendation.
"""
from __future__ import annotations

import json
import sys
import types
from datetime import date

import pytest

import bot  # noqa: F401
import brain
from brain import cio


def _fixture_calibration(tmp_path, agents: dict) -> types.ModuleType:
    """A fake brain.calibration exposing only load()/multiplier() the CIO uses."""
    fake = types.ModuleType("brain.calibration")
    block = {"as_of": "2026-06-15", "min_n": 12, "floor": 0.5, "agents": agents}
    fake.load = lambda: block
    fake.multiplier = lambda a: (agents.get(a) or {}).get("multiplier", 1.0)
    return fake


def _patch(monkeypatch, *, calibration_mod, leaderboard=None, narrate_text=None):
    # calibration (dual-patch)
    monkeypatch.setattr(brain, "calibration", calibration_mod, raising=False)
    monkeypatch.setitem(sys.modules, "brain.calibration", calibration_mod)

    # shadow leaderboard (dual-patch on the portfolio package)
    import portfolio
    sb = types.ModuleType("portfolio.shadow_books")
    sb.load_leaderboard = lambda: (leaderboard if leaderboard is not None else {})
    monkeypatch.setattr(portfolio, "shadow_books", sb, raising=False)
    monkeypatch.setitem(sys.modules, "portfolio.shadow_books", sb)

    # LLM client (dual-patch) — count calls; return scripted prose or None
    calls = {"n": 0}
    client = types.ModuleType("brain.client")

    def _call_model(system, user, *, role="pm", max_tokens=1500):
        calls["n"] += 1
        return (narrate_text, None) if narrate_text else (None, "no_backend")

    client.available = lambda: narrate_text is not None
    client.call_model = _call_model
    monkeypatch.setattr(brain, "client", client, raising=False)
    monkeypatch.setitem(sys.modules, "brain.client", client)

    # outcomes — no resolved synthetic labels (keeps KPI scan inert, offline, deterministic)
    outcomes = types.ModuleType("brain.outcomes")
    outcomes.label_thesis = lambda thesis, asof=None: None
    monkeypatch.setattr(brain, "outcomes", outcomes, raising=False)
    monkeypatch.setitem(sys.modules, "brain.outcomes", outcomes)

    return calls


def _scoring(mult, rel, n=20):
    return {"n": n, "reliability": rel, "mean_confidence": round(rel / mult, 3) if mult else None,
            "multiplier": mult, "status": "scoring"}


def _building():
    return {"n": 3, "reliability": None, "mean_confidence": None, "multiplier": 1.0, "status": "building"}


def test_review_returns_per_seat_reputation(monkeypatch, tmp_path):
    agents = {
        "strategist": _scoring(0.95, 0.62),   # well-calibrated
        "pm": _building(),                     # inert (cold-start)
        "gate": _scoring(0.62, 0.38),          # overconfident
        "risk": _scoring(0.90, 0.55),          # well-calibrated
    }
    _patch(monkeypatch, calibration_mod=_fixture_calibration(tmp_path, agents))
    monkeypatch.setattr(cio, "_OUT", tmp_path / "cio")
    monkeypatch.setattr(cio, "_NAV_BOOKS", [])  # no nav files in the tmp tree

    rep = cio.review(asof=date(2026, 6, 21))
    seats = {s["seat"]: s for s in rep["per_seat"]}
    assert set(seats) == {"strategist", "pm", "gate", "risk"}
    assert seats["pm"]["reputation"] == "inert"
    assert seats["gate"]["reputation"] == "overconfident"
    assert seats["strategist"]["reputation"] == "well_calibrated"
    # overconfident seat is surfaced + carries a recommendation
    assert "GATE OFFICER" in rep["whats_broken"]
    assert any("GATE OFFICER" in t for t in rep["tuning_recommendations"])
    assert seats["gate"]["recommendation"]
    # inert seat gets a "no action" recommendation, not a flag
    assert seats["pm"]["recommendation"] and "inert" in seats["pm"]["recommendation"]


def test_write_persists_both_files(monkeypatch, tmp_path):
    agents = {"strategist": _building(), "pm": _building(), "gate": _building(), "risk": _building()}
    _patch(monkeypatch, calibration_mod=_fixture_calibration(tmp_path, agents))
    out = tmp_path / "cio"
    monkeypatch.setattr(cio, "_OUT", out)
    monkeypatch.setattr(cio, "_NAV_BOOKS", [])

    res = cio.write(asof=date(2026, 6, 21), narrate=False)
    assert res["ok"]
    jp, mp = res["json_path"], res["md_path"]
    assert jp.endswith(".json") and mp.endswith(".md")
    import os
    assert os.path.exists(jp) and os.path.exists(mp)
    body = json.loads((out / f"{res['week']}.json").read_text())
    assert body["per_seat"] and "note_md" in body
    md = (out / f"{res['week']}.md").read_text()
    assert "CIO" in md and "Per-seat calibration" in md


def test_nav_summary_excess(monkeypatch, tmp_path):
    agents = {"strategist": _building(), "pm": _building(), "gate": _building(), "risk": _building()}
    _patch(monkeypatch, calibration_mod=_fixture_calibration(tmp_path, agents))
    monkeypatch.setattr(cio, "_OUT", tmp_path / "cio")
    nav = tmp_path / "nav.jsonl"
    nav.write_text("\n".join([
        json.dumps({"date": "2026-06-01", "nav": 1_000_000.0, "spy_nav": 1_000_000.0}),
        json.dumps({"date": "2026-06-21", "nav": 1_100_000.0, "spy_nav": 1_050_000.0}),
    ]))
    monkeypatch.setattr(cio, "_NAV_BOOKS", [("flagship", nav)])

    rep = cio.review(asof=date(2026, 6, 22))
    assert rep["book_summary"]
    b = rep["book_summary"][0]
    assert b["ret_pct"] == 10.0 and b["spy_ret_pct"] == 5.0 and b["excess_pct"] == 5.0


def test_never_trades(monkeypatch, tmp_path):
    """Faking the paper account and asserting zero calls — the CIO must never touch it."""
    agents = {"strategist": _scoring(0.6, 0.4), "pm": _building(),
              "gate": _building(), "risk": _building()}
    _patch(monkeypatch, calibration_mod=_fixture_calibration(tmp_path, agents))
    monkeypatch.setattr(cio, "_OUT", tmp_path / "cio")
    monkeypatch.setattr(cio, "_NAV_BOOKS", [])

    import portfolio
    pa_calls = {"n": 0}
    pa = types.ModuleType("portfolio.paper_account")

    def _boom(*a, **k):
        pa_calls["n"] += 1
        raise AssertionError("CIO must never call the paper account")

    pa.submit_order = _boom
    pa.buy = _boom
    pa.sell = _boom
    monkeypatch.setattr(portfolio, "paper_account", pa, raising=False)
    monkeypatch.setitem(sys.modules, "portfolio.paper_account", pa)

    cio.write(asof=date(2026, 6, 21), narrate=False)
    assert pa_calls["n"] == 0


def test_empty_inputs_never_raise(monkeypatch, tmp_path):
    _patch(monkeypatch, calibration_mod=_fixture_calibration(tmp_path, {}))
    monkeypatch.setattr(cio, "_OUT", tmp_path / "cio")
    monkeypatch.setattr(cio, "_NAV_BOOKS", [])
    rep = cio.review(asof=date(2026, 6, 21))
    # all four seats still present (defaulted to building), nothing flagged, no raise
    assert len(rep["per_seat"]) == 4
    assert rep["whats_broken"] == []
    res = cio.write(asof=date(2026, 6, 21), narrate=False)
    assert res["ok"]


def test_narrate_upgrades_note(monkeypatch, tmp_path):
    agents = {"strategist": _scoring(0.62, 0.38), "pm": _building(),
              "gate": _building(), "risk": _building()}
    calls = _patch(monkeypatch, calibration_mod=_fixture_calibration(tmp_path, agents),
                   narrate_text="# CIO prose note\n\nStrategist is overconfident.")
    monkeypatch.setattr(cio, "_OUT", tmp_path / "cio")
    monkeypatch.setattr(cio, "_NAV_BOOKS", [])

    res = cio.write(asof=date(2026, 6, 21), narrate=True)
    assert res["narrated"] is True
    assert calls["n"] == 1
    md = (tmp_path / "cio" / f"{res['week']}.md").read_text()
    assert "CIO prose note" in md
