"""Commit-4: D5 dead-capital AUTO-EXIT + low-severity cleanups (runs-table carry guard, intake
string-lean guard). All offline."""
from __future__ import annotations

import sqlite3
from datetime import date
from unittest import mock

import bot  # noqa: F401


# ---------------------------------------------------------------------------
# D5 dead-capital -> a self-mode exit set (what phase2 drops from the book)
# ---------------------------------------------------------------------------

def test_d5_self_fire_yields_exit_subject():
    from bot.phase2 import _build_d5_lots
    from brain import detectors
    opens = [{"ticker": "AAA", "sleeve": "conviction", "time_stop_by": "2026-03-01",
              "entry_price": 100.0, "thesis_id": "t1"}]
    lots = _build_d5_lots(opens, {"AAA": 88.0}, {"AAA": 30.0}, 90.0, date(2026, 6, 20))
    fired = detectors.d5_dead_capital(lots, date(2026, 6, 20), "self")
    assert fired and fired[0]["code"] == "D5" and fired[0]["mode"] == "self"
    # this is exactly the set phase2 drops from the book + closes in the ledger
    exit_set = {d["subject"] for d in fired if d.get("mode") == "self"}
    assert exit_set == {"AAA"}


def test_d5_operator_mode_is_not_an_exit():
    """operator mode is a read-only advisory (severity 'flag'), never a book exit."""
    from bot.phase2 import _build_d5_lots
    from brain import detectors
    opens = [{"ticker": "AAA", "sleeve": "conviction", "time_stop_by": "2026-03-01",
              "entry_price": 100.0, "thesis_id": "t1"}]
    lots = _build_d5_lots(opens, {"AAA": 88.0}, {"AAA": 30.0}, 90.0, date(2026, 6, 20))
    fired = detectors.d5_dead_capital(lots, date(2026, 6, 20), "operator")
    exit_set = {d["subject"] for d in fired if d.get("mode") == "self"}
    assert exit_set == set(), "operator-mode D5 must not trigger a book exit"


# ---------------------------------------------------------------------------
# runs table: a same-day carry-forward must not clobber a successful run
# ---------------------------------------------------------------------------

def _mem_con():
    from data_layer import store
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(store._SCHEMA)
    return con


def test_record_run_carry_does_not_overwrite_successful_run():
    from data_layer import store
    con = _mem_con()
    store.record_run(con, "2026-06-20", True, ["state_change"], "SIG_A", "t1")
    store.record_run(con, "2026-06-20", False, [], "SIG_A", "t2")     # same-day carry
    lr = store.last_run(con)
    assert lr["ran"] == 1, "a carry-forward must not overwrite the day's successful run"
    assert lr["state_sig"] == "SIG_A"


def test_record_run_full_run_replaces_carry():
    from data_layer import store
    con = _mem_con()
    store.record_run(con, "2026-06-21", False, [], "SIG_A", "t1")     # carry first
    store.record_run(con, "2026-06-21", True, ["state_change"], "SIG_B", "t2")  # then a real run
    lr = store.last_run(con)
    assert lr["ran"] == 1 and lr["state_sig"] == "SIG_B"


# ---------------------------------------------------------------------------
# intake: a non-numeric (string) lean must not crash the funnel
# ---------------------------------------------------------------------------

def test_intake_build_tolerates_string_lean(monkeypatch):
    from brain import intake
    monkeypatch.setattr(intake, "_from_standouts",
                        lambda: {"AAA": {"score": 0.7, "reason": "r", "lean": "↑",
                                         "confidence": None, "falsifier": None}})
    monkeypatch.setattr(intake, "_from_radar",
                        lambda: {"BBB": {"score": 0.6, "reason": "r", "lean": 1,
                                         "confidence": None, "falsifier": None}})
    monkeypatch.setattr(intake, "_from_altdata", lambda: {})
    monkeypatch.setattr(intake, "_from_news_surge", lambda: {})
    monkeypatch.setattr(intake, "_from_open_theses", lambda: {})
    out = intake.build(limit=10)                       # must not raise on the string lean
    cands = {c["ticker"]: c for c in out["candidates"]}
    assert "AAA" in cands and "BBB" in cands
    assert cands["AAA"]["lean"] is None, "a string lean is ignored, not summed"
    assert cands["BBB"]["lean"] == 1, "a numeric lean still votes"
