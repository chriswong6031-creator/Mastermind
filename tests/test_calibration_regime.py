"""Guards for the L5a REGIME-CONDITIONAL calibration extension (brain/calibration).

The extension adds two things without changing the existing pooled multiplier math:
  * every graded label carries the RISK-STATE at entry (the day's Strategist backdrop_stance), and
    each book seat now carries a `by_regime` split (risk_on / not_risk_on buckets);
  * the binary beat-SPY label gains a CONDITIONAL BOGEY leg: when the entry regime is NOT risk_on the
    bar rises to max(SPY, defensive) — so a defensive rotation that beat SPY into a down-tape but
    trailed the defensive sleeve is graded honestly, and one that beat BOTH is credited.

Plus the PRE-REGISTERED FALSIFIER: the conditional bogey must CHANGE a multiplier vs raw-SPY, else it
is flagged INERT loudly.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

import bot  # noqa: F401 — bootstraps vendor/macro
from brain import calibration as CAL


@pytest.fixture(autouse=True)
def _reset_caches():
    CAL._reset_run_caches()
    yield
    CAL._reset_run_caches()


# ── _is_risk_on / conditional-beat pure logic ────────────────────────────────
def test_is_risk_on_classifies_states():
    assert CAL._is_risk_on("risk_on") is True
    assert CAL._is_risk_on("RISK_ON") is True
    assert CAL._is_risk_on("caution") is False
    assert CAL._is_risk_on("risk_off") is False
    assert CAL._is_risk_on(None) is False        # unknown → NOT risk_on (no easy-bogey alibi)


def test_conditional_beat_risk_on_is_plain_spy():
    # risk_on → bogey is SPY; beat ⇔ rel_return >= 0 (no defensive edge consulted)
    assert CAL._conditional_beat(0.01, "2026-05-01", "risk_on", dt.date(2026, 6, 1)) == 1
    assert CAL._conditional_beat(-0.01, "2026-05-01", "risk_on", dt.date(2026, 6, 1)) == 0


def test_conditional_beat_down_tape_raises_the_bar(monkeypatch):
    # NOT risk_on + a positive defensive edge (defense out-ran SPY by +4%): a name that beat SPY by
    # only +2% did NOT clear max(SPY, defensive) → graded a MISS; a name that beat by +5% clears it.
    monkeypatch.setattr(CAL, "_defensive_edge", lambda entry, asof: 0.04)
    assert CAL._conditional_beat(0.02, "2026-05-01", "caution", dt.date(2026, 6, 1)) == 0
    assert CAL._conditional_beat(0.05, "2026-05-01", "caution", dt.date(2026, 6, 1)) == 1


def test_conditional_beat_missing_edge_degrades_to_spy(monkeypatch):
    # NOT risk_on but the defensive curve is unavailable → bar collapses to SPY-only (max(0, None))
    monkeypatch.setattr(CAL, "_defensive_edge", lambda entry, asof: None)
    assert CAL._conditional_beat(0.001, "2026-05-01", "caution", dt.date(2026, 6, 1)) == 1
    assert CAL._conditional_beat(-0.001, "2026-05-01", "caution", dt.date(2026, 6, 1)) == 0


def test_negative_defensive_edge_does_not_lower_the_bar(monkeypatch):
    # defense LAGGED SPY (edge -3%) in a down-tape → max(0, -0.03)=0 → bar stays at SPY (never easier)
    monkeypatch.setattr(CAL, "_defensive_edge", lambda entry, asof: -0.03)
    assert CAL._conditional_beat(0.0, "2026-05-01", "risk_off", dt.date(2026, 6, 1)) == 1
    assert CAL._conditional_beat(-0.001, "2026-05-01", "risk_off", dt.date(2026, 6, 1)) == 0


# ── regime tagging from the Strategist verdict ───────────────────────────────
def test_regime_at_reads_backdrop_stance(monkeypatch, tmp_path):
    d = tmp_path / "committee" / "2026-05-01" / "_FLAGSHIP"
    d.mkdir(parents=True)
    (d / "strategist.json").write_text(json.dumps(
        {"agent": "strategist", "verdict": {"backdrop_stance": "caution"}}))
    monkeypatch.setattr(CAL, "_COMMITTEE", tmp_path / "committee")
    CAL._reset_run_caches()
    assert CAL._regime_at("2026-05-01") == "caution"
    assert CAL._regime_at("2099-01-01") is None       # absent → None (P2)


# ── bucketize splits risk_on vs not_risk_on and holds thin buckets inert ─────
def test_bucketize_splits_and_holds_thin_buckets():
    # 10 risk_on rows (enough to score) + 3 not_risk_on rows (below min_bucket_n → inert)
    tagged = [(0, 0.8, "risk_on")] * 5 + [(1, 0.8, "risk_on")] * 5 + [(0, 0.9, "caution")] * 3
    b = CAL._bucketize(tagged)
    assert b["risk_on"]["n"] == 10
    assert b["not_risk_on"]["n"] == 3
    assert b["not_risk_on"]["multiplier"] == 1.0        # thin → inert
    assert b["not_risk_on"]["status"] == "building"


# ── conditional bogey wired into book reliability ────────────────────────────
def _write_book(root, portfolio_id, rows):
    p = root / portfolio_id
    p.mkdir(parents=True, exist_ok=True)
    (p / "decisions.jsonl").write_text("\n".join(json.dumps(r) for r in rows))


def test_book_reliability_carries_by_regime(monkeypatch, tmp_path):
    # one down-tape decision date, two held names; stub the defensive edge + labels
    monkeypatch.setattr(CAL, "_PORTFOLIOS", tmp_path)
    monkeypatch.setattr(CAL, "_COMMITTEE", tmp_path / "committee")
    monkeypatch.setattr(CAL, "_regime_at", lambda d: "caution")
    monkeypatch.setattr(CAL, "_defensive_edge", lambda entry, asof: 0.03)  # defense beat SPY +3%
    monkeypatch.setattr(CAL, "_elapsed", lambda d, asof: True)

    def _label(tk, d_iso, asof, vs="SPY"):
        # NAMEA beat SPY by +5% (clears the +3% defensive bar → WIN);
        # NAMEB beat SPY by only +1% (below the +3% bar → LOSS under conditional bogey)
        return {"resolved": True, "rel_return": {"NAMEA": 0.05, "NAMEB": 0.01}[tk]}
    monkeypatch.setattr(CAL, "_label_name", _label)

    _write_book(tmp_path, "autonomous", [
        {"asof": "2026-05-01", "holdings": [{"ticker": "NAMEA", "conviction": "high"},
                                            {"ticker": "NAMEB", "conviction": "high"}]}])
    CAL._reset_run_caches()
    block = CAL._book_reliability(dt.date(2026, 6, 1), "autonomous", "SPY")
    assert block["n"] == 2
    assert block["reliability"] == 0.5                 # 1 win (NAMEA), 1 loss (NAMEB) under conditional
    assert "by_regime" in block
    assert block["by_regime"]["not_risk_on"]["n"] == 2


# ── the pre-registered falsifier ─────────────────────────────────────────────
def test_conditional_bogey_falsifier_active_when_bar_bites(monkeypatch, tmp_path):
    monkeypatch.setattr(CAL, "_PORTFOLIOS", tmp_path)
    monkeypatch.setattr(CAL, "_COMMITTEE", tmp_path / "committee")
    monkeypatch.setattr(CAL, "_regime_at", lambda d: "caution")
    monkeypatch.setattr(CAL, "_defensive_edge", lambda entry, asof: 0.03)
    monkeypatch.setattr(CAL, "_elapsed", lambda d, asof: True)

    def _label(tk, d_iso, asof, vs="SPY"):
        # every name beat SPY by +1% → raw-SPY grades ALL wins (reliability 1.0);
        # under the +3% conditional bar ALL become losses (reliability 0.0) → the multiplier MOVES.
        return {"resolved": True, "rel_return": 0.01}
    monkeypatch.setattr(CAL, "_label_name", _label)

    _write_book(tmp_path, "autonomous", [
        {"asof": f"2026-05-{i:02d}", "holdings": [{"ticker": f"N{i}", "conviction": "high"}]}
        for i in range(1, 15)])
    CAL._reset_run_caches()
    f = CAL.conditional_bogey_falsifier(dt.date(2026, 6, 1), books=("autonomous",))
    assert f["inert"] is False
    assert "ACTIVE" in f["verdict"]
    row = f["books"][0]
    assert row["raw_spy_multiplier"] != row["conditional_multiplier"]


def test_conditional_bogey_falsifier_inert_says_so_loudly(monkeypatch, tmp_path):
    # all decisions in risk_on → the conditional bogey == plain SPY → the multiplier CANNOT move.
    monkeypatch.setattr(CAL, "_PORTFOLIOS", tmp_path)
    monkeypatch.setattr(CAL, "_COMMITTEE", tmp_path / "committee")
    monkeypatch.setattr(CAL, "_regime_at", lambda d: "risk_on")
    monkeypatch.setattr(CAL, "_defensive_edge", lambda entry, asof: 0.03)
    monkeypatch.setattr(CAL, "_elapsed", lambda d, asof: True)
    monkeypatch.setattr(CAL, "_label_name",
                        lambda tk, d_iso, asof, vs="SPY": {"resolved": True, "rel_return": 0.01})
    _write_book(tmp_path, "autonomous", [
        {"asof": f"2026-05-{i:02d}", "holdings": [{"ticker": f"N{i}", "conviction": "high"}]}
        for i in range(1, 15)])
    CAL._reset_run_caches()
    f = CAL.conditional_bogey_falsifier(dt.date(2026, 6, 1), books=("autonomous",))
    assert f["inert"] is True
    assert "INERT" in f["verdict"]


# ── regime_multiplier accessor ───────────────────────────────────────────────
def test_regime_multiplier_prefers_scoring_bucket(monkeypatch, tmp_path):
    monkeypatch.setattr(CAL, "_PATH", tmp_path / "cal.json")
    block = {"agents": {"autonomous": {
        "multiplier": 0.9,
        "by_regime": {
            "risk_on": {"multiplier": 1.0, "status": "scoring", "n": 20},
            "not_risk_on": {"multiplier": 0.6, "status": "scoring", "n": 12},
        }}}}
    (tmp_path / "cal.json").write_text(json.dumps(block))
    assert CAL.regime_multiplier("autonomous", "caution") == 0.6      # not_risk_on bucket
    assert CAL.regime_multiplier("autonomous", "risk_on") == 1.0      # risk_on bucket


def test_regime_multiplier_falls_back_to_pooled_when_bucket_thin(monkeypatch, tmp_path):
    monkeypatch.setattr(CAL, "_PATH", tmp_path / "cal.json")
    block = {"agents": {"autonomous": {
        "multiplier": 0.9,
        "by_regime": {"not_risk_on": {"multiplier": 0.5, "status": "building", "n": 3}}}}}
    (tmp_path / "cal.json").write_text(json.dumps(block))
    # building bucket → fall back to pooled 0.9 (never over-de-confidence on a thin slice)
    assert CAL.regime_multiplier("autonomous", "caution") == 0.9
    assert CAL.regime_multiplier("unknown_agent", "caution") == 1.0


def test_compute_carries_conditional_bogey_block(monkeypatch, tmp_path):
    # cold-start compute still exposes the falsifier verdict block (unavailable → building)
    monkeypatch.setattr("brain.ledger.all_theses", lambda: [])
    monkeypatch.setattr(CAL, "_COMMITTEE", tmp_path / "none")
    monkeypatch.setattr(CAL, "_PORTFOLIOS", tmp_path / "none")
    block = CAL.compute(dt.date(2026, 6, 1))
    assert "conditional_bogey" in block
    assert "min_bucket_n" in block
