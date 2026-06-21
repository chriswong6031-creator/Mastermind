"""Offline unit tests for the single-name factor production modules.

Pure synthetic panels — no live data, no network. Validates the two bias-critical
pieces: the causal panel cleaning (loop.single_name_panel.sanitize_panel) and the
no-lookahead / PIT-membership / equal-weight materialize logic
(loop.single_name_candidates.FactorCandidate.materialize).
"""
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loop import single_name_panel as snp
from loop.single_name_candidates import FactorCandidate, _rebalance_dates


# --------------------------------------------------------------------------- #
# panel cleaning
# --------------------------------------------------------------------------- #
def test_sanitize_clamps_absurd_jump():
    """An unadjusted split tick (5 -> 170, a 33x one-day return between two VALID >$1
    prices) must be neutralized by the return clamp: the reconstructed path may carry no
    daily return above RET_CAP, and the clamp counter must register it."""
    idx = pd.date_range("2020-01-01", periods=6, freq="D")
    s = pd.Series([10.0, 10.5, 5.0, 170.0, 171.0, 172.0], index=idx)
    out = snp.sanitize_panel(pd.DataFrame({"BAD": s}))
    ret = out["BAD"].pct_change().dropna()
    assert (ret.abs() <= snp.RET_CAP + 1e-9).all(), ret.to_dict()
    assert out.attrs["n_clamped"] >= 1


def test_sanitize_subdollar_becomes_flat_not_teleport():
    """Sub-$1 ticks are masked to NaN; the gap day earns a 0 return (no teleport),
    so the clean path never registers a jump across the masked region."""
    idx = pd.date_range("2020-01-01", periods=5, freq="D")
    s = pd.Series([20.0, 0.4, 0.4, 21.0, 22.0], index=idx)   # middle two are bad
    out = snp.sanitize_panel(pd.DataFrame({"X": s}))["X"]
    ret = out.pct_change().dropna()
    assert (ret.abs() <= snp.RET_CAP + 1e-9).all()
    # first and last valid prices preserved at their real (clean) levels
    assert out.first_valid_index() == idx[0]
    assert pytest.approx(out.iloc[0], rel=1e-9) == 20.0


def test_sanitize_preserves_clean_returns():
    """A clean series must round-trip: reconstructed returns == original returns."""
    idx = pd.date_range("2020-01-01", periods=10, freq="D")
    rng = np.random.default_rng(0)
    rets = rng.normal(0, 0.01, 9)
    px = 50 * np.cumprod(np.concatenate([[1.0], 1 + rets]))
    s = pd.Series(px, index=idx)
    out = snp.sanitize_panel(pd.DataFrame({"C": s}))["C"]
    np.testing.assert_allclose(out.pct_change().dropna().values,
                               s.pct_change().dropna().values, atol=1e-9)
    assert out.attrs["n_clamped"] == 0


# --------------------------------------------------------------------------- #
# helpers for materialize tests
# --------------------------------------------------------------------------- #
def _mem_all(tickers, start="2018-01-01"):
    """Membership table: every ticker a member since `start`, never ended."""
    return pd.DataFrame({
        "ticker": list(tickers),
        "start_date": pd.to_datetime([start] * len(tickers)),
        "end_date": pd.to_datetime([pd.NaT] * len(tickers)),
    })


def _synthetic_closes(n_names=40, days=400, seed=1):
    idx = pd.date_range("2019-01-01", periods=days, freq="B")
    rng = np.random.default_rng(seed)
    cols = {}
    # SPY (benchmark) and IEF (bond) MUST be columns 0 and 1
    cols[snp.BENCH] = 100 * np.cumprod(1 + rng.normal(0.0003, 0.01, days))
    cols[snp.BOND] = 100 * np.cumprod(1 + rng.normal(0.0001, 0.003, days))
    for i in range(n_names):
        vol = 0.005 + 0.02 * (i / n_names)          # monotone vol spread for lowvol test
        cols[f"N{i:02d}"] = 50 * np.cumprod(1 + rng.normal(0.0002, vol, days))
    return pd.DataFrame(cols, index=idx)


# --------------------------------------------------------------------------- #
# materialize: structural invariants
# --------------------------------------------------------------------------- #
def test_materialize_spy_and_ief_never_held():
    closes = _synthetic_closes()
    names = [c for c in closes.columns if c not in (snp.BENCH, snp.BOND)]
    cand = FactorCandidate("lv", "lowvol", 0, side="long", frac=0.20, vol_win=60,
                           mem_df=_mem_all(names))
    W = cand.materialize(closes)
    assert (W[snp.BENCH] == 0).all()
    assert (W[snp.BOND] == 0).all()


def test_materialize_long_only_gross_one_equal_weight():
    closes = _synthetic_closes()
    names = [c for c in closes.columns if c not in (snp.BENCH, snp.BOND)]
    cand = FactorCandidate("lv", "lowvol", 0, side="long", frac=0.20, vol_win=60,
                           mem_df=_mem_all(names))
    W = cand.materialize(closes)
    active = W[(W.abs().sum(axis=1) > 0)]
    assert len(active) > 0
    last = active.iloc[-1]
    held = last[last != 0]
    assert pytest.approx(held.sum(), rel=1e-6) == 1.0          # gross 1.0
    assert np.allclose(held.values, held.values[0])           # equal weight


def test_materialize_longshort_is_dollar_neutral():
    closes = _synthetic_closes()
    names = [c for c in closes.columns if c not in (snp.BENCH, snp.BOND)]
    cand = FactorCandidate("ls", "momentum", 60, skip=5, side="ls", frac=0.20,
                           mem_df=_mem_all(names))
    W = cand.materialize(closes)
    active = W[(W.abs().sum(axis=1) > 0)]
    last = active.iloc[-1]
    assert pytest.approx(last.sum(), abs=1e-9) == 0.0          # net dollar-neutral
    assert pytest.approx(last.abs().sum(), rel=1e-6) == 1.0    # gross ~1.0


def test_materialize_pit_membership_excludes_nonmembers():
    """A name that is NOT a member as-of t must never receive weight at t."""
    closes = _synthetic_closes()
    names = [c for c in closes.columns if c not in (snp.BENCH, snp.BOND)]
    # N00 only becomes a member AFTER the panel ends -> never eligible
    mem = _mem_all(names)
    mem.loc[mem["ticker"] == "N00", "start_date"] = pd.Timestamp("2099-01-01")
    cand = FactorCandidate("lv", "lowvol", 0, side="long", frac=0.50, vol_win=60,
                           mem_df=mem)
    W = cand.materialize(closes)
    assert (W["N00"] == 0).all(), "non-member received weight (PIT leak)"


def test_materialize_no_lookahead_weight_set_at_t_uses_only_past():
    """The weight row on rebalance date t must be computable from prices THROUGH t only:
    mutating any FUTURE price must not change the weights set on/before t."""
    closes = _synthetic_closes(seed=3)
    names = [c for c in closes.columns if c not in (snp.BENCH, snp.BOND)]
    cand = FactorCandidate("lv", "lowvol", 0, side="long", frac=0.20, vol_win=60,
                           mem_df=_mem_all(names))
    W0 = cand.materialize(closes)
    rebs = _rebalance_dates(closes.index)
    t = rebs[len(rebs) // 2]                                   # a mid-sample rebalance
    # perturb ALL prices strictly AFTER t
    perturbed = closes.copy()
    fut = perturbed.index > t
    perturbed.loc[fut, names] *= 1.5
    W1 = cand.materialize(perturbed)
    # weights on every date up to and including t must be identical (no future leakage)
    upto = W0.index[W0.index <= t]
    pd.testing.assert_frame_equal(W0.loc[upto], W1.loc[upto])


def test_lowvol_longs_the_low_vol_names():
    """Sanity: the low-vol factor should overweight the low-index (low-vol) names."""
    closes = _synthetic_closes(n_names=40, seed=5)
    names = [c for c in closes.columns if c not in (snp.BENCH, snp.BOND)]
    cand = FactorCandidate("lv", "lowvol", 0, side="long", frac=0.25, vol_win=60,
                           mem_df=_mem_all(names))
    W = cand.materialize(closes)
    last = W.iloc[-1]
    held = [c for c in names if last[c] > 0]
    held_idx = [int(c[1:]) for c in held]
    # held names should skew toward the low-vol end (low integer index)
    assert np.mean(held_idx) < (len(names) / 2), held_idx


def test_rebalance_dates_are_month_ends():
    idx = pd.date_range("2020-01-01", "2020-06-30", freq="B")
    rebs = _rebalance_dates(idx)
    assert len(rebs) == 6                                      # Jan..Jun
    for t in rebs:
        # each is the last business day of its month present in idx
        same_month = idx[(idx.year == t.year) & (idx.month == t.month)]
        assert t == same_month[-1]


def test_spec_hash_stable_and_membership_independent():
    """spec_hash must depend only on the spec, not on the membership table instance."""
    names = ["N00", "N01"]
    a = FactorCandidate("lv", "lowvol", 0, side="long", frac=0.2, vol_win=60,
                        mem_df=_mem_all(names))
    b = FactorCandidate("lv", "lowvol", 0, side="long", frac=0.2, vol_win=60,
                        mem_df=_mem_all(names, start="1999-01-01"))
    assert a.spec_hash == b.spec_hash
