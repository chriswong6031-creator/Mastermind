"""Calibration capture (brain/calibration.py) — the conscience that joins prediction + outcome + the
decision-time lens snapshot. Offline; tmp-path JSONL + synthetic theses/snapshots/realized."""
from __future__ import annotations

from unittest import mock

import bot  # noqa: F401


def _thesis(tid, subject, prob, *, op="<", thr=-0.05, state_asof="2026-06-21", lean="add", kind="rel_return"):
    chk = {"kind": kind, "op": op, "threshold": thr, "subject_ticker": subject} if kind == "rel_return" \
        else {"kind": "none"}
    return {"id": tid, "subject": subject, "prob_correct": prob, "lean": lean, "horizon_d": 21,
            "sleeve": "conviction", "state_asof": state_asof, "falsifier": {"check": chk}}


def _paths(monkeypatch, tmp_path):
    from brain import calibration as cal
    from brain import signal_history as sh
    monkeypatch.setattr(cal, "_PATH", tmp_path / "cal.jsonl", raising=False)
    monkeypatch.setattr(sh, "_PATH", tmp_path / "sig.jsonl", raising=False)
    return cal, sh


def test_resolve_joins_prediction_outcome_and_snapshot(tmp_path, monkeypatch):
    cal, sh = _paths(monkeypatch, tmp_path)
    sh.archive("2026-06-21", [sh.make_record(
        "2026-06-21", "AVGO", sleeve="conviction", decision="sized", regime={"quad": "Q1"},
        synthesis={"confluence": 0.7, "size_authority": "up"},
        rows=[{"lens": "trend", "direction": "bull"}, {"lens": "sector_rs", "direction": "bull"}])])
    theses = [_thesis("t1", "AVGO", 0.7), _thesis("t2", "NEM", 0.6)]
    # AVGO +10% -> not falsified (op '<' thr -0.05) -> CORRECT (1); NEM -8% < -5% -> falsified -> WRONG (0)
    n = cal.resolve("2026-07-17", {"t1": 0.10, "t2": -0.08}, theses=theses)
    assert n == 2
    recs = {r["thesis_id"]: r for r in cal.load()}
    assert recs["t1"]["outcome"] == 1 and recs["t1"]["prob_correct"] == 0.7
    assert recs["t1"]["realized_rel"] == 0.10 and recs["t1"]["asof_resolved"] == "2026-07-17"
    assert recs["t1"]["lens_dirs"] == {"trend": "bull", "sector_rs": "bull"}
    assert recs["t1"]["confluence_at_entry"] == 0.7 and recs["t1"]["quad_at_entry"] == "Q1"
    assert recs["t2"]["outcome"] == 0
    assert recs["t2"]["lens_dirs"] == {}, "NEM had no decision-time snapshot — graded but no lens record"


def test_keep_first_per_thesis(tmp_path, monkeypatch):
    cal, _ = _paths(monkeypatch, tmp_path)
    theses = [_thesis("t1", "AVGO", 0.7)]
    assert cal.resolve("2026-07-17", {"t1": 0.10}, theses=theses) == 1
    assert cal.resolve("2026-07-18", {"t1": -0.20}, theses=theses) == 0, "already graded -> keep-first"
    assert len(cal.load()) == 1 and cal.load()[0]["outcome"] == 1


def test_non_directional_is_not_graded(tmp_path, monkeypatch):
    cal, _ = _paths(monkeypatch, tmp_path)
    watch = [_thesis("w1", "X", 0.55, kind="none")]
    assert cal.resolve("2026-07-17", {"w1": 0.2}, theses=watch) == 0


def test_summary_reliability_and_lens_edge(tmp_path, monkeypatch):
    cal, sh = _paths(monkeypatch, tmp_path)
    # 4 names, all read trend=bull at entry, prob 0.70; 3 win, 1 loses
    for tk in ("AA", "BB", "CC", "DD"):
        sh.archive("2026-06-21", [sh.make_record(
            "2026-06-21", tk, sleeve="conviction", decision="sized", regime={"quad": "Q1"},
            synthesis={"confluence": 0.7}, rows=[{"lens": "trend", "direction": "bull"}])])
    theses = [_thesis(f"t_{tk}", tk, 0.70) for tk in ("AA", "BB", "CC", "DD")]
    realized = {"t_AA": 0.10, "t_BB": 0.08, "t_CC": 0.05, "t_DD": -0.09}   # DD falsified
    assert cal.resolve("2026-07-17", realized, theses=theses) == 4

    s = cal.summary()
    assert s["n"] == 4 and s["hits"] == 3 and s["hit_rate"] == 0.75 and s["status"] == "scoring"
    # brier = mean((0.7-outcome)^2) = (0.09*3 + 0.49)/4 = 0.19
    assert abs(s["brier"] - 0.19) < 1e-9

    curve = cal.reliability_curve(bins=5)
    bucket = next(b for b in curve if b["bin"] == "0.60-0.80")
    assert bucket["n"] == 4 and bucket["mean_predicted"] == 0.7 and bucket["hit_rate"] == 0.75

    edge = cal.lens_edge()
    trend_bull = next(e for e in edge if e["lens"] == "trend" and e["direction"] == "bull")
    assert trend_bull["n"] == 4 and trend_bull["hit_rate"] == 0.75


def test_empty_when_no_resolutions(tmp_path, monkeypatch):
    cal, _ = _paths(monkeypatch, tmp_path)
    assert cal.resolve("2026-07-17", {}, theses=[]) == 0
    assert cal.summary()["status"] == "building" and cal.reliability_curve() == [] and cal.lens_edge() == []
