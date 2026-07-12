"""tests/test_board_learning.py — the buy-board LEARNING LOOP (edge verdict + shrink-only multiplier).

board_learning reads ONLY through brain.board_track_record (board_stats / audit_row), which is itself
fail-soft and process-safe. Here we monkeypatch that reader to synthesize board states — so NO vendor
data is touched and no real ledger is read. The two load-bearing invariants under test:
  * SHRINK-ONLY: standout_trust_multiplier() ∈ [0.5, 1.0] and can NEVER exceed 1.0.
  * NEUTRAL COLD-START: insufficient / no data → verdict 'insufficient' → multiplier 1.0 (never a penalty).
Plus every accessor is fail-soft: a reader that raises → the neutral read, never a propagated error.
"""
from __future__ import annotations

import pytest

from brain import board_learning as bl


# --------------------------------------------------------------------------- #
# helpers — synthesize a board_stats() block + point board_track_record at it
# --------------------------------------------------------------------------- #

def _stats(n, running, stopped, flat, win_rate, avg_return):
    return {"n": n, "running": running, "stopped": stopped, "flat": flat,
            "win_rate": win_rate, "avg_return": avg_return}


def _patch_board(monkeypatch, stats, as_of="2026-07-10"):
    """Make board_track_record.board_stats()/audit_row() return the synthetic board."""
    import brain.board_track_record as btr
    monkeypatch.setattr(btr, "board_stats", lambda: stats)
    monkeypatch.setattr(btr, "audit_row", lambda: {
        "status": "present", "as_of": as_of, "n_rows": stats.get("n", 0),
        "n_running": stats.get("running", 0), "n_stopped": stats.get("stopped", 0),
        "n_flat": stats.get("flat", 0)})


# =========================================================================== #
# 1. edge_verdict — INSUFFICIENT (no data / below MIN_N)
# =========================================================================== #

def test_edge_insufficient_on_no_data(monkeypatch):
    # an empty board (n=0) → insufficient, regardless of the (None) rates.
    _patch_board(monkeypatch, _stats(0, 0, 0, 0, None, None))
    edge = bl.board_edge()
    assert edge["edge_verdict"] == "insufficient"
    assert edge["n"] == 0


def test_edge_insufficient_below_min_n(monkeypatch):
    # a genuinely strong-looking board but with n just BELOW the floor → still insufficient.
    _patch_board(monkeypatch, _stats(bl.BOARD_MIN_N - 1, 8, 2, 1, 0.80, 5.0))
    assert bl.board_edge()["edge_verdict"] == "insufficient"


def test_edge_at_min_n_boundary_is_judged(monkeypatch):
    # exactly at the floor → judged (not insufficient); this strong board reads 'strong'.
    _patch_board(monkeypatch, _stats(bl.BOARD_MIN_N, 8, 2, 2, 0.80, 5.0))
    assert bl.board_edge()["edge_verdict"] == "strong"


def test_edge_respects_custom_min_n(monkeypatch):
    # a caller-supplied min_n raises the floor: n=12 board is insufficient at min_n=20.
    _patch_board(monkeypatch, _stats(12, 8, 2, 2, 0.80, 5.0))
    assert bl.board_edge(min_n=20)["edge_verdict"] == "insufficient"
    assert bl.board_edge(min_n=10)["edge_verdict"] == "strong"


# =========================================================================== #
# 2. edge_verdict — STRONG / WEAK / NEGATIVE (n >= MIN_N)
# =========================================================================== #

def test_edge_strong(monkeypatch):
    # win_rate >= 0.55 AND avg_return > 0 → strong.
    _patch_board(monkeypatch, _stats(20, 12, 6, 2, 0.60, 3.2))
    edge = bl.board_edge()
    assert edge["edge_verdict"] == "strong"
    assert edge["win_rate"] == pytest.approx(0.60)
    assert edge["avg_return"] == pytest.approx(3.2)


def test_edge_weak_win_rate_between_bands(monkeypatch):
    # win_rate 0.50 (>= 0.40 so not negative, < 0.55 so not strong) with a positive return → weak.
    _patch_board(monkeypatch, _stats(20, 9, 9, 2, 0.50, 1.0))
    assert bl.board_edge()["edge_verdict"] == "weak"


def test_edge_weak_high_win_rate_flat_return(monkeypatch):
    # strong win_rate but a ZERO-ish/None avg_return fails the strong avg_return>0 leg → weak
    # (a positive tiny return that isn't negative). Here avg_return is a small positive.
    _patch_board(monkeypatch, _stats(20, 14, 4, 2, 0.78, 0.5))
    assert bl.board_edge()["edge_verdict"] == "strong"  # both legs hold → strong
    # now the same win_rate but a None avg_return (no numeric returns) → not strong, not negative → weak
    _patch_board(monkeypatch, _stats(20, 14, 4, 2, 0.78, None))
    assert bl.board_edge()["edge_verdict"] == "weak"


def test_edge_negative_on_nonpositive_return(monkeypatch):
    # avg_return <= 0 → negative, EVEN with a decent win_rate (the safer verdict wins the mixed read).
    _patch_board(monkeypatch, _stats(20, 12, 8, 0, 0.60, -0.4))
    assert bl.board_edge()["edge_verdict"] == "negative"
    _patch_board(monkeypatch, _stats(20, 12, 8, 0, 0.60, 0.0))
    assert bl.board_edge()["edge_verdict"] == "negative"


def test_edge_negative_on_low_win_rate(monkeypatch):
    # win_rate < 0.40 → negative, even with a (barely) positive avg_return.
    _patch_board(monkeypatch, _stats(20, 6, 12, 2, 0.333, 0.2))
    assert bl.board_edge()["edge_verdict"] == "negative"


# =========================================================================== #
# 3. standout_trust_multiplier — SHRINK-ONLY mapping + never > 1.0 + fail-soft
# =========================================================================== #

def test_multiplier_insufficient_is_neutral_1(monkeypatch):
    # cold-start: no data → 1.0 (NEVER penalize a board with no forward evidence).
    _patch_board(monkeypatch, _stats(0, 0, 0, 0, None, None))
    assert bl.standout_trust_multiplier() == 1.0


def test_multiplier_strong_is_1(monkeypatch):
    # a proven edge is trusted at par — never BOOSTED above 1.0.
    _patch_board(monkeypatch, _stats(20, 14, 4, 2, 0.78, 4.0))
    assert bl.standout_trust_multiplier() == 1.0


def test_multiplier_weak_is_075(monkeypatch):
    _patch_board(monkeypatch, _stats(20, 9, 9, 2, 0.50, 1.0))
    assert bl.standout_trust_multiplier() == pytest.approx(0.75)


def test_multiplier_negative_is_05(monkeypatch):
    _patch_board(monkeypatch, _stats(20, 6, 12, 2, 0.333, -1.0))
    assert bl.standout_trust_multiplier() == pytest.approx(0.5)


def test_multiplier_never_exceeds_1_across_all_verdicts(monkeypatch):
    # sweep every verdict — the multiplier must stay within [0.5, 1.0] EVERYWHERE.
    for stats in (
        _stats(0, 0, 0, 0, None, None),          # insufficient
        _stats(20, 14, 4, 2, 0.78, 9.9),         # strong
        _stats(20, 9, 9, 2, 0.50, 0.5),          # weak
        _stats(20, 5, 13, 2, 0.28, -3.0),        # negative
    ):
        _patch_board(monkeypatch, stats)
        m = bl.standout_trust_multiplier()
        assert 0.5 <= m <= 1.0, f"multiplier {m} out of shrink-only bounds for {stats}"


def test_multiplier_fail_soft_on_reader_error(monkeypatch):
    # a board_stats() that RAISES → the neutral 1.0 (a broken loop never shrinks a candidate).
    import brain.board_track_record as btr

    def _boom():
        raise RuntimeError("ledger read blew up")
    monkeypatch.setattr(btr, "board_stats", _boom)
    assert bl.standout_trust_multiplier() == 1.0
    assert bl.board_edge()["edge_verdict"] == "insufficient"


# =========================================================================== #
# 4. audit_row
# =========================================================================== #

def test_audit_row_present_strong(monkeypatch):
    _patch_board(monkeypatch, _stats(20, 14, 4, 2, 0.78, 4.0), as_of="2026-07-09")
    row = bl.audit_row()
    assert row["status"] == "strong"
    assert row["edge_verdict"] == "strong"
    assert row["as_of"] == "2026-07-09"
    assert row["n"] == 20
    assert row["multiplier"] == 1.0


def test_audit_row_insufficient_cold_start(monkeypatch):
    _patch_board(monkeypatch, _stats(0, 0, 0, 0, None, None))
    row = bl.audit_row()
    assert row["status"] == "insufficient"
    assert row["n"] == 0
    assert row["multiplier"] == 1.0


def test_audit_row_negative_multiplier(monkeypatch):
    _patch_board(monkeypatch, _stats(20, 5, 13, 2, 0.28, -2.0))
    row = bl.audit_row()
    assert row["status"] == "negative"
    assert row["multiplier"] == pytest.approx(0.5)


def test_audit_row_fail_soft(monkeypatch):
    import brain.board_track_record as btr

    def _boom():
        raise RuntimeError("boom")
    monkeypatch.setattr(btr, "board_stats", _boom)
    row = bl.audit_row()
    assert row["status"] == "insufficient"
    assert row["multiplier"] == 1.0
