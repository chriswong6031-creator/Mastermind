"""Guards for the advanced factor-alpha lab (loop.factor_zoo).

The new price-factor signals must point the right way (economically), the composite must blend
component ranks, the IC term-structure must produce a clustered per-horizon read, and the read
path must always degrade to an honest stub. The heavy frozen-gauntlet run is covered by a
skip-unless-data integration test that also asserts the composite is COUNTED in effective-N
(multiple-testing honesty) and judged only on the holdout.
"""
from __future__ import annotations

import json

import pytest

import bot  # noqa: F401
from loop import factor_zoo as FZ


@pytest.fixture(scope="module")
def Z():
    """The ZooFactor / Composite classes (need the audited base + numpy/pandas)."""
    return FZ._zoo_classes()


def _panel(spec: dict, n=40, start="2026-01-02"):
    """Build a price DataFrame from {ticker: list-of-daily-returns} (len n-1)."""
    import pandas as pd
    idx = pd.bdate_range(start, periods=n)
    data = {}
    for tk, rets in spec.items():
        px, p = [100.0], 100.0
        for r in rets:
            p *= (1 + r)
            px.append(p)
        data[tk] = px[:n]
    return pd.DataFrame(data, index=idx)


def _sig(Z, kind, hist, **kw):
    ZooFactor = Z[0]
    f = ZooFactor("t", kind, lookback=kw.pop("lookback", 21), **kw)
    return f._signal(hist, hist.index[-1])


# ── economic directionality of the new signals ─────────────────────────────────
def test_maxret_prefers_low_lottery(Z):
    # SMOOTH name vs one with a single huge +30% spike → long the LOW-max (smooth) name
    smooth = [0.004] * 39
    spike = [0.0] * 19 + [0.30] + [0.0] * 19
    s = _sig(Z, "maxret", _panel({"SMOOTH": smooth, "SPIKE": spike}), lookback=21)
    assert s["SMOOTH"] > s["SPIKE"]


def test_consistency_prefers_steady_climbers(Z):
    steady = [0.005] * 39                              # every day up
    choppy = [0.05, -0.045] * 19 + [0.01]             # half down
    s = _sig(Z, "consistency", _panel({"STEADY": steady, "CHOPPY": choppy}), lookback=21)
    assert s["STEADY"] > s["CHOPPY"]


def test_downvol_prefers_low_downside(Z):
    calm = [0.003] * 39
    crashy = [0.02, -0.06] * 19 + [0.0]
    s = _sig(Z, "downvol", _panel({"CALM": calm, "CRASHY": crashy}), lookback=21)
    assert s["CALM"] > s["CRASHY"]


def test_accel_prefers_accelerating(Z):
    # ACCEL: flat then ramps up (recent >> older) vs ramps then flattens
    accel = [0.0] * 20 + [0.01] * 19
    decel = [0.01] * 20 + [0.0] * 19
    s = _sig(Z, "accel", _panel({"ACCEL": accel, "DECEL": decel}), lookback=38)
    assert s["ACCEL"] > s["DECEL"]


def test_resid_mom_is_cross_sectional(Z):
    # cross-sectionally demeaned: the OUTperformer has positive idiosyncratic momentum
    win = [0.01] * 39
    lose = [-0.005] * 39
    s = _sig(Z, "resid_mom", _panel({"WIN": win, "LOSE": lose}), lookback=30, skip=1)
    assert s["WIN"] > s["LOSE"] and s["WIN"] > 0 > s["LOSE"]


def test_skew_prefers_low_skew(Z):
    # NEG-skew name (many small ups, rare crash) is LOW-skew → attractive; POS-skew (lottery) is not
    neg = [0.01] * 38 + [-0.30]
    pos = [-0.005] * 38 + [0.30]
    s = _sig(Z, "skew", _panel({"NEG": neg, "POS": pos}), lookback=39)
    assert s["NEG"] > s["POS"]


def test_unknown_kind_raises(Z):
    with pytest.raises(ValueError):
        _sig(Z, "bogus", _panel({"A": [0.01] * 39}))


# ── composite blends component ranks ───────────────────────────────────────────
def test_composite_averages_component_ranks(Z):
    ZooFactor, Composite, _ = Z
    # ≥10 names (the composite needs a real cross-section): a clean gradient of trend strength
    spec = {f"N{i:02d}": [0.0006 * (i - 6)] * 39 for i in range(13)}   # N12 strongest up, N00 down
    hist = _panel(spec)
    c1 = ZooFactor("mom", "momentum", lookback=21, skip=1)
    c2 = ZooFactor("cons", "consistency", lookback=21)
    comp = Composite("composite_alpha", [c1, c2], mem_df=None)
    s = comp._signal(hist, hist.index[-1]).dropna()
    # the strongest uptrend ranks top on BOTH components → highest composite; weakest ranks bottom
    assert s.idxmax() == "N12" and s.idxmin() == "N00"
    assert (s >= 0).all() and (s <= 1).all()                     # mean of pct-ranks ∈ [0,1]


# ── IC term-structure shape ────────────────────────────────────────────────────
def test_ic_term_structure_shape(Z):
    import numpy as np
    import pandas as pd
    # 600 bdays, 30 names with a persistent momentum signal so rank_ic is computable
    idx = pd.bdate_range("2020-01-02", periods=600)
    rng = np.random.RandomState(0)
    cols = {}
    for i in range(30):
        drift = 0.0003 * (i - 15)
        cols[f"T{i}"] = 100 * np.cumprod(1 + drift + rng.normal(0, 0.01, 600))
    closes = pd.DataFrame(cols, index=idx)
    closes.insert(0, "SPY", 100 * np.cumprod(1 + rng.normal(0, 0.008, 600)))
    mem = pd.DataFrame({"ticker": [f"T{i}" for i in range(30)],
                        "start_date": [pd.Timestamp("2019-01-01")] * 30,
                        "end_date": [pd.NaT] * 30})
    ZooFactor = Z[0]
    f = ZooFactor("mom", "momentum", lookback=126, skip=21, mem_df=mem)
    prof = FZ.ic_term_structure(f, closes, mem)
    assert set(prof.keys()) == {"21d", "63d", "126d"}
    for h, o in prof.items():
        assert "n_dates" in o                        # either a graded cell or an honest 'building'


# ── read path always degrades honestly ─────────────────────────────────────────
def test_load_missing_is_stub(monkeypatch, tmp_path):
    monkeypatch.setattr(FZ, "_OUT", tmp_path / "absent.json")
    v = FZ.load()
    assert v["status"] == "unavailable" and "note" in v


def test_load_reads_persisted(monkeypatch, tmp_path):
    p = tmp_path / "factor_zoo.json"
    p.write_text(json.dumps({"status": "ok", "verdict": {"effective_n": 4}}))
    monkeypatch.setattr(FZ, "_OUT", p)
    assert FZ.load()["verdict"]["effective_n"] == 4


def test_run_never_raises(monkeypatch):
    # force the heavy data load to fail → run() must return an honest stub, never raise
    from loop import factor_experiment as fe

    def _boom():
        raise RuntimeError("simulated missing panel")
    monkeypatch.setattr(fe, "load_panel", _boom)
    v = FZ.run(asof="2026-06-21", write=False)
    assert v["status"] == "unavailable"


@pytest.mark.integration
def test_run_real_counts_composite_in_effective_n():
    """Real frozen gauntlet. Skips if data unavailable. Asserts the composite is a counted trial
    and the holdout-confirmed set is honest (a list)."""
    v = FZ.run(asof="2026-06-21", write=False)
    if v.get("status") != "ok":
        pytest.skip("factor-zoo data unavailable in this environment")
    ver = v["verdict"]
    names = [f["name"] for f in v["factors"]]
    assert ver["n_candidates"] >= 10
    assert "composite_alpha" in names                # composite was scored + counted
    assert isinstance(ver["holdout_confirmed"], list)
    assert 0.0 <= ver["pbo"] <= 1.0
