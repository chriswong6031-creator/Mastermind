"""Guardrails for the 2026-06-20 bad-buy class — the four vetoes that silently no-op'd:

  1. NEM  — a gold miner in a gold BEAR market (commodity-regime gate for miners).
  2. LPG  — a 4-day ~-10% freefall the slow MA/52w fields missed (falling-knife velocity veto).
  3. several buys were "not actually asymmetric" (the asymmetry gate).
  4. NVDA — bought then immediately closed (entry/exit hysteresis stops churn).

All LOGIC-level (synthetic rows / monkeypatched price seams) so they stay green as live data
refreshes and run with no network / no submodule checkout.
"""
from __future__ import annotations

from unittest import mock

import bot  # noqa: F401  -> vendor/macro onto sys.path

from portfolio import lenses
import portfolio.conviction as _cv_mod
import portfolio.lenses as _lenses_mod


def _row(lens, direction, value=None):
    return {"lens": lens, "value": value or {}, "status": "validated", "direction": direction, "note": ""}


# ---------------------------------------------------------------------------
# 2. falling-knife velocity veto (LPG): a fresh multi-day collapse blocks a buy
# ---------------------------------------------------------------------------

def test_trend_falling_knife_from_recent_return():
    """A name that looks fine on the slow fields (above both MAs, MACD+, only -8% off high) but
    just dropped ~12% over five sessions must read falling_fast and NOT be a confirmed uptrend."""
    d = {"ticker": "LPG", "tech": {"above50": True, "above200": True, "macd_pos": True,
                                    "rsi14": 52, "off_52w_high_pct": -8, "pct_vs_50dma": 1.0}}
    with mock.patch.object(_lenses_mod, "_recent_return",
                           side_effect=lambda tkr, n: -0.12 if n == 5 else None):
        row = lenses._trend_row(d)
    assert row["value"]["falling_fast"] is True
    assert row["direction"] != "bull", "a fresh freefall is never a confirmed uptrend"


def test_trend_mild_pullback_not_falling_knife():
    """A -3% week is a normal pullback, not a falling knife."""
    d = {"ticker": "AVGO", "tech": {"above50": True, "above200": True, "macd_pos": True,
                                    "rsi14": 55, "off_52w_high_pct": -6, "pct_vs_50dma": 2.0}}
    with mock.patch.object(_lenses_mod, "_recent_return",
                           side_effect=lambda tkr, n: -0.03):
        row = lenses._trend_row(d)
    assert row["value"]["falling_fast"] is False


def test_falling_knife_blocks_buy_but_not_a_hard_exit():
    """price_falling_fast blocks a NEW buy (size_authority != up) but is kept SEPARATE from the
    structural downtrend so a held name isn't force-exited (price_downtrend stays False)."""
    rows = [_row("valuation", "bull"), _row("growth", "bull"),
            _row("asymmetry", "bull", {"upside_downside": 2.0}),
            _row("sector_rs", "neutral"),
            _row("trend", "neutral", {"falling_fast": True, "downtrend": False})]
    s = lenses.synthesize({"rows": rows})
    assert s["price_falling_fast"] is True
    assert s["price_downtrend"] is False           # NOT a structural hard exit
    assert s["size_authority"] != "up"


# ---------------------------------------------------------------------------
# 3. asymmetry gate: a symmetric/inverted cone is not a buy
# ---------------------------------------------------------------------------

def test_weak_asymmetry_blocks_buy():
    rows = [_row("valuation", "bull"), _row("sector_rs", "bull"), _row("flows_13f", "bull"),
            _row("asymmetry", "bear", {"upside_downside": 0.9})]
    s = lenses.synthesize({"rows": rows})
    assert s["weak_asymmetry"] is True
    assert s["size_authority"] != "up", "a non-asymmetric cone must not earn a full buy"


def test_strong_asymmetry_allows_buy():
    rows = [_row("valuation", "bull"), _row("sector_rs", "bull"), _row("flows_13f", "bull"),
            _row("asymmetry", "bull", {"upside_downside": 2.0}),
            # W8: padded to clear the _MIN_VOTES_FOR_UP evidence floor
            _row("trend", "bull"), _row("narrative", "bull"), _row("options", "bull")]
    s = lenses.synthesize({"rows": rows})
    assert s["weak_asymmetry"] is False
    assert s["size_authority"] == "up"


def test_missing_asymmetry_does_not_block():
    """No cone data -> degrade gracefully (do not block), same as before the gate existed."""
    rows = [_row("valuation", "bull"), _row("sector_rs", "bull"), _row("flows_13f", "bull"),
            # W8: padded to clear the _MIN_VOTES_FOR_UP evidence floor
            _row("trend", "bull"), _row("narrative", "bull"), _row("options", "bull")]
    s = lenses.synthesize({"rows": rows})
    assert s["asym_ratio"] is None and s["weak_asymmetry"] is False
    assert s["size_authority"] == "up"


# ---------------------------------------------------------------------------
# 1. commodity-regime gate (NEM): a gold miner in a gold bear market votes bear
# ---------------------------------------------------------------------------

def test_commodity_miner_bear_in_gold_bear():
    """NEM maps to GC=F, which is NOT in the sector-ETF RS table; the commodity-regime fallback
    must derive gold's trend from its own series and vote bear when gold is below trend + falling."""
    bear = {"above_200d_trend": False, "mom_60d_pct": -15.0, "downtrend": True}
    # hermetic: force GC=F ABSENT from the table so the price-series fallback is exercised regardless
    # of whatever the live regime data happens to carry.
    with mock.patch.object(_lenses_mod, "_commodity_regime", return_value=bear), \
         mock.patch.object(_lenses_mod, "_load", return_value={"sector_rs": []}):
        row = lenses._sector_rs_row({"ticker": "NEM", "sector": "Materials"})
    assert row["direction"] == "bear"
    assert row["value"].get("commodity_driven") is True


def test_commodity_miner_bear_fails_leadership_gate():
    """The bear sector_rs vote must flow into the leadership gate so the miner is not a buy."""
    bear = {"above_200d_trend": False, "mom_60d_pct": -15.0, "downtrend": True}
    with mock.patch.object(_lenses_mod, "_commodity_regime", return_value=bear), \
         mock.patch.object(_lenses_mod, "_load", return_value={"sector_rs": []}):
        sr = lenses._sector_rs_row({"ticker": "NEM", "sector": "Materials"})
    rows = [_row("valuation", "bull"), _row("growth", "bull"),
            _row("asymmetry", "bull", {"upside_downside": 2.0}),
            _row("narrative", "bear"), sr]
    s = lenses.synthesize({"rows": rows})
    assert s["sector_lagging"] is True
    assert s["leadership_ok"] is False
    assert s["size_authority"] != "up"


def test_commodity_miner_bull_in_gold_bull_allowed():
    """A gold miner while gold is LEADING is a legitimate buy — the gate must not over-block."""
    bull = {"above_200d_trend": True, "mom_60d_pct": 12.0, "downtrend": False}
    with mock.patch.object(_lenses_mod, "_commodity_regime", return_value=bull), \
         mock.patch.object(_lenses_mod, "_load", return_value={"sector_rs": []}):
        row = lenses._sector_rs_row({"ticker": "NEM", "sector": "Materials"})
    assert row["direction"] == "bull"


def test_commodity_miner_uses_commodity_thresholds_from_regime_table():
    """When GC=F IS in the sector_rs table (the live-data case), a MILD gold bear (below 200d, only
    -5% over 60d) must still read bear via the COMMODITY threshold (<= -2), not the lenient broad-
    sector threshold (<= -12) that would let it pass."""
    regime = {"sector_rs": [{"ticker": "GC=F", "above_200d_trend": False, "mom_60d_pct": -5.0,
                             "pctile_252d": 45, "rank": 8}]}
    with mock.patch.object(_lenses_mod, "_load", return_value=regime):
        row = lenses._sector_rs_row({"ticker": "NEM", "sector": "Materials"})
    assert row["direction"] == "bear"
    assert row["value"].get("source") == "regime_table"


# ---------------------------------------------------------------------------
# 4. entry/exit hysteresis (NVDA churn): a held name isn't dropped on a wobble
# ---------------------------------------------------------------------------

def _fake_full_hold(ticker: str, kind: str = "name"):
    """A name parked in the churn zone: would-be 'hold' (confluence 0.20, just above the exit
    floor) — a NEW name is rejected, a HELD name is retained."""
    return {
        "subject": ticker, "kind": "name",
        "rows": [{"lens": "valuation", "value": {}, "status": "context", "direction": "neutral", "note": ""}],
        "synthesis": {"bull": 1, "bear": 1, "n_scored": 2, "confluence": 0.20,
                      "vetoes": [], "divergences": [], "size_authority": "hold",
                      "price_downtrend": False, "leadership_ok": True,
                      "price_falling_fast": False, "weak_asymmetry": False, "asym_ratio": None},
    }


def test_new_name_in_churn_zone_is_rejected():
    with mock.patch.object(_cv_mod, "candidates", return_value=["ZZZ"]), \
         mock.patch.object(_lenses_mod, "full", side_effect=_fake_full_hold):
        sized, rejected = _cv_mod.build(budget=0.30, name_cap=0.08)
    assert "ZZZ" not in [p["ticker"] for p in sized]
    assert "ZZZ" in [r["ticker"] for r in rejected]


def test_held_name_in_churn_zone_is_retained():
    with mock.patch.object(_cv_mod, "candidates", return_value=["ZZZ"]), \
         mock.patch.object(_lenses_mod, "full", side_effect=_fake_full_hold):
        sized, rejected = _cv_mod.build(budget=0.30, name_cap=0.08, held={"ZZZ"})
    assert "ZZZ" in [p["ticker"] for p in sized], "a held name must survive a marginal wobble"
    held_pos = next(p for p in sized if p["ticker"] == "ZZZ")
    assert held_pos.get("retained") is True


def test_held_name_with_hard_veto_still_exits():
    """Hysteresis must NOT rescue a genuinely broken held name — a hard veto still drops it."""
    def _fake_vetoed(ticker, kind="name"):
        f = _fake_full_hold(ticker, kind)
        f["synthesis"]["vetoes"] = ["parabolic"]
        f["synthesis"]["size_authority"] = "blocked"
        return f
    with mock.patch.object(_cv_mod, "candidates", return_value=["ZZZ"]), \
         mock.patch.object(_lenses_mod, "full", side_effect=_fake_vetoed):
        sized, rejected = _cv_mod.build(budget=0.30, name_cap=0.08, held={"ZZZ"})
    assert "ZZZ" not in [p["ticker"] for p in sized]


def test_held_name_in_downtrend_still_exits():
    """A confirmed STRUCTURAL downtrend is a hard exit even for a held name (no min-hold on a
    broken name) — distinct from a one-week falling-knife, which a held name rides through."""
    def _fake_downtrend(ticker, kind="name"):
        f = _fake_full_hold(ticker, kind)
        f["synthesis"]["price_downtrend"] = True
        return f
    with mock.patch.object(_cv_mod, "candidates", return_value=["ZZZ"]), \
         mock.patch.object(_lenses_mod, "full", side_effect=_fake_downtrend):
        sized, rejected = _cv_mod.build(budget=0.30, name_cap=0.08, held={"ZZZ"})
    assert "ZZZ" not in [p["ticker"] for p in sized]


# ---------------------------------------------------------------------------
# 4b. research-gate hysteresis: a carried name survives a marginal combined-score dip
# ---------------------------------------------------------------------------

def test_research_gate_held_hysteresis():
    from brain import research_paper as rp
    paper = {"research_score": 58, "viability": "fair", "mode": "engine", "recommend": True}
    # confluence 0.1 -> engine_score 55 -> combined round(0.5*55 + 0.5*58) = 56 (below the 60 bar,
    # above the held bar of 52)
    new = rp.score_breakdown(0.1, paper, held=False)
    carried = rp.score_breakdown(0.1, paper, held=True)
    assert new["confirmed"] is False, "a NEW buy needs the full confirm bar"
    assert carried["confirmed"] is True, "a carried name survives down to the hysteresis bar"


def test_research_gate_avoid_still_drops_held():
    """Hysteresis lowers only the score bar — viability 'avoid' still drops a held name."""
    from brain import research_paper as rp
    paper = {"research_score": 90, "viability": "avoid", "mode": "engine", "recommend": True}
    g = rp.score_breakdown(0.5, paper, held=True)
    assert g["confirmed"] is False
