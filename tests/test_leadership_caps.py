"""tests/test_leadership_caps.py — unit + drift-check tests for the shared leadership caps (W2.1).

WHAT THIS GUARDS
----------------
apply_leadership_caps() is the per-leg brake stack on the LEADERSHIP sleeve (architecture Stage 6.1):
per leg, weight *= MIN(overextension clamp off etf_board.etf_trend pct_vs_200d, cycle multiplier). It
is subtract-only; freed weight goes to CASH (never redistributed). These tests pin:
  * the extension clamp (proven ETF-book G4: >40% vs 200d → clamp to 0.08);
  * the cycle halving of a NEW late-cycle leg (0.5), and the EXEMPTION of held/leading legs (masterplan
    §0 refuted the cycle veto on held sectors);
  * the invariant degradations (missing extension / stale cycles / unmapped sector → un-shrunk);
  * the offensive-gross floor tripwire;
  * the DRIFT-CHECK: doctrine.yml's leadership_caps mirror etf_strategy.yml's guardrails.overextension
    (one firm-wide definition of "how extended is too extended" — the tuning surface can't silently fork).

All trend/cycle inputs are dependency-injected (trend_fn / cycles kwargs) so nothing here touches live
data — pure fixtures.
"""
from __future__ import annotations

import bot  # noqa: F401
from portfolio import sleeves


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _leg(ticker: str, weight: float, *, verdict: str = "hold", retained: bool = False) -> dict:
    return {"ticker": ticker, "theme_id": ticker, "sleeve": "leadership",
            "weight": weight, "verdict": verdict, "retained": retained}


def _trend(mapping: dict[str, float]):
    """Return a trend_fn stub: ticker -> {'pct_vs_200d': mapping[ticker]} (or {} if absent)."""
    def _fn(t):
        if t in mapping:
            return {"pct_vs_200d": mapping[t]}
        return {}
    return _fn


def _cycles(**late):
    """Return a cycles() stub keyed by sector ETF with late_cycle flags per kwarg."""
    return {sec: {"late_cycle": bool(v), "entry_favored": not v} for sec, v in late.items()}


# ---------------------------------------------------------------------------
# EXTENSION CLAMP (proven ETF G4)
# ---------------------------------------------------------------------------

class TestExtensionClamp:
    def test_overextended_leg_clamped_to_max_weight(self):
        """A leg >40% vs 200d and above max_weight (0.08) is clamped to 0.08; freed weight to cash."""
        legs = [_leg("SMH", 0.125)]
        out = sleeves.apply_leadership_caps(legs, cycles={}, trend_fn=_trend({"SMH": 55.0}))
        assert legs[0]["weight"] == 0.08
        assert out["freed_to_cash"] == round(0.125 - 0.08, 4)
        assert out["brakes"][0]["reason"] == "overextension"

    def test_under_cap_pct_not_clamped(self):
        """A leg only +20% vs 200d (below the 40% cap) is untouched even if large."""
        legs = [_leg("XLK", 0.125)]
        out = sleeves.apply_leadership_caps(legs, cycles={}, trend_fn=_trend({"XLK": 20.0}))
        assert legs[0]["weight"] == 0.125
        assert out["freed_to_cash"] == 0.0
        assert out["brakes"] == []

    def test_overextended_but_already_below_max_weight_untouched(self):
        """A leg over the pct cap but already <= max_weight is NOT re-shrunk (clamp only reduces)."""
        legs = [_leg("SMH", 0.05)]
        out = sleeves.apply_leadership_caps(legs, cycles={}, trend_fn=_trend({"SMH": 60.0}))
        assert legs[0]["weight"] == 0.05
        assert out["freed_to_cash"] == 0.0

    def test_missing_extension_data_is_no_op(self):
        """No pct_vs_200d (empty trend) → no clamp: missing data degrades to today's weight."""
        legs = [_leg("SMH", 0.125)]
        out = sleeves.apply_leadership_caps(legs, cycles={}, trend_fn=lambda t: {})
        assert legs[0]["weight"] == 0.125
        assert out["freed_to_cash"] == 0.0

    def test_trend_fn_raising_is_swallowed(self):
        """A trend-read exception must never block — the leg is left un-clamped."""
        def _boom(_t):
            raise RuntimeError("price store down")
        legs = [_leg("SMH", 0.125)]
        out = sleeves.apply_leadership_caps(legs, cycles={}, trend_fn=_boom)
        assert legs[0]["weight"] == 0.125
        assert out["freed_to_cash"] == 0.0


# ---------------------------------------------------------------------------
# CYCLE HALVING (entry brake on NEW legs only)
# ---------------------------------------------------------------------------

class TestCycleHalving:
    def test_new_late_cycle_leg_halved(self):
        """A NEW (verdict!='hold', not retained) late-cycle leg is halved by 0.5."""
        legs = [_leg("XLK", 0.10, verdict="add", retained=False)]
        out = sleeves.apply_leadership_caps(
            legs, cycles=_cycles(XLK=True), trend_fn=lambda t: {})
        assert legs[0]["weight"] == 0.05
        assert out["brakes"][0]["reason"] == "late_cycle"
        assert out["freed_to_cash"] == 0.05

    def test_held_late_cycle_leg_exempt(self):
        """A HELD (verdict='hold') leg in a late-cycle sector is NOT halved — refuted veto stays dead."""
        legs = [_leg("XLK", 0.10, verdict="hold")]
        out = sleeves.apply_leadership_caps(
            legs, cycles=_cycles(XLK=True), trend_fn=lambda t: {})
        assert legs[0]["weight"] == 0.10
        assert out["brakes"] == []

    def test_retained_leg_exempt(self):
        """A retained leg is treated as held → cycle-exempt regardless of verdict."""
        legs = [_leg("XLK", 0.10, verdict="add", retained=True)]
        out = sleeves.apply_leadership_caps(
            legs, cycles=_cycles(XLK=True), trend_fn=lambda t: {})
        assert legs[0]["weight"] == 0.10

    def test_thematic_ticker_folds_to_parent_sector(self):
        """SMH (a NEW leg) folds to XLK for the cycle read → late_cycle halving applies."""
        legs = [_leg("SMH", 0.10, verdict="add")]
        out = sleeves.apply_leadership_caps(
            legs, cycles=_cycles(XLK=True), trend_fn=lambda t: {})
        assert legs[0]["weight"] == 0.05

    def test_unmapped_sector_un_shrunk(self):
        """A sector with no cycle row is un-shrunk — a missing mapping only removes a shrink."""
        legs = [_leg("XLF", 0.10, verdict="add")]
        out = sleeves.apply_leadership_caps(
            legs, cycles=_cycles(XLK=True), trend_fn=lambda t: {})   # XLF not in cycles
        assert legs[0]["weight"] == 0.10

    def test_stale_cycles_empty_dict_is_no_op(self):
        """An empty cycles() (stale/absent file) → cycle brake is a no-op (unfiltered = today)."""
        legs = [_leg("XLK", 0.10, verdict="add")]
        out = sleeves.apply_leadership_caps(legs, cycles={}, trend_fn=lambda t: {})
        assert legs[0]["weight"] == 0.10

    def test_explicit_held_set_exempts_leg_despite_verdict(self):
        """When an explicit held set is passed (phase2's path), a leg IN it is exempt even though the
        phase2 legs are all verdict='hold' — the held set is authoritative."""
        legs = [_leg("XLK", 0.10, verdict="hold")]
        out = sleeves.apply_leadership_caps(
            legs, cycles=_cycles(XLK=True), trend_fn=lambda t: {}, held={"XLK"})
        assert legs[0]["weight"] == 0.10          # held → cycle-exempt

    def test_explicit_held_set_new_leg_is_halved_despite_hold_verdict(self):
        """A leg NOT in the held set is NEW even if marked verdict='hold' (phase2 marks all legs hold)
        → the late_cycle halving DOES bite. This is why phase2 passes the real held set."""
        legs = [_leg("XLK", 0.10, verdict="hold")]     # phase2-style 'hold' verdict
        out = sleeves.apply_leadership_caps(
            legs, cycles=_cycles(XLK=True), trend_fn=lambda t: {}, held=set())  # empty held → NEW
        assert legs[0]["weight"] == 0.05
        assert out["brakes"][0]["reason"] == "late_cycle"


# ---------------------------------------------------------------------------
# COMPOSITION (MIN of the two brakes) + subtract-only-to-cash
# ---------------------------------------------------------------------------

class TestComposition:
    def test_min_of_both_brakes_extension_wins(self):
        """A NEW leg that is BOTH over-extended AND late-cycle takes the TIGHTER (min) multiplier.

        weight 0.20, ext clamp → 0.08 (mult 0.40), late-cycle → 0.5 → MIN = 0.40 → 0.08.
        """
        legs = [_leg("SMH", 0.20, verdict="add")]
        out = sleeves.apply_leadership_caps(
            legs, cycles=_cycles(XLK=True), trend_fn=_trend({"SMH": 55.0}))
        assert legs[0]["weight"] == 0.08
        assert out["brakes"][0]["reason"] == "overextension"   # ext (0.40) < cycle (0.50)

    def test_min_of_both_brakes_cycle_wins(self):
        """When the extension clamp is milder than the cycle halving, cycle (0.5) wins.

        weight 0.10, ext to 0.08 (mult 0.80) vs late-cycle 0.5 → MIN=0.5 → 0.05.
        """
        legs = [_leg("SMH", 0.10, verdict="add")]
        out = sleeves.apply_leadership_caps(
            legs, cycles=_cycles(XLK=True), trend_fn=_trend({"SMH": 55.0}))
        assert legs[0]["weight"] == 0.05
        assert out["brakes"][0]["reason"] == "late_cycle"

    def test_freed_weight_goes_to_cash_not_redistributed(self):
        """The freed weight is reported for cash; OTHER legs are never scaled UP."""
        legs = [_leg("SMH", 0.125), _leg("XLV", 0.10, verdict="hold")]
        out = sleeves.apply_leadership_caps(
            legs, cycles={}, trend_fn=_trend({"SMH": 55.0}))
        assert legs[0]["weight"] == 0.08
        assert legs[1]["weight"] == 0.10          # untouched — no redistribution
        assert out["freed_to_cash"] == round(0.125 - 0.08, 4)

    def test_non_leadership_legs_ignored(self):
        """Only sleeve=='leadership' legs are considered."""
        legs = [{"ticker": "NVDA", "sleeve": "conviction", "weight": 0.125}]
        out = sleeves.apply_leadership_caps(
            legs, cycles={}, trend_fn=_trend({"NVDA": 55.0}))
        assert legs[0]["weight"] == 0.125
        assert out["freed_to_cash"] == 0.0


# ---------------------------------------------------------------------------
# OFFENSIVE-GROSS FLOOR TRIPWIRE
# ---------------------------------------------------------------------------

class TestOffensiveGrossTripwire:
    def test_breach_when_below_floor(self):
        """Leadership gross 0.15 vs lead_budget 0.50 (floor 0.25) → breached."""
        legs = [_leg("XLK", 0.08), _leg("XLV", 0.07)]
        tw = sleeves.offensive_gross_tripwire(legs, lead_budget=0.50)
        assert tw["breached"] is True
        assert tw["floor"] == 0.25
        assert "over_degross" in tw["reason"]

    def test_no_breach_at_or_above_floor(self):
        """Gross 0.30 >= floor 0.25 → not breached."""
        legs = [_leg("XLK", 0.15), _leg("XLV", 0.15)]
        tw = sleeves.offensive_gross_tripwire(legs, lead_budget=0.50)
        assert tw["breached"] is False
        assert tw["reason"] is None

    def test_parabolic_veto_suppresses_breach(self):
        """A parabolic hard veto is the sanctioned way below the floor — never a breach."""
        legs = [_leg("XLK", 0.05)]
        tw = sleeves.offensive_gross_tripwire(legs, lead_budget=0.50, parabolic_veto_fired=True)
        assert tw["breached"] is False

    def test_zero_budget_degrades_safe(self):
        tw = sleeves.offensive_gross_tripwire([_leg("XLK", 0.05)], lead_budget=0.0)
        assert tw["breached"] is False


# ---------------------------------------------------------------------------
# DRIFT-CHECK — one firm-wide definition of the overextension threshold
# ---------------------------------------------------------------------------

class TestConfigDriftCheck:
    """doctrine.yml's leadership_caps.overextension MUST mirror etf_strategy.yml's
    guardrails.overextension — one tuning surface, no silent fork between the two books."""

    def test_leadership_caps_mirror_etf_g4(self):
        from portfolio import etf_universe
        lead = sleeves.leadership_caps_cfg()["overextension"]
        etf = etf_universe.guardrails()["overextension"]
        assert lead["pct_vs_200d_cap"] == etf["pct_vs_200d_cap"], (
            "doctrine.yml leadership_caps.overextension.pct_vs_200d_cap has DRIFTED from "
            "etf_strategy.yml guardrails.overextension.pct_vs_200d_cap — one definition firm-wide"
        )
        assert lead["max_weight"] == etf["max_weight"], (
            "doctrine.yml leadership_caps.overextension.max_weight has DRIFTED from "
            "etf_strategy.yml guardrails.overextension.max_weight"
        )

    def test_cfg_reads_doctrine_values(self):
        """The live doctrine.yml values (40 / 0.08 / 0.5) are what the function returns."""
        cfg = sleeves.leadership_caps_cfg()
        assert cfg["overextension"]["pct_vs_200d_cap"] == 40.0
        assert cfg["overextension"]["max_weight"] == 0.08
        assert cfg["late_cycle_mult"] == 0.5
        assert cfg["offensive_gross_floor_frac"] == 0.5
