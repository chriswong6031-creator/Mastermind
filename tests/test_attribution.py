"""Guards for per-seat ATTRIBUTION (brain/attribution) — the active-return decomposition layer.

Attribution answers the PM question calibration doesn't: for a RESOLVED name, how is its realized
active return (rel_return vs SPY) split across the chain of seats that touched it (STRATEGIST /
FORGE / SENTINEL / NEXUS / GATE / RISK)? Properties under test:
  * the per-seat shares sum to 1.0, so per-seat bps sum (within rounding) to the name's rel_bps;
  * a vetoed name credits (dominates) the Gate; an exited name dominates the Risk Officer;
  * a sparse chain stays fully attributed (absent seats fold into FORGE selection);
  * honest stub on empty / missing data — never raises, status="building", zero seats;
  * each attributed name is regime-tagged (for regime-conditional reputation);
  * persist writes the per-asof file + accumulates the cumulative _rollup.

The forward label is stubbed offline via the DUAL patch (package attribute + sys.modules) so a
combined pytest run never hits a real price fetch or LLM. Attribution lazy-imports brain.outcomes
inside _resolved_rel, so patching the brain.outcomes PACKAGE ATTRIBUTE is what binds.
"""
from __future__ import annotations

import datetime as dt
import json
import sys

import pytest

import bot  # noqa: F401 — bootstraps vendor/macro
from brain import attribution as ATTR


# ── offline label stub (DUAL patch: package attr + sys.modules) ───────────────
@pytest.fixture
def stub_rel(monkeypatch):
    """Install a deterministic rel_return for every (ticker) via brain.outcomes.label_thesis.
    Returns a setter the test fills with {TICKER: rel}. Names absent from the map resolve to None
    (unresolved → skipped), mirroring a window that hasn't elapsed / price gap."""
    rels: dict[str, float] = {}

    def _label(thesis, asof=None):
        tk = ((thesis.get("entry_levels") or {}).get("ticker") or "").upper()
        if tk in rels:
            return {"resolved": True, "rel_return": rels[tk]}
        return {"resolved": False, "rel_return": None}

    import brain.outcomes as OUT
    monkeypatch.setattr(OUT, "label_thesis", _label)
    monkeypatch.setitem(sys.modules, "brain.outcomes", OUT)

    def _set(mapping):
        rels.clear()
        rels.update({k.upper(): v for k, v in mapping.items()})
    return _set


# ── artifact writers ─────────────────────────────────────────────────────────
def _committee(root, date_iso, ticker, *, forge=None, sentinel=None, nexus=None):
    d = root / date_iso / ticker.upper()
    d.mkdir(parents=True, exist_ok=True)
    if forge is not None:
        (d / "forge.json").write_text(json.dumps({"agent": "forge", **forge}))
    if sentinel is not None:
        (d / "sentinel.json").write_text(json.dumps({"agent": "sentinel", **sentinel}))
    if nexus is not None:
        (d / "nexus.json").write_text(json.dumps({"agent": "nexus", **nexus}))


def _strategist(root, date_iso, *, themes, cm=1.0):
    d = root / date_iso / "_FLAGSHIP"
    d.mkdir(parents=True, exist_ok=True)
    (d / "strategist.json").write_text(json.dumps(
        {"agent": "strategist",
         "verdict": {"calibration_multiplier": cm, "confirmed_themes": themes}}))


def _decisions(root, date_iso, decisions):
    d = root / date_iso
    d.mkdir(parents=True, exist_ok=True)
    (d / "decisions.json").write_text(json.dumps({"result": {"decisions": decisions}}))


@pytest.fixture
def wire(monkeypatch, tmp_path):
    """Point attribution's artifact roots at tmp dirs and silence the regime read."""
    monkeypatch.setattr(ATTR, "_COMMITTEE", tmp_path / "committee")
    monkeypatch.setattr(ATTR, "_GATE_OFFICER", tmp_path / "gate_officer")
    monkeypatch.setattr(ATTR, "_RISK_OFFICER", tmp_path / "risk_officer")
    monkeypatch.setattr(ATTR, "_OUT", tmp_path / "out")
    monkeypatch.setattr(ATTR, "_ROLLUP", tmp_path / "out" / "_rollup.json")
    monkeypatch.setattr(ATTR, "_read_regime", lambda: {"quad": 2, "quad_name": "reflation", "state": "risk_on"})
    return tmp_path


# ── pure share arithmetic ────────────────────────────────────────────────────
def test_shares_sum_to_one_full_chain():
    touches = {"strategist": {"theme": "x"}, "forge": {"confirmed": True},
               "sentinel": {"stance": "SUPPORT"}, "nexus": {"action": "confirm", "scale": 1.0},
               "gate": {"action": "approve"}, "risk": {"action": "hold"}}
    sh = ATTR._shares(touches)
    assert abs(sum(sh.values()) - 1.0) < 1e-6
    assert all(v > 0 for v in sh.values())


def test_shares_sparse_chain_folds_into_forge():
    # only FORGE touched → it owns ~all the credit
    sh = ATTR._shares({"forge": {"confirmed": True}})
    assert abs(sh["forge"] - 1.0) < 1e-6
    assert sum(sh.values()) == pytest.approx(1.0)


def test_shares_empty_is_zero():
    sh = ATTR._shares({})
    assert sum(sh.values()) == 0.0


def test_shares_dominance_gate_veto():
    base = {"forge": {"confirmed": True}, "nexus": {"action": "confirm", "scale": 1.0},
            "gate": {"action": "approve"}}
    veto = {**base, "gate": {"action": "veto", "scale": 0.0}}
    assert ATTR._shares(veto)["gate"] > ATTR._shares(base)["gate"]


def test_shares_dominance_risk_exit():
    base = {"forge": {"confirmed": True}, "risk": {"action": "hold"}}
    exited = {**base, "risk": {"action": "exit", "scale": 0.0}}
    assert ATTR._shares(exited)["risk"] > ATTR._shares(base)["risk"]


# ── end-to-end attribution ───────────────────────────────────────────────────
def test_attribute_sums_to_rel_bps(wire, stub_rel):
    root = wire / "committee"
    _strategist(root, "2026-05-01", themes=[{"theme": "ai", "stage": "confirmed",
                                             "leadership": 0.7, "names": ["EME"]}])
    _committee(root, "2026-05-01", "EME",
               forge={"confirmed": True, "combined": 78},
               sentinel={"stance": "CONDITIONAL", "confidence": 0.5},
               nexus={"action": "trim", "scale": 0.66})
    stub_rel({"EME": 0.10})        # +10% rel return → 1000 bps
    block = ATTR.attribute(dt.date(2026, 6, 1))
    assert block["status"] == "scoring" and block["n_names"] == 1
    rec = block["names"][0]
    assert rec["ticker"] == "EME" and rec["rel_bps"] == 1000.0
    assert abs(sum(rec["seat_bps"].values()) - rec["rel_bps"]) < 0.5   # bps reconcile
    assert abs(sum(rec["shares"].values()) - 1.0) < 1e-6
    assert rec["regime"]["quad_name"] == "reflation"                   # regime-tagged


def test_attribute_vetoed_name_credits_gate(wire, stub_rel):
    root = wire / "committee"
    _committee(root, "2026-05-01", "BAD",
               forge={"confirmed": True, "combined": 60},
               nexus={"action": "confirm", "scale": 1.0})
    _decisions(wire / "gate_officer", "2026-05-01",
               [{"ticker": "BAD", "action": "veto", "scale": 0.0, "reason": "concentration"}])
    stub_rel({"BAD": -0.08})       # name fell → the gate's veto earned the avoided drag
    block = ATTR.attribute(dt.date(2026, 6, 1))
    rec = block["names"][0]
    assert rec["dominant_seat"] == "gate"
    assert rec["shares"]["gate"] == max(rec["shares"].values())


def test_attribute_exited_name_credits_risk(wire, stub_rel):
    root = wire / "committee"
    _committee(root, "2026-05-01", "RUN",
               forge={"confirmed": True, "combined": 70},
               nexus={"action": "confirm", "scale": 1.0})
    _decisions(wire / "risk_officer", "2026-05-01",
               [{"ticker": "RUN", "action": "exit", "scale": 0.0, "reason": "time stop"}])
    stub_rel({"RUN": -0.05})
    block = ATTR.attribute(dt.date(2026, 6, 1))
    rec = block["names"][0]
    assert rec["dominant_seat"] == "risk"
    assert rec["shares"]["risk"] == max(rec["shares"].values())


def test_attribute_skips_unresolved(wire, stub_rel):
    root = wire / "committee"
    _committee(root, "2026-05-01", "OPEN", forge={"confirmed": True, "combined": 70})
    stub_rel({})                   # nothing resolved → name is skipped
    block = ATTR.attribute(dt.date(2026, 6, 1))
    assert block["n_names"] == 0 and block["status"] == "building"


def test_attribute_skips_not_elapsed(wire, stub_rel):
    root = wire / "committee"
    # decision date == asof → forward window hasn't elapsed → leakage guard excludes it
    _committee(root, "2026-06-01", "TODAY", forge={"confirmed": True, "combined": 70})
    stub_rel({"TODAY": 0.10})
    block = ATTR.attribute(dt.date(2026, 6, 1))
    assert block["n_names"] == 0


def test_attribute_honest_stub_on_empty(wire, stub_rel):
    stub_rel({})
    block = ATTR.attribute(dt.date(2026, 6, 1))
    assert block["status"] == "building" and block["n_names"] == 0
    assert all(block["seats"][s]["attributed_bps"] == 0.0 for s in ATTR.SEATS)
    assert all(block["seats"][s]["n"] == 0 for s in ATTR.SEATS)


def test_attribute_never_raises(monkeypatch):
    # garbage committee root that explodes on iterdir → must degrade, not raise
    class Boom:
        def exists(self):
            return True

        def iterdir(self):
            raise RuntimeError("boom")
    monkeypatch.setattr(ATTR, "_COMMITTEE", Boom())
    monkeypatch.setattr(ATTR, "_read_regime", lambda: {})
    block = ATTR.attribute(dt.date(2026, 6, 1))
    assert block["status"] == "building" and block["n_names"] == 0


def test_seat_rollup_aggregates(wire, stub_rel):
    root = wire / "committee"
    _committee(root, "2026-05-01", "A", forge={"confirmed": True, "combined": 70})
    _committee(root, "2026-05-01", "B", forge={"confirmed": True, "combined": 70})
    stub_rel({"A": 0.10, "B": 0.20})
    block = ATTR.attribute(dt.date(2026, 6, 1))
    # both names are forge-only → all credit to forge; n=2, bps = 1000+2000
    assert block["seats"]["forge"]["n"] == 2
    assert block["seats"]["forge"]["attributed_bps"] == pytest.approx(3000.0, abs=1.0)
    assert block["seats"]["forge"]["mean_bps"] == pytest.approx(1500.0, abs=1.0)


# ── persistence + cumulative rollup ──────────────────────────────────────────
def test_persist_writes_block_and_rollup(wire, stub_rel):
    root = wire / "committee"
    _committee(root, "2026-05-01", "A", forge={"confirmed": True, "combined": 70})
    stub_rel({"A": 0.10})
    block = ATTR.persist(dt.date(2026, 6, 1))
    assert (wire / "out" / "2026-06-01.json").exists()
    assert (wire / "out" / "_rollup.json").exists()
    loaded = ATTR.load(dt.date(2026, 6, 1))
    assert loaded["n_names"] == 1
    roll = ATTR.rollup()
    assert roll["seats"]["forge"]["n"] == 1
    assert roll["seats"]["forge"]["attributed_bps"] == pytest.approx(1000.0, abs=1.0)


def test_rollup_idempotent_per_asof(wire, stub_rel):
    root = wire / "committee"
    _committee(root, "2026-05-01", "A", forge={"confirmed": True, "combined": 70})
    stub_rel({"A": 0.10})
    ATTR.persist(dt.date(2026, 6, 1))
    ATTR.persist(dt.date(2026, 6, 1))   # re-run same asof must NOT double-count
    roll = ATTR.rollup()
    assert roll["seats"]["forge"]["n"] == 1
    assert roll["seats"]["forge"]["attributed_bps"] == pytest.approx(1000.0, abs=1.0)


def test_persist_never_raises_on_unwritable(monkeypatch, tmp_path, stub_rel):
    # _OUT under a path whose parent is a file → mkdir/write fail, but persist must not raise
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    monkeypatch.setattr(ATTR, "_COMMITTEE", tmp_path / "none")
    monkeypatch.setattr(ATTR, "_OUT", blocker / "sub")
    monkeypatch.setattr(ATTR, "_ROLLUP", blocker / "sub" / "_rollup.json")
    monkeypatch.setattr(ATTR, "_read_regime", lambda: {})
    block = ATTR.persist(dt.date(2026, 6, 1))
    assert block["status"] == "building"
