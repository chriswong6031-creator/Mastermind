"""Tests for the portfolio safety engine (portfolio/safety.py).

Deterministic + offline: synthetic geometric-random-walk price series are injected via
monkeypatch, so nothing here touches the network or the vendored parquet store.
"""
from __future__ import annotations

import bot  # noqa: F401  -> vendor/macro bootstrap before portfolio imports

import numpy as np
import pandas as pd
import pytest

from portfolio import safety


def _synth_series(seed: int, n: int = 900, mu: float = 0.0004, sigma: float = 0.013,
                  start_px: float = 100.0) -> pd.Series:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end=pd.Timestamp("2026-06-20"), periods=n)
    rets = rng.normal(mu, sigma, n)
    px = start_px * np.cumprod(1.0 + rets)
    return pd.Series(px, index=idx)


@pytest.fixture
def fake_prices(monkeypatch):
    """Full price coverage for any ticker (stable seed from the symbol) + a market SPY."""
    spy = _synth_series(999, mu=0.0003, sigma=0.009)

    def fake_fetch(ticker: str):
        if ticker == "SPY":
            return spy
        return _synth_series(sum(ord(c) for c in (ticker or "X")))

    monkeypatch.setattr("portfolio.paper_account._fetch_price_series", fake_fetch, raising=False)
    monkeypatch.setattr("data_layer.polygon.daily_closes", lambda *a, **k: {}, raising=False)
    return fake_fetch


def _set_book(monkeypatch, weights: dict[str, float], cash: float) -> None:
    monkeypatch.setattr(safety, "book_weights", lambda pid: {
        "weights": dict(weights), "cash_weight": cash,
        "invested_weight": float(sum(weights.values())), "nav": 1_000_000.0,
        "source": "held", "n_positions": len(weights)})


# --------------------------------------------------------------------------- #
# scoring helpers (pure)
# --------------------------------------------------------------------------- #
def test_score_linear_clamp():
    assert safety._score(0.0, 0.0, 0.45) == 100.0
    assert safety._score(0.45, 0.0, 0.45) == 0.0
    assert safety._score(-1.0, 0.0, 0.45) == 100.0      # past "good" → clamped at 100
    assert safety._score(2.0, 0.0, 0.45) == 0.0         # past "bad"  → clamped at 0
    mid = safety._score(0.225, 0.0, 0.45)
    assert 49.0 < mid < 51.0
    assert safety._score(None, 0.0, 1.0) is None
    assert safety._score(float("nan"), 0.0, 1.0) is None


def test_grade_and_verdict_bands():
    assert safety._grade(90) == "A"
    assert safety._grade(72) == "B"
    assert safety._grade(60) == "C"
    assert safety._grade(45) == "D"
    assert safety._grade(10) == "F"
    assert safety._grade(None) == "—"
    assert isinstance(safety._verdict(80), str) and safety._verdict(None)


# --------------------------------------------------------------------------- #
# full report
# --------------------------------------------------------------------------- #
def test_full_report_shape_and_ranges(monkeypatch, fake_prices):
    _set_book(monkeypatch, {"AAA": 0.3, "BBB": 0.2, "CCC": 0.2, "DDD": 0.1}, 0.2)
    r = safety.compute_safety("flagship")

    assert r["safety_score"] is not None
    assert 0.0 <= r["safety_score"] <= 100.0
    assert r["grade"] in {"A", "B", "C", "D", "F"}

    m = r["metrics"]
    assert m["max_drawdown_pct"] <= 0.0            # drawdown is non-positive
    assert m["ann_volatility_pct"] > 0.0
    assert m["beta_spy"] is not None
    assert m["effective_n"] > 1.0
    assert m["mean_pairwise_corr"] is not None
    assert m["absorption_ratio"] is not None
    assert isinstance(m["per_name"], list) and len(m["per_name"]) == 4
    # risk contributions sum to ~1 across covered names
    rc = [p["risk_contribution"] for p in m["per_name"] if p["risk_contribution"] is not None]
    assert abs(sum(rc) - 1.0) < 0.05

    assert set(r["subscores"]) >= {"drawdown", "volatility", "diversification",
                                   "concentration", "beta", "tail", "cash"}
    assert isinstance(r["breaches"], list)
    assert any("counterfactual" in c.lower() for c in r["caveats"])
    assert r["backtest"]["rebalanced"] is False


def test_bootstrap_ci_present(monkeypatch, fake_prices):
    _set_book(monkeypatch, {"AAA": 0.25, "BBB": 0.25, "CCC": 0.25}, 0.25)
    r = safety.compute_safety("flagship", bootstrap=True)
    m = r["metrics"]
    assert m.get("max_drawdown_ci_pct") and len(m["max_drawdown_ci_pct"]) == 3
    lo, med, hi = m["max_drawdown_ci_pct"]
    assert lo <= med <= hi                          # percentile order


def test_no_bootstrap_skips_ci(monkeypatch, fake_prices):
    _set_book(monkeypatch, {"AAA": 0.5, "BBB": 0.5}, 0.0)
    r = safety.compute_safety("flagship", bootstrap=False)
    assert "max_drawdown_ci_pct" not in r["metrics"]


# --------------------------------------------------------------------------- #
# concentration / diversification sensitivity
# --------------------------------------------------------------------------- #
def test_concentrated_book_scores_worse_than_diversified(monkeypatch, fake_prices):
    _set_book(monkeypatch, {"AAA": 0.95}, 0.05)
    conc = safety.compute_safety("flagship")
    _set_book(monkeypatch, {"AAA": 0.16, "BBB": 0.16, "CCC": 0.16,
                            "DDD": 0.16, "EEE": 0.16}, 0.2)
    div = safety.compute_safety("flagship")

    assert div["subscores"]["concentration"] > conc["subscores"]["concentration"]
    assert div["metrics"]["effective_n"] > conc["metrics"]["effective_n"]


def test_name_cap_breach_excludes_etfs(monkeypatch, fake_prices):
    # XLK (an ETF) at 30% must NOT breach the single-name cap; GOOG (a stock) at 30% must.
    _set_book(monkeypatch, {"XLK": 0.3, "GOOG": 0.3, "AAA": 0.2}, 0.2)
    r = safety.compute_safety("flagship")
    name_cap = [b for b in r["breaches"] if b["code"] == "name_cap"]
    flagged = {b["ticker"] for b in name_cap}
    assert "GOOG" in flagged
    assert "XLK" not in flagged


# --------------------------------------------------------------------------- #
# empty / persistence
# --------------------------------------------------------------------------- #
def test_empty_book_is_graceful(monkeypatch):
    _set_book(monkeypatch, {}, 1.0)
    r = safety.compute_safety("flagship")
    assert r["safety_score"] is None
    assert r["n_positions"] == 0
    assert "note" in r


def test_persist_and_load_roundtrip(monkeypatch, fake_prices, tmp_path):
    monkeypatch.setattr(safety, "_safety_path", lambda pid: tmp_path / f"safety_{pid}.json")
    _set_book(monkeypatch, {"AAA": 0.4, "BBB": 0.3, "CCC": 0.1}, 0.2)
    r = safety.compute_safety("flagship")
    safety.persist(r, "flagship")
    back = safety.load_safety("flagship")
    assert back is not None
    assert back["safety_score"] == r["safety_score"]


def test_safety_for_all_summary(monkeypatch, fake_prices, tmp_path):
    monkeypatch.setattr(safety, "_safety_path", lambda pid: tmp_path / f"safety_{pid}.json")
    _set_book(monkeypatch, {"AAA": 0.5, "BBB": 0.3}, 0.2)
    out = safety.safety_for_all(bootstrap=False)
    assert "portfolios" in out
    # every registered portfolio gets an entry (score or error), never raises
    assert len(out["portfolios"]) >= 1


# --------------------------------------------------------------------------- #
# CONSUMED levers: explicit proposed-book weights, gross overlay, detectors
# --------------------------------------------------------------------------- #
def test_compute_safety_with_explicit_weights(monkeypatch, fake_prices):
    r = safety.compute_safety("flagship", weights={"AAA": 0.4, "BBB": 0.4}, cash_weight=0.2)
    assert r["source"] == "proposed"
    assert r["safety_score"] is not None
    assert r["cash_weight"] == 0.2


def _rep(score=None, invested=1.0, **metrics):
    return {"safety_score": score, "invested_weight": invested, "metrics": metrics, "breaches": []}


def test_gross_overlay_safe_book_is_noop():
    ov = safety.gross_overlay(_rep(85, max_drawdown_pct=-15, beta_spy=0.9, absorption_ratio=0.4))
    assert ov["gross_mult"] == 1.0
    assert ov["cash_added"] == 0.0
    assert ov["reasons"] == []


def test_gross_overlay_never_levers_up():
    ov = safety.gross_overlay(_rep(99, max_drawdown_pct=-2, beta_spy=0.3, absorption_ratio=0.2))
    assert ov["gross_mult"] == 1.0          # subtract-only: never > 1.0


def test_gross_overlay_degrosses_low_score():
    ov = safety.gross_overlay(_rep(40, invested=0.8))   # score_min -> floored to gross_floor
    assert 0.5 <= ov["gross_mult"] < 1.0
    assert ov["cash_added"] > 0.0
    assert "low_safety_score" in ov["reasons"]


def test_gross_overlay_deep_drawdown_and_high_beta():
    dd = safety.gross_overlay(_rep(80, max_drawdown_pct=-45))   # dd_hard -> floor 0.5
    assert dd["gross_mult"] <= 0.6 and "deep_drawdown" in dd["reasons"]
    hb = safety.gross_overlay(_rep(80, beta_spy=2.2))           # 1.10/2.2 = 0.5
    assert abs(hb["gross_mult"] - 0.5) < 0.02 and "high_beta" in hb["reasons"]


def test_gross_overlay_most_conservative_trigger_wins():
    ov = safety.gross_overlay(_rep(55, max_drawdown_pct=-45, beta_spy=2.2))
    # several triggers fire; the smallest (most conservative) gross_mult is taken
    assert ov["gross_mult"] == min(t["gross_mult"] for t in ov["triggers"])


def test_gross_overlay_disabled_is_noop():
    cfg = safety._cfg()
    cfg = {**cfg, "overlay": {**cfg["overlay"], "enabled": False}}
    ov = safety.gross_overlay(_rep(10), cfg=cfg)
    assert ov["gross_mult"] == 1.0 and ov["enabled"] is False


def test_fragility_detectors_shape_and_severity():
    rep = {"breaches": [
        {"code": "deep_drawdown", "severity": "flag", "message": "deep"},
        {"code": "fragile_one_factor", "severity": "warn", "message": "fragile"},
        {"code": "name_cap", "severity": "warn", "ticker": "GOOG", "message": "cap"},
    ]}
    dets = safety.fragility_detectors(rep, "self")
    assert {d["code"] for d in dets} == {"D7"}
    risks = {d["payload"]["risk"] for d in dets}
    assert "deep_drawdown" in risks and "fragile_one_factor" in risks
    assert "name_cap" not in risks                    # name cap is owned by D6, not duplicated
    frag = [d for d in dets if d["payload"]["risk"] == "fragile_one_factor"][0]
    assert frag["severity"] == "veto"                 # warn -> veto in self mode
    assert frag["subject"] == "book"
    # operator mode never vetoes
    assert all(d["severity"] == "flag" for d in safety.fragility_detectors(rep, "operator"))
