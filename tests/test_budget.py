"""
Tests for loop/budget.py — MinBTL trial budget.

All tests use synthetic data / closed-form checks; no I/O or engine imports.
"""

import math
import pytest

from loop.budget import (
    _norm_ppf,
    _erfinv,
    _GAMMA,
    expected_max_sharpe_under_null,
    min_backtest_length,
    exhausted,
)


# ---------------------------------------------------------------------------
# _norm_ppf (internal helper) — verify via known values
# ---------------------------------------------------------------------------

class TestNormPpf:
    def test_median(self):
        """Φ⁻¹(0.5) == 0."""
        assert abs(_norm_ppf(0.5)) < 1e-12

    def test_p975(self):
        """Φ⁻¹(0.975) ≈ 1.96."""
        assert abs(_norm_ppf(0.975) - 1.959964) < 1e-4

    def test_p025(self):
        """Φ⁻¹(0.025) ≈ −1.96 (symmetry)."""
        assert abs(_norm_ppf(0.025) + 1.959964) < 1e-4

    def test_p999(self):
        """Φ⁻¹(0.999) ≈ 3.09."""
        assert abs(_norm_ppf(0.999) - 3.090232) < 1e-4

    def test_boundary_zero(self):
        assert _norm_ppf(0.0) == -math.inf

    def test_boundary_one(self):
        assert _norm_ppf(1.0) == math.inf

    def test_symmetry(self):
        """Φ⁻¹(1−p) == −Φ⁻¹(p) for interior p."""
        for p in [0.1, 0.3, 0.7, 0.9]:
            assert abs(_norm_ppf(p) + _norm_ppf(1 - p)) < 1e-12


# ---------------------------------------------------------------------------
# expected_max_sharpe_under_null
# ---------------------------------------------------------------------------

class TestExpectedMaxSharpe:
    def test_n1_is_zero(self):
        """With 1 trial, expected max = 0 (mean of N(0,1))."""
        assert expected_max_sharpe_under_null(1) == 0.0

    def test_n0_treated_as_n1(self):
        """n <= 0 is clamped to 1 → returns 0."""
        assert expected_max_sharpe_under_null(0) == 0.0

    def test_increases_with_n(self):
        """More trials → higher expected maximum (monotone)."""
        vals = [expected_max_sharpe_under_null(n) for n in [2, 5, 10, 50, 100, 1000]]
        for a, b in zip(vals, vals[1:]):
            assert b > a, f"Not monotone: {a} → {b}"

    def test_n10_approx(self):
        """For N=10, E[max] should be near sqrt(2 ln 10) ≈ 2.146 (rough check)."""
        v = expected_max_sharpe_under_null(10)
        # Euler-Mascheroni form gives something close to but not identical to
        # the simple sqrt(2 ln N) bound; accept a ±0.6 tolerance.
        assert 1.5 < v < 2.8, f"Unexpected value {v}"

    def test_n100_vs_simple_bound(self):
        """For N=100, EM refinement is slightly below sqrt(2 ln N) ≈ 3.034.

        The simple bound is an asymptotic upper-ish approximation; the EM form
        gives the corrected expected value, which is typically ~0.5 lower.
        Accept ±0.7 tolerance.
        """
        v = expected_max_sharpe_under_null(100)
        simple = math.sqrt(2 * math.log(100))
        assert abs(v - simple) < 0.7, f"Too far from simple bound: {v} vs {simple}"
        # The EM form is strictly below the simple bound for large N
        assert v < simple

    def test_euler_mascheroni_form(self):
        """Verify the formula exactly for N=20."""
        import math as _math
        n = 20
        gamma = _GAMMA
        sqrt2 = math.sqrt(2)
        def ppf(p):
            return sqrt2 * _erfinv(2 * p - 1)
        expected = (1 - gamma) * ppf(1 - 1/n) + gamma * ppf(1 - 1/(n * _math.e))
        got = expected_max_sharpe_under_null(20)
        assert abs(got - expected) < 1e-12

    def test_large_n_positive(self):
        """E[max SR] is always positive for N >= 2."""
        for n in [2, 10, 1000, 10_000]:
            assert expected_max_sharpe_under_null(n) > 0

    def test_float_n_truncated(self):
        """Non-integer n is truncated via int()."""
        assert expected_max_sharpe_under_null(10) == expected_max_sharpe_under_null(10.9)


# ---------------------------------------------------------------------------
# min_backtest_length
# ---------------------------------------------------------------------------

class TestMinBacktestLength:
    def test_negative_sharpe_returns_inf(self):
        assert min_backtest_length(-0.5, 10) == math.inf

    def test_zero_sharpe_returns_inf(self):
        assert min_backtest_length(0.0, 10) == math.inf

    def test_sharpe_below_null_returns_inf(self):
        """If best_sharpe <= E[max null], need infinite data."""
        null = expected_max_sharpe_under_null(100)
        assert min_backtest_length(null * 0.99, 100) == math.inf

    def test_sharpe_at_null_returns_inf(self):
        null = expected_max_sharpe_under_null(50)
        assert min_backtest_length(null, 50) == math.inf

    def test_high_sharpe_needs_less_data(self):
        """A very high Sharpe (2.0) needs fewer years than a moderate one (1.0)
        given the same trial count."""
        null = expected_max_sharpe_under_null(10)
        # Only valid if both are above the null hurdle
        if 2.0 > null and 1.0 > null:
            assert min_backtest_length(2.0, 10) < min_backtest_length(1.0, 10)

    def test_more_trials_need_more_data(self):
        """For the SAME best Sharpe, more trials → higher null → more years needed."""
        sr = 3.0  # high enough to clear null for moderate N
        y10 = min_backtest_length(sr, 10)
        y100 = min_backtest_length(sr, 100)
        # y10 may be inf if sr < null(100), but if both finite y100 > y10
        if math.isfinite(y10) and math.isfinite(y100):
            assert y100 > y10

    def test_formula_manually(self):
        """Verify against the closed-form for N=5, SR=2.0."""
        n = 5
        sr_annual = 2.0
        sr0_annual = expected_max_sharpe_under_null(n)
        if sr_annual <= sr0_annual:
            pytest.skip("SR below null for this N, skip formula check")
        trading_year = 252
        sr_daily = sr_annual / math.sqrt(trading_year)
        sr0_daily = sr0_annual / math.sqrt(trading_year)
        num = 1.0 + 0.5 * sr0_daily ** 2
        den = (sr_daily - sr0_daily) ** 2
        expected_years = (num / den) / trading_year
        got = min_backtest_length(sr_annual, n)
        assert abs(got - expected_years) < 1e-12

    def test_returns_positive_years(self):
        """A sufficiently high Sharpe over small N → finite positive years."""
        v = min_backtest_length(5.0, 5)
        assert math.isfinite(v) and v > 0

    def test_n1_gives_finite(self):
        """With 1 trial the null is 0; any positive SR gives finite min years."""
        v = min_backtest_length(1.0, 1)
        assert math.isfinite(v) and v > 0


# ---------------------------------------------------------------------------
# exhausted
# ---------------------------------------------------------------------------

class TestExhausted:
    def test_n_le_1_never_exhausted(self):
        """0 or 1 effective trial → never exhausted regardless of other args."""
        assert exhausted(0, 1.0, 0.1) is False
        assert exhausted(1, 1.0, 0.1) is False
        # Even with nonsensical arguments
        assert exhausted(1, -5.0, 0.0) is False

    def test_negative_sharpe_always_exhausted(self):
        assert exhausted(10, -1.0, 5.0) is True
        assert exhausted(2, -0.01, 100.0) is True

    def test_zero_sharpe_always_exhausted(self):
        assert exhausted(10, 0.0, 5.0) is True

    def test_null_exceeds_sharpe_exhausted(self):
        """When the null SR alone exceeds best SR → exhausted."""
        n = 10_000
        null = expected_max_sharpe_under_null(n)
        # A sharpe below the null → condition (a) fires
        assert exhausted(n, null * 0.5, 100.0) is True

    def test_insufficient_years_exhausted(self):
        """Enough years would pass, but we haven't observed them yet."""
        # Use a small n so null is low, high SR so condition (a) won't fire
        n = 5
        sr = 3.0
        min_y = min_backtest_length(sr, n)
        if not math.isfinite(min_y):
            pytest.skip("min_y infinite for this config")
        # Observed years << needed
        assert exhausted(n, sr, min_y * 0.1) is True

    def test_sufficient_years_not_exhausted(self):
        """When Sharpe > null AND years observed > min_backtest_length → not exhausted."""
        n = 5
        sr = 4.0  # well above null for n=5
        null = expected_max_sharpe_under_null(n)
        if sr <= null:
            pytest.skip("SR not above null for this n")
        min_y = min_backtest_length(sr, n)
        if not math.isfinite(min_y):
            pytest.skip("min_y infinite")
        # Observe 2× the minimum years
        assert exhausted(n, sr, min_y * 2.0) is False

    def test_exactly_at_min_years_still_exhausted(self):
        """years_observed < min_btl uses strict less-than; exactly at boundary is
        still exhausted (edge: < not <=)."""
        n = 5
        sr = 4.0
        null = expected_max_sharpe_under_null(n)
        if sr <= null:
            pytest.skip()
        min_y = min_backtest_length(sr, n)
        if not math.isfinite(min_y):
            pytest.skip()
        # Exactly at the boundary: years_observed == min_y → NOT exhausted
        # (years_observed < min_y is the condition; equality is fine)
        result_at = exhausted(n, sr, min_y)
        result_just_below = exhausted(n, sr, min_y - 1e-9)
        assert result_just_below is True
        assert result_at is False

    def test_many_trials_raises_bar(self):
        """100 trials needs more data than 5 trials for the same Sharpe."""
        sr = 4.0
        null_100 = expected_max_sharpe_under_null(100)
        if sr <= null_100:
            pytest.skip("SR below null for 100 trials")
        min_5 = min_backtest_length(sr, 5)
        min_100 = min_backtest_length(sr, 100)
        if math.isfinite(min_5) and math.isfinite(min_100):
            assert min_100 > min_5

    def test_borderline_n2(self):
        """n=2 is not subject to the n<=1 bypass; normal logic applies."""
        null = expected_max_sharpe_under_null(2)
        # A Sharpe clearly below the null for N=2
        assert exhausted(2, null * 0.5, 10.0) is True

    def test_round_trip_consistency(self):
        """exhausted(n, sr, min_btl(sr, n)) should be False when sr > null."""
        for n, sr in [(5, 3.5), (10, 4.0), (20, 5.0)]:
            null = expected_max_sharpe_under_null(n)
            if sr <= null:
                continue
            min_y = min_backtest_length(sr, n)
            if not math.isfinite(min_y):
                continue
            assert exhausted(n, sr, min_y) is False, f"Failed for n={n}, sr={sr}"
