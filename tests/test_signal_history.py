"""Point-in-time signal memory (brain/signal_history.py) — the substrate the learning loop needs.
Offline; tmp-path JSONL."""
from __future__ import annotations

from unittest import mock

import bot  # noqa: F401


def test_make_record_flattens_lenses_and_regime():
    from brain import signal_history as sh
    rows = [{"lens": "trend", "direction": "bull"}, {"lens": "sector_rs", "direction": "bear"},
            {"lens": "valuation", "direction": None}]            # None-direction lens is dropped
    syn = {"confluence": 0.7, "bull": 5, "bear": 1, "size_authority": "up", "vetoes": [],
           "divergences": [{"pattern": "high_confluence_buy"}], "leadership_ok": True,
           "weak_asymmetry": False, "price_downtrend": False}
    reg = {"quad": "Q1", "quad_name": "Goldilocks", "liquidity_overlay": "expanding",
           "macro_risk": {"score": 0.2}}
    r = sh.make_record("2026-06-21", "avgo", sleeve="conviction", decision="sized", regime=reg,
                       synthesis=syn, rows=rows, verdict="add", weight=0.06, size_stage="full")
    assert r["ticker"] == "AVGO" and r["decision"] == "sized" and r["verdict"] == "add"
    assert r["confluence"] == 0.7 and r["size_authority"] == "up" and r["size_stage"] == "full"
    assert r["lens_dirs"] == {"trend": "bull", "sector_rs": "bear"}
    assert r["divergences"] == ["high_confluence_buy"]
    assert r["quad"] == "Q1" and r["quad_name"] == "Goldilocks" and r["macro_risk"] == 0.2


def test_archive_keep_first_per_day(tmp_path, monkeypatch):
    from brain import signal_history as sh
    monkeypatch.setattr(sh, "_PATH", tmp_path / "signals.jsonl", raising=False)
    n1 = sh.archive("2026-06-21", [{"ticker": "AVGO", "confluence": 0.7},
                                   {"ticker": "NEM", "confluence": -0.3}])
    assert n1 == 2
    # intra-day rebuild: AVGO already recorded -> SKIPPED (keep-first); a NEW name (ANET) is added
    n2 = sh.archive("2026-06-21", [{"ticker": "avgo", "confluence": 0.99},
                                   {"ticker": "ANET", "confluence": 0.6}])
    assert n2 == 1
    by_t = {r["ticker"]: r for r in sh.load("2026-06-21")}
    assert set(by_t) == {"AVGO", "NEM", "ANET"}
    assert by_t["AVGO"]["confluence"] == 0.7, "keep-first: the original PIT read survives, not the rebuild"


def test_archive_independent_across_days(tmp_path, monkeypatch):
    from brain import signal_history as sh
    monkeypatch.setattr(sh, "_PATH", tmp_path / "s.jsonl", raising=False)
    sh.archive("2026-06-21", [{"ticker": "AVGO"}])
    sh.archive("2026-06-22", [{"ticker": "AVGO"}])             # same name, different day -> both kept
    assert len(sh.load()) == 2
    assert len(sh.load("2026-06-22")) == 1


def test_missing_and_empty_degrade(tmp_path, monkeypatch):
    from brain import signal_history as sh
    monkeypatch.setattr(sh, "_PATH", tmp_path / "nope.jsonl", raising=False)
    assert sh.load() == []
    assert sh.archive("2026-06-21", []) == 0
    assert sh.seen_keys("2026-06-21") == set()
