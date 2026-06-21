"""Audit fix (single_name_iterate.py:136) — the non-dry-run enroll path must archive a
{ticker: weight} mapping, not the FactorCandidate spec dict.

The bug: paper.enroll(h, c.spec, ...) passed the whole spec dict (name/kind/lookback/...)
as `weights`. FactorCandidate.spec has no 'weights' key (unlike the ETF candidate), so the
archived snapshot's weights would have been garbage on a real dry_run=False run, corrupting
forward-Brier accounting. The fix materializes W=c.materialize(closes) and passes the
last-row {ticker: weight} mapping.

This test reproduces the corrected transform directly (it does NOT call paper.enroll and
needs no DB / vendored data beyond importing the candidate module, which is skip-guarded).
"""
import numpy as np
import pandas as pd
import pytest

import bot  # noqa: F401


def _synthetic_closes(bench="SPY", bond="IEF", n_names=40, days=400, seed=7):
    """A small survivorship-clean price grid shaped like load_panel()['closes']:
    column 0 = benchmark, column 1 = bond, then single names. Monthly index so the
    materialize rebalance has several dates."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2018-01-02", periods=days)
    cols = [bench, bond] + [f"N{i:03d}" for i in range(n_names)]
    rets = rng.normal(0.0003, 0.012, size=(days, len(cols)))
    px = 100.0 * np.cumprod(1.0 + rets, axis=0)
    return pd.DataFrame(px, index=idx, columns=cols)


def _all_member_mem(closes, bench="SPY", bond="IEF"):
    """PIT membership table where every single name is a member for the whole span."""
    names = [c for c in closes.columns if c not in (bench, bond)]
    return pd.DataFrame({
        "ticker": names,
        "start_date": pd.Timestamp("2000-01-01"),
        "end_date": pd.NaT,
    })


def _candidate_module():
    try:
        from loop import single_name_candidates as snc
    except Exception as e:               # vendored lib.store / engine absent in this env
        pytest.skip(f"single-name candidate module unavailable: {e}")
    return snc


def test_enroll_payload_is_ticker_weight_mapping_long():
    """The corrected enroll path archives a {ticker: weight} mapping that sums to the
    long candidate's gross (1.0) — never the spec dict."""
    snc = _candidate_module()
    closes = _synthetic_closes()
    mem = _all_member_mem(closes)

    c = snc.FactorCandidate("smoke_lowvol", "lowvol", 0, side="long",
                            frac=0.20, vol_win=60, mem_df=mem)

    # exactly the transform single_name_iterate.py now applies before paper.enroll
    W = c.materialize(closes)
    live_weights = {k: float(v) for k, v in W.iloc[-1].to_dict().items() if v != 0.0}

    # it is a {ticker: weight} mapping, NOT the spec dict
    assert "kind" not in live_weights and "lookback" not in live_weights
    assert live_weights, "expected a non-empty live-weight mapping at the last row"
    assert all(isinstance(k, str) for k in live_weights)
    # benchmark + bond are never holdings
    assert "SPY" not in live_weights and "IEF" not in live_weights
    # long-only gross == 1.0
    assert sum(live_weights.values()) == pytest.approx(1.0, abs=1e-9)
    assert all(v > 0 for v in live_weights.values())


def test_enroll_payload_longshort_gross_and_net():
    """A long/short candidate archives a signed mapping: gross (sum |w|) ~ 1.0,
    net (sum w) ~ 0.0."""
    snc = _candidate_module()
    closes = _synthetic_closes()
    mem = _all_member_mem(closes)

    c = snc.FactorCandidate("smoke_mom_ls", "momentum", 252, skip=21, side="ls",
                            frac=0.10, mem_df=mem)
    W = c.materialize(closes)
    live_weights = {k: float(v) for k, v in W.iloc[-1].to_dict().items() if v != 0.0}

    if not live_weights:
        pytest.skip("synthetic panel too short for the 252+21 momentum lookback")

    gross = sum(abs(v) for v in live_weights.values())
    net = sum(live_weights.values())
    assert "SPY" not in live_weights and "IEF" not in live_weights
    assert gross == pytest.approx(1.0, abs=1e-9)
    assert net == pytest.approx(0.0, abs=1e-9)


def test_spec_dict_would_have_been_wrong():
    """Guards the regression: the OLD code passed c.spec, which has no 'weights' key and
    is not a ticker->weight mapping at all."""
    snc = _candidate_module()
    closes = _synthetic_closes()
    mem = _all_member_mem(closes)
    c = snc.FactorCandidate("smoke", "lowvol", 0, side="long", frac=0.20,
                            vol_win=60, mem_df=mem)
    assert "weights" not in c.spec            # the root cause of the bug
    assert set(c.spec) >= {"name", "kind", "lookback"}   # it's the spec, not weights
