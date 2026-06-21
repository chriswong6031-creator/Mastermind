"""Tests for position_log, thesis, and new API routes.

All tests are offline/fast — no LLM, no network, no external services.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import mock

import pytest


# ============================================================
# position_log tests
# ============================================================

def _make_position(ticker: str, sleeve: str, weight: float, price: float | None = None) -> dict:
    p = {"ticker": ticker, "sleeve": sleeve, "weight": weight, "verdict": "hold"}
    if price is not None:
        p["entry_price"] = price
    return p


def test_position_log_opens_new_position(tmp_path):
    """Calling update() with a new position stamps opened_at and creates history[open]."""
    from portfolio import position_log

    ledger_path = tmp_path / "positions_ledger.json"
    with mock.patch.object(position_log, "_LEDGER_PATH", ledger_path):
        positions = [_make_position("AVGO", "conviction", 0.08, 100.0)]
        position_log.update(positions, "2026-01-01")

        ledger = json.loads(ledger_path.read_text())
        assert "conviction:AVGO" in ledger
        entry = ledger["conviction:AVGO"]
        assert entry["still_open"] is True
        assert entry["opened_at"] is not None
        assert entry["entry_price"] == 100.0
        assert len(entry["history"]) == 1
        assert entry["history"][0]["event"] == "open"


def test_position_log_closes_removed_position(tmp_path):
    """A position absent from the next book update is closed with closed_at set."""
    from portfolio import position_log

    ledger_path = tmp_path / "positions_ledger.json"
    with mock.patch.object(position_log, "_LEDGER_PATH", ledger_path):
        # first run: AVGO is in book
        position_log.update([_make_position("AVGO", "conviction", 0.08)], "2026-01-01")

        # second run: AVGO gone
        position_log.update([], "2026-01-02")

        ledger = json.loads(ledger_path.read_text())
        entry = ledger["conviction:AVGO"]
        assert entry["still_open"] is False
        assert entry["closed_at"] is not None
        close_events = [h for h in entry["history"] if h["event"] == "close"]
        assert len(close_events) == 1


def test_position_log_open_and_closed_shapes(tmp_path):
    """open_positions() and closed_positions() return the correct shapes."""
    from portfolio import position_log

    ledger_path = tmp_path / "positions_ledger.json"
    with mock.patch.object(position_log, "_LEDGER_PATH", ledger_path):
        # open SMH, close AVGO
        position_log.update(
            [_make_position("SMH", "leadership", 0.125),
             _make_position("AVGO", "conviction", 0.08)],
            "2026-01-01",
        )
        position_log.update(
            [_make_position("SMH", "leadership", 0.125)],  # AVGO removed
            "2026-01-02",
        )

        open_ = position_log.open_positions()
        closed = position_log.closed_positions()

        assert len(open_) == 1
        assert open_[0]["ticker"] == "SMH"
        assert open_[0]["opened_at"] is not None
        assert "held_days" in open_[0]
        assert "entry_weight" in open_[0]

        assert len(closed) == 1
        assert closed[0]["ticker"] == "AVGO"
        assert closed[0]["closed_at"] is not None
        assert "exit_reason" in closed[0]


def test_position_log_corrupt_file_starts_fresh(tmp_path):
    """A corrupt ledger file is silently discarded and replaced with a clean ledger."""
    from portfolio import position_log

    ledger_path = tmp_path / "positions_ledger.json"
    ledger_path.write_text("{INVALID JSON!!!")

    with mock.patch.object(position_log, "_LEDGER_PATH", ledger_path):
        position_log.update([_make_position("SMH", "leadership", 0.125)], "2026-01-01")
        ledger = json.loads(ledger_path.read_text())
        assert "leadership:SMH" in ledger


# ============================================================
# thesis.compose tests
# ============================================================

def _synth(bull: int = 6, bear: int = 3, confluence: float = 0.33,
           vetoes: list | None = None, divergences: list | None = None) -> dict:
    return {
        "bull": bull, "bear": bear, "n_scored": bull + bear, "confluence": confluence,
        "vetoes": vetoes or [], "divergences": divergences or [],
        "size_authority": "blocked" if vetoes else ("up" if confluence > 0.3 else "hold"),
    }


def _matrix_rows_minimal() -> list[dict]:
    return [
        {"lens": "valuation", "value": {"value_z": 0.5, "cheap_pctile": 70.0}, "status": "context", "direction": "bull", "note": ""},
        {"lens": "extension", "value": {"grade": "ok", "parabolic": False, "pct_vs_200dma": 10.0}, "status": "validated", "direction": "neutral", "note": ""},
        {"lens": "risk_drawdown", "value": {"dd_tail": -18.0}, "status": "validated", "direction": "neutral", "note": ""},
        {"lens": "flows_13f", "value": {"n_buying": 4, "n_selling": 1, "vip": 3}, "status": "context", "direction": "bull", "note": ""},
        {"lens": "macro_risk", "value": {"score": 0.25, "quad": "Q1"}, "status": "context", "direction": "bull", "note": ""},
        {"lens": "cross_asset", "value": {"verdict": "concentrated"}, "status": "context", "direction": "bear", "note": ""},
    ]


def test_thesis_compose_conviction_has_required_keys():
    """thesis_full for a conviction name must contain all contract keys."""
    from portfolio.thesis import compose

    pos = {"ticker": "AVGO", "sleeve": "conviction", "weight": 0.08,
           "thesis_id": "2026-01-01-AVGO-conv", "time_stop_by": "2026-09-14",
           "entry_price": 392.0}
    dec = {"id": "2026-01-01-AVGO-conv", "subject": "AVGO", "lean": "add",
            "prob_correct": 0.68, "horizon_d": 21, "thesis": "test"}
    s = _synth()
    facts = {"_matrix_rows": _matrix_rows_minimal()}

    tf = compose(pos, dec, s, facts)

    for key in ("summary", "why_now", "bull", "bear", "vetoes", "divergences",
                "what_would_prove_wrong", "sizing_rationale", "confluence", "prob_correct"):
        assert key in tf, f"Missing key: {key}"

    assert "AVGO" in tf["summary"]
    assert isinstance(tf["bull"], list)
    assert isinstance(tf["bear"], list)
    assert tf["confluence"] == pytest.approx(0.33, abs=0.01)
    assert tf["prob_correct"] == 0.68


def test_thesis_compose_leadership_lighter_form():
    """Leadership ETF thesis has the lighter shape (no decision required)."""
    from portfolio.thesis import compose

    pos = {"ticker": "SMH", "sleeve": "leadership", "weight": 0.125,
           "rs_pctile": 99.2}

    tf = compose(pos, None, None, None)

    for key in ("summary", "why_now", "sizing_rationale", "what_would_prove_wrong"):
        assert key in tf, f"Missing key: {key}"
    assert "SMH" in tf["summary"]
    assert tf["confluence"] is None
    assert tf["prob_correct"] is None
    assert "RS" in tf["why_now"] or "rank" in tf["why_now"]


def test_thesis_compose_with_veto():
    """When vetoes are present, they appear in thesis_full.vetoes as human-readable strings."""
    from portfolio.thesis import compose

    pos = {"ticker": "NVDA", "sleeve": "conviction", "weight": 0.0}
    dec = {"id": "test-NVDA", "subject": "NVDA", "lean": "watch", "prob_correct": 0.55, "horizon_d": 21}
    s = _synth(vetoes=["parabolic"])
    facts = {"_matrix_rows": []}

    tf = compose(pos, dec, s, facts)
    assert len(tf["vetoes"]) == 1
    assert "parabolic" in tf["vetoes"][0].lower()


# ============================================================
# web route tests
# ============================================================

def _client():
    from app.main import app
    from fastapi.testclient import TestClient
    return TestClient(app, raise_server_exceptions=True)


def test_api_trades_returns_open_closed_shape():
    """/api/trades must return {open: [...], closed: [...]}."""
    # seed an open position in the (test-isolated) ledger so this is self-contained and does not
    # depend on the LIVE ledger having state (the conftest now isolates positions_ledger).
    from portfolio import position_log
    position_log.update([{"ticker": "TEST", "sleeve": "conviction", "weight": 0.05,
                          "entry_price": 100.0}], "2026-06-18")
    client = _client()
    r = client.get("/api/trades")
    assert r.status_code == 200
    data = r.json()
    assert "open" in data
    assert "closed" in data
    assert isinstance(data["open"], list)
    assert isinstance(data["closed"], list)
    # open positions should be non-empty after the book build
    assert len(data["open"]) > 0


def test_api_trades_open_has_required_fields():
    """Each open trade must have the contract-required fields."""
    client = _client()
    r = client.get("/api/trades")
    data = r.json()
    for pos in data["open"]:
        for field in ("ticker", "sleeve", "opened_at", "held_days", "entry_weight", "current_weight"):
            assert field in pos, f"Missing field '{field}' in open trade: {pos}"


def test_api_activity_returns_list():
    """/api/activity must return a list."""
    client = _client()
    r = client.get("/api/activity")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)


def test_api_activity_events_have_required_shape():
    """Each activity event must have ts, kind, title, detail."""
    client = _client()
    r = client.get("/api/activity")
    data = r.json()
    for ev in data:
        for field in ("ts", "kind", "title", "detail"):
            assert field in ev, f"Missing field '{field}' in activity event: {ev}"
        assert ev["kind"] in ("trade", "decision", "research", "run")


def test_api_portfolio_now_has_thesis_full():
    """/api/portfolio positions should now carry thesis_full."""
    client = _client()
    r = client.get("/api/portfolio")
    assert r.status_code == 200
    data = r.json()
    positions = data.get("positions", [])
    assert positions
    for pos in positions:
        assert "thesis_full" in pos, f"thesis_full missing from {pos.get('ticker')}"
        tf = pos["thesis_full"]
        assert isinstance(tf.get("summary"), str) and tf["summary"]
        assert isinstance(tf.get("bull"), list)
