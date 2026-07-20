"""Unit battery for the W8 entry engine + context gate (design §2.1-2.2).

The replay battery (test_flagship_v2_replay.py) pins the triad on REAL fixture data; this file
pins the MECHANICS on synthetic inputs: each verdict class constructible, every fail-open
invariant (missing input withholds nothing), the prophet-geometry overrides, the tighten-only
authority model, and the flags-off inertness contract (§4.2).
"""
from __future__ import annotations

import pandas as pd
import pytest

from portfolio import context_gate as cg
from portfolio import entry_engine as ee


def _series(vals):
    return pd.Series([float(v) for v in vals])


def _flat(n=130, px=100.0):
    return _series([px] * n)


def _ramp(n, start, end):
    step = (end - start) / max(1, n - 1)
    return _series([start + i * step for i in range(n)])


# ── fail-open: nothing in → unknown → withholds nothing ─────────────────────────────────────────
def test_no_inputs_is_unknown_and_fail_open():
    rep = ee.assess("ZZZ", series=None, stockdata=None, signal_gate_row=None, stage_row=None,
                    bottom_state=None, pulse=None, prophet_plan=None, _use_defaults=False)
    assert rep["verdict"] == "unknown"
    assert not rep["buyable"]
    assert rep["park_triggers"] is None          # unknown parks NOTHING (charter P2)
    crep = cg.assess("ZZZ", pulse=None, theme_ctx=None, contagion=None, momentum=None,
                     risk=None, _use_defaults=False)
    assert crep["verdict"] == "neutral"
    assert crep["entry_mult"] == 1.0


# ── verdict construction on synthetic series ────────────────────────────────────────────────────
def test_chase_fires_at_range_top_on_a_rip():
    s = _series([100.0] * 100 + [100 + 2.2 * i for i in range(1, 12)])   # ~+24% in 11 sessions
    rep = ee.assess("AAA", series=s, stockdata={"tech": {}}, _use_defaults=False)
    assert rep["verdict"] == "chase"
    assert not rep["buyable"]
    assert rep["park_triggers"]["not_verdicts"]


def test_late_leg_deep_into_the_move():
    # grind +30% off the low over ~40 sessions, no 10d rip, modest 20dma stretch
    s = _series([100.0] * 80 + [100 + 0.75 * i for i in range(1, 41)])
    rep = ee.assess("AAA", series=s, stockdata={"tech": {}}, _use_defaults=False)
    assert rep["verdict"] in ("late_leg", "chase", "extended")   # adverse family
    assert not rep["buyable"]


def test_rollover_after_the_boil():
    # at the top 5 sessions ago, then a -4% fade
    s = _series([100.0] * 100 + [118, 119, 120, 118.5, 117, 115.2])
    rep = ee.assess("AAA", series=s, stockdata={"tech": {}}, _use_defaults=False)
    assert rep["verdict"] == "rollover"
    assert not rep["buyable"]


def test_knife_reuses_falling_knife_thresholds():
    s = _series([100.0] * 110 + [98, 96, 93, 91, 89.5])          # ~-10% in 5
    rep = ee.assess("AAA", series=s, stockdata={"tech": {}}, _use_defaults=False)
    assert rep["verdict"] == "knife"


def test_pullback_in_trend_is_buyable():
    # long uptrend, then a controlled retreat into the lower half of the range
    s = _series([100 + 0.3 * i for i in range(115)])
    s = pd.concat([s, _series([134, 132.5, 131, 130.2, 129.5, 128.8, 128.2, 127.8, 127.2, 126.8,
                               126.2, 125.8, 125.2, 124.8, 124.2])], ignore_index=True)
    rep = ee.assess("AAA", series=s, stockdata={"tech": {"above200": True}}, _use_defaults=False)
    assert rep["verdict"] in ("pullback_in_trend", "clean")
    assert rep["buyable"]


def test_base_turn_needs_turn_evidence_plus_tier():
    s = _series([100.0] * 90 + [96, 95, 94.5, 95, 95.5, 96, 96.5, 97, 97.2, 97.5])
    base = dict(series=s, stockdata={"tech": {"above200": True}}, _use_defaults=False)
    # no tier, no stage → NOT base_turn (falls through to clean/pullback family)
    r0 = ee.assess("AAA", **base)
    assert r0["verdict"] != "base_turn"
    # sensor turn + eligible tier → base_turn
    r1 = ee.assess("AAA", signal_gate_row={"eligible": True, "tier_cascade": "T2"},
                   bottom_state="BOTTOMING", **base)
    assert r1["verdict"] == "base_turn"
    assert r1["buyable"] and r1["entry_score"] >= 80


def test_prophet_geometry_overrides():
    s = _flat(130, 100.0)
    plan = {"plan_id": "AAA-BULL-1", "entry": 80.0, "invalidation": 72.0, "t1": 92.0,
            "trigger": 81.0, "phase": "triggered_pre_t1", "conviction": 90}
    rep = ee.assess("AAA", series=s, stockdata={"tech": {}}, prophet_plan=plan,
                    _use_defaults=False)
    assert rep["verdict"] == "missed_move"       # price 100 > T1 92
    plan2 = dict(plan, t1=130.0)
    rep2 = ee.assess("AAA", series=s, stockdata={"tech": {}}, prophet_plan=plan2,
                     _use_defaults=False)
    assert rep2["verdict"] == "extended_vs_plan"  # 100 > 80 + 0.5*8 = 84, below T1
    plan3 = dict(plan, entry=98.0, invalidation=88.0, t1=130.0)
    rep3 = ee.assess("AAA", series=s, stockdata={"tech": {}}, prophet_plan=plan3,
                     _use_defaults=False)
    assert rep3["metrics"]["plan"]["status"] == "within_zone"


def test_still_withheld_reason_fails_open(monkeypatch):
    monkeypatch.setattr(ee, "assess", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert ee.still_withheld_reason("AAA") is None


# ── context gate rules on minimal fixtures ──────────────────────────────────────────────────────
def _pulse(theme_id, heat, clean=None):
    row = {"id": theme_id, "heat": heat}
    if clean is not None:
        row["clean_entry"] = {"flag": clean}
    return {"schema": 1, "themes": [row]}


def test_broken_theme_blocks_cooling_only_penalizes():
    blocked = cg.assess("AAA", theme_id="th", pulse=_pulse("th", "broken"),
                        theme_ctx={}, contagion={}, momentum={}, risk={}, _use_defaults=False)
    assert blocked["verdict"] == "blocked"
    cooling = cg.assess("AAA", theme_id="th", pulse=_pulse("th", "cooling"),
                        theme_ctx={}, contagion={}, momentum={}, risk={}, _use_defaults=False)
    assert cooling["verdict"] != "blocked"


def test_breaking_membership_blocks():
    tc = {"leadership": {"state": "steady",
                         "breaking": [{"id": "th", "category": "X", "health": "broken"}]}}
    rep = cg.assess("AAA", theme_id="th", pulse=None, theme_ctx=tc, contagion={},
                    momentum={}, risk={}, _use_defaults=False)
    assert rep["verdict"] == "blocked"


def test_trailing_leader_mid_rotation_is_against():
    tc = {"leadership": {"state": "rotating", "trailing_leader": {"id": "old_leader"},
                         "strength": [], "breaking": []}}
    rep = cg.assess("AAA", theme_id="old_leader", pulse=None, theme_ctx=tc, contagion={},
                    momentum={}, risk={}, _use_defaults=False)
    assert rep["verdict"] == "against"
    assert rep["entry_mult"] == pytest.approx(0.6)


def test_contagion_pressure_blocks_downstream_only():
    contagion = {"origin_complex": "ai_hardware", "leadership_state": "BROKEN",
                 "us_spillover": "contained", "n_alert": 0, "d3_alert": 0,
                 "intl_markets_in_alert": [{"market": "cn", "mature": True},
                                           {"market": "hk", "mature": True}]}
    semis = cg.assess("AAA", sector="Information Technology", pulse=None, theme_ctx={},
                      contagion=contagion, momentum={}, risk={},
                      stockdata={"sector": "Information Technology"}, _use_defaults=False)
    assert semis["verdict"] == "blocked"
    energy = cg.assess("BBB", sector="Energy", pulse=None, theme_ctx={}, contagion=contagion,
                       momentum={}, risk={}, stockdata={"sector": "Energy"},
                       _use_defaults=False)
    assert energy["verdict"] != "blocked"        # NOT in the origin's downstream set


def test_unwind_blocks_vehicles_penalizes_members():
    md = {"pulse": {"mtum_spy_20d_pct": -8.0},
          "top_decile": {"sample": ["MEMBER"]}}
    v = cg.assess("MTUM", is_etf=True, pulse=None, theme_ctx={}, contagion={}, momentum=md,
                  risk={}, _use_defaults=False)
    assert v["verdict"] == "blocked"
    m = cg.assess("MEMBER", pulse=None, theme_ctx={}, contagion={}, momentum=md, risk={},
                  stockdata={}, _use_defaults=False)
    assert m["verdict"] == "against"
    calm = cg.assess("MTUM", is_etf=True, pulse=None, theme_ctx={}, contagion={},
                     momentum={"pulse": {"mtum_spy_20d_pct": 2.0}}, risk={},
                     _use_defaults=False)
    assert calm["verdict"] != "blocked"


def test_risk_ladder_tightens_never_loosens():
    kw = dict(pulse=None, theme_ctx={}, contagion={}, momentum={}, _use_defaults=False,
              stockdata={})
    caution_plain = cg.assess("AAA", entry_verdict="clean", entry_tier_ok=False,
                              risk={"state": "caution"}, **kw)
    assert caution_plain["verdict"] == "against"
    caution_tiered = cg.assess("AAA", entry_verdict="clean", entry_tier_ok=True,
                               risk={"state": "caution"}, **kw)
    assert caution_tiered["verdict"] != "against"
    elevated = cg.assess("AAA", entry_verdict="clean", entry_tier_ok=True,
                         risk={"state": "elevated"}, **kw)
    assert elevated["verdict"] == "blocked"
    elevated_turn = cg.assess("AAA", entry_verdict="base_turn", entry_tier_ok=True,
                              risk={"state": "elevated"}, **kw)
    assert elevated_turn["verdict"] != "blocked"
    risk_off = cg.assess("AAA", entry_verdict="base_turn", risk={"state": "risk_off"}, **kw)
    assert risk_off["verdict"] == "blocked"
    defensive = cg.assess("XLU", is_etf=True, entry_verdict="base_turn",
                          risk={"state": "risk_off"}, **kw)
    assert defensive["verdict"] != "blocked"     # defensives exempt from the risk_off block


def test_favorable_never_overrides_a_block():
    # strength membership + heating/clean AND a broken pulse heat on another membership → BLOCKED
    tc = {"leadership": {"state": "steady",
                         "strength": [{"id": "good"}], "breaking": [{"id": "bad"}]}}
    rep = cg.assess("AAA", theme_id="good", pulse=_pulse("good", "heating", clean=True),
                    theme_ctx=tc, contagion={}, momentum={}, risk={},
                    stockdata={"baskets_membership": [{"slug": "bad"}]}, _use_defaults=False)
    assert rep["verdict"] == "blocked"           # tighten-only: favorable is only a credit


# ── flags-off inertness (§4.2) ──────────────────────────────────────────────────────────────────
def test_entry_gate_flag_off_is_inert(monkeypatch):
    from portfolio import conviction
    monkeypatch.setenv("MASTERMIND_ENTRY_GATE", "0")
    assert conviction._entry_gate_enabled() is False
    monkeypatch.setenv("MASTERMIND_ENTRY_GATE", "1")
    assert conviction._entry_gate_enabled() is True


def test_prophet_flag_off_contributes_nothing(monkeypatch):
    from portfolio import prophet_feed
    monkeypatch.setenv("MASTERMIND_PROPHET_FEED", "0")
    prophet_feed._reset_cache()
    assert prophet_feed.candidate_tickers() == []
    assert prophet_feed.plan_for("TNDM") is None
    monkeypatch.delenv("MASTERMIND_PROPHET_FEED", raising=False)
    prophet_feed._reset_cache()


def test_nw_decision_off_is_inert(monkeypatch):
    from brain import neural_web_context as nwc
    monkeypatch.setenv("MASTERMIND_NW_DECISION", "off")
    assert nwc.nw_decision_mode() == "off"
    sig = nwc.decision_signals("AAPL")
    assert sig["inert"] is True and sig["candidacy"] is None
    monkeypatch.delenv("MASTERMIND_NW_DECISION", raising=False)


def test_rotation_off_is_inert(monkeypatch):
    from bot import phase2
    monkeypatch.setenv("MASTERMIND_ROTATION_IN", "off")
    assert phase2._rotation_in_mode() == "off"
    monkeypatch.delenv("MASTERMIND_ROTATION_IN", raising=False)
    assert phase2._rotation_in_mode() == "watch"   # W8 default
