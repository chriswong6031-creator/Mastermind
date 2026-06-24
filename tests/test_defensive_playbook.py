"""Guards for the driver-aware defensive playbook (portfolio/defensive_playbook).

Pure / offline. Proves archetype selection from the macro read and the CONDITIONAL rate-sensitive
caveat that flips with the regime inflation sign (the whole reason a fixed risk-off list is wrong)."""
from __future__ import annotations

from portfolio import defensive_playbook as DP


def _rs(*, drivers=None, axes=None, growth=0.0, infl=0.0):
    return {"drivers": drivers or [], "axes": axes or {}, "regime_growth": growth,
            "regime_inflation": infl}


def test_ai_capex_unwind_selected_on_liquidity_plus_driver():
    rs = _rs(drivers=[{"id": "ai_buildout"}], axes={"liquidity": {"fragility": 0.7},
                                                    "crowding": {"fragility": 0.6}}, growth=0.3, infl=-0.3)
    assert DP.select_archetype(rs) == "ai_capex_unwind"


def test_inflation_shock_selected():
    rs = _rs(axes={"credit_usd": {"fragility": 0.6}, "liquidity": {"fragility": 0.2}}, infl=0.4)
    assert DP.select_archetype(rs) == "inflation_shock"


def test_credit_event_selected():
    rs = _rs(axes={"credit_usd": {"fragility": 0.7}, "liquidity": {"fragility": 0.2},
                   "crowding": {"fragility": 0.1}, "volatility": {"fragility": 0.3}})
    assert DP.select_archetype(rs) == "credit_event"


def test_growth_scare_favors_duration():
    # ai-capex unwind that is a GROWTH-SCARE (inflation < 0) → duration (TLT) becomes a hedge.
    rs = _rs(drivers=[{"id": "ai_buildout"}], axes={"liquidity": {"fragility": 0.7}},
             growth=-0.4, infl=-0.4)
    tilt = DP.defensive_tilt(rs)
    assert tilt["archetype"] == "ai_capex_unwind"
    assert "TLT" in tilt["favor"]                       # duration helps in a growth-scare
    assert "growth-scare" in tilt["rate_sensitive_note"].lower()


def test_rates_up_avoids_duration_and_reits():
    # the SAME unwind but with a positive inflation impulse → duration/REITs become hazards.
    rs = _rs(drivers=[{"id": "ai_buildout"}], axes={"liquidity": {"fragility": 0.7}},
             growth=0.2, infl=0.4)
    tilt = DP.defensive_tilt(rs)
    assert "TLT" not in tilt["favor"]                   # duration is NOT a hedge when rates rise
    assert "XLRE" in tilt["avoid"] and "TLT" in tilt["avoid"]
    assert "rates-up" in tilt["rate_sensitive_note"].lower()


def test_tilt_is_advisory_and_has_enforced_floor():
    tilt = DP.defensive_tilt(_rs(drivers=[{"id": "ai_buildout"}],
                                 axes={"liquidity": {"fragility": 0.7}}))
    assert tilt["advisory"] is True                     # favor is a suggestion, not an order
    assert 0.0 < tilt["cash_floor"] <= 0.5              # an enforceable cash floor
    assert tilt["avoid"]                                # an enforceable add-block list


def test_never_raises():
    assert DP.defensive_tilt(None)["archetype"]
    assert DP.brief(None)
