"""Posture / strategy-label deriver + /api/posture endpoint tests.

Fully offline: the deriver reads only on-disk book state, so the unit tests build tiny fixture
books in a tmp dir and assert the label/favored/avoided/driver. The endpoint tests hit the live
data fixtures via TestClient and only assert the contract shape + that it never 500s.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# fixtures helpers
# ---------------------------------------------------------------------------
def _write_book(d: Path, book: dict, decisions: list[dict] | None = None) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    (d / "latest.json").write_text(json.dumps(book))
    if decisions is not None:
        (d / "decisions.jsonl").write_text("\n".join(json.dumps(r) for r in decisions) + "\n")
    return d


# ---------------------------------------------------------------------------
# unit tests — the deriver
# ---------------------------------------------------------------------------
def test_posture_offensive_fully_invested(tmp_path):
    from brain import posture
    d = _write_book(tmp_path / "etf", {
        "as_of": "2026-06-22", "gross": 0.97, "cash": 0.03, "nav": 1_010_000,
        "regime": {"quad_name": "Goldilocks", "liquidity_overlay": "contracting"},
        "positions": [
            {"ticker": "QQQ", "weight": 0.17, "rationale": "megacap-growth leadership; growth leadership"},
            {"ticker": "SMH", "weight": 0.13, "rationale": "semiconductor #1 RS leader"},
        ],
        "executed_today": [
            {"ticker": "QQQ", "side": "buy", "value": 23_659},
            {"ticker": "SGOV", "side": "sell", "value": 40_000},
        ],
    }, decisions=[{"asof": "2026-06-22", "summary": "Redeploying the buffer; risk calm.",
                   "risk_state": "calm", "executed": []}])
    p = posture.posture("etf", data_dir=d)
    assert p["available"] is True
    assert p["posture_label"] == "Offensive"
    assert p["posture_tone"] == "up"
    assert "Megacap growth" in p["favored"]
    assert p["cash_pct"] == 3.0 and p["invested_pct"] == 97.0
    assert "Goldilocks" in p["driver"]
    # detail is a fully-composed human sentence
    assert p["detail"] and p["sub_strategy"] in p["detail"]


def test_posture_defensive_cash_cushion(tmp_path):
    from brain import posture
    d = _write_book(tmp_path / "autonomous", {
        "as_of": "2026-06-22", "gross": 0.555, "cash": 0.445, "nav": 1_010_000,
        "regime": {"quad_name": "Goldilocks", "liquidity_overlay": "contracting"},
        "summary": "Defensive cushion into the GDP/PCE prints; keeping dry powder. Hawkish Fed.",
        "positions": [
            {"ticker": "SMH", "weight": 0.11, "rationale": "semiconductor leadership basket"},
            {"ticker": "EME", "weight": 0.10, "rationale": "electrification pick-and-shovel; power grid"},
        ],
        "executed_today": [
            {"ticker": "SMH", "side": "buy", "value": 30_042},
            {"ticker": "HUBB", "side": "sell", "value": 16_333},
        ],
    })
    p = posture.posture("autonomous", data_dir=d)
    assert p["posture_label"] == "Defensive"
    assert p["posture_tone"] == "warn"
    # favored picks up the bought leadership themes; driver names the macro cues
    assert any("Semis" in f or "Electrification" in f for f in p["favored"])
    assert "hawkish Fed" in p["driver"] or "GDP/PCE prints" in p["driver"]
    assert "dry powder" in p["sub_strategy"].lower()


def test_posture_risk_off_stress_regime(tmp_path):
    from brain import posture
    d = _write_book(tmp_path / "x", {
        "as_of": "2026-06-22", "gross": 0.60, "cash": 0.40, "nav": 1_000_000,
        "regime": {"quad_name": "Deflation", "liquidity_overlay": "contracting"},
        "summary": "Cutting risk; raising cash into a deteriorating tape.",
        "positions": [{"ticker": "SGOV", "weight": 0.40, "rationale": "front-end bills"}],
        "executed_today": [{"ticker": "SMH", "side": "sell", "value": 50_000}],
    })
    p = posture.posture("x", data_dir=d)
    assert p["posture_label"] == "Risk-off"
    assert p["posture_tone"] == "down"


def test_posture_skips_empty_run_uses_prior_decision(tmp_path):
    from brain import posture
    # latest.json carries the book; the NEWEST decision row is an empty feed-gated run that must
    # NOT define the posture — the deriver walks back to the meaningful one.
    d = _write_book(tmp_path / "china", {
        "as_of": "2026-06-23", "gross": 0.56, "cash": 0.44, "nav": 1_000_000,
        "regime": {"quad_name": "Stagflation", "liquidity_overlay": "neutral"},
        "positions": [{"ticker": "688411.SS", "name": "海博思创", "weight": 0.28,
                       "rationale": "grid-scale BESS integrator; new power system"}],
    }, decisions=[
        {"asof": "2026-06-22", "summary": "Stagflation; refuse extended entries, hold dry powder.",
         "holdings": [{"ticker": "688411.SS", "weight": 0.28}],
         "executed": [{"ticker": "603301.SS", "side": "buy", "value": 121_411, "name": "振德医疗"}]},
        {"asof": "2026-06-23", "summary": None, "holdings": [], "executed": [], "brain_text": ""},
    ])
    p = posture.posture("china", data_dir=d)
    assert p["available"] is True
    assert p["posture_label"] == "Defensive"   # 44% cash, stagflation
    assert "stagflation" in p["driver"].lower()


def test_posture_absent_book_is_graceful(tmp_path):
    from brain import posture
    p = posture.posture("nonexistent", data_dir=tmp_path / "nope")
    assert p["available"] is False
    assert p["posture_label"] == "—"
    assert p["favored"] == [] and p["avoided"] == []


# ---------------------------------------------------------------------------
# endpoint tests — contract + never-500 over the live fixtures
# ---------------------------------------------------------------------------
def _client():
    from app.main import app
    return TestClient(app, raise_server_exceptions=True)


def test_api_posture_contract():
    r = _client().get("/api/posture?book=flagship")
    assert r.status_code == 200
    d = r.json()
    for k in ("book", "posture_label", "posture_label_zh", "posture_tone", "sub_strategy",
              "favored", "avoided", "driver", "detail", "cash_pct", "invested_pct", "available"):
        assert k in d, f"missing key {k}"
    assert isinstance(d["favored"], list) and isinstance(d["avoided"], list)
    assert d["book"] == "flagship"


def test_api_posture_never_500_across_books():
    client = _client()
    for book in ("flagship", "autonomous", "heavyweight", "china", "hk", "etf", "bogus_book"):
        r = client.get(f"/api/posture?book={book}")
        assert r.status_code == 200, f"{book} -> {r.status_code}"
        d = r.json()
        assert d["book"] == book
        # tone is always one of the design-token classes the chip CSS knows about
        assert d["posture_tone"] in ("up", "down", "warn", "info", "muted")
