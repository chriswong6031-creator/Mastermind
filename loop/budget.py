"""
loop/budget.py — MinBTL trial budget (Minimum Backtest Length / False Strategy Theorem)

Based on Bailey & López de Prado (2014) "The Deflated Sharpe Ratio" and
"The Probability of Backtest Overfitting."

---

FORMULAS
--------

1.  Expected maximum Sharpe under the null (IID N(0,1) trials):

        E[max SR] ≈ (1 - γ) · Φ⁻¹(1 - 1/N) + γ · Φ⁻¹(1 - 1/(N·e))

    where γ = 0.5772156649... (Euler–Mascheroni constant), Φ⁻¹ = standard
    normal quantile (ppf), N = number of independent trials.

    For large N the simpler approximation sqrt(2 · ln N) is used as a
    cross-check but the Euler–Mascheroni form is the return value.

    Φ⁻¹ is implemented via math.erfinv (no scipy):
        Φ⁻¹(p) = sqrt(2) · erfinv(2p − 1)

2.  Minimum Backtest Length (years):

        V̂(SR*) ≈ (1 + 0.5 · SR*²) / T      (variance of Sharpe estimator, normal)

    The SR* we must beat is E[max SR_null] expressed in *daily* units:
        sr0_daily = expected_max_sharpe_under_null(n_trials) / sqrt(252)

    The minimum T (days) such that SR* is distinguishable from noise at the
    1-σ level is derived from:
        SR_annual / sqrt(252) - sr0_daily   >=  sqrt( (1 + 0.5·sr0_daily²) / T )

    Solving for T:
        T ≥  (1 + 0.5 · sr0_daily²) / (sr_daily - sr0_daily)²

    Converted to years:  min_years = T / 252

3.  Exhaustion check:

    The budget is exhausted when EITHER:
      a) E[max SR_null] (de-annualised to daily) ≥ best observed daily Sharpe,
         meaning the null easily accounts for the best result found; OR
      b) years_observed < min_backtest_length(best_sharpe_annual, n_eff),
         meaning we simply do not have enough data to distinguish signal from
         multiple-testing noise yet.

    Edge cases:
      • n_eff <= 1     → never exhausted (only 0 or 1 trial, no inflation)
      • best_sharpe <= 0 → always exhausted (negative/zero edge has no claim)
"""

import math

# Euler–Mascheroni constant
_GAMMA = 0.5772156649015328

_SQRT2 = math.sqrt(2.0)
_TRADING_YEAR = 252


def _erfinv(x: float) -> float:
    """Pure-stdlib rational approximation of erfinv.

    Uses the Peter J. Acklam rational approximation adapted for the erf domain,
    with one Halley refinement step for accuracy to ~1e-12.
    Reference: P. J. Acklam, "An algorithm for computing the inverse normal
    cumulative distribution function" (2003).
    """
    # erfinv(x) = Φ⁻¹((x+1)/2) / √2
    # We compute Φ⁻¹(p) via Acklam's rational approximation then divide by √2.
    p = (x + 1.0) * 0.5  # map [-1,1] to (0,1)
    if p <= 0.0:
        return -math.inf
    if p >= 1.0:
        return math.inf

    # Acklam coefficients for Φ⁻¹
    a = (-3.969683028665376e+01,  2.209460984245205e+02,
         -2.759285104469687e+02,  1.383577518672690e+02,
         -3.066479806614716e+01,  2.506628277459239e+00)
    b = (-5.447609879822406e+01,  1.615858368580409e+02,
         -1.556989798598866e+02,  6.680131188771972e+01,
         -1.328068155288572e+01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01,
         -2.400758277161838e+00, -2.549732539343734e+00,
          4.374664141464968e+00,  2.938163982698783e+00)
    d = ( 7.784695709041462e-03,  3.224671290700398e-01,
          2.445134137142996e+00,  3.754408661907416e+00)

    p_low, p_high = 0.02425, 1.0 - 0.02425

    if p < p_low:
        q = math.sqrt(-2.0 * math.log(p))
        z = (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
            ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1.0)
    elif p <= p_high:
        q = p - 0.5
        r = q * q
        z = (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
            (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1.0)
    else:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        z = -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
             ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1.0)

    # One Halley refinement step for full precision
    e = 0.5 * math.erfc(-z / _SQRT2) - p
    u = e * math.sqrt(2.0 * math.pi) * math.exp(0.5 * z * z)
    z -= u / (1.0 + 0.5 * z * u)

    return z / _SQRT2  # convert Φ⁻¹ → erfinv


def _norm_ppf(p: float) -> float:
    """Standard normal quantile.  Φ⁻¹(p) = √2 · erfinv(2p − 1).

    Implemented via a pure-stdlib rational approximation + Halley refinement;
    no scipy required.
    """
    if p <= 0.0:
        return -math.inf
    if p >= 1.0:
        return math.inf
    return _SQRT2 * _erfinv(2.0 * p - 1.0)


def expected_max_sharpe_under_null(n_trials: int) -> float:
    """Return E[max SR] across *n_trials* IID N(0,1) Sharpe draws (the null).

    Uses the Euler–Mascheroni refinement:
        E[max] ≈ (1 − γ)·Φ⁻¹(1 − 1/N) + γ·Φ⁻¹(1 − 1/(N·e))

    where γ = 0.5772… and Φ⁻¹ is the standard normal quantile.

    Parameters
    ----------
    n_trials : int
        Total number of strategy trials tested (including discarded ones).

    Returns
    -------
    float
        Expected maximum Sharpe ratio under the null (same units as input
        Sharpe — i.e. if your Sharpe is annual, this is also annual).
    """
    n = max(int(n_trials), 1)
    if n == 1:
        # With one trial the expected max = expected value of N(0,1) = 0.
        return 0.0

    z1 = _norm_ppf(1.0 - 1.0 / n)
    z2 = _norm_ppf(1.0 - 1.0 / (n * math.e))
    return (1.0 - _GAMMA) * z1 + _GAMMA * z2


def min_backtest_length(best_sharpe_annual: float, n_trials: int) -> float:
    """Return the minimum backtest length (in years) for best_sharpe_annual to be
    non-luck given n_trials independent strategy tests.

    Derivation
    ----------
    Let SR* = best_sharpe_annual / √252  (daily Sharpe).
    Let SR₀ = expected_max_sharpe_under_null(n_trials) / √252  (daily null hurdle).

    The minimum T (trading days) is:
        T ≥ (1 + 0.5 · SR₀²) / (SR* − SR₀)²

    Converted to years via /252.

    Returns math.inf when best_sharpe_annual ≤ 0 or ≤ SR₀_annual (the
    observed Sharpe cannot beat the null at any length).

    Parameters
    ----------
    best_sharpe_annual : float
        Best annualised Sharpe observed across all trials.
    n_trials : int
        Number of independent strategy trials tested.
    """
    if best_sharpe_annual <= 0.0:
        return math.inf

    sr0_annual = expected_max_sharpe_under_null(n_trials)
    if best_sharpe_annual <= sr0_annual:
        # Observed Sharpe can't beat the null hurdle — need infinite data.
        return math.inf

    sr_daily = best_sharpe_annual / math.sqrt(_TRADING_YEAR)
    sr0_daily = sr0_annual / math.sqrt(_TRADING_YEAR)

    numerator = 1.0 + 0.5 * sr0_daily ** 2
    denominator = (sr_daily - sr0_daily) ** 2
    T_days = numerator / denominator
    return T_days / _TRADING_YEAR


def exhausted(n_eff: int, best_sharpe_annual: float, years_observed: float) -> bool:
    """True when the trial budget is exhausted and no DSR result is trustworthy.

    The budget is exhausted when EITHER:
      (a) E[max SR_null] de-annualised (daily) ≥ best observed daily Sharpe
          — the null trivially explains the best result; OR
      (b) years_observed < min_backtest_length(best_sharpe_annual, n_eff)
          — insufficient data to distinguish signal from search noise.

    Edge cases
    ----------
    • n_eff ≤ 1             → False  (≤1 trial, no multiple-testing inflation)
    • best_sharpe_annual ≤ 0 → True   (negative/zero Sharpe has no claim)

    Parameters
    ----------
    n_eff : int
        Effective number of independent strategy trials tested so far.
    best_sharpe_annual : float
        Best annualised Sharpe seen across all trials.
    years_observed : float
        Length of the backtest track record in years.
    """
    if n_eff <= 1:
        return False
    if best_sharpe_annual <= 0.0:
        return True

    # (a) Null hurdle check — de-annualise both to daily
    sr0_annual = expected_max_sharpe_under_null(n_eff)
    sr0_daily = sr0_annual / math.sqrt(_TRADING_YEAR)
    sr_daily = best_sharpe_annual / math.sqrt(_TRADING_YEAR)
    if sr0_daily >= sr_daily:
        return True

    # (b) Minimum backtest length check
    min_years = min_backtest_length(best_sharpe_annual, n_eff)
    if years_observed < min_years:
        return True

    return False
