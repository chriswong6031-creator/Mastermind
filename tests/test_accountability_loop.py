"""Commit-3: the accountability/lifecycle loop — rebalance missing-price safety, the Brier
realize-returns input, the D5 dead-capital lot-builder, and time-stop persistence.

The phase2 INTEGRATION of these (paper_account.rebalance/mark wiring, D5 dispatch) needs the
vendor/macro submodule + live prices to exercise end-to-end and is verified by review; here we
unit-test every pure/seam-mockable piece offline.
"""
from __future__ import annotations

from datetime import date
from unittest import mock

import bot  # noqa: F401


# ---------------------------------------------------------------------------
# rebalance: a targeted position with a MISSING price this run must be CARRIED, not liquidated
# ---------------------------------------------------------------------------

def _seed(pa, tmp_path, positions, cash=500_000.0):
    pa._save_account({"inception_date": "2026-06-20", "starting_nav": 1_000_000.0,
                      "cash": cash, "positions": positions, "spy_shares": None})


def _patch_paths(pa, tmp_path, monkeypatch):
    monkeypatch.setattr(pa, "_DATA", tmp_path, raising=False)
    monkeypatch.setattr(pa, "_ACCOUNT_PATH", tmp_path / "account.json", raising=False)
    monkeypatch.setattr(pa, "_FILLS_PATH", tmp_path / "fills.jsonl", raising=False)
    monkeypatch.setattr(pa, "_NAV_PATH", tmp_path / "nav.jsonl", raising=False)


def test_rebalance_carries_targeted_position_when_price_missing(tmp_path, monkeypatch):
    from portfolio import paper_account as pa
    _patch_paths(pa, tmp_path, monkeypatch)
    _seed(pa, tmp_path, {"AAA": {"shares": 100.0, "avg_cost": 50.0}})
    # AAA is still TARGETED but has no price this run (only SPY priced)
    pa.rebalance({"AAA": 0.05}, {"SPY": 400.0}, "2026-06-21")
    st = pa._load_account()
    assert "AAA" in st["positions"], "a targeted held position must NOT be liquidated on a missing quote"
    assert st["positions"]["AAA"]["shares"] == 100.0


def test_rebalance_still_closes_genuinely_dropped_position(tmp_path, monkeypatch):
    from portfolio import paper_account as pa
    _patch_paths(pa, tmp_path, monkeypatch)
    _seed(pa, tmp_path, {"AAA": {"shares": 100.0, "avg_cost": 50.0},
                         "BBB": {"shares": 200.0, "avg_cost": 10.0}})
    # BBB is genuinely dropped from the target (not merely unpriced) -> must be closed
    pa.rebalance({"AAA": 0.05}, {"AAA": 50.0, "BBB": 10.0, "SPY": 400.0}, "2026-06-21")
    st = pa._load_account()
    assert "BBB" not in st["positions"], "a position dropped from the target must be closed"
    assert "AAA" in st["positions"]


# ---------------------------------------------------------------------------
# scorer.realize_returns — feeds realized rel-returns to the Brier track record
# ---------------------------------------------------------------------------

def _thesis(tid="d1", subject="X", *, lean_kind="rel_return", state_asof="2026-05-01",
            check_by="2026-06-01", entry_price=100.0, status="open"):
    chk = ({"kind": "rel_return", "subject_ticker": subject, "op": "<", "threshold": -0.05}
           if lean_kind == "rel_return" else {"kind": "none", "subject_ticker": subject})
    return {"id": tid, "subject": subject, "status": status, "state_asof": state_asof,
            "check_by": check_by, "falsifier": {"check": chk}, "entry_levels": {"price": entry_price}}


def test_realize_returns_computes_rel_return(monkeypatch):
    from brain import scorer
    from portfolio import paper_account as pa
    import pandas as pd
    spy = pd.Series([100.0, 110.0], index=pd.to_datetime(["2026-05-01", "2026-06-20"]))  # SPY +10%
    monkeypatch.setattr(pa, "_fetch_price_series", lambda t: spy)
    monkeypatch.setattr(pa, "_current_price", lambda t: 120.0)                            # subject 100->120 = +20%
    monkeypatch.setattr(scorer, "all_theses", lambda: [_thesis(entry_price=100.0)])
    out = scorer.realize_returns("2026-06-20")
    assert out["d1"] == 0.1, "rel = subject +20% - SPY +10% = +10%"


def test_realize_returns_skips_non_due_and_non_directional(monkeypatch):
    from brain import scorer
    from portfolio import paper_account as pa
    import pandas as pd
    spy = pd.Series([100.0, 110.0], index=pd.to_datetime(["2026-05-01", "2026-06-20"]))
    monkeypatch.setattr(pa, "_fetch_price_series", lambda t: spy)
    monkeypatch.setattr(pa, "_current_price", lambda t: 120.0)
    theses = [
        _thesis("future", check_by="2027-01-01"),          # not due
        _thesis("watch", lean_kind="none"),                # non-directional
        _thesis("done", status="resolved"),                # already closed
    ]
    monkeypatch.setattr(scorer, "all_theses", lambda: theses)
    out = scorer.realize_returns("2026-06-20")
    assert out == {}


def test_track_record_scores_once_realized_injected(monkeypatch):
    from brain import scorer
    monkeypatch.setattr(scorer, "all_theses",
                        lambda: [{**_thesis("d1", entry_price=100.0), "prob_correct": 0.6}])
    # realized rel-return -0.10 -> below the -0.05 falsifier threshold (op '<') -> MISS (outcome 0)
    tr = scorer.track_record(asof=date(2026, 6, 20), realized={"d1": -0.10})
    assert tr["n"] == 1 and tr["hits"] == 0 and tr["status"] == "scoring"


# ---------------------------------------------------------------------------
# _build_d5_lots + d5_dead_capital firing
# ---------------------------------------------------------------------------

def test_build_d5_lots_and_fire():
    from bot.phase2 import _build_d5_lots
    from brain import detectors
    opens = [
        {"ticker": "AAA", "sleeve": "conviction", "time_stop_by": "2026-03-01",
         "entry_price": 100.0, "thesis_id": "t1"},          # elapsed, -10%, lagging -> dead capital
        {"ticker": "BBB", "sleeve": "leadership", "time_stop_by": "2026-03-01", "entry_price": 50.0},  # skip
        {"ticker": "CCC", "sleeve": "conviction", "entry_price": 10.0},                                # no time-stop
    ]
    lots = _build_d5_lots(opens, prices={"AAA": 90.0}, sector_pctile={"AAA": 40.0},
                          leader_pctile=85.0, asof_date=date(2026, 6, 20))
    assert len(lots) == 1
    lot = lots[0]
    assert lot["ticker"] == "AAA"
    assert lot["rel_return_since_entry"] == -0.1
    assert abs(lot["rs_leader_gap"] - 0.45) < 1e-9          # (85 - 40) / 100
    assert lot["time_stop_by"] == date(2026, 3, 1)

    fired = detectors.d5_dead_capital(lots, date(2026, 6, 20), "self")
    assert len(fired) == 1 and fired[0]["code"] == "D5" and fired[0]["severity"] == "veto"


def test_build_d5_lots_not_elapsed_does_not_fire():
    from bot.phase2 import _build_d5_lots
    from brain import detectors
    opens = [{"ticker": "AAA", "sleeve": "conviction", "time_stop_by": "2026-12-01",
              "entry_price": 100.0, "thesis_id": "t1"}]
    lots = _build_d5_lots(opens, {"AAA": 90.0}, {"AAA": 40.0}, 85.0, date(2026, 6, 20))
    # time_stop_by is in the FUTURE -> not elapsed -> no D5
    assert detectors.d5_dead_capital(lots, date(2026, 6, 20), "self") == []


# ---------------------------------------------------------------------------
# position_log persists the ORIGINAL time_stop_by (clock must not reset each rebuild)
# ---------------------------------------------------------------------------

def test_position_log_persists_original_time_stop(tmp_path, monkeypatch):
    from portfolio import position_log as pl
    monkeypatch.setattr(pl, "_LEDGER_PATH", tmp_path / "pos.json", raising=False)
    pl.update([{"ticker": "NVDA", "sleeve": "conviction", "weight": 0.05,
                "time_stop_by": "2026-09-01"}], "2026-06-20")
    assert pl.get_entry_info("conviction", "NVDA")["time_stop_by"] == "2026-09-01"
    # a later rebuild recomputes a NEW time_stop_by — the persisted ORIGINAL must win (else the
    # time-stop clock resets every run and D5 could never elapse)
    pl.update([{"ticker": "NVDA", "sleeve": "conviction", "weight": 0.05,
                "time_stop_by": "2026-12-01"}], "2026-06-21")
    assert pl.get_entry_info("conviction", "NVDA")["time_stop_by"] == "2026-09-01"
    assert pl.open_positions()[0]["time_stop_by"] == "2026-09-01"
