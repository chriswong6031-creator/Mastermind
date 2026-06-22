"""Guards for PIT fundamental factors (loop.fundamentals).

The one thing that MUST hold is point-in-time correctness — a fundamental may only be used on/after
its `asof_date`. That's tested on synthetic vintages (exact) and the real panel (sampled). Plus
metric directionality, the long-short materialize, and honest degradation.
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

import bot  # noqa: F401
from loop import fundamentals as FN


# ── PIT lookup: never uses a filing dated after t ───────────────────────────────
def _synth_edgar():
    # ticker AAA: two vintages; BBB: only a LATE filing (after most rebalances)
    return {
        "AAA": (np.array(["2020-06-01", "2023-06-01"], dtype="datetime64[ns]"),
                [{"ticker": "AAA", "assets": 100.0, "equity": 50.0, "ni": 10.0, "cfo": 8.0,
                  "gross_profit": 40.0, "revenue": 80.0, "shares": 1e6, "assets_prior": 90.0,
                  "dividends": 1.0, "repurchases": 2.0},
                 {"ticker": "AAA", "assets": 200.0, "equity": 80.0, "ni": 5.0, "cfo": 4.0,
                  "gross_profit": 50.0, "revenue": 90.0, "shares": 1e6, "assets_prior": 100.0,
                  "dividends": 1.0, "repurchases": 0.0}]),
        "BBB": (np.array(["2024-01-01"], dtype="datetime64[ns]"),
                [{"ticker": "BBB", "assets": 300.0, "equity": 100.0, "ni": 20.0, "cfo": 18.0,
                  "gross_profit": 90.0, "revenue": 150.0, "shares": 2e6, "assets_prior": 250.0,
                  "dividends": 0.0, "repurchases": 5.0}]),
    }


def test_pit_picks_latest_filing_on_or_before_t():
    ed = _synth_edgar()
    # at 2021-01-01 only the 2020-06 vintage is known
    r = FN._pit_row(ed, "AAA", pd.Timestamp("2021-01-01"))
    assert r is not None and r["assets"] == 100.0 and r["ni"] == 10.0
    # at 2024-01-01 the 2023-06 vintage is known
    r2 = FN._pit_row(ed, "AAA", pd.Timestamp("2024-01-01"))
    assert r2["assets"] == 200.0 and r2["ni"] == 5.0


def test_pit_none_before_first_filing():
    ed = _synth_edgar()
    assert FN._pit_row(ed, "AAA", pd.Timestamp("2019-01-01")) is None   # no look-ahead to 2020 filing
    assert FN._pit_row(ed, "BBB", pd.Timestamp("2021-01-01")) is None   # BBB only filed in 2024


def test_pit_unknown_ticker_none():
    assert FN._pit_row(_synth_edgar(), "ZZZ", pd.Timestamp("2022-01-01")) is None


@pytest.mark.integration
def test_pit_no_leak_on_real_panel():
    ed = FN.load_edgar()
    if not ed:
        pytest.skip("EDGAR panel unavailable")
    bad = 0
    for tk in list(ed)[:200]:
        for t in pd.to_datetime(["2013-04-01", "2017-08-15", "2021-02-01", "2024-05-01"]):
            r = FN._pit_row(ed, tk, t)
            if r is not None and pd.Timestamp(r["asof_date"]) > t:
                bad += 1
    assert bad == 0


# ── metric correctness + directionality ────────────────────────────────────────
def test_metrics_compute_and_sign():
    row = _synth_edgar()["AAA"][1][0]   # assets100 eq50 ni10 cfo8 gp40 rev80 shares1e6 ap90
    px = 50.0
    mktcap = px * 1e6
    assert abs(FN._metric(row, "ep", px) - 10.0 / mktcap) < 1e-12
    assert abs(FN._metric(row, "bp", px) - 50.0 / mktcap) < 1e-12
    assert abs(FN._metric(row, "roe", None) - 10.0 / 50.0) < 1e-12
    assert abs(FN._metric(row, "roa", None) - 10.0 / 100.0) < 1e-12
    assert abs(FN._metric(row, "gross_prof", None) - 40.0 / 100.0) < 1e-12
    assert abs(FN._metric(row, "accruals", None) - (10.0 - 8.0) / 100.0) < 1e-12
    assert abs(FN._metric(row, "asset_growth", None) - (100.0 / 90.0 - 1.0)) < 1e-12
    assert abs(FN._metric(row, "shareholder_yield", px) - (1.0 + 2.0) / mktcap) < 1e-12


def test_shareholder_yield_nan_safe():
    # regression: np.nan is truthy, so `x or 0.0` would propagate NaN and silently DROP the name.
    # A missing dividend with a real buyback (or vice-versa) must still compute a valid yield.
    mktcap = 10.0 * 1e6
    only_buyback = {"shares": 1e6, "dividends": float("nan"), "repurchases": 5e6}
    assert abs(FN._metric(only_buyback, "shareholder_yield", 10.0) - 5e6 / mktcap) < 1e-9
    only_div = {"shares": 1e6, "dividends": 3e6, "repurchases": float("nan")}
    assert abs(FN._metric(only_div, "shareholder_yield", 10.0) - 3e6 / mktcap) < 1e-9
    neither = {"shares": 1e6, "dividends": float("nan"), "repurchases": float("nan")}
    assert FN._metric(neither, "shareholder_yield", 10.0) == 0.0   # no payout → 0, not dropped


def test_metric_missing_fields_none():
    row = {"assets": None, "equity": 0.0, "ni": 5.0, "shares": None}
    assert FN._metric(row, "roe", None) is None      # equity 0 → no divide
    assert FN._metric(row, "ep", 10.0) is None        # shares None → no mktcap
    assert FN._metric(row, "roa", None) is None        # assets None


# ── materialize: PIT eligibility + long-short construction ──────────────────────
def _synth_panel(n=30, days=400, start="2019-01-02"):
    idx = pd.bdate_range(start, periods=days)
    cols = {"SPY": 100 * np.cumprod(1 + np.full(days, 0.0003))}
    for i in range(n):
        cols[f"T{i:02d}"] = np.linspace(20, 20 + i, days)   # all ≥ $5, distinct levels
    return pd.DataFrame(cols, index=idx)


def _synth_mem(n=30):
    return pd.DataFrame({"ticker": [f"T{i:02d}" for i in range(n)],
                         "start_date": [pd.Timestamp("2010-01-01")] * n,
                         "end_date": [pd.NaT] * n})


def _synth_edgar_many(n=30, asof="2018-06-01"):
    ed = {}
    a = np.array([asof], dtype="datetime64[ns]")
    for i in range(n):
        ed[f"T{i:02d}"] = (a, [{"ticker": f"T{i:02d}", "assets": 100.0 + i, "equity": 50.0,
                                "ni": float(i), "cfo": float(i) - 1, "gross_profit": 30.0 + i,
                                "revenue": 80.0, "shares": 1e6, "assets_prior": 95.0,
                                "dividends": 0.0, "repurchases": 0.0}])
    return ed


def test_materialize_long_short_is_market_neutral():
    F = FN._fund_class()
    mem, ed = _synth_mem(), _synth_edgar_many()
    cand = F("roe_ls", "roe", +1, 0.10, ed, mem, side="ls")
    W = cand.materialize(_synth_panel())
    last = W.iloc[-1]
    assert abs(last.sum()) < 1e-9                      # market-neutral: longs +0.5, shorts -0.5
    assert last.max() > 0 and last.min() < 0
    assert last["SPY"] == 0.0


def test_materialize_excludes_names_without_pit_data():
    # fundamentals only become known at 2018-06; before that, materialize must hold NOTHING
    F = FN._fund_class()
    mem = _synth_mem()
    ed = _synth_edgar_many(asof="2018-06-01")
    cand = F("roa", "roa", +1, 0.20, ed, mem, side="long")
    W = cand.materialize(_synth_panel(days=400, start="2019-01-02"))  # all dates after the filing
    assert W.iloc[-1].abs().sum() > 0                 # has positions once PIT data exists
    # a panel entirely BEFORE the filing → no positions (no look-ahead to the 2018 filing)
    early = _synth_panel(days=200, start="2016-01-04")
    W2 = cand.materialize(early)
    assert W2.abs().sum().sum() == 0.0


# ── honest degradation ──────────────────────────────────────────────────────────
def test_load_missing_stub(monkeypatch, tmp_path):
    monkeypatch.setattr(FN, "_OUT", tmp_path / "absent.json")
    assert FN.load()["status"] == "unavailable"


def test_run_never_raises(monkeypatch):
    monkeypatch.setattr(FN, "load_edgar", lambda: None)
    assert FN.run(asof="2026-06-21", write=False)["status"] == "unavailable"


@pytest.mark.integration
def test_run_real_no_lookahead_verdict():
    v = FN.run(asof="2026-06-21", write=False)
    if v.get("status") != "ok":
        pytest.skip("fundamentals data unavailable")
    ver = v["verdict"]
    assert ver["n_candidates"] >= 10 and 0.0 <= ver["pbo"] <= 1.0
    assert isinstance(ver["holdout_confirmed"], list)
    assert any(g for g in v.get("data_gaps", []) if "volume" in g.lower())   # gap disclosed
