"""Interim trajectory checkpoints (brain.interim_marks, #11) — day-5/day-10 early reads on open theses.

Pins: only OPEN directional theses get marks; both checkpoints logged once elapsed; KEEP-FIRST (re-run
adds nothing); the falsifier-consistent hit-rate + underwater early-warnings. The price labeler is
monkeypatched so it never hits a parquet store / price feed.
"""
from __future__ import annotations

from brain import interim_marks as IM


def _thesis(tid, subj, prob=0.7, status="open", kind="rel_return"):
    chk = ({"kind": "rel_return", "op": "<", "threshold": -0.05, "subject_ticker": subj}
           if kind == "rel_return" else {"kind": "none"})
    return {"id": tid, "subject": subj, "status": status, "state_asof": "2026-06-21",
            "prob_correct": prob, "horizon_d": 21,
            "entry_levels": {"ticker": subj, "price": 100.0}, "falsifier": {"check": chk}}


def test_record_logs_both_checkpoints_keep_first(tmp_path, monkeypatch):
    monkeypatch.setattr(IM, "_PATH", tmp_path / "im.jsonl")
    monkeypatch.setattr(IM, "all_theses", lambda: [
        _thesis("t1", "AAA"), _thesis("w", "X", kind="none"), _thesis("c", "C", status="resolved")])
    monkeypatch.setattr("brain.outcomes.label_thesis",
                        lambda syn, asof: {"resolved": True, "rel_return": 0.04, "barrier": "time"})
    out = IM.record("2026-07-10")
    assert out["new"] == 2                                    # 2 checkpoints for AAA; non-dir + closed skipped
    rows = IM._load()
    assert {r["checkpoint"] for r in rows} == {5, 10} and all(r["subject"] == "AAA" for r in rows)
    assert IM.record("2026-07-11")["new"] == 0               # keep-first → no duplicates


def test_scorecard_hit_rate_and_underwater(tmp_path, monkeypatch):
    monkeypatch.setattr(IM, "_PATH", tmp_path / "im.jsonl")
    monkeypatch.setattr(IM, "all_theses", lambda: [_thesis("t1", "AAA"), _thesis("t2", "BBB")])
    rels = {"AAA": 0.04, "BBB": -0.09}                       # BBB falsified (< -5%) AND underwater (<= -3%)
    monkeypatch.setattr("brain.outcomes.label_thesis",
                        lambda syn, asof: {"resolved": True, "rel_return": rels[syn["subject"]], "barrier": "time"})
    IM.record("2026-07-10")
    cp5 = IM.scorecard()["by_checkpoint"]["5"]
    assert cp5["n"] == 2 and cp5["hit_rate"] == 0.5 and cp5["n_underwater"] == 1
    ew = IM.early_warnings()
    assert [w["subject"] for w in ew] == ["BBB"]             # only the underwater name is flagged


def test_record_never_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(IM, "_PATH", tmp_path / "im.jsonl")
    monkeypatch.setattr(IM, "all_theses", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert IM.record("2026-07-10") == {"n_marks": 0, "new": 0}   # degrade-safe
