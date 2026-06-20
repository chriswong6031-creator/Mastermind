"""Tests for bear-case visibility:
  - Leadership ETF thesis_full.bear is non-empty and concrete.
  - conviction.build() now returns (sized, rejected); rejected contains vetoed/low-confluence names.
  - /api/portfolio carries top-level rejected[] with the required shape.
All tests are offline/fast — no LLM, no network.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import portfolio.conviction as _cv_mod
import portfolio.lenses as _lenses_mod


# ============================================================
# thesis._leadership_thesis bear-case tests
# ============================================================

def _leadership_pos(ticker: str = "SMH", rs_pctile: float = 99.2, weight: float = 0.125) -> dict:
    return {"ticker": ticker, "sleeve": "leadership", "weight": weight, "rs_pctile": rs_pctile}


def test_leadership_bear_is_non_empty():
    """An ETF position must always carry a non-empty bear list."""
    from portfolio.thesis import compose

    pos = _leadership_pos()
    tf = compose(pos, None, None, None)
    assert isinstance(tf["bear"], list), "bear must be a list"
    assert len(tf["bear"]) > 0, "leadership ETF must have at least one bear bullet"


def test_leadership_bear_mentions_rotation_risk():
    """The first bear bullet must mention leadership-rotation / RS exit."""
    from portfolio.thesis import compose

    pos = _leadership_pos(ticker="XLK", rs_pctile=95.0)
    tf = compose(pos, None, None, None)
    first_bear = tf["bear"][0].lower()
    assert "rs" in first_bear or "rotation" in first_bear or "percentile" in first_bear, (
        f"First bear bullet should mention RS rotation risk; got: {tf['bear'][0]}"
    )


def test_leadership_bear_mentions_breadth():
    """One bear bullet must mention breadth or narrowing."""
    from portfolio.thesis import compose

    pos = _leadership_pos(ticker="MTUM")
    tf = compose(pos, None, None, None)
    combined = " ".join(tf["bear"]).lower()
    assert "breadth" in combined or "narrow" in combined or "concentration" in combined, (
        f"Bear case should mention breadth/narrowing risk; got: {tf['bear']}"
    )


def test_leadership_bear_extension_with_real_pct():
    """When pct_vs_200dma is passed in facts, the bear bullet includes the real number."""
    from portfolio.thesis import compose

    pos = _leadership_pos(ticker="SMH", rs_pctile=99.0)
    facts = {"pct_vs_200dma": 28.5}
    tf = compose(pos, None, None, facts)
    combined = " ".join(tf["bear"])
    assert "28.5" in combined, (
        f"Expected pct_vs_200dma=28.5 in bear bullet; got: {tf['bear']}"
    )


def test_leadership_bear_extension_high_warns_elevated():
    """pct_vs_200dma >= 20 should trigger 'elevated' / mean-reversion language."""
    from portfolio.thesis import compose

    pos = _leadership_pos()
    tf = compose(pos, None, None, {"pct_vs_200dma": 32.1})
    combined = " ".join(tf["bear"]).lower()
    assert "mean-reversion" in combined or "elevated" in combined, (
        "High extension should produce elevated mean-reversion warning in bear case"
    )


def test_leadership_bear_mechanical_sleeve_note():
    """One bear bullet must explain the mechanical nature (macro de-risk trims first)."""
    from portfolio.thesis import compose

    pos = _leadership_pos()
    tf = compose(pos, None, None, None)
    combined = " ".join(tf["bear"]).lower()
    assert "mechanical" in combined or "macro" in combined or "systematic" in combined, (
        "Bear case must note mechanical/macro de-risk exit behaviour"
    )


# ============================================================
# conviction.build() now returns (sized, rejected) tuple
# ============================================================

def _fake_lenses_full(ticker: str, kind: str = "name"):
    """Returns a realistic lenses.full() output:
    - AVGO passes (size_authority=up, no vetoes, positive confluence)
    - NVDA is vetoed as parabolic
    - MU has negative confluence (no veto, but won't size)
    """
    if ticker == "AVGO":
        return {
            "subject": "AVGO", "kind": "name",
            "rows": [
                {"lens": "valuation", "value": {"value_z": 0.4, "cheap_pctile": 68.0}, "status": "context", "direction": "bull", "note": ""},
                {"lens": "extension", "value": {"grade": "ok", "parabolic": False, "pct_vs_200dma": 12.0}, "status": "validated", "direction": "neutral", "note": ""},
                {"lens": "flows_13f", "value": {"n_buying": 5, "n_selling": 1}, "status": "context", "direction": "bull", "note": ""},
                {"lens": "macro_risk", "value": {"score": 0.2}, "status": "context", "direction": "bull", "note": ""},
            ],
            "synthesis": {
                "bull": 3, "bear": 0, "n_scored": 3, "confluence": 1.0,
                "vetoes": [], "divergences": [],
                "size_authority": "up",
            },
        }
    if ticker == "NVDA":
        return {
            "subject": "NVDA", "kind": "name",
            "rows": [
                {"lens": "extension", "value": {"grade": "parabolic", "parabolic": True, "pct_vs_200dma": 55.0}, "status": "validated", "direction": "bear", "note": ""},
                {"lens": "flows_13f", "value": {"n_buying": 3, "n_selling": 2}, "status": "context", "direction": "bull", "note": ""},
            ],
            "synthesis": {
                "bull": 1, "bear": 1, "n_scored": 2, "confluence": 0.0,
                "vetoes": ["parabolic"], "divergences": [],
                "size_authority": "blocked",
            },
        }
    if ticker == "MU":
        return {
            "subject": "MU", "kind": "name",
            "rows": [
                {"lens": "valuation", "value": {"value_z": -0.5, "cheap_pctile": 28.0}, "status": "context", "direction": "bear", "note": ""},
                {"lens": "flows_13f", "value": {"n_buying": 1, "n_selling": 4}, "status": "context", "direction": "bear", "note": ""},
                {"lens": "macro_risk", "value": {"score": 0.72}, "status": "context", "direction": "bear", "note": ""},
            ],
            "synthesis": {
                "bull": 0, "bear": 3, "n_scored": 3, "confluence": -1.0,
                "vetoes": [], "divergences": [],
                "size_authority": "down",
            },
        }
    # default: missing data name — skip
    raise ValueError(f"no fake data for {ticker}")


def test_conviction_build_returns_tuple():
    """conviction.build() must return a 2-tuple of (sized_positions, rejected)."""
    from portfolio import conviction

    with mock.patch.object(_cv_mod, "candidates", return_value=["AVGO", "NVDA", "MU"]), \
         mock.patch.object(_lenses_mod, "full", side_effect=_fake_lenses_full):
        result = conviction.build(budget=0.30, name_cap=0.08)

    assert isinstance(result, tuple) and len(result) == 2, (
        "conviction.build() must return a (sized, rejected) 2-tuple"
    )
    sized, rejected = result
    assert isinstance(sized, list)
    assert isinstance(rejected, list)


def test_conviction_build_avgo_sized():
    """AVGO (all-sides positive) must appear in sized, not rejected."""
    from portfolio import conviction

    with mock.patch.object(_cv_mod, "candidates", return_value=["AVGO", "NVDA", "MU"]), \
         mock.patch.object(_lenses_mod, "full", side_effect=_fake_lenses_full):
        sized, rejected = conviction.build(budget=0.30, name_cap=0.08)

    tickers_sized = [p["ticker"] for p in sized]
    tickers_rejected = [r["ticker"] for r in rejected]
    assert "AVGO" in tickers_sized, f"AVGO should be sized; sized={tickers_sized}"
    assert "AVGO" not in tickers_rejected


def test_conviction_build_nvda_rejected_with_veto():
    """NVDA (parabolic veto) must appear in rejected with vetoes=['parabolic']."""
    from portfolio import conviction

    with mock.patch.object(_cv_mod, "candidates", return_value=["AVGO", "NVDA", "MU"]), \
         mock.patch.object(_lenses_mod, "full", side_effect=_fake_lenses_full):
        sized, rejected = conviction.build(budget=0.30, name_cap=0.08)

    nvda = next((r for r in rejected if r["ticker"] == "NVDA"), None)
    assert nvda is not None, "NVDA should be in rejected"
    assert "parabolic" in nvda["vetoes"], f"NVDA vetoes should contain 'parabolic'; got {nvda['vetoes']}"
    assert "Vetoed" in nvda["reason"], f"NVDA reason should mention veto; got {nvda['reason']}"


def test_conviction_build_nvda_rejected_has_bear():
    """NVDA rejected entry must carry non-empty bear bullets."""
    from portfolio import conviction

    with mock.patch.object(_cv_mod, "candidates", return_value=["AVGO", "NVDA", "MU"]), \
         mock.patch.object(_lenses_mod, "full", side_effect=_fake_lenses_full):
        sized, rejected = conviction.build(budget=0.30, name_cap=0.08)

    nvda = next((r for r in rejected if r["ticker"] == "NVDA"), None)
    assert nvda is not None
    assert isinstance(nvda["bear"], list) and len(nvda["bear"]) > 0, (
        f"NVDA rejected entry should have bear bullets; got {nvda['bear']}"
    )


def test_conviction_build_mu_rejected_negative_confluence():
    """MU (all-sides bearish) must be rejected with negative confluence."""
    from portfolio import conviction

    with mock.patch.object(_cv_mod, "candidates", return_value=["AVGO", "NVDA", "MU"]), \
         mock.patch.object(_lenses_mod, "full", side_effect=_fake_lenses_full):
        sized, rejected = conviction.build(budget=0.30, name_cap=0.08)

    mu = next((r for r in rejected if r["ticker"] == "MU"), None)
    assert mu is not None, "MU should be in rejected"
    assert mu["confluence"] < 0, f"MU confluence should be negative; got {mu['confluence']}"
    assert len(mu["bear"]) > 0, "MU rejected entry should have bear bullets"


def test_rejected_sorted_worst_first():
    """Rejected list must be sorted worst-confluence first."""
    from portfolio import conviction

    with mock.patch.object(_cv_mod, "candidates", return_value=["AVGO", "NVDA", "MU"]), \
         mock.patch.object(_lenses_mod, "full", side_effect=_fake_lenses_full):
        sized, rejected = conviction.build(budget=0.30, name_cap=0.08)

    if len(rejected) >= 2:
        conf_values = [r["confluence"] for r in rejected]
        assert conf_values == sorted(conf_values), (
            f"Rejected should be sorted worst-confluence first; got {conf_values}"
        )


def test_rejected_entry_has_required_shape():
    """Each rejected entry must match the API contract: ticker, reason, vetoes, bear, confluence."""
    from portfolio import conviction

    with mock.patch.object(_cv_mod, "candidates", return_value=["AVGO", "NVDA", "MU"]), \
         mock.patch.object(_lenses_mod, "full", side_effect=_fake_lenses_full):
        sized, rejected = conviction.build(budget=0.30, name_cap=0.08)

    for entry in rejected:
        for field in ("ticker", "reason", "vetoes", "bear", "confluence"):
            assert field in entry, f"Rejected entry missing field '{field}': {entry}"
        assert isinstance(entry["vetoes"], list)
        assert isinstance(entry["bear"], list)
        assert isinstance(entry["reason"], str) and entry["reason"]
        assert isinstance(entry["confluence"], float)


# ============================================================
# /api/portfolio carries rejected[] (integration, uses real latest.json)
# ============================================================

def test_api_portfolio_carries_rejected_list():
    """/api/portfolio must include a top-level 'rejected' key containing a list."""
    from app.main import app
    from fastapi.testclient import TestClient

    client = TestClient(app, raise_server_exceptions=True)
    r = client.get("/api/portfolio")
    assert r.status_code == 200, f"Expected 200; got {r.status_code}"
    data = r.json()
    assert "rejected" in data, (
        "portfolio response must carry top-level 'rejected' key — "
        f"keys present: {list(data.keys())}"
    )
    assert isinstance(data["rejected"], list), "rejected must be a list"


def test_api_portfolio_rejected_entry_shape():
    """Each rejected entry in /api/portfolio must have ticker, reason, vetoes, bear, confluence."""
    from app.main import app
    from fastapi.testclient import TestClient

    client = TestClient(app, raise_server_exceptions=True)
    r = client.get("/api/portfolio")
    data = r.json()
    rejected = data.get("rejected", [])

    # only assert shape when there are entries (shortlist may be empty in offline env)
    for entry in rejected:
        for field in ("ticker", "reason", "vetoes", "bear", "confluence"):
            assert field in entry, f"Rejected entry missing '{field}': {entry}"


def test_api_portfolio_leadership_bear_non_empty():
    """Every leadership ETF in /api/portfolio must have non-empty thesis_full.bear."""
    from app.main import app
    from fastapi.testclient import TestClient

    client = TestClient(app, raise_server_exceptions=True)
    r = client.get("/api/portfolio")
    data = r.json()
    for pos in data.get("positions", []):
        if pos.get("sleeve") == "leadership":
            bear = pos.get("thesis_full", {}).get("bear", [])
            assert len(bear) > 0, (
                f"Leadership ETF {pos['ticker']} has empty thesis_full.bear"
            )
