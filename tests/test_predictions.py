"""Guards for the universe-wide prediction log + date-clustered cross-sectional scoring.

Safety-critical properties:
  * the labeler is LEAKAGE-FREE — anchors both baselines to the same close ≤ entry, resolves only
    once the horizon has elapsed, and never reads a close after asof;
  * predictions are deduped-while-open (one bet per name) and isolated to the sandbox (never the prod
    conviction ledger);
  * the cross-sectional scorer recovers a real signal (positive, significant IC when score predicts
    forward returns) and stays honestly 'building' below the date-cluster minimum;
  * nothing raises on missing data.
"""
from __future__ import annotations

import datetime as dt

import pytest

import bot  # noqa: F401 — vendor/macro on sys.path
from portfolio import predictions as P


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "_PRED_DIR", tmp_path)
    monkeypatch.setattr(P, "_LEDGER", tmp_path / "ledger.jsonl")
    return tmp_path


def _bdays(start: str, n: int):
    import pandas as pd
    return pd.bdate_range(start=start, periods=n)


def _panel(subj_vals, spy_vals, start="2026-01-05", subj="AAA"):
    import pandas as pd
    idx = _bdays(start, len(subj_vals))
    return ({subj: pd.Series(subj_vals, index=idx)},
            pd.Series(spy_vals, index=_bdays(start, len(spy_vals))))


# ── prob ──────────────────────────────────────────────────────────────────────
def test_prob_clamps():
    assert P._prob(100) == 0.75 and P._prob(-100) == 0.35
    assert P._prob(0) == 0.5 and P._prob("x") == 0.5


# ── leakage-free labeler ────────────────────────────────────────────────────────
def test_label_resolves_with_correct_rel_return():
    # subject +10% over 5 bdays, SPY flat → rel ≈ +0.10
    panel, spy = _panel([100, 102, 104, 106, 108, 110, 110], [400] * 7)
    rel = P._label(panel, spy, "AAA", "2026-01-05", 5, "2026-02-01")
    assert rel is not None and abs(rel - 0.10) < 1e-6


def test_label_subtracts_spy():
    # subject +10%, SPY +6% over 5 bdays → rel ≈ +0.04
    panel, spy = _panel([100, 102, 104, 106, 108, 110], [400, 404, 408, 412, 420, 424])
    rel = P._label(panel, spy, "AAA", "2026-01-05", 5, "2026-02-01")
    assert rel is not None and abs(rel - (0.10 - 0.06)) < 0.02


def test_label_unresolved_when_horizon_not_elapsed():
    panel, spy = _panel([100, 101, 102], [400, 400, 400])     # only 3 bdays, horizon 5
    assert P._label(panel, spy, "AAA", "2026-01-05", 5, "2026-02-01") is None


def test_label_no_lookahead_past_asof():
    # 10 bdays of data, but asof caps at the 3rd → horizon 5 can't resolve (no future peeking)
    panel, spy = _panel(list(range(100, 110)), [400] * 10)
    asof = _bdays("2026-01-05", 3)[-1].strftime("%Y-%m-%d")
    assert P._label(panel, spy, "AAA", "2026-01-05", 5, asof) is None


def test_label_missing_ticker_is_none():
    panel, spy = _panel([100, 101], [400, 400])
    assert P._label(panel, spy, "ZZZ", "2026-01-05", 5, "2026-02-01") is None


# ── ledger: dedup-while-open + isolation ────────────────────────────────────────
def test_record_dedups_while_open(sandbox, monkeypatch):
    monkeypatch.setattr(P, "universe", lambda: [{"ticker": "AAA", "dir": "up", "score": 40,
                                                 "band": "high", "price": 100.0}])
    monkeypatch.setattr(P, "_load_panel", lambda: None)   # no labeling → stays open
    monkeypatch.setattr(P, "_spy_series", lambda: None)
    P.record("2026-06-01")
    P.record("2026-06-02")                                  # same name, still open
    led = P._load_ledger()
    aaa = [r for r in led if r["ticker"] == "AAA"]
    # one OPEN row per horizon tier, NOT duplicated across the two build days (dedup on (ticker,horizon))
    assert len(aaa) == len(P._HORIZONS)
    assert sorted(r["horizon_d"] for r in aaa) == sorted(P._HORIZONS)
    assert (sandbox / "ledger.jsonl").exists()


def test_record_opens_one_canonical_id_per_name(sandbox, monkeypatch):
    # the canonical 21-bday tier keeps the legacy '...-pred' id (back-compat with existing rows); the
    # short tiers get '...-pred3/5/10'. This guarantees old 21-rows fold into the (tk,21) tier cleanly.
    monkeypatch.setattr(P, "universe", lambda: [{"ticker": "AAA", "dir": "up", "score": 40,
                                                 "band": "high", "price": 100.0}])
    monkeypatch.setattr(P, "_load_panel", lambda: None)
    monkeypatch.setattr(P, "_spy_series", lambda: None)
    P.record("2026-06-01")
    ids = {r["horizon_d"]: r["id"] for r in P._load_ledger() if r["ticker"] == "AAA"}
    assert ids[21] == "2026-06-01-AAA-pred"
    assert ids[3] == "2026-06-01-AAA-pred3"


def test_record_resolves_matured(sandbox, monkeypatch):
    monkeypatch.setattr(P, "universe", lambda: [{"ticker": "AAA", "dir": "up", "score": 40,
                                                 "band": "high", "price": 100.0}])
    panel, spy = _panel([100, 102, 104, 106, 108, 110, 110], [400] * 7, subj="AAA")
    monkeypatch.setattr(P, "_load_panel", lambda: panel)
    monkeypatch.setattr(P, "_spy_series", lambda: spy)
    # backdate the open prediction so the horizon has elapsed
    P._save_ledger([{"id": "x", "ticker": "AAA", "asof": "2026-01-05", "dir": "up", "score": 40,
                     "band": "high", "prob": 0.6, "entry_px": 100.0, "horizon_d": 5,
                     "status": "open", "realized": None, "resolved_on": None}])
    P.record("2026-02-01")
    led = P._load_ledger()
    aaa = [r for r in led if r["ticker"] == "AAA" and r["asof"] == "2026-01-05"][0]
    assert aaa["status"] == "resolved" and abs(aaa["realized"] - 0.10) < 1e-6


def test_record_never_raises_on_no_universe(sandbox, monkeypatch):
    monkeypatch.setattr(P, "universe", lambda: [])
    P.record("2026-06-01")                                  # must not raise


# ── cross-sectional scoring ─────────────────────────────────────────────────────
def _resolved(score, realized, date, ticker, direction="up"):
    return {"id": f"{date}-{ticker}", "ticker": ticker, "asof": date, "dir": direction,
            "score": score, "band": "neutral", "prob": P._prob(score), "entry_px": 100.0,
            "horizon_d": 21, "status": "resolved", "realized": realized, "resolved_on": date}


def test_score_recovers_real_signal(monkeypatch):
    # 9 entry dates spaced ~35 calendar days apart (unambiguously > the 21-bday horizon → all survive
    # thinning as INDEPENDENT clusters), each a strong cross-section → high positive IC, significant.
    import pandas as pd
    base = pd.Timestamp("2026-01-05")
    rows = []
    for di in range(9):
        d = (base + pd.Timedelta(days=35 * di)).strftime("%Y-%m-%d")
        for n in range(20):
            sc = (n - 10) * 10                                  # -100..+90
            realized = sc / 1000.0 + (0.0005 * (n % 3))         # monotone in score + tiny noise
            rows.append(_resolved(sc, realized, d, f"T{n}", "up" if sc >= 0 else "down"))
    monkeypatch.setattr(P, "_load_ledger", lambda: rows)
    sc = P.score("2026-12-01")
    assert sc["status"] == "scoring" and sc["effective_n"] >= 8
    assert sc["ic"]["mean_ic"] > 0.5 and sc["ic"]["significant"] is True
    assert sc["ic"]["ci95"][0] > 0                              # CI excludes the null


def test_score_building_below_min_dates(monkeypatch):
    rows = [_resolved((n - 10) * 10, (n - 10) * 0.01, "2026-01-15", f"T{n}") for n in range(20)]
    monkeypatch.setattr(P, "_load_ledger", lambda: rows)       # only ONE entry date
    sc = P.score("2026-03-01")
    assert sc["status"] == "building" and sc["effective_n"] == 1
    assert sc["ic"].get("mean_ic") is None or "note" in sc["ic"]


def test_score_thins_overlapping_dates_to_independent_clusters(monkeypatch):
    # 25 CONSECUTIVE business-day entry dates, each a strong cross-section. Overlapping 21-bday
    # windows are NOT independent — naive pooling would report ~25 clusters and false significance.
    # Thinning must collapse them to a handful of independent clusters → honest 'building', not a
    # spuriously-significant edge.
    import pandas as pd
    dates = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-01-05", periods=25)]
    rows = []
    for d in dates:
        for n in range(20):
            sc = (n - 10) * 10
            rows.append(_resolved(sc, sc / 1000.0, d, f"T{n}", "up" if sc >= 0 else "down"))
    monkeypatch.setattr(P, "_load_ledger", lambda: rows)
    sc = P.score("2026-03-01")
    assert sc["effective_n"] <= 3            # 25 overlapping dates → ~1-2 independent windows
    assert sc["status"] == "building" and not sc["ic"].get("significant")


def test_score_empty_is_valid(monkeypatch):
    monkeypatch.setattr(P, "_load_ledger", lambda: [])
    sc = P.score("2026-03-01")
    assert sc["status"] == "building" and sc["n_resolved"] == 0


def _resolved_h(score, realized, date, ticker, horizon, direction="up"):
    return {"id": f"{date}-{ticker}-pred{horizon}", "ticker": ticker, "asof": date, "dir": direction,
            "score": score, "band": "neutral", "prob": P._prob(score), "entry_px": 100.0,
            "horizon_d": horizon, "status": "resolved", "realized": realized, "resolved_on": date}


def test_fast_tier_matures_while_canonical_tier_is_empty(monkeypatch):
    # 10 entry dates each 4 BUSINESS days apart (≥ the 3-bday window → all independent for the 3-tier).
    # Rows are tagged horizon_d=3 ONLY. score(horizon=3) must reach 'scoring' (≥8 clusters) while
    # score(horizon=21) sees no 21-tier rows → honestly 'building'. This is the fast-tier unlock.
    import pandas as pd
    dates = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-01-05", periods=40)[::4]]
    rows = []
    for d in dates:
        for n in range(20):
            sc = (n - 10) * 10
            rows.append(_resolved_h(sc, sc / 1000.0, d, f"T{n}", 3, "up" if sc >= 0 else "down"))
    monkeypatch.setattr(P, "_load_ledger", lambda: rows)
    fast = P.score("2026-12-01", horizon=3)
    slow = P.score("2026-12-01", horizon=21)
    assert fast["status"] == "scoring" and fast["effective_n"] >= 8 and fast["horizon_d"] == 3
    assert fast["ic"]["mean_ic"] > 0.5 and fast["ic"]["significant"] is True
    assert slow["status"] == "building" and slow["n_resolved"] == 0   # no 21-tier rows → empty, honest


def test_score_filters_strictly_by_horizon(monkeypatch):
    # a 3-tier and a 21-tier row for the same name/date must not bleed into each other's scorecard
    rows = [_resolved_h(50, 0.05, "2026-01-05", "AAA", 3),
            _resolved_h(50, -0.09, "2026-01-05", "AAA", 21)]
    monkeypatch.setattr(P, "_load_ledger", lambda: rows)
    assert P.score("2026-06-01", horizon=3)["n_resolved"] == 1
    assert P.score("2026-06-01", horizon=21)["n_resolved"] == 1


def test_directional_reliability_curve_and_tilt(monkeypatch):
    # 30 'up' predictions stated at prob 0.70 but right only ~half the time → the engine is
    # OVERCONFIDENT: mean_predicted (0.70) >> realized hit-rate (~0.50). The reliability decomposition
    # (#6) must surface a non-empty curve, a positive calibration_error, and confidence_tilt.
    import pandas as pd
    base = pd.Timestamp("2026-01-05")
    rows = []
    for i in range(30):
        d = (base + pd.Timedelta(days=35 * (i % 9))).strftime("%Y-%m-%d")
        realized = 0.05 if i % 2 == 0 else -0.05            # 'up' is right exactly half the time
        rows.append({"id": f"x{i}", "ticker": f"T{i}", "asof": d, "dir": "up",
                     "score": 80, "band": "high", "prob": 0.70, "entry_px": 100.0,
                     "horizon_d": 21, "status": "resolved", "realized": realized, "resolved_on": d})
    monkeypatch.setattr(P, "_load_ledger", lambda: rows)
    dirn = P.score("2026-12-01", horizon=21)["directional"]
    assert dirn["reliability_curve"]                         # non-empty diagram
    bucket = next(b for b in dirn["reliability_curve"] if b["bin"] == "0.60-0.80")
    assert bucket["n"] == 30 and bucket["mean_predicted"] == 0.7 and bucket["hit_rate"] == 0.5
    assert dirn["calibration_error"] == 0.2                  # |0.7-0.5| weighted
    assert dirn["confidence_tilt"] == "overconfident"


def test_summary_shape(monkeypatch, sandbox):
    monkeypatch.setattr(P, "_load_ledger", lambda: [])
    s = P.summary("2026-06-21")
    assert "coverage" in s and "scorecard" in s and s["horizon_d"] == P._HORIZON
    # per-horizon scorecards present, one per tier, each carrying its own horizon_d
    assert set(s["scorecards_by_horizon"]) == {str(h) for h in P._HORIZONS}
    assert s["scorecards_by_horizon"]["3"]["horizon_d"] == 3


def test_calibration_index_pins_to_canonical_horizon(monkeypatch):
    # the calibration fast-arm keys realized returns on (ticker, date); with the ledger now carrying
    # multiple horizons per (ticker, date), the index MUST take only the canonical 21-tier or the short
    # tiers would silently overwrite it. Guards brain/calibration._pred_resolved_index.
    from brain import calibration as C
    rows = [_resolved_h(50, 0.07, "2026-01-05", "AAA", 21),
            _resolved_h(50, -0.20, "2026-01-05", "AAA", 3)]   # short-tier value must be ignored
    monkeypatch.setattr(P, "_load_ledger", lambda: rows)
    idx = C._pred_resolved_index()
    assert idx.get(("AAA", "2026-01-05")) == 0.07            # the 21-tier value, not the 3-tier -0.20
