"""Guardrails for the engine rebuild — the measures that stop the 'topping-financials' bug class.

These are LOGIC-level (synthetic rows) where possible, so they stay green as the live data
refreshes — they assert the gate's *behaviour*, not today's market.
"""
import bot  # noqa: F401

from portfolio import lenses, conviction


def _row(lens, direction, value=None):
    return {"lens": lens, "value": value or {}, "status": "validated", "direction": direction, "note": ""}


# ---------------------------------------------------------------------------
# de-correlation: a correlated value bloc must not over-count
# ---------------------------------------------------------------------------

def test_fundamental_bloc_collapses_to_one_vote():
    """Five correlated fundamental bulls + one independent bear should NOT read as 5-vs-1."""
    rows = [_row("valuation", "bull"), _row("quality", "bull"), _row("growth", "bull"),
            _row("solvency", "bull"), _row("asymmetry", "bull"), _row("sector_rs", "bear")]
    s = lenses.synthesize({"rows": rows})
    # fundamentals collapse to ONE bull; sector_rs is the one independent bear -> net flat-ish
    assert s["bloc_fund"] == "bull"
    assert s["bull"] == 1 and s["bear"] == 1
    assert s["confluence"] == 0.0


def test_macro_bloc_collapses_to_one_vote():
    rows = [_row("macro_risk", "bull"), _row("rate_inflation", "bull"),
            _row("fed_path", "bear"), _row("cross_asset", "bear"), _row("trend", "bull")]
    s = lenses.synthesize({"rows": rows})
    # macro net is 0 (2 bull, 2 bear) -> no macro vote; only the independent trend bull remains
    assert s["bloc_macro"] == "neutral"
    assert s["bull"] == 1 and s["bear"] == 0


# ---------------------------------------------------------------------------
# leadership gate: a cheap name in a hard-lagging sector is not a BUY
# ---------------------------------------------------------------------------

def test_leadership_gate_blocks_lagging_sector():
    """High fundamental confluence but a lagging sector (and no leading-theme escape) -> not 'up'."""
    rows = [_row("valuation", "bull"), _row("quality", "bull"), _row("growth", "bull"),
            _row("asymmetry", "bull"), _row("conviction", "bull"),
            _row("sector_rs", "bear"), _row("narrative", "bear")]
    s = lenses.synthesize({"rows": rows})
    assert s["sector_lagging"] is True
    assert s["leadership_ok"] is False
    assert s["size_authority"] != "up"          # confirmation over prediction


def test_leadership_gate_allows_leader():
    """Same fundamentals in a LEADING sector -> a clean buy."""
    rows = [_row("valuation", "bull"), _row("quality", "bull"), _row("growth", "bull"),
            _row("asymmetry", "bull"), _row("sector_rs", "bull"), _row("flows_13f", "bull")]
    s = lenses.synthesize({"rows": rows})
    assert s["sector_lagging"] is False and s["leadership_ok"] is True
    assert s["size_authority"] == "up"


def test_lagging_sector_with_leading_theme_escape():
    """A name in a soft sector but a genuine LEADING theme can still earn a buy."""
    rows = [_row("valuation", "bull"), _row("growth", "bull"), _row("flows_13f", "bull"),
            _row("sector_rs", "bear"), _row("narrative", "bull")]   # theme is the escape hatch
    s = lenses.synthesize({"rows": rows})
    assert s["leadership_ok"] is True


# ---------------------------------------------------------------------------
# trend lens: downtrend = bear; a pullback in an uptrend is NOT a downtrend
# ---------------------------------------------------------------------------

def test_trend_downtrend_is_bear():
    d = {"tech": {"above50": False, "above200": False, "macd_pos": False,
                  "rsi14": 38, "off_52w_high_pct": -22, "pct_vs_50dma": -6}}
    assert lenses._trend_row(d)["direction"] == "bear"


def test_trend_pullback_in_uptrend_not_bear():
    # below the 50dma but still above a rising 200dma, near the high -> a pullback, not a downtrend
    d = {"tech": {"above50": False, "above200": True, "macd_pos": False,
                  "rsi14": 48, "off_52w_high_pct": -8, "pct_vs_50dma": -2}}
    assert lenses._trend_row(d)["direction"] != "bear"


# ---------------------------------------------------------------------------
# sector-concentration cap (position sizing discipline)
# ---------------------------------------------------------------------------

def test_sector_cap_holds_in_real_book():
    """No single sector may exceed SECTOR_MAX_NAMES in the sized conviction book."""
    sized, _rej = conviction.build(budget=0.40, name_cap=0.08)
    counts: dict[str, int] = {}
    for p in sized:
        sec = conviction._sector_of(p["ticker"])
        counts[sec] = counts.get(sec, 0) + 1
    assert all(c <= conviction.SECTOR_MAX_NAMES for c in counts.values()), counts
    assert all(p["weight"] <= 0.08 + 1e-9 for p in sized)


# ---------------------------------------------------------------------------
# research score mirrors the leadership discipline
# ---------------------------------------------------------------------------

def test_research_score_penalises_lagging_sector():
    from brain import research_paper as rp
    leader = [_row("valuation", "bull"), _row("growth", "bull"), _row("sector_rs", "bull")]
    laggard = [_row("valuation", "bull"), _row("growth", "bull"), _row("sector_rs", "bear")]
    assert rp._deterministic_score(leader, 0.4) > rp._deterministic_score(laggard, 0.4)


# ---------------------------------------------------------------------------
# falling-knife gate + the theme lens must actually fire (the slug regression)
# ---------------------------------------------------------------------------

def test_downtrend_blocks_buy():
    """A confirmed downtrend must not be a BUY even with strong fundamentals (no falling knives)."""
    rows = [_row("valuation", "bull"), _row("growth", "bull"), _row("asymmetry", "bull"),
            _row("sector_rs", "neutral"), _row("trend", "bear")]
    s = lenses.synthesize({"rows": rows})
    assert s["price_downtrend"] is True
    assert s["size_authority"] != "up"


def test_theme_lens_fires_end_to_end():
    """Regression guard for the slug bug: the per-name narrative/theme lens must actually resolve
    from real stockdata membership (keyed on 'slug') — not silently read 'missing'."""
    # find any stockdata name that carries baskets_membership, then assert its narrative votes
    import glob, json, pathlib
    found = None
    for f in glob.glob(str(pathlib.Path(lenses._V) / "site" / "stockdata" / "*.json"))[:400]:
        try:
            d = json.loads(pathlib.Path(f).read_text())
        except Exception:
            continue
        mem = d.get("baskets_membership")
        if isinstance(mem, list) and mem and isinstance(mem[0], dict) and mem[0].get("slug"):
            found = d.get("ticker") or pathlib.Path(f).stem
            break
    if not found:
        import pytest
        pytest.skip("no stockdata with basket membership available")
    nar = next((r for r in lenses.full(found, "name")["rows"] if r["lens"] == "narrative"), {})
    assert nar.get("status") == "validated", f"{found} theme lens still dead (slug regression)"
    assert nar.get("direction") in ("bull", "bear", "neutral")


# ---------------------------------------------------------------------------
# profit calc: cost-basis reset wipes unrealized P&L
# ---------------------------------------------------------------------------

def test_position_log_idempotent_per_build_date(tmp_path, monkeypatch):
    """Rebuilding the book many times with the SAME asof must not spam ADD/TRIM history (the
    AVGO 'ADD 4.8% / TRIM 3.5% / ADD 4.8% within a minute' churn bug)."""
    from portfolio import position_log as pl
    monkeypatch.setattr(pl, "_LEDGER_PATH", tmp_path / "led.json", raising=False)
    pl.update([{"ticker": "ZZZ", "sleeve": "conviction", "weight": 0.05, "entry_price": 10.0}], "2026-06-18")
    # three intra-day rebuilds with jittering weights, same asof
    for w in (0.03, 0.06, 0.04):
        pl.update([{"ticker": "ZZZ", "sleeve": "conviction", "weight": w, "entry_price": 10.0}], "2026-06-18")
    import json
    hist = json.loads((tmp_path / "led.json").read_text())["conviction:ZZZ"]["history"]
    assert len(hist) == 1 and hist[0]["event"] == "open", hist     # one event for the build date
    # a NEW build date with a material change logs exactly one rebalance
    pl.update([{"ticker": "ZZZ", "sleeve": "conviction", "weight": 0.02, "entry_price": 10.0}], "2026-06-19")
    hist = json.loads((tmp_path / "led.json").read_text())["conviction:ZZZ"]["history"]
    assert len(hist) == 2 and hist[-1]["event"] == "trim"


def test_reset_cost_basis_zeroes_unrealized(tmp_path, monkeypatch):
    from portfolio import paper_account as pa
    monkeypatch.setattr(pa, "_DATA", tmp_path, raising=False)
    monkeypatch.setattr(pa, "_ACCOUNT_PATH", tmp_path / "account.json", raising=False)
    pa._save_account({"inception_date": "2026-06-20", "starting_nav": 1_000_000.0,
                      "cash": 500_000.0, "positions": {"AAA": {"shares": 100, "avg_cost": 50.0}},
                      "spy_shares": None})
    upd = pa.reset_cost_basis_to_market(prices={"AAA": 73.0})
    assert upd["AAA"] == 73.0
    pnl = pa.positions_pnl({"AAA": 73.0})
    assert pnl["AAA"]["unrealized_pnl"] == 0.0
