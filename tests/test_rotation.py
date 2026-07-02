"""tests/test_rotation.py — the deterministic DEF_SLEEVE rotation floor (W4, task B2).

INTENT-ONLY assertions (house rule): no live market state is pinned. Everything is exercised through
injected cfg / candidates so the suite is deterministic and never touches the doctrine.yml lru_cache
or the vendored artifacts.

Coverage (task B2 test spec):
  * DEF_SLEEVE_MAX=0 → byte-identical book (empty sleeve, the control arm).
  * fragility ladder → monotone def budgets.
  * candidates empty → sleeve empty (no-op, never raises).
  * cluster/caps interaction (theme_id='DEFENSIVE_<archetype>' survives enforce_book_caps).
  * the gross invariant (redeploys freed cash; never exceeds the un-flexed engine ceiling).
  * the floor is respected / exposed for the judgment layer.
"""
from __future__ import annotations

import copy

import pytest

from portfolio import rotation as R


# ---------------------------------------------------------------------------
# cfg helpers — build an armed / control cfg WITHOUT touching doctrine.yml
# ---------------------------------------------------------------------------

def _cfg(max_=0.35, **over):
    c = dict(R._DEF_SLEEVE_FALLBACK)
    c["max"] = max_
    c.update(over)
    return c


def _armed_module(monkeypatch, max_=0.35, midpoint=0.50, **over):
    """Force rotation to see an ARMED cfg + a fixed midpoint (no doctrine.yml I/O)."""
    cfg = _cfg(max_, **over)
    monkeypatch.setattr(R, "_cfg", lambda: cfg)
    monkeypatch.setattr(R, "_budget_midpoint", lambda: midpoint)
    return cfg


_CANDS = [
    {"ticker": "XLV", "source": "playbook", "archetype": "quality_defensive"},
    {"ticker": "TLT", "source": "playbook", "archetype": "duration"},
    {"ticker": "SGOV", "source": "playbook", "archetype": "ballast_cash"},
]


def _book(lead=0.30, conv=0.10):
    """A book de-grossed below the midpoint (leadership 0.30 vs midpoint 0.50 → 0.20 freed)."""
    out = []
    if lead:
        out.append({"ticker": "SMH", "sleeve": "leadership", "weight": lead})
    if conv:
        out.append({"ticker": "NVDA", "sleeve": "conviction", "weight": conv})
    return out


# ---------------------------------------------------------------------------
# 1. CONTROL ARM — DEF_SLEEVE_MAX = 0 → byte-identical book
# ---------------------------------------------------------------------------

class TestControlArm:
    def test_max_zero_default_is_inert(self, monkeypatch):
        """The DOCTRINE DEFAULT (max=0) yields no legs regardless of how fragile the tape is."""
        monkeypatch.setattr(R, "_cfg", lambda: _cfg(0.0))
        out = R.build_def_sleeve(_book(), {"state": "risk_off"},
                                 {"confidence": 0.1, "transition_state": "WEAKENING"},
                                 candidates=_CANDS)
        assert out["legs"] == []
        assert out["def_actual"] == 0.0
        assert "inert" in out["reason"] or "DEF_SLEEVE_MAX=0" in out["reason"]

    def test_max_zero_book_unchanged(self, monkeypatch):
        """Byte-identical: the input book is never mutated and no legs are produced."""
        monkeypatch.setattr(R, "_cfg", lambda: _cfg(0.0))
        book = _book()
        before = copy.deepcopy(book)
        out = R.build_def_sleeve(book, {"state": "risk_off"},
                                 {"confidence": 0.1, "transition_state": "WEAKENING"},
                                 candidates=_CANDS)
        assert book == before          # not mutated
        assert out["legs"] == []

    def test_config_cannot_uncap_past_armed_ceiling(self):
        """Even a config over-setting max beyond the armed ceiling is hard-clamped (invariant)."""
        # exercise the real _cfg clamp path via a monkeypatched doctrine loader
        import bot.doctrine_config as dc
        orig = dc.load_doctrine
        try:
            dc.load_doctrine = lambda: {"def_sleeve": {"max": 0.99, "armed_ceiling": 0.35}}
            cfg = R._cfg()
            assert cfg["max"] == 0.35   # clamped, never 0.99
        finally:
            dc.load_doctrine = orig


# ---------------------------------------------------------------------------
# 2. FRAGILITY LADDER — monotone def budgets
# ---------------------------------------------------------------------------

class TestFragilityLadder:
    def test_signal_monotone_in_dwell(self):
        cfg = _cfg()
        bi = {"confidence": 0.5, "transition_state": "STABLE"}
        s_on = R.fragility_signal({"state": "risk_on"}, bi, cfg)
        s_caut = R.fragility_signal({"state": "caution"}, bi, cfg)
        s_off = R.fragility_signal({"state": "risk_off"}, bi, cfg)
        assert s_on < s_caut < s_off

    def test_signal_monotone_in_low_confidence(self):
        cfg = _cfg()
        rs = {"state": "caution"}
        s_hi = R.fragility_signal(rs, {"confidence": 0.9, "transition_state": "STABLE"}, cfg)
        s_lo = R.fragility_signal(rs, {"confidence": 0.1, "transition_state": "STABLE"}, cfg)
        assert s_lo > s_hi

    def test_weakening_bump_raises_signal(self):
        cfg = _cfg()
        rs = {"state": "caution"}
        s_stable = R.fragility_signal(rs, {"confidence": 0.5, "transition_state": "STABLE"}, cfg)
        s_weak = R.fragility_signal(rs, {"confidence": 0.5, "transition_state": "WEAKENING"}, cfg)
        assert s_weak > s_stable

    def test_def_budget_monotone_across_the_ladder(self):
        """def_budget = max·signal → strictly non-decreasing as the tape gets more fragile."""
        cfg = _cfg(0.35)
        b_on = R.def_budget({"state": "risk_on"}, {"confidence": 0.8, "transition_state": "STABLE"}, cfg)
        b_caut = R.def_budget({"state": "caution"}, {"confidence": 0.5, "transition_state": "STABLE"}, cfg)
        b_off = R.def_budget({"state": "risk_off"}, {"confidence": 0.2, "transition_state": "WEAKENING"}, cfg)
        assert b_on < b_caut < b_off
        assert b_off <= 0.35 + 1e-9   # never exceeds max

    def test_signal_clamped_to_unit(self):
        """An extreme read can never push the signal past 1.0."""
        cfg = _cfg()
        s = R.fragility_signal({"state": "risk_off"},
                               {"confidence": 0.0, "transition_state": "WEAKENING"}, cfg)
        assert 0.0 <= s <= 1.0


# ---------------------------------------------------------------------------
# 3. MISSING DATA SHRINKS, NEVER INFLATES (the invariant)
# ---------------------------------------------------------------------------

class TestMissingDataShrinks:
    def test_missing_confidence_contributes_zero(self):
        """Missing confidence → treated as 1.0 → (1-conf)=0 term (never a phantom inflation)."""
        cfg = _cfg()
        rs = {"state": "risk_on"}
        s_missing = R.fragility_signal(rs, {"transition_state": "STABLE"}, cfg)
        s_full_conf = R.fragility_signal(rs, {"confidence": 1.0, "transition_state": "STABLE"}, cfg)
        assert s_missing == s_full_conf == 0.0

    def test_unknown_dwell_state_is_zero(self):
        cfg = _cfg()
        s = R.fragility_signal({"state": "banana"}, {"confidence": 1.0, "transition_state": "STABLE"}, cfg)
        assert s == 0.0

    def test_none_inputs_never_raise(self):
        cfg = _cfg(0.35)
        assert R.fragility_signal(None, None, cfg) == 0.0
        out = R.build_def_sleeve(_book(), None, None, candidates=_CANDS)
        assert isinstance(out, dict) and "legs" in out


# ---------------------------------------------------------------------------
# 4. CANDIDATES EMPTY → SLEEVE EMPTY (no-op)
# ---------------------------------------------------------------------------

class TestEmptyCandidates:
    def test_empty_candidates_empty_sleeve(self, monkeypatch):
        _armed_module(monkeypatch)
        out = R.build_def_sleeve(_book(), {"state": "risk_off"},
                                 {"confidence": 0.1, "transition_state": "WEAKENING"},
                                 candidates=[])
        assert out["legs"] == []
        assert out["def_actual"] == 0.0
        assert "candidate" in out["reason"]

    def test_empty_candidates_never_raises(self, monkeypatch):
        _armed_module(monkeypatch)
        # None candidates + a broken generator import must still degrade to []
        out = R.build_def_sleeve(_book(), {"state": "risk_off"},
                                 {"confidence": 0.1, "transition_state": "WEAKENING"},
                                 candidates=None)
        assert isinstance(out["legs"], list)   # whatever the live generator returns, never a raise


# ---------------------------------------------------------------------------
# 5. THE GROSS INVARIANT (the load-bearing one)
# ---------------------------------------------------------------------------

class TestGrossInvariant:
    def test_redeploys_freed_cash_up_to_unflexed_ceiling(self, monkeypatch):
        """gross ON <= un-flexed engine gross (conv + midpoint); cash floor respected."""
        _armed_module(monkeypatch, max_=0.35, midpoint=0.50)
        book = _book(lead=0.30, conv=0.10)   # freed = 0.50-0.30 = 0.20
        out = R.build_def_sleeve(book, {"state": "risk_off"},
                                 {"confidence": 0.2, "transition_state": "WEAKENING"},
                                 candidates=_CANDS)
        gross_off = sum(p["weight"] for p in book)
        gross_on = gross_off + out["def_actual"]
        unflexed_ceiling = 0.10 + 0.50      # conv gross + midpoint budget
        assert gross_on <= unflexed_ceiling + 1e-9
        assert (1.0 - gross_on) >= 0.05 - 1e-9   # cash floor
        # and it DID redeploy (the whole point) — gross_on strictly above gross_off here
        assert gross_on > gross_off

    def test_headroom_is_leadership_degross(self, monkeypatch):
        """The freed headroom equals the leadership de-gross when cash is not the binding limit."""
        _armed_module(monkeypatch, max_=0.35, midpoint=0.50)
        book = _book(lead=0.30, conv=0.10)
        room = R._headroom(book, {}, R._cfg())
        assert abs(room - 0.20) < 1e-9        # midpoint 0.50 - lead 0.30

    def test_cash_floor_binds_when_book_is_full(self, monkeypatch):
        """A nearly-full book leaves little cash headroom → the sleeve is bounded by the floor."""
        _armed_module(monkeypatch, max_=0.35, midpoint=0.50)
        # leadership 0.30 (freed 0.20) but conviction 0.60 → gross 0.90 → cash headroom = 1-.05-.90=.05
        book = [{"ticker": "SMH", "sleeve": "leadership", "weight": 0.30},
                {"ticker": "NVDA", "sleeve": "conviction", "weight": 0.60}]
        out = R.build_def_sleeve(book, {"state": "risk_off"},
                                 {"confidence": 0.2, "transition_state": "WEAKENING"},
                                 candidates=_CANDS)
        gross_on = sum(p["weight"] for p in book) + out["def_actual"]
        assert (1.0 - gross_on) >= 0.05 - 1e-9
        assert out["def_actual"] <= 0.05 + 1e-9   # cash floor is the binding limit here

    def test_no_degross_no_sleeve(self, monkeypatch):
        """Leadership at/above midpoint → no freed cash → no sleeve (never levers new gross)."""
        _armed_module(monkeypatch, max_=0.35, midpoint=0.50)
        book = _book(lead=0.50, conv=0.10)   # leadership AT the midpoint → nothing freed
        out = R.build_def_sleeve(book, {"state": "risk_off"},
                                 {"confidence": 0.2, "transition_state": "WEAKENING"},
                                 candidates=_CANDS)
        assert out["legs"] == []
        assert out["def_actual"] == 0.0
        assert "headroom" in out["reason"]


# ---------------------------------------------------------------------------
# 6. LEG CONSTRUCTION + CLUSTER/CAPS INTERACTION
# ---------------------------------------------------------------------------

class TestLegsAndCaps:
    def test_theme_id_is_defensive_archetype(self, monkeypatch):
        _armed_module(monkeypatch)
        out = R.build_def_sleeve(_book(), {"state": "risk_off"},
                                 {"confidence": 0.2, "transition_state": "WEAKENING"},
                                 candidates=_CANDS)
        assert out["legs"]
        for leg in out["legs"]:
            assert leg["theme_id"].startswith("DEFENSIVE_")
            assert leg["sleeve"] == "defensive"
            assert leg["def_sleeve"] is True

    def test_equal_weight_across_candidates(self, monkeypatch):
        _armed_module(monkeypatch)
        out = R.build_def_sleeve(_book(), {"state": "risk_off"},
                                 {"confidence": 0.2, "transition_state": "WEAKENING"},
                                 candidates=_CANDS)
        weights = [leg["weight"] for leg in out["legs"]]
        # frozen equal-weight prior → all legs within a rounding cent of each other
        assert max(weights) - min(weights) <= 0.0001

    def test_defensive_legs_survive_enforce_book_caps(self, monkeypatch):
        """XLV sits in no semis cluster — a defensive pick must not accidentally breach a cluster."""
        from portfolio.sleeves import enforce_book_caps
        _armed_module(monkeypatch)
        out = R.build_def_sleeve(_book(), {"state": "risk_off"},
                                 {"confidence": 0.2, "transition_state": "WEAKENING"},
                                 candidates=[{"ticker": "XLV", "archetype": "quality_defensive"}])
        book = _book() + out["legs"]
        # inject a benign cluster_fn: XLV is its own singleton, SMH is semis_ai
        def _cluster(t):
            return {"SMH": "semis_ai"}.get(str(t).upper(), str(t).upper())
        capped = enforce_book_caps(book, cluster_fn=_cluster)
        xlv = [p for p in capped["positions"] if p["ticker"] == "XLV"]
        assert xlv, "XLV should still be in the book"
        # no cluster breach was raised on XLV (it is a singleton defensive)
        assert not any(b["kind"] == "cluster" and b["subject"] == "XLV"
                       for b in capped["breaches"])

    def test_zero_weight_candidate_dropped(self):
        legs = R._make_legs(0.20, [{"ticker": "XLV", "archetype": "quality_defensive"}],
                            {"XLV": 0.5, "TLT": 0.0})
        tickers = {leg["ticker"] for leg in legs}
        assert "TLT" not in tickers   # zero frozen weight → dropped


# ---------------------------------------------------------------------------
# 7. floor_legs() — the contract the judgment layer respects
# ---------------------------------------------------------------------------

class TestFloorContract:
    def test_floor_empty_when_disabled(self, monkeypatch):
        monkeypatch.setattr(R, "_cfg", lambda: _cfg(0.0))
        legs = R.floor_legs(_book(), {"state": "risk_off"},
                            {"confidence": 0.2, "transition_state": "WEAKENING"},
                            candidates=_CANDS)
        assert legs == []   # no deterministic floor when the sleeve is off

    def test_floor_matches_build_when_armed(self, monkeypatch):
        _armed_module(monkeypatch)
        args = (_book(), {"state": "risk_off"},
                {"confidence": 0.2, "transition_state": "WEAKENING"})
        built = R.build_def_sleeve(*args, candidates=_CANDS)["legs"]
        floor = R.floor_legs(*args, candidates=_CANDS)
        assert [l["ticker"] for l in floor] == [l["ticker"] for l in built]
        assert [l["weight"] for l in floor] == [l["weight"] for l in built]

    def test_floor_never_raises(self, monkeypatch):
        # even with a book that trips a bad path, floor_legs degrades to []
        _armed_module(monkeypatch)
        assert isinstance(R.floor_legs(None, None, None, candidates=None), list)


# ---------------------------------------------------------------------------
# 8. NEVER RAISES — the module-wide guarantee
# ---------------------------------------------------------------------------

class TestNeverRaises:
    @pytest.mark.parametrize("book", [None, [], [{}], [{"weight": "bad"}]])
    def test_build_tolerates_junk_books(self, monkeypatch, book):
        _armed_module(monkeypatch)
        out = R.build_def_sleeve(book, {"state": "risk_off"},
                                 {"confidence": 0.2, "transition_state": "WEAKENING"},
                                 candidates=_CANDS)
        assert isinstance(out, dict) and "legs" in out

    def test_junk_candidates_tolerated(self, monkeypatch):
        _armed_module(monkeypatch)
        out = R.build_def_sleeve(_book(), {"state": "risk_off"},
                                 {"confidence": 0.2, "transition_state": "WEAKENING"},
                                 candidates=[{}, {"ticker": None}, "notadict", {"ticker": "XLV"}])
        assert isinstance(out["legs"], list)
