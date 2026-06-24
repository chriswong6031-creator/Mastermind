"""REWARD / INFLUENCE wiring (brain/reputation + committee.nexus) — task #5.

Calibration de-confidences a seat; reputation lets a WELL-CALIBRATED seat compound influence —
REGIME-CONDITIONALLY and CAPPED. Properties under test:

  * REGIME-CONDITIONAL — a seat that was a genius in a TREND quad is COLD in a fresh CHOP quad until
    it earns effective-n IN that quad; its trend streak cannot buy influence in chop.
  * BOUNDED — influence_weight ∈ [W_FLOOR, W_CEIL] = [0.5, 1.0]; never inflates a vote above nominal.
  * CAPPED — cap_influence clamps the effective vote to MAX_QUORUM_SHARE: no seat dominates the quorum.
  * NET-NEGATIVE DAMPER — a seat with negative cumulative attributed bps is floored to W_FLOOR.
  * FLAG-OFF BYTE-IDENTICAL — with MASTERMIND_REPUTATION_WEIGHTING unset, influence_weight==1.0 and
    committee.nexus() reproduces the legacy drop-bar (0.6) / trim (0.5) branches EXACTLY.
  * SUBTRACT-ONLY PRESERVED — influence can lower the OPPOSE bar / deepen the trim, but can NEVER
    raise a scale above the nominal ceiling, lift the bar above 0.6, or confirm a name FORGE dropped.

OFFLINE: no LLM, no vendor parquet, no network. The forward label is stubbed via the DUAL patch
(package attr brain.outcomes.label_thesis + sys.modules) and brain.ledger is faked the same way, per
the test lesson — so a combined pytest run never hits a real price fetch or LLM and never hangs.
"""
from __future__ import annotations

import json
import sys
import types
from datetime import date, timedelta

import pytest

import brain
from brain import calibration, committee, reputation


ASOF = date(2026, 6, 23)


# ───────────────────────────── offline substrate ─────────────────────────────
def _root(monkeypatch, tmp_path):
    """Re-root every on-disk artifact tree the reputation/calibration graders read at tmp_path."""
    committee_root = tmp_path / "committee"
    monkeypatch.setattr(reputation, "_COMMITTEE", committee_root)
    monkeypatch.setattr(reputation, "_GATE_OFFICER", tmp_path / "gate_officer")
    monkeypatch.setattr(reputation, "_RISK_OFFICER", tmp_path / "risk_officer")
    monkeypatch.setattr(reputation, "_VENDOR", tmp_path / "vendor")
    monkeypatch.setattr(calibration, "_COMMITTEE", committee_root)
    return committee_root


def _set_live_regime(monkeypatch, tmp_path, regime: dict):
    p = tmp_path / "vendor" / "data" / "regime"
    p.mkdir(parents=True, exist_ok=True)
    (p / "latest.json").write_text(json.dumps(regime))


def _strategist(committee_root, d, themes, regime: dict):
    """Write a Strategist artifact carrying input.regime (the regime live on date d) + confirmed themes."""
    p = committee_root / d / "_FLAGSHIP"
    p.mkdir(parents=True, exist_ok=True)
    (p / "strategist.json").write_text(json.dumps({
        "agent": "strategist",
        "input": {"regime": regime},
        "verdict": {"confirmed_themes": themes},
    }))


def _stub_rel(monkeypatch, mapping):
    """Stub brain.outcomes.label_thesis to a deterministic rel_return per ticker (DUAL patch)."""
    rels = {k.upper(): v for k, v in mapping.items()}

    def _label(thesis, asof=None):
        tk = ((thesis.get("entry_levels") or {}).get("ticker") or "").upper()
        if tk in rels:
            return {"resolved": True, "rel_return": rels[tk]}
        return {"resolved": False, "rel_return": None}

    import brain.outcomes as OUT
    monkeypatch.setattr(OUT, "label_thesis", _label, raising=False)
    monkeypatch.setitem(sys.modules, "brain.outcomes", OUT)


def _stub_ledger(monkeypatch, theses):
    fake = types.ModuleType("brain.ledger")
    fake.all_theses = lambda: theses
    monkeypatch.setattr(brain, "ledger", fake, raising=False)
    monkeypatch.setitem(sys.modules, "brain.ledger", fake)


def _stub_attribution(monkeypatch, bps_by_seat):
    """Fake brain.attribution.rollup so the net-negative damper is testable offline."""
    fake = types.ModuleType("brain.attribution")
    fake.rollup = lambda: {"seats": {s: {"attributed_bps": v} for s, v in bps_by_seat.items()}}
    monkeypatch.setattr(brain, "attribution", fake, raising=False)
    monkeypatch.setitem(sys.modules, "brain.attribution", fake)


def _dates_before(n, *, start=date(2024, 1, 2), step=40):
    """n decision dates `step` calendar days apart, all well before ASOF (fully elapsed)."""
    return [(start + timedelta(days=step * k)).isoformat() for k in range(n)]


def _arm(monkeypatch):
    monkeypatch.setenv("MASTERMIND_REPUTATION_WEIGHTING", "1")


def _disarm(monkeypatch):
    monkeypatch.delenv("MASTERMIND_REPUTATION_WEIGHTING", raising=False)


# ═══════════════════════════ REGIME-CONDITIONAL ═══════════════════════════
def test_streak_in_trend_is_cold_in_fresh_chop(monkeypatch, tmp_path):
    """SENTINEL is a genius in quad1 (TREND): 14 resolved OPPOSE calls that were all right. The live
    regime is now quad3 (CHOP) where it has ZERO grades. Its quad3 reputation must be COLD → influence
    weight 1.0 (nominal). The trend streak cannot buy influence in chop."""
    _arm(monkeypatch)
    committee_root = _root(monkeypatch, tmp_path)
    trend = {"quad": 1, "quad_name": "trend"}
    chop = {"quad": 3, "quad_name": "chop"}
    _set_live_regime(monkeypatch, tmp_path, chop)        # live = CHOP

    # 14 OPPOSE calls (high conf) made in TREND, all correct (name fell vs SPY)
    theses, rels = [], {}
    for i, d in enumerate(_dates_before(14)):
        tk = f"TR{i}"
        sd = committee_root / d / tk
        sd.mkdir(parents=True, exist_ok=True)
        (sd / "sentinel.json").write_text(json.dumps(
            {"agent": "sentinel", "stance": "OPPOSE", "raw_confidence": 0.9}))
        _strategist(committee_root, d, [], trend)        # regime-on-date = TREND
        theses.append({"entry_levels": {"ticker": tk}, "state_asof": d})
        rels[tk] = -0.05                                 # fell → OPPOSE was right
    _stub_ledger(monkeypatch, theses)
    _stub_rel(monkeypatch, rels)
    _stub_attribution(monkeypatch, {"sentinel": 500.0})

    # In TREND the seat is scoring (14 ≥ MIN_N) and reliable → influence may rise.
    trend_rel = reputation.regime_reliability("sentinel", "quad1", ASOF)
    assert trend_rel["n"] == 14 and trend_rel["status"] == "scoring"
    assert reputation.influence_weight("sentinel", "quad1", ASOF) >= 0.9

    # In the LIVE chop regime it has no grades → cold → nominal weight 1.0.
    chop_rel = reputation.regime_reliability("sentinel", asof=ASOF)   # defaults to live = chop
    assert chop_rel["n"] == 0 and chop_rel["status"] == "building"
    assert reputation.influence_weight("sentinel", asof=ASOF) == 1.0


# ═══════════════════════════ BOUNDED + CAPPED ═══════════════════════════
def test_influence_weight_is_bounded(monkeypatch, tmp_path):
    """Even a perfectly-calibrated, hugely-positive-attribution seat is bounded at W_CEIL=1.0 — the
    influence weight NEVER inflates a vote above nominal (subtract-only spirit)."""
    _arm(monkeypatch)
    committee_root = _root(monkeypatch, tmp_path)
    reg = {"quad": 1}
    _set_live_regime(monkeypatch, tmp_path, reg)
    theses, rels = [], {}
    for i, d in enumerate(_dates_before(13)):
        tk = f"P{i}"
        sd = committee_root / d / tk
        sd.mkdir(parents=True, exist_ok=True)
        (sd / "sentinel.json").write_text(json.dumps(
            {"agent": "sentinel", "stance": "OPPOSE", "raw_confidence": 0.5}))   # well-calibrated
        _strategist(committee_root, d, [], reg)
        theses.append({"entry_levels": {"ticker": tk}, "state_asof": d})
        rels[tk] = -0.05
    _stub_ledger(monkeypatch, theses)
    _stub_rel(monkeypatch, rels)
    _stub_attribution(monkeypatch, {"sentinel": 9999.0})

    w = reputation.influence_weight("sentinel", "quad1", ASOF)
    assert reputation.W_FLOOR <= w <= reputation.W_CEIL
    assert w == pytest.approx(reputation.W_CEIL)         # reliability 1.0 / mean_conf 0.5 → clamp to 1.0


def test_cap_influence_clamps_to_quorum_share():
    """No seat's effective vote may exceed MAX_QUORUM_SHARE, however high its weight/confidence."""
    assert reputation.cap_influence(1.0, 1.0) == pytest.approx(reputation.MAX_QUORUM_SHARE)
    assert reputation.cap_influence(1.0, 0.9) == pytest.approx(reputation.MAX_QUORUM_SHARE)
    # a small effective vote is passed through unclamped
    assert reputation.cap_influence(0.5, 0.2) == pytest.approx(0.1)
    # never negative / never above the cap
    assert 0.0 <= reputation.cap_influence(1.0, 5.0) <= reputation.MAX_QUORUM_SHARE


def test_net_negative_attribution_is_floored(monkeypatch, tmp_path):
    """A seat that has been NET-NEGATIVE on the cumulative attribution rollup is floored to W_FLOOR,
    even if its in-regime reliability looks good — a damper against rewarding a destructive seat."""
    _arm(monkeypatch)
    committee_root = _root(monkeypatch, tmp_path)
    reg = {"quad": 2}
    _set_live_regime(monkeypatch, tmp_path, reg)
    theses, rels = [], {}
    for i, d in enumerate(_dates_before(13)):
        tk = f"N{i}"
        sd = committee_root / d / tk
        sd.mkdir(parents=True, exist_ok=True)
        (sd / "sentinel.json").write_text(json.dumps(
            {"agent": "sentinel", "stance": "OPPOSE", "raw_confidence": 0.6}))
        _strategist(committee_root, d, [], reg)
        theses.append({"entry_levels": {"ticker": tk}, "state_asof": d})
        rels[tk] = -0.05                                 # in-regime reliability is strong
    _stub_ledger(monkeypatch, theses)
    _stub_rel(monkeypatch, rels)
    _stub_attribution(monkeypatch, {"sentinel": -1200.0})  # but it has DESTROYED value cumulatively

    assert reputation.influence_weight("sentinel", "quad2", ASOF) == reputation.W_FLOOR


# ═══════════════════════════ FLAG-OFF BYTE-IDENTICAL ═══════════════════════════
def test_flag_off_influence_weight_is_one(monkeypatch, tmp_path):
    """With the flag unset, influence_weight is 1.0 for any seat regardless of track record."""
    _disarm(monkeypatch)
    _root(monkeypatch, tmp_path)
    assert reputation.influence_weight("sentinel", "quad1", ASOF) == 1.0
    assert reputation.influence_weight("strategist", "quad3", ASOF) == 1.0


@pytest.mark.parametrize("stance,conf,exp_action,exp_scale", [
    ("OPPOSE", 0.9, "drop", 0.0),     # high-conf oppose → drop
    ("OPPOSE", 0.55, "trim", 0.5),    # low-conf oppose → halve
    ("CONDITIONAL", 0.7, "trim", 0.66),
    ("SUPPORT", 0.8, "confirm", 1.0),
])
def test_flag_off_nexus_byte_identical(monkeypatch, stance, conf, exp_action, exp_scale):
    """With the flag OFF, committee.nexus() reproduces the legacy branches EXACTLY: drop-bar 0.6,
    OPPOSE-trim 0.5, CONDITIONAL 0.66, SUPPORT 1.0 — byte-identical to pre-task synthesis."""
    _disarm(monkeypatch)
    bd = {"confirmed": True, "combined": 0.7}
    out = committee.nexus(bd, {"stance": stance, "confidence": conf, "strongest_bear": "x"})
    assert out["action"] == exp_action
    assert out["scale"] == exp_scale


def test_flag_off_nexus_drop_bar_is_exactly_point6(monkeypatch):
    """The 0.6 OPPOSE→drop threshold is unchanged when OFF: conf 0.6 drops, 0.59 trims."""
    _disarm(monkeypatch)
    bd = {"confirmed": True, "combined": 0.7}
    assert committee.nexus(bd, {"stance": "OPPOSE", "confidence": 0.6})["action"] == "drop"
    assert committee.nexus(bd, {"stance": "OPPOSE", "confidence": 0.59})["action"] == "trim"


# ═══════════════════════════ SUBTRACT-ONLY PRESERVED (flag ON) ═══════════════════════════
def _arm_scoring_sentinel(monkeypatch, tmp_path, *, raw_conf, attributed_bps=500.0, quad=1):
    """Build a regime-scoring, reliable SENTINEL and arm the flag, returning the influence weight."""
    _arm(monkeypatch)
    committee_root = _root(monkeypatch, tmp_path)
    reg = {"quad": quad}
    _set_live_regime(monkeypatch, tmp_path, reg)
    theses, rels = [], {}
    for i, d in enumerate(_dates_before(14)):
        tk = f"S{i}"
        sd = committee_root / d / tk
        sd.mkdir(parents=True, exist_ok=True)
        (sd / "sentinel.json").write_text(json.dumps(
            {"agent": "sentinel", "stance": "OPPOSE", "raw_confidence": raw_conf}))
        _strategist(committee_root, d, [], reg)
        theses.append({"entry_levels": {"ticker": tk}, "state_asof": d})
        rels[tk] = -0.05
    _stub_ledger(monkeypatch, theses)
    _stub_rel(monkeypatch, rels)
    _stub_attribution(monkeypatch, {"sentinel": attributed_bps})
    return reputation.influence_weight("sentinel", f"quad{quad}", ASOF)


def test_armed_high_influence_can_drop_on_lower_bar(monkeypatch, tmp_path):
    """A well-calibrated adversary may veto on slightly LOWER stated confidence than 0.6 — but the
    bar is floored at 0.45 (never trivial), and this only DE-RISKS (drop), never adds."""
    w = _arm_scoring_sentinel(monkeypatch, tmp_path, raw_conf=0.55)
    assert w == pytest.approx(reputation.W_CEIL)         # reliability 1.0 / mean_conf 0.55 → 1.0
    bd = {"confirmed": True, "combined": 0.7}
    # conf 0.55 < legacy 0.6 → would have TRIMMED before; armed high-influence → DROP.
    out = committee.nexus(bd, {"stance": "OPPOSE", "confidence": 0.55, "strongest_bear": "late"})
    assert out["action"] == "drop"
    assert out["scale"] == 0.0                           # still subtract-only — never additive


def test_armed_influence_never_raises_scale_above_ceiling(monkeypatch, tmp_path):
    """SUBTRACT-ONLY: armed influence can only DEEPEN a trim (scale ≤ 0.5) and never CONFIRM a name
    FORGE dropped. A confirm path stays ≤ 1.0; an OPPOSE-trim stays ≤ 0.5; FORGE-unconfirmed → drop."""
    _arm_scoring_sentinel(monkeypatch, tmp_path, raw_conf=0.5)
    bd = {"confirmed": True, "combined": 0.7}
    # OPPOSE just under the (lowered) bar still trims, and the trim is ≤ 0.5 (deeper, never higher).
    trim = committee.nexus(bd, {"stance": "OPPOSE", "confidence": 0.40})
    assert trim["action"] == "trim" and 0.0 < trim["scale"] <= 0.5
    # SUPPORT cannot be turned into >1.0 size.
    sup = committee.nexus(bd, {"stance": "SUPPORT", "confidence": 0.9})
    assert sup["action"] == "confirm" and sup["scale"] <= 1.0
    # FORGE did NOT confirm → committee can never rescue, no matter the influence weight.
    dropped = committee.nexus({"confirmed": False}, {"stance": "SUPPORT", "confidence": 0.9})
    assert dropped["action"] == "drop" and dropped["scale"] == 0.0


def test_armed_but_cold_seat_is_byte_identical(monkeypatch, tmp_path):
    """Flag ON but the seat is below MIN_N in this regime → influence_weight 1.0 → nexus identical to
    OFF. Cold-start inertia holds even when armed."""
    _arm(monkeypatch)
    committee_root = _root(monkeypatch, tmp_path)
    reg = {"quad": 1}
    _set_live_regime(monkeypatch, tmp_path, reg)
    # only 3 grades in-regime — below MIN_N
    theses, rels = [], {}
    for i, d in enumerate(_dates_before(3)):
        tk = f"F{i}"
        sd = committee_root / d / tk
        sd.mkdir(parents=True, exist_ok=True)
        (sd / "sentinel.json").write_text(json.dumps(
            {"agent": "sentinel", "stance": "OPPOSE", "raw_confidence": 0.9}))
        _strategist(committee_root, d, [], reg)
        theses.append({"entry_levels": {"ticker": tk}, "state_asof": d})
        rels[tk] = -0.05
    _stub_ledger(monkeypatch, theses)
    _stub_rel(monkeypatch, rels)
    _stub_attribution(monkeypatch, {"sentinel": 100.0})

    assert reputation.influence_weight("sentinel", "quad1", ASOF) == 1.0
    bd = {"confirmed": True, "combined": 0.7}
    assert committee.nexus(bd, {"stance": "OPPOSE", "confidence": 0.55})["action"] == "trim"
    assert committee.nexus(bd, {"stance": "OPPOSE", "confidence": 0.55})["scale"] == 0.5


def test_regime_reliability_unknown_seat_is_inert(monkeypatch, tmp_path):
    """A seat with no regime row-builder (forge/pm/timing) is inert here — nominal weight, building."""
    _arm(monkeypatch)
    _root(monkeypatch, tmp_path)
    rel = reputation.regime_reliability("forge", "quad1", ASOF)
    assert rel["status"] == "building" and rel["multiplier"] == 1.0
    assert reputation.influence_weight("forge", "quad1", ASOF) == 1.0


def test_never_raises_on_garbage(monkeypatch, tmp_path):
    """Exploding/garbage substrate → influence_weight degrades to nominal 1.0, never raises."""
    _arm(monkeypatch)
    committee_root = _root(monkeypatch, tmp_path)
    (committee_root).mkdir(parents=True, exist_ok=True)
    bad = committee_root / "not-a-date"
    bad.mkdir()
    (bad / "junk").write_text("{ not json")
    # no ledger / no outcomes stub installed → graders degrade, weight stays nominal
    assert reputation.influence_weight("sentinel", "quad1", ASOF) == 1.0
    assert reputation.regime_reliability("strategist", "quad1", ASOF)["status"] == "building"
