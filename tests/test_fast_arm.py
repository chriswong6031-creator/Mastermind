"""FAST-ARM — universe-fed per-seat grading (brain.calibration).

The owned-book grader resolves ~one thesis per name per month, so a seat needs many months to clear
MIN_N. The macro engine logs a forward, leakage-free label on ~1,600 names every build
(portfolio/predictions.py). The universe-fed path grades the STRATEGIST (confirmed-theme member
tickers), PM-CONVICTION (single-name picks) and the entry-TIMING read ('up' calls) against THAT log,
date-clustered to independent windows so effective-n is honest. We keep owned-book grading and prefer
whichever has the higher effective sample.

OFFLINE: no LLM, no vendor parquet, no network. We feed a SYNTHETIC predictions ledger via a dual
patch (package attr portfolio.predictions._load_ledger + sys.modules) and synthetic committee/ledger
artifacts under a tmp_path-rooted tree, exactly as test_role_grading.py does for the owned-book seats.

We prove: (1) a seat reaches scoring via the universe path while the owned-book path alone is still
building; (2) the date-clustering collapses many same-window entry dates to one independent cluster
(no fake power); (3) cold-start inertia below MIN_N clusters; (4) compute() registers the new `timing`
seat, records BOTH n's, prefers the larger, and never raises.
"""
from __future__ import annotations

import json
import sys
import types
from datetime import date

import pytest

import brain
from brain import calibration


ASOF = date(2026, 6, 23)


# ───────────────────────────── synthetic substrate ─────────────────────────────
def _patch_predictions(monkeypatch, ledger_rows, horizon=21):
    """Dual-patch portfolio.predictions with a fake exposing _load_ledger / _thin_independent / _HORIZON.
    _thin_independent is the REAL greedy algorithm (we want the genuine clustering), reimplemented here
    so the fake is self-contained and matches the production one bit-for-bit."""
    import numpy as np
    import pandas as pd

    fake = types.ModuleType("portfolio.predictions")
    fake._HORIZON = horizon

    def _load_ledger():
        return list(ledger_rows)

    def _thin_independent(pairs):
        kept, last = [], None
        for d, v in sorted(pairs, key=lambda x: x[0]):
            try:
                ts = pd.Timestamp(d)
            except Exception:
                continue
            if last is None or int(np.busday_count(last.date(), ts.date())) >= horizon:
                kept.append(v)
                last = ts
        return kept

    fake._load_ledger = _load_ledger
    fake._thin_independent = _thin_independent

    import portfolio
    monkeypatch.setattr(portfolio, "predictions", fake, raising=False)
    monkeypatch.setitem(sys.modules, "portfolio.predictions", fake)
    return fake


def _resolved(ticker, asof_iso, realized, *, dir_="up", prob=0.7):
    return {"ticker": ticker, "asof": asof_iso, "status": "resolved",
            "realized": realized, "dir": dir_, "prob": prob}


def _root(monkeypatch, tmp_path):
    committee = tmp_path / "committee"
    monkeypatch.setattr(calibration, "_COMMITTEE", committee)
    monkeypatch.setattr(calibration, "_GATE_OFFICER", tmp_path / "gate_officer")
    monkeypatch.setattr(calibration, "_RISK_OFFICER", tmp_path / "risk_officer")
    return committee


def _write_strategist(committee_root, d, themes):
    p = committee_root / d / "_FLAGSHIP"
    p.mkdir(parents=True, exist_ok=True)
    (p / "strategist.json").write_text(json.dumps(
        {"agent": "strategist", "verdict": {"confirmed_themes": themes}}))


def _patch_ledger(monkeypatch, theses):
    fake = types.ModuleType("brain.ledger")
    fake.all_theses = lambda: theses
    monkeypatch.setattr(brain, "ledger", fake, raising=False)
    monkeypatch.setitem(sys.modules, "brain.ledger", fake)
    return fake


def _well_spaced_dates(n):
    """n entry dates a fixed 40 CALENDAR days apart — 40 calendar days is always >= 21 business days
    (28 business days at most), so each survives _thin_independent as its own independent cluster.
    Anchored well before ASOF so every window is fully elapsed (leakage-free)."""
    from datetime import timedelta
    base = date(2024, 1, 2)
    return [(base + timedelta(days=40 * k)).isoformat() for k in range(n)]


# ───────────────────────────── TIMING (purest universe-native seat) ─────────────────────────────
def test_timing_arms_via_universe_in_weeks(monkeypatch, tmp_path):
    """14 independent entry-date clusters of 'up' calls that all went up → reliability ~1.0, scoring.
    Owned book has NO timing grader (returns empty) so it would NEVER arm — the universe path does."""
    _root(monkeypatch, tmp_path)
    rows = []
    for d in _well_spaced_dates(14):
        # each cluster: 20 names that day, all up and all positive → date hit-rate 1.0
        rows += [_resolved(f"T{d}_{i}", d, +0.04, dir_="up") for i in range(20)]
    _patch_predictions(monkeypatch, rows)

    out = calibration._universe_timing(ASOF, calibration._pred_resolved_index())
    assert out["effective_n"] == 14            # 14 independent date-clusters
    assert out["status"] == "scoring"
    assert out["reliability"] == pytest.approx(1.0)


def test_timing_clustering_collapses_overlapping_dates(monkeypatch, tmp_path):
    """30 entry dates all within ONE 21-business-day window collapse to a SINGLE independent cluster —
    breadth cannot manufacture independent samples. effective_n must be 1, status building."""
    _root(monkeypatch, tmp_path)
    rows = []
    for day in range(1, 16):                   # 2026-01-01..-15 — all inside one 21-bday window
        d = date(2026, 1, day).isoformat()
        rows += [_resolved(f"U{day}_{i}", d, +0.03, dir_="up") for i in range(20)]
    _patch_predictions(monkeypatch, rows)

    out = calibration._universe_timing(ASOF, calibration._pred_resolved_index())
    assert out["effective_n"] == 1             # one cluster despite 15 dates × 20 names
    assert out["status"] == "building"
    assert out["multiplier"] == 1.0            # cold-start inert


def test_timing_cold_start_inert_below_min_n(monkeypatch, tmp_path):
    _root(monkeypatch, tmp_path)
    rows = []
    for d in _well_spaced_dates(5):            # only 5 independent clusters < MIN_N=12
        rows += [_resolved(f"C{d}_{i}", d, -0.05, dir_="up") for i in range(15)]
    _patch_predictions(monkeypatch, rows)

    out = calibration._universe_timing(ASOF, calibration._pred_resolved_index())
    assert out["effective_n"] == 5
    assert out["status"] == "building"
    assert out["multiplier"] == 1.0


# ───────────────────────────── STRATEGIST: universe arms, owned-book starves ─────────────────────────────
def test_strategist_universe_arms_while_owned_book_builds(monkeypatch, tmp_path):
    """Same committee artifacts feed both paths. The universe ledger has resolved labels for the theme
    members across 13 well-spaced dates → 13 independent clusters → universe SCORING. The owned-book
    path resolves via label_thesis; we stub it to return NOTHING (names not yet resolved on the owned
    horizon), so owned-book n=0 / building. _prefer must choose the universe path."""
    committee = _root(monkeypatch, tmp_path)
    dates = _well_spaced_dates(13)
    ledger_rows = []
    for d in dates:
        members = [f"M{d}_{i}" for i in range(6)]
        _write_strategist(committee, d, [
            {"theme": "data_center_power", "stage": "confirmed", "leadership": 0.8, "names": members}])
        ledger_rows += [_resolved(m, d, +0.03, dir_="up") for m in members]   # all beat SPY
    _patch_predictions(monkeypatch, ledger_rows)

    # owned-book path: label_thesis resolves NOTHING (universe is the only matured sample)
    fake_out = types.ModuleType("brain.outcomes")
    fake_out.label_thesis = lambda thesis, asof=None: {"resolved": False, "rel_return": None}
    monkeypatch.setattr(brain, "outcomes", fake_out, raising=False)
    monkeypatch.setitem(sys.modules, "brain.outcomes", fake_out)

    idx = calibration._pred_resolved_index()
    uni = calibration._universe_strategist(ASOF, idx)
    assert uni["effective_n"] == 13 and uni["status"] == "scoring"

    owned = calibration._strategist_reliability(ASOF)
    assert owned["n"] == 0 and owned["status"] == "building"

    combined = calibration._prefer(owned, uni)
    assert combined["source"] == "universe"
    assert combined["status"] == "scoring"
    assert combined["owned_n"] == 0
    assert combined["universe_effective_n"] == 13
    assert "owned_book" in combined["paths"] and "universe" in combined["paths"]


def test_strategist_universe_skips_emerging_and_unresolved(monkeypatch, tmp_path):
    """Emerging-stage themes are excluded; members absent from the resolved ledger are skipped."""
    committee = _root(monkeypatch, tmp_path)
    d = "2026-01-02"
    _write_strategist(committee, d, [
        {"theme": "x", "stage": "emerging", "leadership": 0.9, "names": ["E1", "E2"]},
        {"theme": "y", "stage": "confirmed", "leadership": 0.7, "names": ["C1", "GHOST"]},
    ])
    # only C1 is resolved in the ledger; GHOST and the emerging members are not
    _patch_predictions(monkeypatch, [_resolved("C1", d, +0.02)])
    out = calibration._universe_strategist(ASOF, calibration._pred_resolved_index())
    assert out["effective_n"] == 1             # one date-cluster, only C1 contributed
    assert out["reliability"] == pytest.approx(1.0)


# ───────────────────────────── PM-CONVICTION: universe path ─────────────────────────────
def test_pm_universe_grades_conv_theses(monkeypatch, tmp_path):
    """PM single-name picks (id …-conv) graded against the universe ledger by (ticker, state_asof)."""
    _root(monkeypatch, tmp_path)
    dates = _well_spaced_dates(12)
    theses, ledger_rows = [], []
    for k, d in enumerate(dates):
        tk = f"P{k}"
        theses.append({"id": f"{d}-{tk}-conv", "state_asof": d, "raw_prob_correct": 0.65,
                       "entry_levels": {"ticker": tk},
                       "falsifier": {"check": {"kind": "rel_return", "subject_ticker": tk}}})
        ledger_rows.append(_resolved(tk, d, +0.05 if k % 2 == 0 else -0.05))
    _patch_predictions(monkeypatch, ledger_rows)
    _patch_ledger(monkeypatch, theses)

    out = calibration._universe_pm(ASOF, calibration._pred_resolved_index())
    assert out["effective_n"] == 12 and out["status"] == "scoring"
    assert out["reliability"] == pytest.approx(0.5)   # half the picks beat SPY
    assert out["mean_confidence"] == pytest.approx(0.65)


def test_pm_universe_ignores_non_conv_theses(monkeypatch, tmp_path):
    _root(monkeypatch, tmp_path)
    d = "2026-01-02"
    theses = [{"id": f"{d}-FORGE1-thesis", "state_asof": d, "raw_prob_correct": 0.6,
               "entry_levels": {"ticker": "FORGE1"},
               "falsifier": {"check": {"kind": "rel_return", "subject_ticker": "FORGE1"}}}]
    _patch_predictions(monkeypatch, [_resolved("FORGE1", d, +0.05)])
    _patch_ledger(monkeypatch, theses)
    out = calibration._universe_pm(ASOF, calibration._pred_resolved_index())
    assert out["effective_n"] == 0             # non-conv thesis not a PM pick


# ───────────────────────────── _prefer + compute() integration ─────────────────────────────
def test_prefer_picks_higher_effective_sample_and_records_both():
    owned = {"n": 4, "reliability": 0.5, "mean_confidence": 0.6, "multiplier": 1.0, "status": "building"}
    uni = {"n": 13, "effective_n": 13, "reliability": 0.7, "mean_confidence": 0.7,
           "multiplier": 1.0, "status": "scoring"}
    out = calibration._prefer(owned, uni)
    assert out["source"] == "universe"
    assert out["owned_n"] == 4 and out["universe_effective_n"] == 13
    # owned wins when it has the larger sample
    owned2 = {**owned, "n": 20}
    uni2 = {**uni, "effective_n": 3}
    out2 = calibration._prefer(owned2, uni2)
    assert out2["source"] == "owned_book"
    assert out2["owned_n"] == 20 and out2["universe_effective_n"] == 3


def test_compute_registers_timing_seat_and_never_raises(monkeypatch, tmp_path):
    _root(monkeypatch, tmp_path)
    _patch_ledger(monkeypatch, [])
    # universe ledger arms TIMING; owned-book seats stay inert (label_thesis resolves nothing)
    dates = _well_spaced_dates(13)
    rows = []
    for d in dates:
        rows += [_resolved(f"Z{d}_{i}", d, +0.04, dir_="up") for i in range(15)]
    _patch_predictions(monkeypatch, rows)
    fake_out = types.ModuleType("brain.outcomes")
    fake_out.label_thesis = lambda thesis, asof=None: {"resolved": False, "rel_return": None}
    monkeypatch.setattr(brain, "outcomes", fake_out, raising=False)
    monkeypatch.setitem(sys.modules, "brain.outcomes", fake_out)

    block = calibration.compute(ASOF)
    for seat in ("forge", "sentinel", "strategist", "pm", "timing", "gate", "risk"):
        assert seat in block["agents"]
    # universe armed TIMING in "weeks" of clusters; it carries both n's and source
    timing = block["agents"]["timing"]
    assert timing["source"] == "universe"
    assert timing["status"] == "scoring"
    assert timing["universe_effective_n"] == 13
    assert "paths" in timing


def test_compute_universe_unavailable_is_inert(monkeypatch, tmp_path):
    """If portfolio.predictions can't load (no ledger), the universe path is empty and compute() falls
    back to owned-book without raising; the new fast-arm seats are simply cold."""
    _root(monkeypatch, tmp_path)
    _patch_ledger(monkeypatch, [])
    fake = types.ModuleType("portfolio.predictions")
    fake._HORIZON = 21
    fake._load_ledger = lambda: (_ for _ in ()).throw(RuntimeError("no ledger"))
    fake._thin_independent = lambda pairs: []
    import portfolio
    monkeypatch.setattr(portfolio, "predictions", fake, raising=False)
    monkeypatch.setitem(sys.modules, "portfolio.predictions", fake)
    fake_out = types.ModuleType("brain.outcomes")
    fake_out.label_thesis = lambda thesis, asof=None: {"resolved": False, "rel_return": None}
    monkeypatch.setattr(brain, "outcomes", fake_out, raising=False)
    monkeypatch.setitem(sys.modules, "brain.outcomes", fake_out)

    block = calibration.compute(ASOF)
    assert block["agents"]["timing"]["multiplier"] == 1.0   # cold / inert, no raise
    assert "timing" in block["agents"]
