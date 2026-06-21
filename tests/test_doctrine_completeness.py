"""Doctrine completeness: the interval rebuild cadence, the D1/D2/D4 behavioural detectors, and the
catalyst/confirmation full-vs-initial sizing gate. All offline."""
from __future__ import annotations

from unittest import mock

import bot  # noqa: F401


# ---------------------------------------------------------------------------
# gate interval cadence — a stable regime still refreshes after `interval_days`
# ---------------------------------------------------------------------------

def test_gate_interval_fires_when_stale():
    from brain import gate
    prev = {"state_sig": "SIG", "asof": "2026-06-15"}
    d = gate.should_run("SIG", prev, interval_days=1, force=False, asof="2026-06-20")
    assert d["run"] is True and "interval" in d["triggers"]


def test_gate_no_rerun_same_day():
    from brain import gate
    prev = {"state_sig": "SIG", "asof": "2026-06-20"}
    d = gate.should_run("SIG", prev, interval_days=1, force=False, asof="2026-06-20")
    assert d["run"] is False and d["triggers"] == []


def test_gate_state_change_still_fires():
    from brain import gate
    prev = {"state_sig": "OLD", "asof": "2026-06-20"}
    d = gate.should_run("NEW", prev, interval_days=5, force=False, asof="2026-06-20")
    assert "state_change" in d["triggers"] and d["run"] is True


def test_gate_interval_respects_longer_window():
    from brain import gate
    prev = {"state_sig": "SIG", "asof": "2026-06-18"}     # 2 days elapsed, interval 5 -> carry
    d = gate.should_run("SIG", prev, interval_days=5, force=False, asof="2026-06-20")
    assert d["run"] is False


# ---------------------------------------------------------------------------
# D1 / D2 / D4 behavioural detectors
# ---------------------------------------------------------------------------

def test_d1_flags_held_loser_past_patience():
    from brain import detectors
    lots = [{"ticker": "AAA", "rel_return_since_entry": -0.08, "held_days": 40, "time_stop_td": 63, "id": "t1"}]
    fired = detectors.d1_disposition(lots, "self")
    assert len(fired) == 1 and fired[0]["code"] == "D1" and fired[0]["subject"] == "AAA"


def test_d1_ignores_winner_and_fresh_loser():
    from brain import detectors
    lots = [{"ticker": "WIN", "rel_return_since_entry": 0.05, "held_days": 40, "time_stop_td": 63},
            {"ticker": "NEW", "rel_return_since_entry": -0.10, "held_days": 5, "time_stop_td": 63}]
    assert detectors.d1_disposition(lots, "self") == []


def test_d2_flags_extended_new_buy_only():
    from brain import detectors
    nb = [{"ticker": "EXT", "extension_bear": True, "parabolic": False, "id": "t1"},
          {"ticker": "PARA", "extension_bear": False, "parabolic": True},
          {"ticker": "OK", "extension_bear": False, "parabolic": False}]
    fired = detectors.d2_late_stage_reach(nb, "self")
    assert sorted(f["subject"] for f in fired) == ["EXT", "PARA"]


def test_d4_flags_add_into_lagging_loser_only():
    from brain import detectors
    lots = [{"ticker": "AVG", "is_add": True, "rel_return_since_entry": -0.07, "rs_leader_gap": 0.20, "id": "t1"},
            {"ticker": "HOLD", "is_add": False, "rel_return_since_entry": -0.07, "rs_leader_gap": 0.20},
            {"ticker": "LEAD", "is_add": True, "rel_return_since_entry": -0.07, "rs_leader_gap": 0.05}]
    fired = detectors.d4_avg_down_into_divergence(lots, "self")
    assert [f["subject"] for f in fired] == ["AVG"]


# ---------------------------------------------------------------------------
# catalyst/confirmation graduated sizing — full vs initial
# ---------------------------------------------------------------------------

def _full(ticker, kind="name", *, trend="bull", sector="bull", narrative=None):
    rows = [{"lens": "trend", "direction": trend, "value": {}},
            {"lens": "sector_rs", "direction": sector, "value": {}}]
    if narrative is not None:
        rows.append({"lens": "narrative", "direction": narrative, "value": {}})
    return {"subject": ticker, "kind": "name", "rows": rows,
            "synthesis": {"bull": 3, "bear": 0, "n_scored": 3, "confluence": 0.8, "vetoes": [],
                          "divergences": [], "size_authority": "up", "price_downtrend": False,
                          "leadership_ok": True, "price_falling_fast": False,
                          "weak_asymmetry": False, "asym_ratio": None}}


def _build_one(monkeypatch_full):
    import portfolio.conviction as cv
    import portfolio.lenses as L
    with mock.patch.object(cv, "candidates", return_value=["AAA"]), \
         mock.patch.object(L, "full", side_effect=monkeypatch_full):
        sized, _ = cv.build(budget=0.30, name_cap=0.08)
    return next(p for p in sized if p["ticker"] == "AAA")


def test_confirmed_leader_gets_full_size():
    p = _build_one(lambda t, k="name": _full(t, trend="bull", sector="bull"))
    assert p["size_stage"] == "full" and p["weight"] == 0.08


def test_unconfirmed_name_gets_initial_size():
    full_w = _build_one(lambda t, k="name": _full(t, trend="bull", sector="bull"))["weight"]
    p = _build_one(lambda t, k="name": _full(t, trend="neutral", sector="neutral"))
    assert p["size_stage"] == "initial"
    assert p["weight"] < full_w
    assert abs(p["weight"] - round(full_w * 0.7, 4)) < 1e-9


def test_leading_theme_earns_full_size_without_price_confirmation():
    p = _build_one(lambda t, k="name": _full(t, trend="neutral", sector="neutral", narrative="bull"))
    assert p["size_stage"] == "full"
