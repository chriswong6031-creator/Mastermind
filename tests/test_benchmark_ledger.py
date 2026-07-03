"""THE benchmark ledger (brain.benchmark_ledger) — W-L / L1.

Guards the renorm math, the regime-conditional bogey switch, and — THE ACCEPTANCE the whole
program started from — that on the incident-window fixtures the defensive basket beats the Brain.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import bot  # noqa: F401 — bootstraps vendor/macro
from brain import benchmark_ledger as B

_FIXT = Path(__file__).resolve().parent / "incident_replays" / "fixtures" / "2026-07-02-semis-breakdown"


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "_BENCH_DIR", tmp_path)
    return tmp_path


def _incident_series() -> dict:
    """{TICKER: {date: px}} from the real trimmed incident closes (SMH/XLV/XLK/XLU/SPY)."""
    return json.loads((_FIXT / "etf_closes.json").read_text())


# ── renorm math ───────────────────────────────────────────────────────────────
def test_single_name_renorm_is_growth_of_dollar():
    series = {"SPY": {"2026-01-02": 500.0, "2026-01-03": 550.0}}
    curve = B.renorm_basket(series, ["SPY"])
    assert curve["2026-01-02"] == 1.0
    assert curve["2026-01-03"] == 1.1                      # +10%


def test_equal_weight_basket_renorm():
    series = {"A": {"d1": 100.0, "d2": 110.0}, "B": {"d1": 50.0, "d2": 45.0}}
    curve = B.renorm_basket(series, ["A", "B"])
    assert curve["d1"] == 1.0
    # mean(1.10, 0.90) = 1.0
    assert curve["d2"] == 1.0


def test_common_inception_skips_pre_alignment_dates():
    # B only starts on d2, so the basket inception is d2 (not d1)
    series = {"A": {"d1": 100.0, "d2": 110.0, "d3": 120.0},
              "B": {"d2": 50.0, "d3": 55.0}}
    curve = B.renorm_basket(series, ["A", "B"])
    assert "d1" not in curve
    assert curve["d2"] == 1.0
    assert min(curve) == "d2"


# ── regime-conditional bogey ────────────────────────────────────────────────────
def test_regime_max_uses_defensive_when_not_risk_on():
    spy = {"d1": 1.0, "d2": 1.01}
    dfn = {"d1": 1.0, "d2": 1.06}
    out = B.regime_max_curve(spy, dfn, {"state": "risk_off"})
    assert out["d2"] == 1.06                                # took the defensive max


def test_regime_max_uses_defensive_when_weakening_even_if_risk_on():
    spy = {"d1": 1.0, "d2": 1.01}
    dfn = {"d1": 1.0, "d2": 1.06}
    out = B.regime_max_curve(spy, dfn, {"state": "risk_on", "transition_state": "WEAKENING"})
    assert out["d2"] == 1.06


def test_regime_max_is_plain_spy_in_risk_on():
    spy = {"d1": 1.0, "d2": 1.01}
    dfn = {"d1": 1.0, "d2": 1.06}
    out = B.regime_max_curve(spy, dfn, {"state": "risk_on"})
    assert out["d2"] == 1.01                                # cash-hoarding still loses to the market


def test_regime_missing_degrades_to_plain_spy():
    spy = {"d1": 1.0, "d2": 1.01}
    dfn = {"d1": 1.0, "d2": 1.06}
    out = B.regime_max_curve(spy, dfn, None)
    assert out["d2"] == 1.01                                # no defensive alibi without evidence


# ── THE ACCEPTANCE — the fact the whole program started from ──────────────────────
def test_defensive_basket_beats_every_brain_book_on_incident_window(sandbox):
    """On the semis-unwind window (SMH −7.2%, XLV +6.3%), the equal-weight defensive basket must
    out-return SPY *and* every Brain book. The peer books were piled into SMH/tech, so their carry
    curve tracks the offensive names down while the defensive sleeve rides XLU/XLV/XLF/XLP.

    The defensive basket in this fixture is XLU/XLV/XLF/XLP; the fixture ships XLU/XLV (the two that
    dominate the divergence), so we build the defensive curve from those and the offensive Brain
    curve from SMH/XLK (the pile-up the audit named)."""
    series = _incident_series()
    # the Brain books' carry curve = the SMH/tech pile-up (equal-weight of the offensive names the
    # peer_books held) — this stands in for the do-nothing carry of a tech-heavy book.
    brain_curve = B.renorm_basket(series, ["SMH", "XLK"])
    # defensive sleeve available in the fixture (XLU + XLV — utilities + healthcare)
    def_curve = B.renorm_basket(series, ["XLU", "XLV"])

    ledger = B.build(series, asof="2026-07-01", regime={"state": "risk_off"},
                     book_curves={"autonomous": brain_curve, "heavyweight": brain_curve})

    lb = ledger["leaderboard"]
    by_id = {r["id"]: r for r in lb}
    # the defensive bogey out-returns SPY and every book row
    def_ret = by_id["defensive"]["return_pct"]
    spy_ret = by_id["spy"]["return_pct"]
    book_rets = [r["return_pct"] for r in lb if r["kind"] == "book"]
    assert def_ret is not None and def_ret > spy_ret, (def_ret, spy_ret)
    assert all(def_ret > br for br in book_rets), (def_ret, book_rets)
    # and the leaderboard is actually ranked (defensive above the offensive books)
    ids_in_order = [r["id"] for r in lb]
    assert ids_in_order.index("defensive") < ids_in_order.index("autonomous")


def test_build_persists_and_loads(sandbox):
    series = _incident_series()
    B.build(series, asof="2026-07-01", regime={"state": "risk_off"})
    loaded = B.load("2026-07-01")
    assert loaded["as_of"] == "2026-07-01"
    assert "defensive" in loaded["bogeys"]
    assert B.latest()["as_of"] == "2026-07-01"


def test_build_degrades_on_empty_series(sandbox):
    ledger = B.build({}, asof="2026-07-01", regime=None)
    # no curves → empty leaderboard, no crash
    assert ledger["leaderboard"] == []
    assert ledger["bogeys"]["spy"]["curve"] == {}
