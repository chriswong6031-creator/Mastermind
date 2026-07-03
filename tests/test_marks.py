"""THE marking layer (portfolio.marks) — W-L / L1.

Guards the safety-critical properties of the one price source:
  * logged source precedence polygon-EOD → yahoo-parquet → last-good-carry;
  * NEVER avg_cost — an unpriceable name is returned UNPRICED (absent), not marked to a cost;
  * a successful live mark refreshes the carry store; a missing feed carries the last-good forward
    with a growing stale_days count; a carry too old is dropped;
  * fully offline (both live sources are injected) — no network, no vendor engine.
"""
from __future__ import annotations

import json

import pytest

import bot  # noqa: F401 — bootstraps vendor/macro onto sys.path
from portfolio import marks as M


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """Redirect the carry store + per-day audit logs into a temp dir."""
    monkeypatch.setattr(M, "_MARKS_DIR", tmp_path)
    monkeypatch.setattr(M, "_CARRY_PATH", tmp_path / "last_good.json")
    return tmp_path


# ── source precedence ────────────────────────────────────────────────────────
def test_polygon_wins_over_yahoo_and_carry(sandbox):
    res = M.mark_symbols(
        ["AAPL"], "2026-01-05",
        polygon_fn=lambda s: 200.0,
        yahoo_fn=lambda s, a: 190.0,
        carry={"AAPL": {"price": 180.0, "asof": "2026-01-05"}},
    )
    assert res["prices"]["AAPL"] == 200.0
    assert res["sources"]["AAPL"]["source"] == M.SOURCE_POLYGON
    assert res["counts"][M.SOURCE_POLYGON] == 1


def test_yahoo_when_polygon_missing(sandbox):
    res = M.mark_symbols(
        ["AAPL"], "2026-01-05",
        polygon_fn=lambda s: None,
        yahoo_fn=lambda s, a: 190.0,
    )
    assert res["prices"]["AAPL"] == 190.0
    assert res["sources"]["AAPL"]["source"] == M.SOURCE_YAHOO


def test_carry_when_both_live_sources_miss(sandbox):
    res = M.mark_symbols(
        ["AAPL"], "2026-01-06",
        polygon_fn=lambda s: None,
        yahoo_fn=lambda s, a: None,
        carry={"AAPL": {"price": 180.0, "asof": "2026-01-05"}},
    )
    assert res["prices"]["AAPL"] == 180.0
    assert res["sources"]["AAPL"]["source"] == M.SOURCE_CARRY
    assert res["sources"]["AAPL"]["stale_days"] == 1        # 1 calendar day old


def test_seed_short_circuits_everything(sandbox):
    res = M.mark_symbols(
        ["AAPL"], "2026-01-05",
        seed={"AAPL": 111.0},
        polygon_fn=lambda s: 999.0,
        yahoo_fn=lambda s, a: 888.0,
    )
    assert res["prices"]["AAPL"] == 111.0
    assert res["sources"]["AAPL"]["source"] == "seed"


# ── NEVER avg_cost / unpriced discipline ──────────────────────────────────────
def test_unpriceable_name_is_unpriced_not_avg_cost(sandbox):
    res = M.mark_symbols(
        ["ZZZZ"], "2026-01-05",
        polygon_fn=lambda s: None,
        yahoo_fn=lambda s, a: None,
        carry={},
    )
    assert "ZZZZ" not in res["prices"]                     # absent — never a cost/garbage mark
    assert res["sources"]["ZZZZ"]["source"] is None
    assert res["counts"]["unpriced"] == 1


# ── carry store lifecycle ─────────────────────────────────────────────────────
def test_live_mark_refreshes_carry_store(sandbox):
    M.mark_symbols(["AAPL"], "2026-01-05", polygon_fn=lambda s: 200.0,
                   yahoo_fn=lambda s, a: None)
    carry = json.loads((sandbox / "last_good.json").read_text())
    assert carry["AAPL"]["price"] == 200.0
    assert carry["AAPL"]["asof"] == "2026-01-05"
    # next day the feed dies → the freshly-stored carry rescues the mark
    res = M.mark_symbols(["AAPL"], "2026-01-06", polygon_fn=lambda s: None,
                         yahoo_fn=lambda s, a: None)
    assert res["prices"]["AAPL"] == 200.0
    assert res["sources"]["AAPL"]["source"] == M.SOURCE_CARRY


def test_carry_does_not_restamp_its_own_freshness(sandbox):
    """A carry must NOT reset its asof each run (that would let a stale mark live forever)."""
    M.mark_symbols(["AAPL"], "2026-01-05", polygon_fn=lambda s: 200.0, yahoo_fn=lambda s, a: None)
    # a run priced only by carry
    M.mark_symbols(["AAPL"], "2026-01-10", polygon_fn=lambda s: None, yahoo_fn=lambda s, a: None)
    carry = json.loads((sandbox / "last_good.json").read_text())
    assert carry["AAPL"]["asof"] == "2026-01-05"           # still the ORIGINAL live date


def test_carry_too_old_is_dropped(sandbox):
    res = M.mark_symbols(
        ["AAPL"], "2026-03-01",
        polygon_fn=lambda s: None, yahoo_fn=lambda s, a: None,
        carry={"AAPL": {"price": 180.0, "asof": "2026-01-01"}},   # ~59d old > 30d
    )
    assert "AAPL" not in res["prices"]
    assert res["counts"]["unpriced"] == 1


# ── convenience wrappers ──────────────────────────────────────────────────────
def test_mark_one_and_prices_for(sandbox):
    px = M.mark_one("AAPL", "2026-01-05", polygon_fn=lambda s: 200.0, yahoo_fn=lambda s, a: None)
    assert px == 200.0
    d = M.prices_for(["AAPL", "MSFT"], "2026-01-05",
                     polygon_fn=lambda s: {"AAPL": 200.0, "MSFT": 400.0}.get(s),
                     yahoo_fn=lambda s, a: None)
    assert d == {"AAPL": 200.0, "MSFT": 400.0}


def test_never_raises_on_garbage(sandbox):
    # a source that throws must not sink the run (P2 degrade)
    def _boom(*a, **k):
        raise RuntimeError("feed down")
    res = M.mark_symbols(["AAPL"], "2026-01-05", polygon_fn=_boom, yahoo_fn=_boom, carry={})
    assert res["prices"] == {}
    assert res["counts"]["unpriced"] == 1
