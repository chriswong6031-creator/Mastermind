"""Guards for deterministic outcome labeling (brain/outcomes) — the learning-signal layer.

The triple-barrier + rel-return logic is graded against synthetic price paths (so the result
is exact and offline), plus one live-data integration check, plus proof that realized outcomes
actually move the Brier track record off 'building (n=0)'.
"""
from __future__ import annotations

import datetime as dt

import pytest

import bot  # noqa: F401 — bootstraps vendor/macro
from brain import outcomes as O


def _series(start: str, vals: list[float]) -> dict:
    """{iso_weekday: px} for consecutive business days starting at `start`."""
    d, out = dt.date.fromisoformat(start), {}
    while len(out) < len(vals):
        if d.weekday() < 5:
            out[d.isoformat()] = vals[len(out)]
        d += dt.timedelta(days=1)
    return out


def _patch(monkeypatch, subj_vals, spy_vals, subj="AME", start="2026-05-01"):
    subj_s, spy_s = _series(start, subj_vals), _series(start, spy_vals)
    monkeypatch.setattr(O, "_closes", lambda tk, lo, hi, cache=True: subj_s if tk.upper() == subj else spy_s)


def _thesis(subj="AME", start="2026-05-01", horizon=5, entry_px=100.0):
    return {"id": f"t-{subj}", "state_asof": start, "entry_levels": {"ticker": subj, "price": entry_px},
            "falsifier": {"check": {"kind": "rel_return", "subject_ticker": subj, "vs": "SPY",
                                    "op": "<", "threshold": -0.05, "horizon_d": horizon}}}


def test_barrier_target(monkeypatch):
    # +16% by day 2 (target is +15%); flat SPY
    _patch(monkeypatch, [100, 108, 116, 116, 116, 116, 116], [400] * 7)
    lab = O.label_thesis(_thesis(), dt.date(2026, 6, 1))
    assert lab["barrier"] == "target" and lab["resolved"] is True


def test_barrier_stop(monkeypatch):
    # -12% by day 2 (stop is -10%); flat SPY
    _patch(monkeypatch, [100, 95, 88, 88, 88, 88, 88], [400] * 7)
    lab = O.label_thesis(_thesis(), dt.date(2026, 6, 1))
    assert lab["barrier"] == "stop" and lab["resolved"] is True


def test_barrier_time_and_rel_return(monkeypatch):
    # stays within barriers; subject +5% vs SPY +0% over horizon → rel ≈ +0.05, barrier time
    _patch(monkeypatch, [100, 101, 102, 103, 104, 105, 105], [400] * 7)
    lab = O.label_thesis(_thesis(horizon=5), dt.date(2026, 6, 1))
    assert lab["barrier"] == "time" and lab["resolved"] is True
    assert abs(lab["abs_return"] - 0.05) < 1e-6 and abs(lab["vs_return"]) < 1e-9
    assert abs(lab["rel_return"] - 0.05) < 1e-6


def test_rel_return_subtracts_benchmark(monkeypatch):
    # subject +5%, SPY +8% over horizon → rel ≈ -3%
    _patch(monkeypatch, [100, 101, 102, 103, 104, 105], [400, 405, 410, 420, 428, 432])
    lab = O.label_thesis(_thesis(horizon=5), dt.date(2026, 6, 1))
    assert lab["resolved"] and lab["rel_return"] < 0 and abs(lab["rel_return"] - (0.05 - 0.08)) < 0.01


def test_in_progress_when_horizon_not_elapsed(monkeypatch):
    # only 3 trading days but horizon is 5 → not resolved
    _patch(monkeypatch, [100, 101, 102], [400, 401, 402])
    lab = O.label_thesis(_thesis(horizon=5), dt.date(2026, 6, 1))
    assert lab["status"] == "in_progress" and lab["resolved"] is False


def test_no_data_is_unresolved(monkeypatch):
    monkeypatch.setattr(O, "_closes", lambda tk, lo, hi, cache=True: {})
    lab = O.label_thesis(_thesis(), dt.date(2026, 6, 1))
    assert lab["status"] == "unresolved" and lab["rel_return"] is None


def test_baselines_anchor_to_same_date_on_nontrading_entry(monkeypatch):
    # entry on a Saturday → both subject and SPY baselines must anchor to the prior trading day,
    # not drift the SPY baseline forward (the bug the review caught).
    subj = _series("2026-05-01", [100, 101, 102, 103, 104])   # Fri,Mon,Tue,Wed,Thu
    spy = _series("2026-05-01", [400, 420, 420, 420, 420])     # +5% Fri→Mon then flat
    monkeypatch.setattr(O, "_closes", lambda tk, lo, hi, cache=True: dict(subj) if tk == "AME" else dict(spy))
    th = _thesis(start="2026-05-02", horizon=3, entry_px=100.0)  # 05-02 is a Saturday
    lab = O.label_thesis(th, dt.date(2026, 6, 1))
    assert lab["resolved"]
    # SPY anchored at Fri(400) → Wed(420) = +5%; subject +3% → rel ≈ -2% (NOT +3% with a Mon anchor)
    assert abs(lab["rel_return"] - (-0.02)) < 0.01


def test_no_lookahead_caps_at_asof(monkeypatch):
    subj = _series("2026-05-01", [100, 101, 102, 103, 104, 105, 106, 107, 108, 109])
    spy = _series("2026-05-01", [400] * 10)
    monkeypatch.setattr(O, "_closes", lambda tk, lo, hi, cache=True: dict(subj) if tk == "AME" else dict(spy))
    asof_iso = sorted(subj)[2]   # only 3 trading days have elapsed
    lab = O.label_thesis(_thesis(start=sorted(subj)[0], horizon=5, entry_px=100.0),
                         dt.date.fromisoformat(asof_iso))
    assert lab["status"] == "in_progress" and lab["resolved"] is False
    assert lab["exit_date"] <= asof_iso          # never grades on a close after asof


def test_daily_closes_drops_nonpositive(monkeypatch):
    from data_layer import polygon

    class _Resp:
        ok = True

        def json(self):
            return {"results": [{"t": 1714521600000, "c": 100.0}, {"t": 1714608000000, "c": 0.0},
                                {"t": 1714694400000, "c": -5.0}, {"t": 1714780800000, "c": 110.0}]}

    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp())
    out = polygon.daily_closes("AME", "2024-05-01", "2024-05-04", cache=False)
    assert len(out) == 2 and all(v > 0 for v in out.values())


def test_non_rel_return_falsifier_returns_none():
    th = {"id": "x", "state_asof": "2026-05-01", "falsifier": {"check": {"kind": "drawdown"}}}
    assert O.label_thesis(th, dt.date(2026, 6, 1)) is None


def test_label_never_raises_on_garbage():
    for bad in ({}, {"falsifier": None}, {"falsifier": {"check": {"kind": "rel_return"}}},
                {"falsifier": {"check": {"kind": "rel_return", "subject_ticker": "ZZZ",
                                         "horizon_d": "bad"}}, "state_asof": "nope"}):
        O.label_thesis(bad, dt.date(2026, 6, 1))  # must not raise


def test_realized_feeds_scorer(monkeypatch):
    # a resolved, due thesis with rel_return below threshold → scorer grades it (n>0, miss)
    from brain import scorer
    th = _thesis()
    th["check_by"] = "2026-05-20"
    th["id"] = "graded-1"
    th["prob_correct"] = 0.7
    monkeypatch.setattr(O, "all_theses", lambda: [th])
    monkeypatch.setattr(scorer, "all_theses", lambda: [th])
    _patch(monkeypatch, [100, 95, 90, 90, 90, 90, 90], [400] * 7)  # subject down hard → rel < -0.05
    realized = O.realized_returns(dt.date(2026, 6, 1))
    assert "graded-1" in realized and realized["graded-1"] < -0.05
    tr = scorer.track_record(dt.date(2026, 6, 1), realized=realized)
    assert tr["n"] >= 1 and tr["brier"] is not None and tr["status"] == "scoring"


@pytest.mark.integration
def test_live_label_resolves_real_thesis():
    """Real price path (yahoo SPY + Polygon single name). Skips if data/network unavailable."""
    lab = O.label_thesis(_thesis(subj="AME", start="2026-05-01", horizon=21, entry_px=None),
                         dt.date(2026, 6, 20))
    if lab["status"] == "unresolved":
        pytest.skip("price data unavailable (offline / no key)")
    assert lab["resolved"] and lab["rel_return"] is not None and lab["entry_px"] > 0
