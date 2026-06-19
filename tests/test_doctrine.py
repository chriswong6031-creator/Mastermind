"""Acceptance tests for the doctrine integration's pure decision logic."""
from datetime import date

import bot  # noqa: F401

from portfolio.scorecard import confluence_gate
from portfolio.stages import ThemeFields, classify_stage
from portfolio.sleeves import enforce_book_caps, no_rotation_capacity
from brain.bottleneck import leading_order
from brain.detectors import d3_no_rotation_capacity, d6_cap_breach


def test_confluence_gate():
    # single dim alone -> forbidden
    assert confluence_gate({"rs": "confirmed"})["size"] == "none"
    # rs + breadth -> initial
    assert confluence_gate({"rs": "confirmed", "breadth": "confirmed"})["size"] == "initial"
    # catalyst (the full-size gate) + 3 of {rs,breadth,flow,volume} -> full
    full = confluence_gate({"rs": "confirmed", "breadth": "confirmed", "flow": "confirmed",
                            "catalyst": "confirmed"})
    assert full["size"] == "full" and full["catalyst_present"]
    # no catalyst -> never full
    assert confluence_gate({"rs": "confirmed", "breadth": "confirmed", "flow": "confirmed",
                            "volume": "confirmed"})["size"] != "full"


def test_stage_classifier():
    latent = ThemeFields("neutral", "quiet", 0.3, 0.3, 0.0, 0.0, False, False)
    assert classify_stage(latent)["stage"] == 0
    mature = ThemeFields("dominant", "confirmed", 0.9, 0.7, 1.5, 0.4, True, False)
    r = classify_stage(mature)
    assert r["stage"] == 3 and r["fourth_deriv_tell"]


def test_book_caps_subtract_only():
    pos = [{"ticker": "NVDA", "theme_id": "ai", "sleeve": "conviction", "weight": 0.14},
           {"ticker": "AVGO", "theme_id": "ai", "sleeve": "conviction", "weight": 0.14}]
    out = enforce_book_caps(pos)
    # single-name cap 0.08 clamps each; theme then already <= 0.25
    assert all(p["weight"] <= 0.08 + 1e-9 for p in out["positions"])
    assert any(b["kind"] == "name" for b in out["breaches"])


def test_detectors():
    assert no_rotation_capacity(0.0, 0.24) is True
    assert d3_no_rotation_capacity(0.0, 0.24, "self")[0]["severity"] == "veto"
    breaches = [{"code": "D6", "kind": "theme", "subject": "ai", "weight": 0.31, "cap": 0.25}]
    assert d6_cap_breach(breaches, "self")[0]["severity"] == "veto"


def test_bottleneck_leading_order():
    rs = {"ai_semiconductors": 0.55, "memory_storage": 0.62, "data_center_power": 0.88,
          "grid_electrification": 0.81}
    a = leading_order("ai_buildout", rs)
    assert a["leading_order"] == 3  # power/grid is leading -> constraint migrated to electricity
