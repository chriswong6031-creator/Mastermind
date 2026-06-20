"""
tests/test_pbo.py — deterministic tests for loop/pbo.py

All tests use synthetic data so maths can be verified by hand.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from loop.pbo import cscv, _sharpe_block, _build_perf_matrix, _sample_combo_indices


# ── fixtures ──────────────────────────────────────────────────────────────────

DATE_RANGE = pd.date_range("2020-01-02", periods=500, freq="B")


def _series(seed: int, mu: float = 0.0, sigma: float = 0.01) -> pd.Series:
    rng = np.random.default_rng(seed)
    return pd.Series(rng.normal(mu, sigma, len(DATE_RANGE)), index=DATE_RANGE)


def _pool(*args) -> dict:
    """Build a pool from (seed, mu, sigma) tuples."""
    return {f"cand_{i}": _series(seed, mu, sigma) for i, (seed, mu, sigma) in enumerate(args)}


# ── unit: _sharpe_block ───────────────────────────────────────────────────────

class TestSharpeBlock:
    def test_positive_mean_gives_positive_sharpe(self):
        # Must use noisy series (constant → std=0 → guarded → 0)
        rng = np.random.default_rng(0)
        s = pd.Series(rng.normal(0.01, 0.01, 252))
        assert _sharpe_block(s) > 0

    def test_zero_std_returns_zero(self):
        # Constant series: std=0 → guard returns 0
        s = pd.Series([0.0] * 50)
        assert _sharpe_block(s) == 0.0

    def test_length_one_returns_zero(self):
        s = pd.Series([0.05])
        assert _sharpe_block(s) == 0.0

    def test_known_value(self):
        # Non-constant series with large positive mean → Sharpe >> 0
        rng = np.random.default_rng(0)
        s = pd.Series(rng.normal(0.01, 0.01, 252))
        sh = _sharpe_block(s)
        assert sh > 0  # positive mean → positive Sharpe
        # Verify approximate magnitude: annualised Sharpe ≈ 1.0 for mu/σ=1 per day
        # For mu=0.01, sd≈0.01, sqrt(252)*1 ≈ 15.87; allow wide tolerance for noise
        assert sh > 5.0

    def test_negative_mean_gives_negative_sharpe(self):
        rng = np.random.default_rng(1)
        s = pd.Series(rng.normal(-0.01, 0.01, 252))
        assert _sharpe_block(s) < 0


# ── unit: _sample_combo_indices ───────────────────────────────────────────────

class TestSampleComboIndices:
    def test_small_s_returns_all(self):
        combos = _sample_combo_indices(4, 2, 2000)
        # C(4,2) = 6
        assert len(combos) == 6

    def test_capped_at_max(self):
        # C(16, 8) = 12870 > 2000; should return exactly 2000
        combos = _sample_combo_indices(16, 8, 2000)
        assert len(combos) == 2000

    def test_deterministic(self):
        c1 = _sample_combo_indices(16, 8, 2000)
        c2 = _sample_combo_indices(16, 8, 2000)
        assert c1 == c2

    def test_no_duplicates_when_small(self):
        combos = _sample_combo_indices(6, 3, 2000)
        assert len(combos) == len(set(combos))


# ── unit: _build_perf_matrix ─────────────────────────────────────────────────

class TestBuildPerfMatrix:
    def test_shape(self):
        df = pd.DataFrame({
            "a": _series(0),
            "b": _series(1),
            "c": _series(2),
        })
        M = _build_perf_matrix(df, S=4)
        assert M.shape == (4, 3)

    def test_strong_positive_candidate_scores_higher(self):
        """Candidate with high mu should have higher block Sharpe than flat one."""
        rng_a = np.random.default_rng(10)
        rng_b = np.random.default_rng(11)
        data_a = rng_a.normal(0.001, 0.01, 200)   # near-zero
        data_b = rng_b.normal(0.05,  0.01, 200)   # strongly positive daily
        df = pd.DataFrame({"a": data_a, "b": data_b})
        M = _build_perf_matrix(df, S=4)
        # Every block: col 1 (strong) should beat col 0 (weak)
        assert (M[:, 1] > M[:, 0]).all()


# ── integration: cscv ────────────────────────────────────────────────────────

class TestCSCV:
    def test_returns_none_for_single_candidate(self):
        pool = {"only": _series(0)}
        res = cscv(pool, S=8)
        assert res["pbo"] is None
        assert res["n_candidates"] == 1

    def test_returns_none_for_empty_pool(self):
        res = cscv({}, S=8)
        assert res["pbo"] is None

    def test_pbo_in_unit_interval(self):
        pool = _pool((0, 0.0, 0.01), (1, 0.0, 0.01), (2, 0.0, 0.01))
        res = cscv(pool, S=8)
        assert res["pbo"] is not None
        assert 0.0 <= res["pbo"] <= 1.0

    def test_lambdas_length_matches_n_splits(self):
        pool = _pool((0, 0.0, 0.01), (1, 0.0, 0.01))
        res = cscv(pool, S=4)
        assert len(res["lambdas"]) == res["n_splits"]
        # C(4,2) = 6 → exactly 6 splits
        assert res["n_splits"] == 6

    def test_high_pbo_for_noise_pool(self):
        """
        A pool of pure noise candidates: IS selection is random, so OOS rank
        is near-uniform → PBO should be close to 0.5.  We check it's > 0.30
        (strict 0.5 would be fragile; any reasonable noise pool should be >0.3).
        """
        rng = np.random.default_rng(42)
        noise_pool = {
            f"c{i}": pd.Series(rng.normal(0, 0.01, 500), index=DATE_RANGE)
            for i in range(10)
        }
        res = cscv(noise_pool, S=8)
        assert res["pbo"] is not None
        assert res["pbo"] >= 0.30

    def test_low_pbo_for_dominant_winner(self):
        """
        One candidate has a very high Sharpe; it should win IS AND OOS reliably.
        PBO should be low (the selection procedure is not overfitting).
        """
        rng = np.random.default_rng(99)
        # 9 noise + 1 very strong signal
        pool: dict = {
            f"noise_{i}": pd.Series(rng.normal(0.0, 0.01, 500), index=DATE_RANGE)
            for i in range(9)
        }
        pool["winner"] = pd.Series(rng.normal(0.005, 0.01, 500), index=DATE_RANGE)
        res = cscv(pool, S=8)
        assert res["pbo"] is not None
        assert res["pbo"] < 0.50, f"Expected low PBO for dominant winner, got {res['pbo']}"

    def test_s16_caps_at_2000_splits(self):
        """C(16,8)=12870 > 2000 → sampled=True, n_splits=2000."""
        pool = _pool(*([(i, 0.0, 0.01) for i in range(5)]))
        res = cscv(pool, S=16)
        assert res["sampled"] is True
        assert res["n_splits"] == 2000

    def test_s_reduced_for_short_series(self):
        """Very short series: S should be auto-reduced to fit."""
        short = {
            "a": pd.Series([0.01, -0.01, 0.005, -0.005, 0.002, -0.002,
                            0.003, 0.001, -0.002, 0.004]),
            "b": pd.Series([0.002, 0.003, -0.001, 0.001, -0.003, 0.002,
                            -0.001, 0.004, -0.002, 0.001]),
        }
        # S=16 would give block_size=0 → should auto-reduce without error
        res = cscv(short, S=16)
        # Either completes or returns pbo=None gracefully, but must not throw
        assert "pbo" in res

    def test_odd_s_made_even(self):
        """Odd S must be rounded down to even (CSCV requires equal splits)."""
        pool = _pool((0, 0.0, 0.01), (1, 0.0, 0.01))
        res_odd  = cscv(pool, S=5)
        res_even = cscv(pool, S=4)
        # Both should use n_blocks=4; n_splits should match
        assert res_odd["n_blocks"] == 4
        assert res_even["n_blocks"] == 4
        assert res_odd["n_splits"] == res_even["n_splits"]

    def test_two_candidates_structure(self):
        """Two candidates: logit is either >0 or ≤0, checking structure."""
        pool = {
            "a": pd.Series(np.random.default_rng(7).normal(0, 0.01, 500), index=DATE_RANGE),
            "b": pd.Series(np.random.default_rng(8).normal(0, 0.01, 500), index=DATE_RANGE),
        }
        res = cscv(pool, S=4)
        assert res["n_candidates"] == 2
        # Each lambda must be finite
        assert all(math.isfinite(lam) for lam in res["lambdas"])

    def test_unaligned_series_handled(self):
        """Candidates with partial date overlap: aligned automatically."""
        idx_a = pd.date_range("2020-01-02", periods=400, freq="B")
        idx_b = pd.date_range("2020-06-01", periods=400, freq="B")
        pool = {
            "a": pd.Series(np.random.default_rng(3).normal(0, 0.01, 400), index=idx_a),
            "b": pd.Series(np.random.default_rng(4).normal(0, 0.01, 400), index=idx_b),
        }
        # After alignment, overlap is the intersection; pbo may be None if too short
        res = cscv(pool, S=8)
        assert "pbo" in res   # must not raise

    def test_n_blocks_reported(self):
        pool = _pool((0, 0.0, 0.01), (1, 0.0, 0.01))
        res = cscv(pool, S=8)
        assert res["n_blocks"] == 8

    def test_pbo_0_5_interpretation(self):
        """
        Docstring check: PBO >= 0.50 means the selection procedure is mining noise.
        The module returns a float; verify that 0.5 threshold is meaningful.
        """
        # This is a semantic test — just verify pbo is a proper float
        pool = _pool((0, 0.0, 0.01), (1, 0.0, 0.01), (2, 0.0, 0.01))
        res = cscv(pool, S=4)
        pbo = res["pbo"]
        assert isinstance(pbo, float)
        # PBO of 0.5 exactly or above = noise-mining signal
        assert pbo >= 0.0
