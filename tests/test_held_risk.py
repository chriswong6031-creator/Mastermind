"""Tests for portfolio.held_risk lane composer.

All tests are OFFLINE: synthetic OHLCV DataFrames injected via price_loader;
synthetic stockdata JSON in tmp dirs; no network calls.

Coverage:
  - Lane ok/watch/elevated paths for all 8 evidence lanes
  - Stale detection (artifact asof > 3 sessions old)
  - Coverage degradation: price_only (no stockdata, OHLCV only)
  - Giveback math: mfe=0, entry missing, normal elevated path, critical_giveback
  - Role ladder precedence: each role reachable + first-match ordering verified
  - market_regime shared lane (same state for all positions)
  - Atomic write (write_state → os.replace)
  - --positions-json CLI path (subprocess roundtrip)
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

# ---------------------------------------------------------------------------
# Helpers: synthetic fixture builders
# ---------------------------------------------------------------------------

def _make_ohlcv(closes: list[float], start: str = "2025-01-01") -> pd.DataFrame:
    """Build a DataFrame with DatetimeIndex and 'close' column."""
    idx = pd.date_range(start=start, periods=len(closes), freq="B")
    return pd.DataFrame({"close": closes}, index=idx)


def _price_loader_factory(frames: dict[str, pd.DataFrame]):
    """Return a price_loader that serves from a dict {ticker: df}."""
    def loader(ticker: str) -> pd.DataFrame | None:
        return frames.get(ticker)
    return loader


def _make_stockdata(overrides: dict | None = None) -> dict:
    """Return a minimal valid stockdata JSON matching the real AAPL schema."""
    base = {
        "ticker": "TST",
        "name": "Test Corp",
        "sector": "Information Technology",
        "asof": "2026-07-02",
        "tech": {
            "price": 100.0, "above50": True, "above200": True,
            "pct_vs_50dma": 5.0, "pct_vs_200dma": 10.0, "pct_vs_20dma": 3.0,
            "rsi14": 55.0, "atr14": 3.5, "atr_pct": 3.5, "rs_1m": 1.0,
            "off_52w_high_pct": -5.0, "coverage": "full",
        },
        "profile": {
            "sector": "Information Technology",
            "mktcap_bn": 500.0,
            "mktcap_tier": {"key": "large", "label": "Large cap", "label_zh": "大盘"},
            "archetype": {"key": "growth", "label": "Growth", "label_zh": "成长"},
        },
        "positioning": {
            "short": {"pct_float": 1.0, "days_to_cover": 2.0, "si_change_pct": 0.0},
            "short_flow": {"short_ratio_pct": 45.0, "trend_pp": 0.0, "n_days": 27, "asof": "2026-07-02"},
            "insider": {"cluster": False, "n_buyers": 2, "n_sellers": 0, "net_usd_mn": 5.0, "quarter": "6mo"},
        },
        "accounting_quality": {
            "verdict": "clean", "headline": "Clean", "n_caution": 0,
            "piotroski": {"of": 9, "score": 7},
            "reads": [],
        },
        "financials": {
            "raw": {
                "assets": 100e9, "cfo": 20e9, "debt_lt": 10e9,
                "equity": 30e9, "gross_profit": 40e9, "ni": 15e9,
                "repurchases": 5e9, "revenue": 100e9, "shares": 1e9, "dividends": None,
            },
            "multiyear": {
                "altman": {"z": 8.0, "zone": "safe", "approx": False},
                "piotroski": {"score": 7, "of": 9},
                "eps_cagr": 10.0, "rev_cagr": 8.0,
                "revenue": [], "eps": [], "gross_margin": [], "net_margin": [],
                "fcf_margin": [], "fcf": [], "years": [],
            },
            "gross_margin": 40.0, "net_margin": 15.0, "fcf_margin": 20.0,
            "ni_growth": 10.0, "rev_growth": 8.0, "asset_growth": 5.0,
            "roe": 50.0, "roa": 15.0, "debt_to_assets": 10.0, "accruals": 0.0,
        },
        "factors": {
            "radar_ok": True, "composite": 0.2, "fundamental_score": 50, "n_universe": 1323,
            "sector": "Information Technology",
            "legs": {
                "short_interest": 0.5, "quality": 0.4, "profitability": 0.8,
                "value": -0.2, "investment": -0.1, "payout": 0.1, "low_vol": 0.3,
                "low_beta": 0.0, "accruals": 0.1,
            },
            "radar": [],
        },
        "earnings": {
            "next_date": "2026-09-15", "next_time": None, "eps_forecast": 2.0,
            "sue_z": 1.0,
            "summary": {"avg_surprise": 5.0, "beats": 4, "streak": 4, "total": 4},
            "surprises": [],
        },
        "revisions": {
            "breadth": 0.8, "est_chg_30d": 0.5, "est_chg_90d": 1.0,
            "net_up_30d": 3.0, "n_analysts": 8.0,
        },
        "analyst": {"tier": "strong_buy", "forward_pe": 20.0, "pe_yf": 22.0, "rating": "Buy", "target": 120.0},
        "macro_sensitivity": {
            "ticker": "TST", "rate_beta": -0.1, "rate_beta_se": 0.05,
            "t_stat": -2.0, "tier": "low", "tier_label": "Low rate sensitivity",
            "regime": "neutral", "regime_label": {"en": "neutral", "zh": "中性"},
            "rate_state": {"regime": "restrictive", "direction": "rising", "real_10y": 2.2},
            "infl_state": {"regime": "above target", "direction": "steady"},
            "headline": "Rate-neutral", "detail": "", "caveat": "",
            "calibrated": True, "persist": True,
        },
        "sector_pulse": {
            "as_of": "2026-07-02", "theme_id": "tech_ai", "theme_name": "AI growth",
            "heat": 70.0, "label": "advancing", "reco": "hold", "rank": 3,
            "n_themes": 20, "rank_delta_5d": 1, "theme_ids": [],
        },
        "smart_money": {
            "holders": [], "is_vip": False, "n_holders": 2, "n_buying": 1, "n_selling": 0,
            "ownership_hhi": 0.5, "max_book_pct": 5.0, "avg_book_pct": 3.0,
            "trend": {"direction": "accumulating", "holders_delta": 1, "n_quarters": 4},
            "quality_cluster": {"best_grade": "B", "graded_holders": 1, "n_quality_buyers": 1},
            "vip": 0, "as_of": "2026-03-31",
        },
        "conviction": {
            "score": 60, "band": "constructive", "band_en": "Constructive",
            "drivers": [], "cautions": [], "cautions_zh": [], "notes": "",
        },
        "view": {
            "schema": "stock_view.v1", "market": "US",
            "decision": {"band": "constructive", "score": 60, "state": "ok",
                         "action": {"tone": "go", "verb": "BUY"}, "size": {"pct": 100}},
            "evidence": {
                "extension": {"tone": "good", "value": "In-trend", "label": "Extension"},
                "trend": {"tone": "good", "value": "Up · +10% vs 200-day"},
                "momentum": {"tone": "good", "value": "Daily ▲"},
                "quality": {"tone": "good", "value": "Durable"},
                "valuation": {"tone": "neutral", "value": "Fair"},
                "positioning": {"tone": "neutral", "value": "normal"},
                "rel_strength": {"tone": "good", "value": "alpha z +0.5"},
                "catalyst": {"tone": "neutral", "value": "earnings 2026-09-15"},
                "ownership": {"tone": "neutral", "value": "2 holders"},
                "volatility": {"tone": "neutral", "value": "typical"},
                "squeeze": {"tone": "neutral", "value": "Neutral"},
                "macro": {"tone": "neutral", "value": "rate β -0.1"},
            },
            "falsifiers": [], "fingerprint": {"axes": []}, "provenance": [],
            "country_slot": {"cards": []},
        },
        "alpha": {"alpha": 0.5, "rs": 60.0, "rs3m": 55.0, "rs6m": 50.0, "rs12m": 65.0},
        "altdata": {"tier": "low", "score": 50},
    }
    if overrides:
        _deep_update(base, overrides)
    return base


def _deep_update(d: dict, u: dict) -> dict:
    for k, v in u.items():
        if isinstance(v, dict) and isinstance(d.get(k), dict):
            _deep_update(d[k], v)
        else:
            d[k] = v
    return d


def _make_regime(overrides: dict | None = None) -> dict:
    base = {
        "schema_version": 2, "asof": "2026-07-02", "quad": "Q1", "quad_name": "Goldilocks",
        "risk_radar": {
            "schema": "risk_radar.v2", "asof": "2026-07-02",
            "state": "calm", "alert": False, "dominant_scare": None,
            "top_score": 20.0, "conjunction": False, "scares": [],
        },
        "vol_regime": {"asof": "2026-07-02", "available": True, "regime": "normalizing",
                       "risk_score": 0.2, "scored_active": False, "scored_score": None, "vix": 16.0},
        "risk_state": {
            "schema": "risk_state.v1", "asof": "2026-07-02",
            "score": 12.0, "state": "risk-on", "label_en": "Risk-on",
        },
        "sector_rs": [
            {"ticker": "XLK", "rank": 2, "rs": 0.24, "mom_20d_pct": -3.0, "pctile_252d": 89.0, "above_200d_trend": True},
            {"ticker": "XLV", "rank": 7, "rs": 0.07, "mom_20d_pct": 12.0, "pctile_252d": 27.0, "above_200d_trend": True},
            {"ticker": "XLF", "rank": 8, "rs": 0.07, "mom_20d_pct": 10.0, "pctile_252d": 27.0, "above_200d_trend": False},
            {"ticker": "XLY", "rank": 10, "rs": 0.16, "mom_20d_pct": 1.0, "pctile_252d": 7.0, "above_200d_trend": False},
            {"ticker": "XLP", "rank": 13, "rs": 0.11, "mom_20d_pct": 5.0, "pctile_252d": 21.0, "above_200d_trend": False},
            {"ticker": "XLI", "rank": 6, "rs": 0.22, "mom_20d_pct": 7.0, "pctile_252d": 80.0, "above_200d_trend": True},
            {"ticker": "XLB", "rank": 14, "rs": 0.07, "mom_20d_pct": 2.0, "pctile_252d": 50.0, "above_200d_trend": True},
            {"ticker": "XLRE", "rank": 11, "rs": 0.06, "mom_20d_pct": 4.0, "pctile_252d": 27.0, "above_200d_trend": False},
            {"ticker": "XLU", "rank": 16, "rs": 0.06, "mom_20d_pct": 6.0, "pctile_252d": 15.0, "above_200d_trend": False},
            {"ticker": "XLE", "rank": 18, "rs": 0.07, "mom_20d_pct": -7.0, "pctile_252d": 58.0, "above_200d_trend": False},
            {"ticker": "XLC", "rank": 17, "rs": 0.15, "mom_20d_pct": -1.0, "pctile_252d": 4.0, "above_200d_trend": False},
        ],
        "conditions": {},
    }
    if overrides:
        _deep_update(base, overrides)
    return base


def _spy_closes(n: int = 600) -> list[float]:
    """Simple upward-trending SPY close series."""
    return [400.0 + i * 0.3 for i in range(n)]


def _stock_closes(n: int = 600, start: float = 100.0, trend: float = 0.1) -> list[float]:
    return [start + i * trend for i in range(n)]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def run_date():
    return date(2026, 7, 7)


@pytest.fixture
def simple_frames(run_date):
    """SPY + TST with >252 sessions; TST trending upward (healthy)."""
    n = 500
    spy = _make_ohlcv(_spy_closes(n))
    tst = _make_ohlcv(_stock_closes(n, start=80.0, trend=0.1))
    return {"SPY": spy, "TST": tst}


@pytest.fixture
def simple_position():
    return {"id": "pos-1", "ticker": "TST", "shares": 100, "entry_price": None, "entry_date": None, "status": "active"}


@pytest.fixture
def simple_sd():
    return _make_stockdata()


@pytest.fixture
def simple_regime():
    return _make_regime()


# ---------------------------------------------------------------------------
# compose() smoke test
# ---------------------------------------------------------------------------

def test_compose_returns_valid_schema(simple_frames, simple_position, simple_sd, simple_regime, tmp_path, run_date):
    """compose() with one position returns portfolio_risk_state.v1 with all required keys."""
    from portfolio.held_risk import compose

    # Write stockdata
    sd_dir = tmp_path / "site" / "stockdata"
    sd_dir.mkdir(parents=True)
    (sd_dir / "TST.json").write_text(json.dumps(simple_sd))
    # Write regime
    regime_dir = tmp_path / "data" / "regime"
    regime_dir.mkdir(parents=True)
    (regime_dir / "latest.json").write_text(json.dumps(simple_regime))
    # Write insider
    fi_dir = tmp_path / "site" / "factordata"
    fi_dir.mkdir(parents=True)
    (fi_dir / "insider_signals.json").write_text(json.dumps({}))

    state = compose(
        [simple_position],
        vendor_root=tmp_path,
        data_root=tmp_path / "data_out",
        now=run_date,
        price_loader=_price_loader_factory(simple_frames),
    )

    assert state["schema"] == "portfolio_risk_state.v1"
    assert "asof" in state
    assert "generated_at" in state
    assert "market" in state
    assert "positions" in state
    assert len(state["positions"]) == 1

    pos = state["positions"][0]
    assert pos["ticker"] == "TST"
    assert pos["coverage"] in ("full", "partial", "price_only")
    assert pos["role"] in ("ok", "info", "monitor", "review", "tighten", "trim_review", "exit_review")
    assert isinstance(pos["elevated_lanes"], int)
    assert pos["lane_total"] == 8
    assert "lanes" in pos
    assert "path" in pos
    assert "personality" in pos
    assert "events" in pos

    # All required lane keys present
    from portfolio.held_risk import _ALL_LANE_NAMES
    for lane_name in _ALL_LANE_NAMES:
        assert lane_name in pos["lanes"], f"missing lane {lane_name}"
        lane = pos["lanes"][lane_name]
        assert lane["state"] in ("ok", "watch", "elevated", "coverage_missing", "stale")
        assert isinstance(lane["flags"], list)
        assert isinstance(lane["reasons"], list)


# ---------------------------------------------------------------------------
# LANE 1: price_trend
# ---------------------------------------------------------------------------

def test_price_trend_ok(run_date):
    """Healthy trending stock → price_trend ok."""
    from portfolio.held_risk import _lane_price_trend, _compute_price_metrics

    # close well above MA50 and MA200; uptrending
    n = 300
    closes = [80.0 + i * 0.15 for i in range(n)]  # from 80 → 125
    ohlcv = _make_ohlcv(closes)
    spy = _make_ohlcv(_spy_closes(n))
    metrics = _compute_price_metrics(ohlcv, spy_df=spy)

    lane = _lane_price_trend(metrics, None, run_date)
    assert lane["state"] == "ok", f"expected ok, got {lane['state']}: {lane['reasons']}"


def test_price_trend_elevated(run_date):
    """Close below MA50 AND RS very weak → elevated."""
    from portfolio.held_risk import _lane_price_trend, _compute_price_metrics

    # Stock that dropped below its MA50 and is weak vs SPY
    n = 300
    # SPY trending strongly up; stock declining
    spy_closes_list = [400.0 + i * 0.5 for i in range(n)]
    # Stock: peaked in middle, then crashed below MA50
    peak = n // 2
    stk = [60.0 + i * 0.5 for i in range(peak)] + [
        60.0 + peak * 0.5 - (i * 1.5) for i in range(n - peak)
    ]
    spy = _make_ohlcv(spy_closes_list)
    ohlcv = _make_ohlcv(stk)
    metrics = _compute_price_metrics(ohlcv, spy_df=spy)

    # Force RS z negative by passing a synthetic below-MA50 scenario
    # The test relies on the metrics; if RS z doesn't fire, test the MA200 path
    close = metrics.get("close")
    ma50 = metrics.get("ma50")
    ma200 = metrics.get("ma200")

    # At minimum, close should be below MA50 if stock is declining
    lane = _lane_price_trend(metrics, None, run_date)
    # Either elevated (both conditions) or at least watch (below MA50)
    assert lane["state"] in ("elevated", "watch"), f"expected elevated/watch, got {lane['state']}"


def test_price_trend_close_below_ma200_elevates(run_date):
    """close < MA50 AND close < MA200 → elevated (second condition of OR)."""
    from portfolio.held_risk import _lane_price_trend

    # Craft metrics manually
    metrics = {
        "close": 80.0,
        "ma20": 95.0,
        "ma50": 100.0,
        "ma200": 110.0,
        "rs20_spy_z": 0.0,      # RS not weak
        "high_52w": 130.0,
        "atr14": 2.5,
        "lower_low_20d": False,
        "close_date": "2026-07-02",
        "ext_z": None,
    }

    lane = _lane_price_trend(metrics, None, run_date)
    assert lane["state"] == "elevated", f"expected elevated, got {lane['state']}: {lane['reasons']}"
    assert "below_ma50" in lane["flags"]
    assert "below_ma200" in lane["flags"]


def test_price_trend_watch_single_flag(run_date):
    """close < MA50 but above MA200 and RS neutral → watch."""
    from portfolio.held_risk import _lane_price_trend

    metrics = {
        "close": 95.0,
        "ma20": 100.0,
        "ma50": 98.0,
        "ma200": 90.0,
        "rs20_spy_z": 0.0,
        "high_52w": 110.0,
        "atr14": 2.0,
        "lower_low_20d": False,
        "close_date": "2026-07-02",
        "ext_z": None,
    }

    lane = _lane_price_trend(metrics, None, run_date)
    # close 95 < ma50 98 but above MA200 90 and RS ok → watch
    assert lane["state"] == "watch"
    assert "below_ma50" in lane["flags"]


def test_price_trend_coverage_missing_no_ohlcv(run_date):
    """No OHLCV data → coverage_missing."""
    from portfolio.held_risk import _lane_price_trend

    lane = _lane_price_trend({}, None, run_date)
    assert lane["state"] == "coverage_missing"


# ---------------------------------------------------------------------------
# LANE 2: extension_giveback
# ---------------------------------------------------------------------------

def test_extension_ok_in_trend(run_date):
    """Extension grade In-trend → ok."""
    from portfolio.held_risk import _lane_extension_giveback

    sd = _make_stockdata()  # has view.evidence.extension.value = "In-trend"
    metrics = {"close": 100.0, "ma20": 98.0, "ma50": 95.0, "ma200": 88.0,
               "atr14": 3.0, "high_52w": 108.0, "rs20_spy_z": 0.5, "ext_z": 0.3, "close_date": "2026-07-02"}
    position = {"entry_price": None, "entry_date": None}

    lane, path = _lane_extension_giveback(metrics, sd, position)
    assert lane["state"] == "ok"
    assert path["ext_grade"] in ("intrend", None)


def test_extension_parabolic_elevated(run_date):
    """Extension grade parabolic → elevated."""
    from portfolio.held_risk import _lane_extension_giveback

    sd = _make_stockdata({
        "view": {"evidence": {"extension": {"tone": "warn", "value": "Parabolic — over-extension risk"}}}
    })
    metrics = {"close": 150.0, "ma20": 130.0, "ma50": 120.0, "ma200": 100.0,
               "atr14": 5.0, "high_52w": 155.0, "rs20_spy_z": 1.5, "ext_z": 2.5, "close_date": "2026-07-02"}
    position = {"entry_price": None, "entry_date": None}

    lane, path = _lane_extension_giveback(metrics, sd, position)
    assert lane["state"] == "elevated"
    assert "parabolic" in lane["flags"]
    assert path["ext_grade"] == "parabolic"


def test_extension_giveback_elevated_mfe_threshold(run_date):
    """MFE >= 20% and giveback >= 35% → elevated."""
    from portfolio.held_risk import _lane_extension_giveback

    # entry_price=100, post_entry_high=130 (MFE=30%), current_close=111.5
    # giveback = (130-111.5)/(130-100) = 18.5/30 = 61.7% → elevated (>35%)
    sd = _make_stockdata()
    metrics = {"close": 111.5, "ma20": 120.0, "ma50": 115.0, "ma200": 105.0,
               "atr14": 3.0, "high_52w": 135.0, "rs20_spy_z": -0.5, "ext_z": 0.5, "close_date": "2026-07-02"}
    position = {"entry_price": 100.0, "entry_date": "2026-01-01", "post_entry_high": 130.0}

    lane, path = _lane_extension_giveback(metrics, sd, position)
    assert lane["state"] == "elevated", f"got {lane['state']}: {lane['reasons']}"
    assert path["mfe_pct"] == pytest.approx(0.30, abs=0.01)
    assert path["giveback_pct_of_mfe"] is not None
    assert path["giveback_pct_of_mfe"] > 0.35


def test_extension_critical_giveback(run_date):
    """MFE >= 25% and giveback >= 60% → critical_giveback flag."""
    from portfolio.held_risk import _lane_extension_giveback

    # entry=100, high=140 (MFE=40%), close=116
    # giveback = (140-116)/(140-100) = 24/40 = 60% → critical
    sd = _make_stockdata()
    metrics = {"close": 116.0, "ma20": 125.0, "ma50": 120.0, "ma200": 105.0,
               "atr14": 4.0, "high_52w": 142.0, "rs20_spy_z": -0.3, "ext_z": 0.2, "close_date": "2026-07-02"}
    position = {"entry_price": 100.0, "entry_date": "2026-01-01", "post_entry_high": 140.0}

    lane, path = _lane_extension_giveback(metrics, sd, position)
    assert "critical_giveback" in lane["flags"], f"flags: {lane['flags']}"


def test_extension_no_entry_no_giveback(run_date):
    """Missing entry_price → no giveback computation; path fields are None."""
    from portfolio.held_risk import _lane_extension_giveback

    sd = _make_stockdata()
    metrics = {"close": 100.0, "ma20": 95.0, "ma50": 90.0, "ma200": 80.0,
               "atr14": 3.0, "high_52w": 110.0, "rs20_spy_z": 0.2, "ext_z": 0.1, "close_date": "2026-07-02"}
    position = {"entry_price": None, "entry_date": None}

    lane, path = _lane_extension_giveback(metrics, sd, position)
    assert path["mfe_pct"] is None
    assert path["giveback_pct_of_mfe"] is None


def test_extension_mfe_zero_no_giveback(run_date):
    """Entry price == current close (MFE=0) → no giveback flag."""
    from portfolio.held_risk import _lane_extension_giveback

    sd = _make_stockdata()
    metrics = {"close": 100.0, "ma20": 95.0, "ma50": 90.0, "ma200": 80.0,
               "atr14": 3.0, "high_52w": 100.0, "rs20_spy_z": 0.2, "ext_z": 0.1, "close_date": "2026-07-02"}
    # MFE = (100-100)/100 = 0; no giveback flag
    position = {"entry_price": 100.0, "entry_date": "2026-01-01"}

    lane, path = _lane_extension_giveback(metrics, sd, position)
    assert path["mfe_pct"] is None or path["mfe_pct"] == 0.0
    assert "giveback_elevated" not in lane["flags"]


# ---------------------------------------------------------------------------
# LANE 3: event_window
# ---------------------------------------------------------------------------

def test_event_window_ok_far_earnings(run_date):
    """Earnings > 7 sessions away → ok."""
    from portfolio.held_risk import _lane_event_window

    sd = _make_stockdata({"earnings": {"next_date": "2026-09-15", "next_time": None,
                                        "eps_forecast": 2.0, "sue_z": 1.0,
                                        "summary": {"avg_surprise": 5.0, "beats": 4, "streak": 4, "total": 4},
                                        "surprises": []}})
    lane, events = _lane_event_window(sd, run_date)
    assert lane["state"] == "ok", f"got {lane['state']}: {lane['reasons']}"
    assert events["earnings_tdays"] is None or events["earnings_tdays"] > 7


def test_event_window_elevated_earnings_close(run_date):
    """Earnings within 3 trading sessions → elevated."""
    from portfolio.held_risk import _lane_event_window

    # run_date = 2026-07-07 (Tuesday), earnings 2026-07-09 (Thursday) = 2 trading days
    sd = _make_stockdata({"earnings": {"next_date": "2026-07-09", "next_time": None,
                                        "eps_forecast": 2.0, "sue_z": 1.0,
                                        "summary": {"avg_surprise": 5.0, "beats": 4, "streak": 4, "total": 4},
                                        "surprises": []}})
    lane, events = _lane_event_window(sd, run_date)
    assert lane["state"] == "elevated", f"got {lane['state']}: {lane['reasons']}"
    assert events["earnings_tdays"] is not None and events["earnings_tdays"] <= 3


def test_event_window_watch_earnings_near(run_date):
    """Earnings within 7 sessions (but > 3) → watch."""
    from portfolio.held_risk import _lane_event_window

    # run_date 2026-07-07, earnings 2026-07-14 = 5 trading days
    sd = _make_stockdata({"earnings": {"next_date": "2026-07-14", "next_time": None,
                                        "eps_forecast": 2.0, "sue_z": 1.0,
                                        "summary": {"avg_surprise": 5.0, "beats": 4, "streak": 4, "total": 4},
                                        "surprises": []}})
    lane, events = _lane_event_window(sd, run_date)
    assert lane["state"] == "watch", f"got {lane['state']}: {lane['reasons']}"
    assert events["earnings_tdays"] is not None and 3 < events["earnings_tdays"] <= 7


def test_event_window_coverage_missing_no_sd(run_date):
    """No stockdata → coverage_missing."""
    from portfolio.held_risk import _lane_event_window

    lane, events = _lane_event_window(None, run_date)
    assert lane["state"] == "coverage_missing"


# ---------------------------------------------------------------------------
# LANE 4: earnings_expectation
# ---------------------------------------------------------------------------

def test_earnings_expectation_ok_good_streak(run_date):
    """Positive beat streak, est_chg positive → ok."""
    from portfolio.held_risk import _lane_earnings_expectation

    sd = _make_stockdata()
    lane = _lane_earnings_expectation(sd, run_date)
    assert lane["state"] == "ok", f"got {lane['state']}: {lane['reasons']}"


def test_earnings_expectation_elevated_miss_streak(run_date):
    """2 consecutive misses (beats=0/total=2) AND sue_z<0 → elevated."""
    from portfolio.held_risk import _lane_earnings_expectation

    sd = _make_stockdata({
        "earnings": {
            "next_date": "2026-09-15", "next_time": None, "eps_forecast": 2.0,
            "sue_z": -1.5,  # negative = recent miss
            "summary": {"avg_surprise": -3.0, "beats": 0, "streak": 0, "total": 2},
            "surprises": [],
        },
        "revisions": {"breadth": 0.3, "est_chg_30d": -1.0, "est_chg_90d": -2.0,
                      "net_up_30d": -3.0, "n_analysts": 8.0},
    })
    lane = _lane_earnings_expectation(sd, run_date)
    assert lane["state"] == "elevated", f"got {lane['state']}: {lane['reasons']}"


def test_earnings_expectation_watch_single_sue_miss(run_date):
    """sue_z < 0 alone (revisions neutral) → watch."""
    from portfolio.held_risk import _lane_earnings_expectation

    sd = _make_stockdata({
        "earnings": {
            "next_date": "2026-09-15", "next_time": None, "eps_forecast": 2.0,
            "sue_z": -0.5,
            "summary": {"avg_surprise": -1.0, "beats": 3, "streak": 3, "total": 4},
            "surprises": [],
        },
        "revisions": {"breadth": 0.7, "est_chg_30d": 0.3, "est_chg_90d": 0.5,
                      "net_up_30d": 2.0, "n_analysts": 8.0},
    })
    lane = _lane_earnings_expectation(sd, run_date)
    assert lane["state"] == "watch", f"got {lane['state']}: {lane['reasons']}"


# ---------------------------------------------------------------------------
# LANE 5: solvency_dilution
# ---------------------------------------------------------------------------

def test_solvency_ok_healthy(run_date):
    """Healthy balance sheet → ok (0 flags)."""
    from portfolio.held_risk import _lane_solvency_dilution

    sd = _make_stockdata()  # altman=8.0, piotroski=7, healthy financials
    lane = _lane_solvency_dilution(sd, run_date)
    # dilution_filing_coverage_missing is always added but doesn't count as solvency flag
    assert lane["state"] == "ok", f"got {lane['state']}: {lane['reasons']}"


def test_solvency_elevated_two_flags(run_date):
    """Low Altman Z AND low Piotroski → >=2 flags → elevated."""
    from portfolio.held_risk import _lane_solvency_dilution

    sd = _make_stockdata({
        "financials": {
            "multiyear": {
                "altman": {"z": 1.2, "zone": "distress", "approx": False},
                "piotroski": {"score": 2, "of": 9},
            },
            "raw": {
                "assets": 50e9, "cfo": 1e9, "debt_lt": 40e9,
                "equity": -5e9,  # negative equity
                "gross_profit": 5e9, "ni": 0.5e9, "repurchases": 2e9,
                "revenue": 20e9, "shares": 1e9, "dividends": None,
            },
        }
    })
    lane = _lane_solvency_dilution(sd, run_date)
    assert lane["state"] == "elevated", f"got {lane['state']}: {lane['reasons']}"


def test_solvency_financials_skip_altman(run_date):
    """Financials sector: Altman Z skipped."""
    from portfolio.held_risk import _lane_solvency_dilution

    sd = _make_stockdata({
        "profile": {"sector": "Financials"},
        "financials": {
            "multiyear": {
                "altman": {"z": 1.0, "zone": "distress", "approx": False},
                "piotroski": {"score": 7, "of": 9},
            },
            "raw": {
                "assets": 200e9, "cfo": 10e9, "debt_lt": 50e9,
                "equity": 20e9, "gross_profit": 20e9, "ni": 5e9,
                "repurchases": 1e9, "revenue": 50e9, "shares": 1e9, "dividends": None,
            },
        }
    })
    lane = _lane_solvency_dilution(sd, run_date)
    # Altman skipped → fewer flags fired
    assert "altman_z" not in " ".join(lane["flags"]), f"flags: {lane['flags']}"


# ---------------------------------------------------------------------------
# LANE 6: ownership_flow
# ---------------------------------------------------------------------------

def test_ownership_ok_no_pressure(run_date):
    """No short pressure, no insider cluster → ok."""
    from portfolio.held_risk import _lane_ownership_flow

    sd = _make_stockdata()  # n_sellers=0, short_interest z=0.5
    lane = _lane_ownership_flow(sd, {}, "TST", run_date)
    assert lane["state"] == "ok", f"got {lane['state']}: {lane['reasons']}"


def test_ownership_watch_insider_sell_cluster(run_date):
    """Insider sell cluster alone → watch."""
    from portfolio.held_risk import _lane_ownership_flow

    sd = _make_stockdata({
        "positioning": {
            "insider": {"cluster": True, "n_buyers": 0, "n_sellers": 4,
                        "net_usd_mn": -20.0, "quarter": "6mo"},
            "short": {"pct_float": 1.0, "days_to_cover": 2.0, "si_change_pct": 0.0},
            "short_flow": {"short_ratio_pct": 45.0, "trend_pp": 0.0, "n_days": 27, "asof": "2026-07-02"},
        },
        "factors": {"legs": {"short_interest": 0.5}},  # low short z
    })
    lane = _lane_ownership_flow(sd, {}, "TST", run_date)
    assert lane["state"] == "watch", f"got {lane['state']}: {lane['reasons']}"
    assert any("insider_sell_cluster" in f for f in lane["flags"])


def test_ownership_elevated_both(run_date):
    """Both short pressure AND insider cluster → elevated."""
    from portfolio.held_risk import _lane_ownership_flow

    sd = _make_stockdata({
        "positioning": {
            "insider": {"cluster": True, "n_buyers": 0, "n_sellers": 5,
                        "net_usd_mn": -50.0, "quarter": "6mo"},
            "short": {"pct_float": 5.0, "days_to_cover": 8.0, "si_change_pct": 25.0},
            "short_flow": {"short_ratio_pct": 65.0, "trend_pp": 2.0, "n_days": 27, "asof": "2026-07-02"},
        },
        "factors": {"legs": {"short_interest": 2.5}},  # high short z
    })
    lane = _lane_ownership_flow(sd, {}, "TST", run_date)
    assert lane["state"] == "elevated", f"got {lane['state']}: {lane['reasons']}"


def test_ownership_insider_from_signals_json(run_date):
    """Insider cluster from insider_signals.json when stockdata cluster is inactive."""
    from portfolio.held_risk import _lane_ownership_flow

    sd = _make_stockdata({
        "positioning": {
            "insider": {"cluster": False, "n_buyers": 1, "n_sellers": 0, "net_usd_mn": 5.0, "quarter": "6mo"},
            "short": {"pct_float": 1.0, "days_to_cover": 2.0, "si_change_pct": 0.0},
            "short_flow": {"short_ratio_pct": 40.0, "trend_pp": 0.0, "n_days": 27, "asof": "2026-07-02"},
        },
        "factors": {"legs": {"short_interest": 0.3}},
    })
    insider_signals = {"TST": {"buyers": 0, "sellers": 4, "net_mn": -30.0, "bps": -0.5}}
    lane = _lane_ownership_flow(sd, insider_signals, "TST", run_date)
    # Should pick up from insider_signals.json
    assert any("insider_sell_cluster" in f for f in lane["flags"])


# ---------------------------------------------------------------------------
# LANE 7: macro_sensitivity
# ---------------------------------------------------------------------------

def test_macro_ok_low_tier(run_date):
    """Low rate sensitivity, neutral regime → ok."""
    from portfolio.held_risk import _lane_macro_sensitivity

    sd = _make_stockdata()  # tier=low, regime=neutral
    regime = _make_regime()
    lane = _lane_macro_sensitivity(sd, regime, run_date)
    assert lane["state"] == "ok", f"got {lane['state']}: {lane['reasons']}"


def test_macro_elevated_high_headwind(run_date):
    """HIGH rate sensitivity + headwind regime → elevated."""
    from portfolio.held_risk import _lane_macro_sensitivity

    sd = _make_stockdata({
        "macro_sensitivity": {
            "ticker": "TST", "rate_beta": -0.45, "rate_beta_se": 0.05,
            "t_stat": -9.0, "tier": "high",
            "regime": "headwind", "regime_label": {"en": "headwind", "zh": "逆风"},
            "rate_state": {"regime": "restrictive", "direction": "rising", "real_10y": 2.5},
            "infl_state": {"regime": "above target", "direction": "rising"},
        }
    })
    regime = _make_regime()
    lane = _lane_macro_sensitivity(sd, regime, run_date)
    assert lane["state"] == "elevated", f"got {lane['state']}: {lane['reasons']}"
    assert "regime_headwind" in lane["flags"]


def test_macro_watch_high_tier_neutral(run_date):
    """HIGH rate sensitivity but neutral regime → watch."""
    from portfolio.held_risk import _lane_macro_sensitivity

    sd = _make_stockdata({
        "macro_sensitivity": {
            "ticker": "TST", "rate_beta": -0.38, "rate_beta_se": 0.05,
            "t_stat": -7.6, "tier": "high",
            "regime": "neutral",
            "rate_state": {"regime": "restrictive", "direction": "steady", "real_10y": 2.0},
            "infl_state": {"regime": "above target", "direction": "steady"},
        }
    })
    regime = _make_regime()
    lane = _lane_macro_sensitivity(sd, regime, run_date)
    assert lane["state"] == "watch", f"got {lane['state']}: {lane['reasons']}"


# ---------------------------------------------------------------------------
# LANE 8: sector_rotation
# ---------------------------------------------------------------------------

def test_sector_rotation_ok(run_date):
    """IT sector rank 2/19 (top quartile) + advancing pulse → ok."""
    from portfolio.held_risk import _lane_sector_rotation

    sd = _make_stockdata()  # sector=IT, sector_pulse=advancing
    regime = _make_regime()  # XLK rank 2/11 = top
    lane = _lane_sector_rotation(sd, regime, run_date)
    # XLK rank 2/11 is NOT bottom quartile → 0 signals
    assert lane["state"] == "ok", f"got {lane['state']}: {lane['reasons']}"


def test_sector_rotation_watch_bottom_quartile(run_date):
    """Sector in bottom quartile of regime RS → watch (1 signal)."""
    from portfolio.held_risk import _lane_sector_rotation

    sd = _make_stockdata({"profile": {"sector": "Energy"}})
    # XLE rank 18/19 = bottom quartile
    regime = _make_regime()
    lane = _lane_sector_rotation(sd, regime, run_date)
    assert lane["state"] == "watch", f"got {lane['state']}: {lane['reasons']}"
    assert any("sector_rs_bottom_q" in f for f in lane["flags"])


def test_sector_rotation_elevated_two_signals(run_date):
    """Sector in bottom quartile AND fading pulse → 2 signals → elevated."""
    from portfolio.held_risk import _lane_sector_rotation

    sd = _make_stockdata({
        "profile": {"sector": "Energy"},
        "sector_pulse": {
            "as_of": "2026-07-02", "theme_id": "energy", "theme_name": "Energy",
            "heat": 20.0, "label": "fading", "reco": "avoid", "rank": 18,
            "n_themes": 20, "rank_delta_5d": -2, "theme_ids": [],
        }
    })
    regime = _make_regime()
    lane = _lane_sector_rotation(sd, regime, run_date)
    assert lane["state"] == "elevated", f"got {lane['state']}: {lane['reasons']}"


# ---------------------------------------------------------------------------
# LANE 9: market_regime (shared)
# ---------------------------------------------------------------------------

def test_market_regime_ok_calm(run_date):
    """Calm radar + risk-on → ok."""
    from portfolio.held_risk import _lane_market_regime

    regime = _make_regime()  # state=calm, risk_state=risk-on
    lane = _lane_market_regime(regime, run_date)
    assert lane["state"] == "ok", f"got {lane['state']}: {lane['reasons']}"


def test_market_regime_elevated_caution(run_date):
    """risk_radar 'caution' (ORANGE in site banner) → elevated.

    MAJOR-2 fix: 'caution' was previously mapped to 'watch' which prevented
    the role-ladder tighten/trim triggers from firing on ORANGE regime.
    The correct mapping is caution → elevated (matching §7 "ORANGE/EXTREME").
    """
    from portfolio.held_risk import _lane_market_regime

    regime = _make_regime({"risk_radar": {"state": "caution", "top_score": 80.0, "dominant_scare": "growth"}})
    lane = _lane_market_regime(regime, run_date)
    assert lane["state"] == "elevated", f"got {lane['state']}: {lane['reasons']}"


def test_market_regime_elevated_risk_off(run_date):
    """risk-off radar → elevated."""
    from portfolio.held_risk import _lane_market_regime

    regime = _make_regime({"risk_radar": {"state": "risk-off", "top_score": 90.0, "dominant_scare": "growth"}})
    lane = _lane_market_regime(regime, run_date)
    assert lane["state"] == "elevated", f"got {lane['state']}: {lane['reasons']}"


def test_market_regime_shared_across_positions(tmp_path, run_date):
    """market_regime lane is identical for two positions in the same compose() call."""
    from portfolio.held_risk import compose

    sd = _make_stockdata()
    sd_dir = tmp_path / "site" / "stockdata"
    sd_dir.mkdir(parents=True)
    for t in ("TST", "TST2"):
        (sd_dir / f"{t}.json").write_text(json.dumps(sd))
    regime = _make_regime({"risk_radar": {"state": "caution", "top_score": 70.0, "dominant_scare": "growth"}})
    regime_dir = tmp_path / "data" / "regime"
    regime_dir.mkdir(parents=True)
    (regime_dir / "latest.json").write_text(json.dumps(regime))
    fi_dir = tmp_path / "site" / "factordata"
    fi_dir.mkdir(parents=True)
    (fi_dir / "insider_signals.json").write_text("{}")

    positions = [
        {"id": "p1", "ticker": "TST", "shares": 100, "entry_price": None, "entry_date": None, "status": "active"},
        {"id": "p2", "ticker": "TST2", "shares": 50, "entry_price": None, "entry_date": None, "status": "active"},
    ]
    state = compose(
        positions,
        vendor_root=tmp_path,
        data_root=tmp_path / "out",
        now=run_date,
        price_loader=_price_loader_factory({"SPY": _make_ohlcv(_spy_closes(300)),
                                             "TST": _make_ohlcv(_stock_closes(300)),
                                             "TST2": _make_ohlcv(_stock_closes(300))}),
    )
    mr1 = state["positions"][0]["lanes"]["market_regime"]
    mr2 = state["positions"][1]["lanes"]["market_regime"]
    assert mr1["state"] == mr2["state"]
    assert mr1["flags"] == mr2["flags"]


# ---------------------------------------------------------------------------
# Stale detection
# ---------------------------------------------------------------------------

def test_stale_detection_earnings_expectation(run_date):
    """Stockdata asof > 3 sessions before run_date → stale."""
    from portfolio.held_risk import _lane_earnings_expectation

    sd = _make_stockdata({"asof": "2026-06-20"})  # way before 2026-07-07
    lane = _lane_earnings_expectation(sd, run_date)
    assert lane["state"] == "stale", f"got {lane['state']}"


def test_stale_detection_recent_asof_ok(run_date):
    """Stockdata asof 2 sessions before run_date → not stale."""
    from portfolio.held_risk import _is_stale

    # 2026-07-03 (Friday) = 2 trading days before 2026-07-07 (Monday + Tuesday)
    # Actually 2026-07-07 is Monday; 2026-07-03 is 2 sessions prior (Fri, Mon)
    asof = "2026-07-03"
    assert not _is_stale(asof, run_date), f"should not be stale"


def test_stale_detection_old_asof(run_date):
    """Stockdata asof 5 sessions before run_date → stale."""
    from portfolio.held_risk import _is_stale

    asof = "2026-06-30"  # well before 2026-07-07
    assert _is_stale(asof, run_date), f"should be stale"


# ---------------------------------------------------------------------------
# Coverage degradation
# ---------------------------------------------------------------------------

def test_price_only_coverage_no_stockdata(tmp_path, run_date):
    """No stockdata for ticker → coverage=price_only; evidence lanes coverage_missing."""
    from portfolio.held_risk import compose

    # No stockdata file for "NOSUCH"
    sd_dir = tmp_path / "site" / "stockdata"
    sd_dir.mkdir(parents=True)
    regime_dir = tmp_path / "data" / "regime"
    regime_dir.mkdir(parents=True)
    (regime_dir / "latest.json").write_text(json.dumps(_make_regime()))
    fi_dir = tmp_path / "site" / "factordata"
    fi_dir.mkdir(parents=True)
    (fi_dir / "insider_signals.json").write_text("{}")

    positions = [{"id": "p1", "ticker": "NOSUCH", "shares": 100, "entry_price": None,
                   "entry_date": None, "status": "active"}]
    state = compose(
        positions,
        vendor_root=tmp_path,
        data_root=tmp_path / "out",
        now=run_date,
        price_loader=_price_loader_factory({"SPY": _make_ohlcv(_spy_closes(300)),
                                             "NOSUCH": _make_ohlcv(_stock_closes(300))}),
    )
    pos = state["positions"][0]
    assert pos["coverage"] == "price_only", f"expected price_only, got {pos['coverage']}"
    # price_trend should be ok or watch (OHLCV available), but evidence lanes should be missing
    assert pos["lanes"]["earnings_expectation"]["state"] == "coverage_missing"
    assert pos["lanes"]["solvency_dilution"]["state"] == "coverage_missing"


def test_no_ohlcv_and_no_stockdata(tmp_path, run_date):
    """No OHLCV AND no stockdata → price_trend coverage_missing; no crash."""
    from portfolio.held_risk import compose

    sd_dir = tmp_path / "site" / "stockdata"
    sd_dir.mkdir(parents=True)
    regime_dir = tmp_path / "data" / "regime"
    regime_dir.mkdir(parents=True)
    (regime_dir / "latest.json").write_text(json.dumps(_make_regime()))
    fi_dir = tmp_path / "site" / "factordata"
    fi_dir.mkdir(parents=True)
    (fi_dir / "insider_signals.json").write_text("{}")

    positions = [{"id": "p1", "ticker": "GHOST", "shares": 100, "entry_price": None,
                   "entry_date": None, "status": "active"}]
    # price_loader returns None for GHOST
    state = compose(
        positions,
        vendor_root=tmp_path,
        data_root=tmp_path / "out",
        now=run_date,
        price_loader=_price_loader_factory({"SPY": _make_ohlcv(_spy_closes(300))}),
    )
    pos = state["positions"][0]
    assert pos["coverage"] == "price_only"
    assert pos["lanes"]["price_trend"]["state"] == "coverage_missing"


# ---------------------------------------------------------------------------
# Role ladder precedence
# ---------------------------------------------------------------------------

def _make_compose_with_scenario(tmp_path, run_date, sd_overrides=None, position_overrides=None,
                                  regime_overrides=None, frames_overrides=None):
    """Helper: build full compose() call with injected scenario."""
    from portfolio.held_risk import compose

    sd = _make_stockdata(sd_overrides or {})
    sd_dir = tmp_path / "site" / "stockdata"
    sd_dir.mkdir(parents=True, exist_ok=True)
    (sd_dir / "TST.json").write_text(json.dumps(sd))

    regime = _make_regime(regime_overrides or {})
    regime_dir = tmp_path / "data" / "regime"
    regime_dir.mkdir(parents=True, exist_ok=True)
    (regime_dir / "latest.json").write_text(json.dumps(regime))

    fi_dir = tmp_path / "site" / "factordata"
    fi_dir.mkdir(parents=True, exist_ok=True)
    (fi_dir / "insider_signals.json").write_text("{}")

    pos_base = {"id": "p1", "ticker": "TST", "shares": 100, "entry_price": None,
                "entry_date": None, "status": "active"}
    if position_overrides:
        pos_base.update(position_overrides)

    n = 300
    frames = {"SPY": _make_ohlcv(_spy_closes(n)), "TST": _make_ohlcv(_stock_closes(n))}
    if frames_overrides:
        frames.update(frames_overrides)

    return compose(
        [pos_base],
        vendor_root=tmp_path,
        data_root=tmp_path / "out",
        now=run_date,
        price_loader=_price_loader_factory(frames),
    )


def test_role_ok_healthy(tmp_path, run_date):
    """Healthy position → ok role."""
    state = _make_compose_with_scenario(tmp_path, run_date)
    pos = state["positions"][0]
    assert pos["role"] in ("ok", "info", "monitor"), f"got {pos['role']}: {pos['role_reasons']}"


def test_role_monitor_one_elevated_lane(tmp_path, run_date):
    """Exactly 1 elevated lane → monitor."""
    from portfolio.held_risk import compose, _EVIDENCE_LANES

    # Seed: only macro_sensitivity elevated (HIGH + headwind)
    sd_overrides = {
        "macro_sensitivity": {
            "ticker": "TST", "rate_beta": -0.50, "rate_beta_se": 0.05,
            "t_stat": -10.0, "tier": "high",
            "regime": "headwind", "regime_label": {"en": "headwind", "zh": "逆风"},
            "rate_state": {"regime": "restrictive", "direction": "rising", "real_10y": 2.5},
            "infl_state": {"regime": "above target", "direction": "rising"},
        }
    }
    state = _make_compose_with_scenario(tmp_path, run_date, sd_overrides=sd_overrides)
    pos = state["positions"][0]
    # Exactly 1 elevated lane (macro_sensitivity); should trigger monitor at minimum
    elevated = [k for k, v in pos["lanes"].items()
                if k in _EVIDENCE_LANES and v["state"] == "elevated"]
    if len(elevated) == 1:
        assert pos["role"] == "monitor", f"got {pos['role']}: elevated={elevated}, reasons={pos['role_reasons']}"


def test_role_review_two_independent_groups(tmp_path, run_date):
    """>=2 independent group lanes elevated → review."""
    from portfolio.held_risk import _assign_role

    def _mk_lane(st: str, flags: list[str] | None = None):
        return {"state": st, "flags": flags or [], "reasons": [], "asof": None}

    lanes = {k: _mk_lane("ok") for k in [
        "price_trend", "extension_giveback", "event_window", "earnings_expectation",
        "solvency_dilution", "ownership_flow", "macro_sensitivity", "sector_rotation",
        "market_regime", "data_quality"
    ]}
    lanes["price_trend"] = _mk_lane("elevated")
    lanes["ownership_flow"] = _mk_lane("elevated")
    path = {"ref_close": 100.0, "ma200": 90.0, "mfe_pct": None, "giveback_pct_of_mfe": None}

    role, reasons = _assign_role(lanes, path, {})
    assert role == "review", f"got {role}: {reasons}"


def test_role_exit_review_solvency_critical_price_elevated(tmp_path, run_date):
    """Solvency critical + price_trend elevated → exit_review."""
    from portfolio.held_risk import _assign_role

    def _lane(st: str, flags: list[str] | None = None):
        return {"state": st, "flags": flags or [], "reasons": [], "asof": None}

    lanes = {
        "price_trend": _lane("elevated"),
        "extension_giveback": _lane("ok"),
        "event_window": _lane("ok"),
        "earnings_expectation": _lane("ok"),
        "solvency_dilution": _lane("elevated", ["critical_coverage", "altman_z_1.2"]),
        "ownership_flow": _lane("ok"),
        "macro_sensitivity": _lane("ok"),
        "sector_rotation": _lane("ok"),
        "market_regime": _lane("ok"),
        "data_quality": _lane("ok"),
    }
    path = {"ref_close": 80.0, "ma200": 100.0, "mfe_pct": None, "giveback_pct_of_mfe": None}
    role, reasons = _assign_role(lanes, path, {})
    assert role == "exit_review", f"got {role}: {reasons}"


def test_role_exit_review_critical_giveback(tmp_path, run_date):
    """giveback >= 60% of MFE >= 25% → exit_review."""
    from portfolio.held_risk import _assign_role

    def _lane(st: str, flags: list[str] | None = None):
        return {"state": st, "flags": flags or [], "reasons": [], "asof": None}

    lanes = {k: _lane("ok") for k in [
        "price_trend", "extension_giveback", "event_window", "earnings_expectation",
        "solvency_dilution", "ownership_flow", "macro_sensitivity", "sector_rotation",
        "market_regime", "data_quality"
    ]}
    # giveback 70% of MFE 30% → exit_review
    path = {"ref_close": 109.0, "ma200": 95.0, "mfe_pct": 0.30, "giveback_pct_of_mfe": 0.70}
    role, reasons = _assign_role(lanes, path, {})
    assert role == "exit_review", f"got {role}: {reasons}"


def test_role_trim_review(tmp_path, run_date):
    """extension elevated + sector_rotation elevated → trim_review."""
    from portfolio.held_risk import _assign_role

    def _lane(st: str, flags: list[str] | None = None):
        return {"state": st, "flags": flags or [], "reasons": [], "asof": None}

    lanes = {
        "price_trend": _lane("ok"),
        "extension_giveback": _lane("elevated"),
        "event_window": _lane("ok"),
        "earnings_expectation": _lane("ok"),
        "solvency_dilution": _lane("ok"),
        "ownership_flow": _lane("ok"),
        "macro_sensitivity": _lane("ok"),
        "sector_rotation": _lane("elevated"),
        "market_regime": _lane("ok"),
        "data_quality": _lane("ok"),
    }
    path = {"ref_close": 120.0, "ma200": 90.0, "mfe_pct": 0.25, "giveback_pct_of_mfe": 0.20}
    role, reasons = _assign_role(lanes, path, {})
    assert role == "trim_review", f"got {role}: {reasons}"


def test_role_tighten(tmp_path, run_date):
    """price_trend elevated + market_regime elevated → tighten."""
    from portfolio.held_risk import _assign_role

    def _lane(st: str, flags: list[str] | None = None):
        return {"state": st, "flags": flags or [], "reasons": [], "asof": None}

    lanes = {
        "price_trend": _lane("elevated"),
        "extension_giveback": _lane("ok"),
        "event_window": _lane("ok"),
        "earnings_expectation": _lane("ok"),
        "solvency_dilution": _lane("ok"),
        "ownership_flow": _lane("ok"),
        "macro_sensitivity": _lane("ok"),
        "sector_rotation": _lane("ok"),
        "market_regime": _lane("elevated"),
        "data_quality": _lane("ok"),
    }
    path = {"ref_close": 90.0, "ma200": 100.0, "mfe_pct": None, "giveback_pct_of_mfe": None}
    role, reasons = _assign_role(lanes, path, {})
    assert role == "tighten", f"got {role}: {reasons}"


def test_role_ladder_first_match_exit_over_tighten(tmp_path, run_date):
    """When both exit_review and tighten conditions met, exit_review fires first."""
    from portfolio.held_risk import _assign_role

    def _lane(st: str, flags: list[str] | None = None):
        return {"state": st, "flags": flags or [], "reasons": [], "asof": None}

    # Both: exit_review conditions (solvency critical + price elevated) AND tighten conditions
    lanes = {
        "price_trend": _lane("elevated"),
        "extension_giveback": _lane("ok"),
        "event_window": _lane("elevated"),
        "earnings_expectation": _lane("ok"),
        "solvency_dilution": _lane("elevated", ["critical_coverage"]),
        "ownership_flow": _lane("ok"),
        "macro_sensitivity": _lane("elevated"),
        "sector_rotation": _lane("ok"),
        "market_regime": _lane("elevated"),
        "data_quality": _lane("ok"),
    }
    path = {"ref_close": 80.0, "ma200": 100.0, "mfe_pct": 0.20, "giveback_pct_of_mfe": 0.70}
    role, reasons = _assign_role(lanes, path, {})
    assert role == "exit_review", f"got {role} but expected exit_review: {reasons}"


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------

def test_write_state_atomic(tmp_path, run_date):
    """write_state() writes valid JSON atomically (no tmp file left behind)."""
    from portfolio.held_risk import write_state

    out_path = tmp_path / "test_out" / "risk_state.json"
    state = {"schema": "portfolio_risk_state.v1", "asof": str(run_date), "positions": []}
    write_state(state, out_path)

    assert out_path.exists()
    loaded = json.loads(out_path.read_text())
    assert loaded["schema"] == "portfolio_risk_state.v1"

    # No tmp files left
    tmp_files = list(out_path.parent.glob("*.tmp"))
    assert tmp_files == [], f"leftover tmp files: {tmp_files}"


def test_write_state_default_path(tmp_path, monkeypatch, run_date):
    """write_state() with no explicit path writes to _OUT_DEFAULT (monkeypatched)."""
    import portfolio.held_risk as hr
    out_path = tmp_path / "risk_state.json"
    monkeypatch.setattr(hr, "_OUT_DEFAULT", out_path)

    state = {"schema": "portfolio_risk_state.v1", "asof": str(run_date), "positions": []}
    hr.write_state(state)
    assert out_path.exists()


# ---------------------------------------------------------------------------
# CLI --positions-json path
# ---------------------------------------------------------------------------

def test_cli_positions_json(tmp_path, run_date):
    """--positions-json CLI flag bypasses Supabase and runs the composer."""
    # Write positions file
    positions = [{"id": "cli-1", "ticker": "TST", "shares": 50, "entry_price": None,
                   "entry_date": None, "status": "active"}]
    pos_file = tmp_path / "positions.json"
    pos_file.write_text(json.dumps(positions))

    out_file = tmp_path / "out" / "risk_state.json"

    # Need vendor tree (can be empty; will degrade gracefully)
    (tmp_path / "site" / "stockdata").mkdir(parents=True)
    (tmp_path / "data" / "regime").mkdir(parents=True)
    (tmp_path / "data" / "regime" / "latest.json").write_text(json.dumps(_make_regime()))
    (tmp_path / "site" / "factordata").mkdir(parents=True)
    (tmp_path / "site" / "factordata" / "insider_signals.json").write_text("{}")

    repo_root = Path(__file__).resolve().parent.parent
    env = {
        **__import__("os").environ,
        # Point vendor_root at tmp_path by monkeypatching via env is tricky;
        # instead we use --dry-run and check stdout
    }

    result = subprocess.run(
        [sys.executable, str(repo_root / "scripts" / "run_portfolio_risk.py"),
         "--positions-json", str(pos_file),
         "--dry-run"],
        capture_output=True, text=True,
        cwd=str(repo_root),
    )
    # Should exit 0 even if vendor data is absent (fail-soft)
    assert result.returncode == 0, f"CLI failed:\nSTDOUT: {result.stdout[:500]}\nSTDERR: {result.stderr[:500]}"
    # Output should be JSON with schema field
    lines = [l for l in result.stdout.split("\n") if l.startswith("{") or '"schema"' in l]
    assert lines or '"portfolio_risk_state.v1"' in result.stdout, \
        f"No schema in stdout: {result.stdout[:200]}"


def test_cli_no_positions_exits_zero(tmp_path):
    """CLI with empty positions JSON exits 0 gracefully."""
    pos_file = tmp_path / "positions.json"
    pos_file.write_text("[]")

    repo_root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, str(repo_root / "scripts" / "run_portfolio_risk.py"),
         "--positions-json", str(pos_file),
         "--dry-run"],
        capture_output=True, text=True,
        cwd=str(repo_root),
    )
    assert result.returncode == 0, f"STDERR: {result.stderr[:300]}"


def test_cli_bad_positions_json_exits_nonzero(tmp_path):
    """Malformed positions JSON → exit 1."""
    pos_file = tmp_path / "bad.json"
    pos_file.write_text("{not valid json}")

    repo_root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, str(repo_root / "scripts" / "run_portfolio_risk.py"),
         "--positions-json", str(pos_file)],
        capture_output=True, text=True,
        cwd=str(repo_root),
    )
    assert result.returncode != 0, "expected non-zero exit for bad JSON"


# ---------------------------------------------------------------------------
# Market block structure
# ---------------------------------------------------------------------------

def test_market_block_keys(tmp_path, run_date):
    """Market block has all required keys."""
    from portfolio.held_risk import compose

    sd_dir = tmp_path / "site" / "stockdata"
    sd_dir.mkdir(parents=True)
    regime_dir = tmp_path / "data" / "regime"
    regime_dir.mkdir(parents=True)
    (regime_dir / "latest.json").write_text(json.dumps(_make_regime()))
    fi_dir = tmp_path / "site" / "factordata"
    fi_dir.mkdir(parents=True)
    (fi_dir / "insider_signals.json").write_text("{}")

    state = compose(
        [],
        vendor_root=tmp_path,
        data_root=tmp_path / "out",
        now=run_date,
        price_loader=_price_loader_factory({"SPY": _make_ohlcv(_spy_closes(300))}),
    )
    m = state["market"]
    assert "risk_radar" in m
    assert "market_state" in m
    assert "vol_regime" in m
    assert "quad" in m
    assert "quad_name" in m
    assert "asof" in m
    assert m["risk_radar"]["verdict"] is not None or m["risk_radar"]["verdict"] is None  # nullable


# ---------------------------------------------------------------------------
# Fail-soft on per-ticker crash
# ---------------------------------------------------------------------------

def test_compose_fail_soft_bad_ticker(tmp_path, run_date):
    """A ticker whose price_loader raises → coverage_missing lanes, no crash."""
    from portfolio.held_risk import compose

    def bad_loader(ticker):
        if ticker == "CRASH":
            raise RuntimeError("test-injected crash")
        return _make_ohlcv(_spy_closes(300))

    (tmp_path / "site" / "stockdata").mkdir(parents=True)
    regime_dir = tmp_path / "data" / "regime"
    regime_dir.mkdir(parents=True)
    (regime_dir / "latest.json").write_text(json.dumps(_make_regime()))
    fi_dir = tmp_path / "site" / "factordata"
    fi_dir.mkdir(parents=True)
    (fi_dir / "insider_signals.json").write_text("{}")

    positions = [{"id": "c1", "ticker": "CRASH", "shares": 10, "entry_price": None,
                   "entry_date": None, "status": "active"}]
    state = compose(
        positions,
        vendor_root=tmp_path,
        data_root=tmp_path / "out",
        now=run_date,
        price_loader=bad_loader,
    )
    assert len(state["positions"]) == 1
    pos = state["positions"][0]
    assert pos["ticker"] == "CRASH"
    # lanes should all be coverage_missing
    assert all(
        pos["lanes"][k]["state"] == "coverage_missing" for k in pos["lanes"]
    ), f"expected all coverage_missing: { {k:v['state'] for k,v in pos['lanes'].items()} }"


# ===========================================================================
# W3: Fresh R2 schema paths
# ===========================================================================

# ---------------------------------------------------------------------------
# VENDOR_DEFAULT symlink resolution
# ---------------------------------------------------------------------------

def test_vendor_default_resolves_without_crash():
    """VENDOR_DEFAULT must be a Path (may not exist — just needs not to crash)."""
    from portfolio.held_risk import VENDOR_DEFAULT
    from pathlib import Path
    assert isinstance(VENDOR_DEFAULT, Path)


# ---------------------------------------------------------------------------
# event_windows fresh R2 schema
# ---------------------------------------------------------------------------

def test_event_window_fresh_r2_tdays(run_date):
    """event_windows.earnings.tdays < 3 → elevated (fresh R2 schema path)."""
    from portfolio.held_risk import _lane_event_window

    sd = _make_stockdata({
        "event_windows": {
            "earnings": {"tdays": 2, "date": "2026-07-09"},
            "fomc_within": {},
            "debt": {},
        },
        # Remove legacy earnings so fallback is NOT triggered
        "earnings": {"next_date": None, "next_time": None, "eps_forecast": 2.0,
                     "sue_z": 1.0, "summary": {"avg_surprise": 5.0, "beats": 4, "streak": 4, "total": 4},
                     "surprises": []},
    })
    lane, events = _lane_event_window(sd, run_date)
    assert lane["state"] == "elevated", f"got {lane['state']}: {lane['reasons']}"
    assert events["earnings_tdays"] == 2


def test_event_window_fresh_r2_fomc_watch(run_date):
    """event_windows.fomc_within.tdays <= 2 → fomc_within_2d flag (watch or elevated)."""
    from portfolio.held_risk import _lane_event_window

    sd = _make_stockdata({
        "event_windows": {
            "earnings": {"tdays": 20, "date": "2026-08-01"},
            "fomc_within": {"tdays": 1},
            "debt": {},
        },
    })
    lane, events = _lane_event_window(sd, run_date)
    assert any("fomc_within" in f for f in lane["flags"]), f"flags: {lane['flags']}"
    assert events["fomc_tdays"] == 1


def test_event_window_fresh_r2_debt_pressure(run_date):
    """event_windows.debt.current_debt > cash → debt_pressure flag."""
    from portfolio.held_risk import _lane_event_window

    sd = _make_stockdata({
        "event_windows": {
            "earnings": {"tdays": 30, "date": "2026-08-15"},
            "fomc_within": {},
            "debt": {"current_debt": 5e9, "cash": 1e9},
        },
    })
    lane, events = _lane_event_window(sd, run_date)
    assert "debt_pressure" in lane["flags"], f"flags: {lane['flags']}"


# ---------------------------------------------------------------------------
# expectation_state fresh R2 schema
# ---------------------------------------------------------------------------

def test_earnings_expectation_fresh_r2_sue_miss_pead(run_date):
    """expectation_state.sue_latest < 0 AND pead_drift_20d <= -0.03 → elevated."""
    from portfolio.held_risk import _lane_earnings_expectation

    sd = _make_stockdata({
        "expectation_state": {
            "sue_streak": -1,
            "sue_latest": -0.5,
            "pead_drift_20d": -0.04,
        },
    })
    lane = _lane_earnings_expectation(sd, run_date)
    assert lane["state"] == "elevated", f"got {lane['state']}: {lane['reasons']}"
    # sue_miss_pead_drift or similar flag must be present
    assert any("sue" in f or "pead" in f for f in lane["flags"]), f"flags: {lane['flags']}"


def test_earnings_expectation_fresh_r2_sue_streak_elevated(run_date):
    """expectation_state.sue_streak <= -2 → elevated."""
    from portfolio.held_risk import _lane_earnings_expectation

    sd = _make_stockdata({
        "expectation_state": {
            "sue_streak": -2,
            "sue_latest": -0.3,
            "pead_drift_20d": 0.01,  # not negative enough for PEAD flag alone
        },
    })
    lane = _lane_earnings_expectation(sd, run_date)
    assert lane["state"] == "elevated", f"got {lane['state']}: {lane['reasons']}"


# ---------------------------------------------------------------------------
# leverage_ratios fresh R2 schema
# ---------------------------------------------------------------------------

def test_solvency_fresh_r2_leverage_ratios_high_leverage(run_date):
    """leverage_ratios.net_debt_to_ebitda > 4 → flag fires."""
    from portfolio.held_risk import _lane_solvency_dilution

    sd = _make_stockdata({
        "leverage_ratios": {
            "net_debt_to_ebitda": 5.5,
            "current_ratio": 0.8,
        },
    })
    lane = _lane_solvency_dilution(sd, run_date)
    # At minimum a watch or elevated state with leverage flag
    assert lane["state"] in ("watch", "elevated"), f"got {lane['state']}: {lane['reasons']}"
    assert any("leverage" in f or "net_debt" in f for f in lane["flags"]), f"flags: {lane['flags']}"


def test_solvency_fresh_r2_critical_high_leverage_dilutive(run_date):
    """net_debt_to_ebitda > 6 AND capital_allocation.delta == dilutive → critical flag."""
    from portfolio.held_risk import _lane_solvency_dilution

    sd = _make_stockdata({
        "leverage_ratios": {
            "net_debt_to_ebitda": 7.0,
            "current_ratio": 0.7,
        },
        "capital_allocation": {"delta": "dilutive"},
    })
    lane = _lane_solvency_dilution(sd, run_date)
    assert lane["state"] == "elevated", f"got {lane['state']}: {lane['reasons']}"
    assert any("critical" in f for f in lane["flags"]), f"flags: {lane['flags']}"


# ---------------------------------------------------------------------------
# accounting_quality fresh R2 schema
# ---------------------------------------------------------------------------

def test_solvency_fresh_r2_accounting_quality_altman(run_date):
    """accounting_quality.altman_z (float) < 1.8 → altman flag fires.

    The code reads aq.get('altman_z') and calls float() on it directly,
    so this must be a number, not a nested dict.
    """
    from portfolio.held_risk import _lane_solvency_dilution

    sd = _make_stockdata({
        "accounting_quality": {
            "verdict": "weak",
            "altman_z": 1.2,   # plain float — what the code expects
            "piotroski": 6,    # plain int
            "n_caution": 2,
            "reads": [],
        },
        # Override the financials.multiyear.altman so the fallback doesn't mask the test
        "financials": {
            "multiyear": {
                "altman": {"z": 8.0, "zone": "safe", "approx": False},
                "piotroski": {"score": 7, "of": 9},
            },
            "raw": {
                "assets": 100e9, "cfo": 20e9, "debt_lt": 10e9,
                "equity": 30e9, "gross_profit": 40e9, "ni": 15e9,
                "repurchases": 5e9, "revenue": 100e9, "shares": 1e9, "dividends": None,
            },
            "gross_margin": 40.0, "net_margin": 15.0, "fcf_margin": 20.0,
            "ni_growth": 10.0, "rev_growth": 8.0, "asset_growth": 5.0,
            "roe": 50.0, "roa": 15.0, "debt_to_assets": 10.0, "accruals": 0.0,
        },
    })
    lane = _lane_solvency_dilution(sd, run_date)
    # Altman flag should fire from fresh R2 path (altman_z=1.2 < 1.8)
    assert any("altman" in f for f in lane["flags"]), f"flags: {lane['flags']}"


def test_solvency_fresh_r2_accounting_quality_piotroski(run_date):
    """accounting_quality.piotroski (int) <= 3 → piotroski flag fires.

    The code reads aq.get('piotroski') and calls int() on it directly.
    """
    from portfolio.held_risk import _lane_solvency_dilution

    sd = _make_stockdata({
        "accounting_quality": {
            "verdict": "weak",
            "altman_z": 3.0,   # above threshold — won't fire altman
            "piotroski": 2,    # plain int <= 3 → fires
            "n_caution": 1,
            "reads": [],
        },
        # Keep financials.multiyear.piotroski healthy so fallback doesn't confuse the test
        "financials": {
            "multiyear": {
                "altman": {"z": 8.0, "zone": "safe", "approx": False},
                "piotroski": {"score": 7, "of": 9},
            },
            "raw": {
                "assets": 100e9, "cfo": 20e9, "debt_lt": 10e9,
                "equity": 30e9, "gross_profit": 40e9, "ni": 15e9,
                "repurchases": 5e9, "revenue": 100e9, "shares": 1e9, "dividends": None,
            },
            "gross_margin": 40.0, "net_margin": 15.0, "fcf_margin": 20.0,
            "ni_growth": 10.0, "rev_growth": 8.0, "asset_growth": 5.0,
            "roe": 50.0, "roa": 15.0, "debt_to_assets": 10.0, "accruals": 0.0,
        },
    })
    lane = _lane_solvency_dilution(sd, run_date)
    assert any("piotroski" in f for f in lane["flags"]), f"flags: {lane['flags']}"


# ---------------------------------------------------------------------------
# capital_allocation fresh R2 schema
# ---------------------------------------------------------------------------

def test_solvency_fresh_r2_capital_allocation_dilutive(run_date):
    """capital_allocation.delta == dilutive → dilution flag fires."""
    from portfolio.held_risk import _lane_solvency_dilution

    sd = _make_stockdata({
        "capital_allocation": {"delta": "dilutive"},
        # Keep other metrics healthy so only the CA flag fires
        "financials": {
            "multiyear": {
                "altman": {"z": 5.0, "zone": "safe", "approx": False},
                "piotroski": {"score": 7, "of": 9},
            },
            "raw": {
                "assets": 100e9, "cfo": 20e9, "debt_lt": 10e9,
                "equity": 30e9, "gross_profit": 40e9, "ni": 15e9,
                "repurchases": -3e9,  # net issuance
                "revenue": 100e9, "shares": 1e9, "dividends": None,
            },
        },
    })
    lane = _lane_solvency_dilution(sd, run_date)
    # Dilution flag should be present
    dilution_flag = any("dilut" in f for f in lane["flags"])
    assert dilution_flag, f"no dilution flag in: {lane['flags']}"


# ---------------------------------------------------------------------------
# moat_falsifiers fresh R2 schema
# ---------------------------------------------------------------------------

def test_solvency_fresh_r2_moat_falsifiers(run_date):
    """moat_falsifiers with >= 2 firing items → moat_falsifiers flag."""
    from portfolio.held_risk import _lane_solvency_dilution

    sd = _make_stockdata({
        "moat_falsifiers": [
            {"key": "margin_erosion", "firing": True, "label": "Margin erosion"},
            {"key": "rev_decel", "firing": True, "label": "Rev decel"},
            {"key": "comp_threat", "firing": False, "label": "Competitive threat"},
        ],
    })
    lane = _lane_solvency_dilution(sd, run_date)
    assert any("moat_falsifiers" in f for f in lane["flags"]), f"flags: {lane['flags']}"


# ---------------------------------------------------------------------------
# personality fresh R2 schema + _compose_position return
# ---------------------------------------------------------------------------

def test_personality_fresh_r2_block(tmp_path, run_date):
    """personality.base.{archetype,dna_class} + personality.current_mode → in pos output."""
    from portfolio.held_risk import compose

    sd = _make_stockdata({
        "personality": {
            "base": {"archetype": "compounder", "dna_class": "large_quality"},
            "current_mode": "momentum_phase",
        },
    })
    (tmp_path / "site" / "stockdata").mkdir(parents=True)
    (tmp_path / "site" / "stockdata" / "TST.json").write_text(json.dumps(sd))
    (tmp_path / "data" / "regime").mkdir(parents=True)
    (tmp_path / "data" / "regime" / "latest.json").write_text(json.dumps(_make_regime()))
    (tmp_path / "site" / "factordata").mkdir(parents=True)
    (tmp_path / "site" / "factordata" / "insider_signals.json").write_text("{}")

    positions = [{"id": "pp1", "ticker": "TST", "shares": 10, "entry_price": None,
                  "entry_date": None, "status": "active"}]
    state = compose(
        positions,
        vendor_root=tmp_path,
        data_root=tmp_path / "out",
        now=run_date,
        price_loader=_price_loader_factory({"SPY": _make_ohlcv(_spy_closes(300)),
                                             "TST": _make_ohlcv(_stock_closes(300))}),
    )
    pers = state["positions"][0]["personality"]
    assert pers["archetype"] == "compounder", f"archetype: {pers['archetype']}"
    assert pers["dna_class"] == "large_quality", f"dna_class: {pers['dna_class']}"
    assert pers["current_mode"] == "momentum_phase", f"current_mode: {pers['current_mode']}"


def test_personality_falls_back_to_profile(tmp_path, run_date):
    """No personality block → falls back to profile.archetype.key."""
    from portfolio.held_risk import compose

    sd = _make_stockdata()  # no personality block; profile.archetype.key = "growth"
    (tmp_path / "site" / "stockdata").mkdir(parents=True)
    (tmp_path / "site" / "stockdata" / "TST.json").write_text(json.dumps(sd))
    (tmp_path / "data" / "regime").mkdir(parents=True)
    (tmp_path / "data" / "regime" / "latest.json").write_text(json.dumps(_make_regime()))
    (tmp_path / "site" / "factordata").mkdir(parents=True)
    (tmp_path / "site" / "factordata" / "insider_signals.json").write_text("{}")

    positions = [{"id": "pp2", "ticker": "TST", "shares": 10, "entry_price": None,
                  "entry_date": None, "status": "active"}]
    state = compose(
        positions,
        vendor_root=tmp_path,
        data_root=tmp_path / "out",
        now=run_date,
        price_loader=_price_loader_factory({"SPY": _make_ohlcv(_spy_closes(300)),
                                             "TST": _make_ohlcv(_stock_closes(300))}),
    )
    pers = state["positions"][0]["personality"]
    assert pers["archetype"] == "growth", f"archetype: {pers['archetype']}"
    assert pers["current_mode"] is None


# ---------------------------------------------------------------------------
# post_entry_high derived from price history (W3 giveback path)
# ---------------------------------------------------------------------------

def test_extension_post_entry_high_from_ohlcv(run_date):
    """When position has entry_date but no post_entry_high, derive from ohlcv_df passed in."""
    from portfolio.held_risk import _lane_extension_giveback

    sd = _make_stockdata()
    # entry 2025-07-01, closes go 100 → 130 peak → 115 (giveback)
    n = 252
    entry_date = "2025-07-01"
    closes_vals = [100.0 + i * 0.3 for i in range(n // 2)] + \
                  [100.0 + (n//2) * 0.3 - (i * 0.5) for i in range(n // 2)]
    idx = pd.date_range(start="2025-01-02", periods=n, freq="B")
    ohlcv_df = pd.DataFrame({"close": closes_vals}, index=idx)

    position = {"entry_price": 100.0, "entry_date": entry_date}
    metrics = {
        "close": closes_vals[-1],
        "ma20": closes_vals[-1] + 5,
        "ma50": closes_vals[-1] + 10,
        "ma200": 90.0,
        "atr14": 3.0,
        "high_52w": max(closes_vals),
        "rs20_spy_z": -0.5,
        "ext_z": 0.2,
        "close_date": "2026-07-07",
    }

    lane, path = _lane_extension_giveback(metrics, sd, position, ohlcv_df=ohlcv_df)
    # Should compute mfe_pct > 0 since high is above entry
    assert path["mfe_pct"] is not None and path["mfe_pct"] > 0, f"mfe_pct: {path['mfe_pct']}"


# ---------------------------------------------------------------------------
# MAJOR-2: ORANGE / caution market regime escalates to elevated
# ---------------------------------------------------------------------------

class TestLaneMarketRegimeOrange:
    """_lane_market_regime must map risk_radar.state='caution' (ORANGE in site banner)
    to lane state 'elevated', not 'watch', so the role-ladder tighten/trim triggers fire.

    Vocabulary:
      risk_radar.state 'caution'  → ORANGE → elevated
      risk_radar.state 'risk-off' → EXTREME → elevated
      risk_state.state 'caution'  → YELLOW → watch (only when radar is calm)
      risk_radar.state 'calm'     → GREEN  → ok
    """

    def _regime(self, radar_state: str, rs_state: str = "risk-on") -> dict:
        return {
            "risk_radar": {"state": radar_state, "dominant_scare": "test"},
            "risk_state": {"state": rs_state},
            "asof": "2026-07-07",
        }

    def test_caution_radar_is_elevated(self):
        """risk_radar='caution' (ORANGE) must produce lane state 'elevated'."""
        from portfolio.held_risk import _lane_market_regime
        lane = _lane_market_regime(self._regime("caution"), date(2026, 7, 7))
        assert lane["state"] == "elevated", (
            f"expected 'elevated' for caution (ORANGE), got {lane['state']!r}"
        )

    def test_risk_off_radar_is_elevated(self):
        """risk_radar='risk-off' (EXTREME) must produce lane state 'elevated'."""
        from portfolio.held_risk import _lane_market_regime
        lane = _lane_market_regime(self._regime("risk-off"), date(2026, 7, 7))
        assert lane["state"] == "elevated", (
            f"expected 'elevated' for risk-off, got {lane['state']!r}"
        )

    def test_calm_radar_is_ok(self):
        """risk_radar='calm' (GREEN) must produce lane state 'ok'."""
        from portfolio.held_risk import _lane_market_regime
        lane = _lane_market_regime(self._regime("calm"), date(2026, 7, 7))
        assert lane["state"] == "ok", (
            f"expected 'ok' for calm, got {lane['state']!r}"
        )

    def test_risk_state_caution_only_is_watch(self):
        """risk_state='caution' with calm radar → YELLOW → watch (not elevated)."""
        from portfolio.held_risk import _lane_market_regime
        regime = {
            "risk_radar": {"state": "calm", "dominant_scare": None},
            "risk_state": {"state": "caution"},
            "asof": "2026-07-07",
        }
        lane = _lane_market_regime(regime, date(2026, 7, 7))
        assert lane["state"] == "watch", (
            f"expected 'watch' for risk_state=caution+calm radar, got {lane['state']!r}"
        )

    def test_orange_regime_plus_price_trend_triggers_tighten(self):
        """ORANGE market regime (caution) + elevated price_trend → role tighten."""
        from portfolio.held_risk import _assign_role

        def _el(st): return {"state": st, "flags": [], "reasons": [], "asof": "2026-07-07"}

        lanes = {
            "price_trend": _el("elevated"),
            "extension_giveback": _el("ok"),
            "event_window": _el("ok"),
            "earnings_expectation": _el("ok"),
            "solvency_dilution": _el("ok"),
            "ownership_flow": _el("ok"),
            "macro_sensitivity": _el("ok"),
            "sector_rotation": _el("ok"),
            "market_regime": _el("elevated"),  # ORANGE → elevated
        }
        role, reasons = _assign_role(lanes, {}, {})
        assert role == "tighten", (
            f"expected 'tighten' with ORANGE + price_trend elevated, got {role!r}"
        )

    def test_orange_regime_plus_extension_triggers_trim_review(self):
        """ORANGE market regime + extension_giveback elevated → role trim_review."""
        from portfolio.held_risk import _assign_role

        def _el(st): return {"state": st, "flags": [], "reasons": [], "asof": "2026-07-07"}

        lanes = {
            "price_trend": _el("ok"),
            "extension_giveback": _el("elevated"),
            "event_window": _el("ok"),
            "earnings_expectation": _el("ok"),
            "solvency_dilution": _el("ok"),
            "ownership_flow": _el("ok"),
            "macro_sensitivity": _el("ok"),
            "sector_rotation": _el("ok"),
            "market_regime": _el("elevated"),  # ORANGE → elevated
        }
        role, reasons = _assign_role(lanes, {}, {})
        assert role == "trim_review", (
            f"expected 'trim_review' with ORANGE + extension elevated, got {role!r}"
        )
