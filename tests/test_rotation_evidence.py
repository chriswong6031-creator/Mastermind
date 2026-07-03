"""tests/test_rotation_evidence.py — ROTATION-EVIDENCE wiring (Incident Wave W-I, task 6).

The incident's DETECTION gap: the deployed W0-W4 RESPONSE stack cuts correctly once fed a truthful
read, but every book sat on a wrong-but-fresh Goldilocks/expanding label the bot TRUSTED — with no
machinery to COUNT the dashboard's own disagreeing planes.  This wave builds that machinery: a
shrink-only AGREEMENT count over four disagreement sources ({nowcast doubt, liquidity stress/hollow,
radar caution, defensive-RS crossover}), routed into two ALREADY-VALIDATED levers:

  (1) regime_frame.budget()      — a shrink-only DAMP on the flex (2 agree ×0.9, 3+ ×0.8), floored at
                                   the 0.40 clamp.
  (2) rotation.fragility_signal() — a +0.15-per-source-beyond-the-first LIFT on the DEF_SLEEVE (the
                                   '7% when it should be 23%' unthrottle).  With DEF_SLEEVE_MAX=0
                                   (default) this changes NOTHING live — asserted byte-identical here.

INVARIANT (governs every assertion): missing/stale/wrong data may coarsen / freeze / SHRINK — never
un-cap, raise authority, or flip direction.  A MISSING source counts as NON-agreeing (never as
evidence).  On a calm tape (0 agreement) the damp is 1.0 and the lift is 0.0 → BYTE-IDENTICAL to
pre-W-I.  Every source degrades to None (absent) on missing data — the damp/lift are shrink-only.

TEST STYLE (house rule): INTENT-ONLY — no live market state is pinned.  Every source verdict is
INJECTED (tri-state True/False/None) so the shared mutating vendor store is never live-read; the
incident-replay class injects trimmed FIXTURES (copied into tests/fixtures/) rather than live data.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import bot  # noqa: F401  -> vendor/macro onto sys.path
from brain import regime_frame as RF
from portfolio import rotation as ROT

_FIX = Path(__file__).resolve().parent / "fixtures"
_INCIDENT = _FIX.parent / "incident_replays" / "fixtures" / "2026-07-02-semis-breakdown"


# ───────────────────────────────────────────────────────────────────────────────────────────────────
# helpers — build a regime file + evidence dicts without touching the live vendor store
# ───────────────────────────────────────────────────────────────────────────────────────────────────

def _regime_file(tmp_path: Path, payload: dict) -> Path:
    p = tmp_path / "regime_latest.json"
    p.write_text(json.dumps(payload))
    return p


def _patch_us(monkeypatch, tmp_path: Path, payload: dict) -> None:
    monkeypatch.setitem(RF._REGION_PATHS, "us", _regime_file(tmp_path, payload))


# The incident-shaped regime the bot TRUSTED on 07-01: conf 0.327, STABLE, flip_margin 0.05 (<0.15).
_INCIDENT_REGIME = {
    "confidence": 0.327,
    "transition_state": "STABLE",
    "flip_condition": {"margin": 0.05},
    "quad": "Q1",
    "quad_name": "Goldilocks",
}


def _ev(n_true: int) -> dict:
    """A rotation_evidence dict with exactly ``n_true`` agreeing sources (rest absent → None)."""
    keys = ("nowcast_doubt", "liquidity_stress", "radar_caution", "defensive_rs_cross")
    kw = {k: (True if i < n_true else None) for i, k in enumerate(keys)}
    return RF.rotation_evidence(**kw)


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# 1 — rotation_evidence(): agreement counting is monotone; missing NEVER counts
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

class TestAgreementCount:
    def test_all_absent_is_zero(self):
        ev = RF.rotation_evidence()
        assert ev["n_agree"] == 0
        assert ev["sources"] == {
            "nowcast_doubt": None, "liquidity_stress": None,
            "radar_caution": None, "defensive_rs_cross": None,
        }

    def test_true_sources_count(self):
        assert RF.rotation_evidence(nowcast_doubt=True)["n_agree"] == 1
        assert RF.rotation_evidence(nowcast_doubt=True, liquidity_stress=True)["n_agree"] == 2
        assert RF.rotation_evidence(
            nowcast_doubt=True, liquidity_stress=True,
            radar_caution=True, defensive_rs_cross=True)["n_agree"] == 4

    def test_false_and_none_never_agree(self):
        """Only affirmative True agrees — a definite False AND an absent None both count 0."""
        ev = RF.rotation_evidence(nowcast_doubt=False, liquidity_stress=None,
                                  radar_caution=False, defensive_rs_cross=None)
        assert ev["n_agree"] == 0

    def test_non_bool_junk_is_absent_never_true(self):
        """A stray non-bool (e.g. a dict) must degrade to absent — never fabricate a True agreement."""
        ev = RF.rotation_evidence(nowcast_doubt={"stray": 1}, liquidity_stress=1)
        assert ev["n_agree"] == 0
        assert ev["sources"]["nowcast_doubt"] is None
        # a bare int 1 is not a bool → absent (never counted)
        assert ev["sources"]["liquidity_stress"] is None


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# 2 — budget() evidence DAMP: shrink-only, floored, byte-identical on calm
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

class TestBudgetDamp:
    def test_calm_is_byte_identical_to_pre_wi(self, monkeypatch, tmp_path):
        """0 agreement (or no evidence at all) → D=1.0 → budget byte-identical to the un-damped call."""
        _patch_us(monkeypatch, tmp_path, _INCIDENT_REGIME)
        base = RF.budget("us")["lead_budget"]
        assert RF.budget("us", evidence=None)["lead_budget"] == base
        assert RF.budget("us", evidence=_ev(0))["lead_budget"] == base
        assert RF.budget("us", evidence=_ev(1))["lead_budget"] == base  # 1 agree still D=1.0

    def test_two_agree_damps_flex_by_0p9(self, monkeypatch, tmp_path):
        _patch_us(monkeypatch, tmp_path, _INCIDENT_REGIME)
        out = RF.budget("us", evidence=_ev(2))
        # flex = 0.20·0.327·1.0(STABLE)·0.75(flip<0.15) ; damped ×0.9
        expected = 0.40 + 0.20 * 0.327 * 1.0 * 0.75 * 0.9
        assert out["lead_budget"] == pytest.approx(expected)
        assert out["inputs"]["D"] == pytest.approx(0.9)
        assert out["inputs"]["evidence_n_agree"] == 2

    def test_three_plus_agree_damps_flex_by_0p8(self, monkeypatch, tmp_path):
        _patch_us(monkeypatch, tmp_path, _INCIDENT_REGIME)
        for n in (3, 4):
            out = RF.budget("us", evidence=_ev(n))
            expected = 0.40 + 0.20 * 0.327 * 1.0 * 0.75 * 0.8
            assert out["lead_budget"] == pytest.approx(expected), f"n_agree={n}"
            assert out["inputs"]["D"] == pytest.approx(0.8)

    def test_damp_is_monotone_shrinking(self, monkeypatch, tmp_path):
        _patch_us(monkeypatch, tmp_path, _INCIDENT_REGIME)
        b0 = RF.budget("us", evidence=_ev(0))["lead_budget"]
        b2 = RF.budget("us", evidence=_ev(2))["lead_budget"]
        b3 = RF.budget("us", evidence=_ev(3))["lead_budget"]
        assert b0 >= b2 >= b3, "the evidence damp must only ever SHRINK the budget"

    def test_damp_never_below_the_0p40_floor(self, monkeypatch, tmp_path):
        """Even a maximally-damped flex re-clamps at the 0.40 floor — never below."""
        payload = {"confidence": 1.0, "transition_state": "DETERIORATING",
                   "flip_condition": {"margin": 0.01}}
        _patch_us(monkeypatch, tmp_path, payload)
        for n in range(5):
            lb = RF.budget("us", evidence=_ev(n))["lead_budget"]
            assert lb >= 0.40 - 1e-12, f"n_agree={n} damped below the floor: {lb}"

    def test_damp_can_only_lower_not_raise(self, monkeypatch, tmp_path):
        """A very-high-confidence tape at the ceiling: the damp still only lowers it."""
        payload = {"confidence": 5.0, "transition_state": "STABLE",
                   "flip_condition": {"margin": 0.40}}  # clamps to conf 1.0 → ceil 0.60
        _patch_us(monkeypatch, tmp_path, payload)
        assert RF.budget("us", evidence=_ev(0))["lead_budget"] == pytest.approx(0.60)
        assert RF.budget("us", evidence=_ev(3))["lead_budget"] < 0.60

    def test_inputs_expose_evidence_sources(self, monkeypatch, tmp_path):
        _patch_us(monkeypatch, tmp_path, _INCIDENT_REGIME)
        out = RF.budget("us", evidence=_ev(3))
        assert out["inputs"]["evidence_sources"]["nowcast_doubt"] is True
        assert out["inputs"]["evidence_sources"]["defensive_rs_cross"] is None

    def test_missing_frame_midpoint_untouched_by_evidence(self, monkeypatch, tmp_path):
        """A missing regime degrades to the 0.50 midpoint; the damp does NOT further move it (no flex
        to damp) — but D and n_agree are still surfaced for the runlog."""
        _patch_us(monkeypatch, tmp_path, {"transition_state": "STABLE"})  # no confidence
        out = RF.budget("us", evidence=_ev(3))
        assert out["lead_budget"] == pytest.approx(0.50)
        assert out["inputs"]["D"] == pytest.approx(0.8)
        assert out["inputs"]["evidence_n_agree"] == 3


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# 3 — fragility_signal() LIFT: +0.15/source beyond the first; DEF_SLEEVE_MAX=0 byte-identical
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

class TestFragilityLift:
    _RS = {"state": "caution"}
    _BI = {"confidence": 0.327, "transition_state": "STABLE"}

    def test_no_evidence_is_byte_identical(self):
        base = ROT.fragility_signal(self._RS, self._BI)
        assert ROT.fragility_signal(self._RS, self._BI, evidence=None) == base
        assert ROT.fragility_signal(self._RS, self._BI, evidence=_ev(0)) == base
        assert ROT.fragility_signal(self._RS, self._BI, evidence=_ev(1)) == base  # 1 → no lift

    def test_lift_is_0p15_per_source_beyond_first(self):
        base = ROT.fragility_signal(self._RS, self._BI)
        f2 = ROT.fragility_signal(self._RS, self._BI, evidence=_ev(2))
        f3 = ROT.fragility_signal(self._RS, self._BI, evidence=_ev(3))
        assert f2 == pytest.approx(min(1.0, base + 0.15))
        assert f3 == pytest.approx(min(1.0, base + 0.30))

    def test_lift_clamped_at_one(self):
        rs = {"state": "risk_off"}                     # dwell 1.0 → high base already
        bi = {"confidence": 0.0, "transition_state": "WEAKENING"}
        assert ROT.fragility_signal(rs, bi, evidence=_ev(4)) == pytest.approx(1.0)

    def test_lift_only_raises_never_lowers(self):
        base = ROT.fragility_signal(self._RS, self._BI)
        for n in range(5):
            assert ROT.fragility_signal(self._RS, self._BI, evidence=_ev(n)) >= base

    def test_def_sleeve_max0_byte_identical_regardless_of_evidence(self):
        """The MODULE default (DEF_SLEEVE_MAX=0) → def_budget 0 for ANY evidence — nothing live changes."""
        assert ROT.def_budget(self._RS, self._BI) == 0.0
        assert ROT.def_budget(self._RS, self._BI, evidence=_ev(4)) == 0.0

    def test_build_def_sleeve_max0_byte_identical(self):
        book = [{"ticker": "SMH", "sleeve": "leadership", "weight": 0.30}]
        cands = [{"ticker": "XLV", "archetype": "quality_defensive"}]
        d_noev = ROT.build_def_sleeve(book, self._RS, self._BI, candidates=cands)
        d_ev = ROT.build_def_sleeve(book, self._RS, self._BI, candidates=cands, evidence=_ev(4))
        assert d_noev["legs"] == [] and d_ev["legs"] == []
        assert d_noev["def_actual"] == d_ev["def_actual"] == 0.0

    def test_armed_sleeve_grows_with_evidence(self, monkeypatch):
        """When ARMED (max>0), evidence lifts the fragility → a LARGER def budget (the unthrottle)."""
        armed = dict(ROT._DEF_SLEEVE_FALLBACK)
        armed["max"] = 0.35
        monkeypatch.setattr(ROT, "_cfg", lambda: armed)
        monkeypatch.setattr(ROT, "_budget_midpoint", lambda: 0.50)
        book = [{"ticker": "SMH", "sleeve": "leadership", "weight": 0.20}]  # leaves headroom
        cands = [{"ticker": "XLV", "archetype": "quality_defensive"},
                 {"ticker": "XLU", "archetype": "sector_rotation"}]
        d0 = ROT.build_def_sleeve(book, self._RS, self._BI, candidates=cands)
        d4 = ROT.build_def_sleeve(book, self._RS, self._BI, candidates=cands, evidence=_ev(4))
        assert d4["def_budget"] > d0["def_budget"], "evidence must unthrottle the armed def budget"


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# 4 — GUARD RAILS: calm-tape invariance + over_degross floor recompute off the DAMPED budget
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

class TestGuardRails:
    def test_calm_fixture_budget_and_def_sleeve_byte_identical(self, monkeypatch, tmp_path):
        """A calm fixture (no doubt, benign liquidity, no radar caution, no RS cross) → 0 agreement →
        budget AND def_sleeve byte-identical to the pre-W-I (no-evidence) path."""
        _patch_us(monkeypatch, tmp_path, _INCIDENT_REGIME)
        calm = RF.rotation_evidence(
            nowcast_doubt=False, liquidity_stress=False,
            radar_caution=False, defensive_rs_cross=False)
        assert calm["n_agree"] == 0
        # budget
        assert RF.budget("us", evidence=calm)["lead_budget"] == RF.budget("us")["lead_budget"]
        # def sleeve (module default MAX=0 → both empty; also assert the fragility is untouched)
        rs, bi = {"state": "risk_on"}, {"confidence": 0.60, "transition_state": "STABLE"}
        assert ROT.fragility_signal(rs, bi, evidence=calm) == ROT.fragility_signal(rs, bi)

    def test_over_degross_floor_recomputes_off_damped_budget(self):
        """The over_degross tripwire's floor = floor_frac · lead_budget.  Because phase2 passes the
        DAMPED lead_budget, the floor tracks the damp — a book that is legal against the UN-damped
        budget can still be legal against the (lower) damped budget: NO false alarm on a de-grossed
        book whose gross fell alongside the budget.  Asserted directly against the tripwire."""
        from portfolio import sleeves as SL
        # A leadership book whose gross was itself damped down with the budget.
        undamped_budget = 0.445
        damped_budget = 0.445 * 0.8  # 3+ agree
        # gross that is comfortably above the DAMPED floor but below the UNDAMPED floor
        frac = SL.leadership_caps_cfg()["offensive_gross_floor_frac"]
        gross = frac * damped_budget + 0.01
        book = [{"ticker": "SMH", "sleeve": "leadership", "weight": gross}]
        tw_undamped = SL.offensive_gross_tripwire(book, undamped_budget, parabolic_veto_fired=False)
        tw_damped = SL.offensive_gross_tripwire(book, damped_budget, parabolic_veto_fired=False)
        # Against the undamped (higher) budget the same gross MIGHT breach; against the damped budget
        # (the one phase2 actually passes) it must NOT — the floor moved down WITH the budget.
        assert tw_damped["breached"] is False, (
            "the over_degross floor must recompute off the damped budget (no false alarm)")
        assert tw_damped["floor"] < tw_undamped["floor"], (
            "damping the budget must lower the over_degross floor in lock-step")

    def test_over_degross_still_fires_on_a_real_breach(self):
        """Sanity: the tripwire still catches a genuinely over-de-grossed book against the damped
        budget (the damp coarsens the floor, it does not disable the alarm)."""
        from portfolio import sleeves as SL
        damped_budget = 0.445 * 0.8
        frac = SL.leadership_caps_cfg()["offensive_gross_floor_frac"]
        gross = frac * damped_budget - 0.02  # clearly below even the damped floor
        book = [{"ticker": "SMH", "sleeve": "leadership", "weight": max(0.0, gross)}]
        tw = SL.offensive_gross_tripwire(book, damped_budget, parabolic_veto_fired=False)
        assert tw["breached"] is True


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# 5 — SOURCE degrade-safety: every reader degrades to None (absent) on missing data
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

class TestSourceDegradeSafety:
    def test_nowcast_source_tri_state(self):
        assert RF._nowcast_doubt_source({"stance": "strong-doubt", "applies": True}) is True
        assert RF._nowcast_doubt_source({"stance": "doubt", "applies": True}) is True
        assert RF._nowcast_doubt_source({"stance": "confirm", "applies": True}) is False
        # applies==False (defensive label no-op) is NOT a doubt
        assert RF._nowcast_doubt_source({"stance": "doubt", "applies": False}) is False
        assert RF._nowcast_doubt_source({}) is None
        assert RF._nowcast_doubt_source("not a dict") is None

    def test_liquidity_source_tri_state(self):
        assert RF._liquidity_stress_source({"label": "stress-expansion"}) is True
        assert RF._liquidity_stress_source({"label": "neutral-hollow"}) is True
        assert RF._liquidity_stress_source({"label": "benign-expansion"}) is False
        assert RF._liquidity_stress_source({"label": "contracting"}) is False
        # 'unknown' (cannot even measure liquidity) is UNDETERMINABLE — never counts
        assert RF._liquidity_stress_source({"label": "unknown"}) is None
        assert RF._liquidity_stress_source({}) is None

    def test_radar_source_tri_state(self):
        assert RF._radar_caution_source("caution") is True
        assert RF._radar_caution_source("risk_off") is True
        assert RF._radar_caution_source("risk-off") is True
        assert RF._radar_caution_source("risk_on") is False
        assert RF._radar_caution_source("neutral") is False

    def test_radar_source_missing_log_degrades_to_none(self, monkeypatch, tmp_path):
        """The incident's BLINDED-FEED failure: a missing radar must degrade to None (non-agreeing) —
        never a fabricated caution AND never a fabricated all-clear."""
        monkeypatch.setattr(RF, "_RADAR_FORWARD_LOG", tmp_path / "does_not_exist.jsonl", raising=False)
        # no injected state, no risk_prior on this base, missing log → None
        assert RF._radar_caution_source() is None

    def test_defensive_rs_source_tri_state(self):
        assert RF._defensive_rs_source(True) is True
        assert RF._defensive_rs_source(False) is False
        # a series_fn that yields no usable names → crossed None → None
        assert RF._defensive_rs_source(series_fn=lambda tk: None) is None


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# 6 — INCIDENT REPLAY EXTENSION: the composed stack on 07-01 inputs
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

class TestIncidentReplay:
    """Compose the FULL evidence stack from the June-July fixture set and assert the 07-01 posture.

    Sources on 07-01 (each computed from a trimmed fixture, never live data):
      * liquidity  → stress-expansion (RRP exhausted $6.4bn + mechanical composition)
      * nowcast    → strong-doubt (all 3 price legs fired the day before the SMH rebuy)
      * defensive-RS → crossed (XLV/XLU/XLP leading SMH/XLK, diff > 0)
      * radar      → caution (the dashboard's own risk_radar/forward_log, restored to the sparse set)
    => 4 agreeing sources => damp ×0.8, lift +0.45.
    """

    def _series_fn(self):
        import pandas as pd
        pdir = _FIX / "regime_nowcast" / "incident_2026_0607"

        def fn(tk):
            p = pdir / f"{tk}.csv"
            if not p.exists():
                return None
            df = pd.read_csv(p)
            df["date"] = pd.to_datetime(df["date"])
            return df.set_index("date").sort_index()["close"].astype(float)

        return fn

    def _incident_evidence(self):
        import pandas as pd
        from brain import liquidity_quality as LQ, regime_nowcast as NC
        from portfolio import distribution_tells as DT

        asof = pd.Timestamp("2026-07-01")
        sfn = self._series_fn()

        # liquidity — the stress/hollow read from the balance-sheet fixtures
        liq_out = LQ.classify(LQ.series_from_json_fixtures(_FIX / "liquidity_quality"))
        liq = RF._liquidity_stress_source(liq_out)

        # nowcast — strong-doubt on 07-01
        nc_out = NC.nowcast(sfn, quad="Q1", quad_name="Goldilocks", asof=asof)
        nowcast = RF._nowcast_doubt_source(nc_out)

        # defensive-RS crossover as of 07-01
        def _asof_fn(tk):
            s = sfn(tk)
            return None if s is None else s[s.index <= asof]
        rs = DT.defensive_offensive_rs_diff(series_fn=_asof_fn)
        rs_cross = RF._defensive_rs_source(rs.get("crossed"))

        # radar — the trimmed caution forward-log row (the validated plane the bot was blind to)
        radar_row = json.loads(
            (_FIX / "risk_radar_forward_log_caution.jsonl").read_text().strip().splitlines()[-1])
        radar = RF._radar_caution_source(radar_row.get("state"))

        return RF.rotation_evidence(nowcast_doubt=nowcast, liquidity_stress=liq,
                                    radar_caution=radar, defensive_rs_cross=rs_cross), \
            {"liq": liq_out["label"], "nowcast": nc_out["stance"], "rs": rs.get("crossed"),
             "radar": radar_row.get("state")}

    def test_all_four_sources_fire_on_0701(self):
        ev, diag = self._incident_evidence()
        assert diag["liq"] == "stress-expansion", diag
        assert diag["nowcast"] == "strong-doubt", diag
        assert diag["rs"] is True, diag
        assert diag["radar"] == "caution", diag
        assert ev["n_agree"] == 4, f"expected all 4 sources to agree on 07-01, got {ev}"

    def test_budget_damped_to_0p40_range_on_0701(self, monkeypatch, tmp_path):
        """With 4 sources agreeing, budget() on the 07-01 regime fixture damps into the 0.40x-range.

        Un-damped 07-01 budget = 0.40 + 0.20·0.327·1.0(STABLE)·0.75(flip 0.05<0.15) = 0.44905.
        Damped ×0.8 = 0.40 + 0.0490·0.8 = 0.43924 — strictly below the un-damped value, in [0.40,0.44).
        """
        # use the REAL 07-01 regime fixture from the incident replay dir
        _patch_us(monkeypatch, tmp_path, json.loads((_INCIDENT / "regime_latest.json").read_text()))
        ev, _ = self._incident_evidence()
        undamped = RF.budget("us")["lead_budget"]
        damped = RF.budget("us", evidence=ev)["lead_budget"]
        assert undamped == pytest.approx(0.44905, abs=1e-4)
        assert damped == pytest.approx(0.40 + (0.44905 - 0.40) * 0.8, abs=1e-4)
        assert 0.40 <= damped < 0.44, f"07-01 damped budget must land in the 0.40x range, got {damped}"
        assert damped < undamped, "the evidence damp must cut the budget below the un-damped read"

    def test_def_sleeve_signal_ge_0p5_at_armed_max_0p35(self, monkeypatch):
        """At the armed max 0.35, the 4-source lift raises the DEF_SLEEVE fragility to >= 0.5 —
        the '23%' rotation the forensics said a truthful read should size, vs the ~0.20 throttle the
        incident measured on the wrong risk_on/STABLE labels.

        Fragility with the incident's dwell=caution + STABLE label + 4-source lift:
          base = w_dwell·0.5(caution) + w_conf·(1−0.327) + 0(STABLE, no weakening bump)
               = 0.5·0.5 + 0.3·0.673 = 0.25 + 0.2019 = 0.4519
          lift = 0.15·(4−1) = 0.45  → clamp(0.4519 + 0.45) = 1.0  (>= 0.5) ; def_budget 0.35·1.0
        Even the pessimistic risk_on-dwell read (dwell 0.0) clears 0.5 with the lift:
          0.3·0.673 + 0.45 = 0.6519.
        """
        armed = dict(ROT._DEF_SLEEVE_FALLBACK)
        armed["max"] = 0.35
        monkeypatch.setattr(ROT, "_cfg", lambda: armed)
        ev, _ = self._incident_evidence()
        # the incident's held CAUTION dwell + STABLE budget-inputs
        rs_state = {"state": "caution"}
        bi = {"confidence": 0.327, "transition_state": "STABLE"}
        sig = ROT.fragility_signal(rs_state, bi, evidence=ev)
        assert sig >= 0.5, f"armed DEF_SLEEVE fragility must be >= 0.5 with 4-source evidence, got {sig}"
        # contrast: the throttled read WITHOUT evidence (the incident's measured ~0.20)
        throttled = ROT.fragility_signal({"state": "risk_on"}, bi)  # risk_on dwell (the wrong label)
        assert throttled == pytest.approx(0.3 * (1 - 0.327), abs=1e-4)  # ~0.2019, the measured throttle
        assert sig > throttled + 0.3, "evidence must unthrottle well above the measured ~0.20"

    def test_calm_2025_05_never_fires(self, monkeypatch):
        """The calm 2025-05 offense-led uptrend fixture: nowcast confirm + no RS cross → the price
        sources do NOT agree, so the composed damp/lift are no-ops (the negative-control tape)."""
        import pandas as pd
        from brain import regime_nowcast as NC
        from portfolio import distribution_tells as DT
        pdir = _FIX / "regime_nowcast" / "calm_2025_05"

        def sfn(tk):
            p = pdir / f"{tk}.csv"
            if not p.exists():
                return None
            df = pd.read_csv(p)
            df["date"] = pd.to_datetime(df["date"])
            return df.set_index("date").sort_index()["close"].astype(float)

        nc = NC.nowcast(sfn, quad="Q1", quad_name="Goldilocks")
        rs = DT.defensive_offensive_rs_diff(series_fn=sfn)
        ev = RF.rotation_evidence(
            nowcast_doubt=RF._nowcast_doubt_source(nc),
            defensive_rs_cross=RF._defensive_rs_source(rs.get("crossed")))
        # the calm tape must not reach the 2-agree damp bar from the price sources alone
        assert nc["stance"] == "confirm", f"calm tape should confirm, got {nc['stance']}"
        assert ev["n_agree"] < 2, f"calm tape must stay below the damp bar, got {ev}"
