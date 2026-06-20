"""
tests/test_cluster.py — Deterministic tests for loop/cluster.py.

Each test proves the math for a specific edge case using synthetic Series
so there are no external dependencies and results are fully reproducible.
"""

import numpy as np
import pandas as pd
import pytest

from loop.cluster import clusters, effective_n, assign_label, MIN_OVERLAP

# ---------------------------------------------------------------------------
# Helpers to build synthetic daily-return Series
# ---------------------------------------------------------------------------

def _dates(n: int = 252) -> pd.DatetimeIndex:
    return pd.date_range("2020-01-01", periods=n, freq="B")


def _series(values, n: int = 252, seed: int = 0) -> pd.Series:
    """Return a Series on a business-day index of length n."""
    dates = _dates(n)
    return pd.Series(values, index=dates)


def _randn(n: int = 252, seed: int = 0) -> pd.Series:
    """IID normal returns — a fresh independent draw."""
    rng = np.random.default_rng(seed)
    return _series(rng.standard_normal(n), n=n)


def _clone(base: pd.Series, noise: float = 0.0, seed: int = 99) -> pd.Series:
    """Return base + tiny noise so the two are highly correlated."""
    rng = np.random.default_rng(seed)
    return base + noise * rng.standard_normal(len(base))


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_empty_pool():
    """Empty pool → effective_n = 0, clusters = [], assign_label = {}."""
    assert effective_n({}) == 0
    assert clusters({}) == []
    assert assign_label({}) == {}


def test_single_candidate():
    """Single entry → effective_n = 1."""
    pool = {"a": _randn(seed=1)}
    assert effective_n(pool) == 1
    assert len(clusters(pool)) == 1
    assert assign_label(pool) == {"a": 0}


def test_all_identical_series():
    """
    Three copies of the same series have |corr| = 1.0 >= 0.90.
    All should collapse to 1 cluster → effective_n = 1.
    """
    base = _randn(seed=42)
    pool = {"h1": base.copy(), "h2": base.copy(), "h3": base.copy()}
    assert effective_n(pool) == 1
    grps = clusters(pool)
    assert len(grps) == 1
    assert sorted(grps[0]) == ["h1", "h2", "h3"]
    labels = assign_label(pool)
    # All share the same label
    assert len(set(labels.values())) == 1


def test_all_orthogonal_series():
    """
    Five truly independent random series should have low pairwise correlations
    (well below 0.90) → effective_n = 5 (each its own cluster).
    """
    pool = {f"h{i}": _randn(n=500, seed=i * 17) for i in range(5)}
    # Verify none exceed rho by construction (with 500 obs, random |corr| << 0.9)
    n = effective_n(pool, rho=0.90)
    assert n == 5
    assert len(clusters(pool, rho=0.90)) == 5
    lbl = assign_label(pool, rho=0.90)
    assert len(set(lbl.values())) == 5


def test_two_near_duplicate_one_independent():
    """
    h1 ≈ h2 (near-duplicate), h3 orthogonal.
    Expected: 2 clusters → effective_n = 2.
    """
    base = _randn(n=300, seed=7)
    clone = _clone(base, noise=0.02, seed=8)   # |corr| > 0.99
    indep = _randn(n=300, seed=99)

    pool = {"h1": base, "h2": clone, "h3": indep}
    n = effective_n(pool, rho=0.90)
    assert n == 2

    grps = clusters(pool, rho=0.90)
    # h1 and h2 are in the same group
    flat = {h: g for g, grp in enumerate(grps) for h in grp}
    assert flat["h1"] == flat["h2"]
    assert flat["h1"] != flat["h3"]


def test_short_overlap_treated_as_uncorrelated():
    """
    Two otherwise-identical series that share fewer than MIN_OVERLAP dates
    must NOT be merged — we treat the pair as uncorrelated.
    """
    base = _randn(n=252, seed=5)

    # h1: first 252 days; h2: last few days only (overlap < MIN_OVERLAP)
    h1 = base.copy()
    h2 = base.iloc[-(MIN_OVERLAP - 1):].copy()  # 29 shared points < 30

    pool = {"h1": h1, "h2": h2}
    # Despite being the same underlying series, overlap is too short to detect
    n = effective_n(pool, rho=0.90)
    assert n == 2   # kept as separate clusters


def test_exactly_min_overlap_is_used():
    """
    Two identical series sharing exactly MIN_OVERLAP dates should be merged
    (the threshold is >=, so 30 is included).
    """
    base = _randn(n=252, seed=13)

    h1 = base.copy()
    h2 = base.iloc[-MIN_OVERLAP:].copy()  # exactly 30 shared points

    pool = {"h1": h1, "h2": h2}
    n = effective_n(pool, rho=0.90)
    assert n == 1   # merged — corr = 1.0 on the 30 shared points


def test_rho_threshold_boundary():
    """
    Verify that the rho threshold is respected:
    - at rho=0.50 two moderately correlated series merge
    - at rho=0.95 the same pair stays separate
    """
    rng = np.random.default_rng(77)
    common = rng.standard_normal(300)
    noise1 = rng.standard_normal(300)
    noise2 = rng.standard_normal(300)

    # Mix: ~70% common factor → |corr| ≈ 0.70
    alpha = np.sqrt(0.70)
    beta  = np.sqrt(0.30)
    s1 = _series(alpha * common + beta * noise1, n=300)
    s2 = _series(alpha * common + beta * noise2, n=300)

    actual_corr = abs(float(np.corrcoef(s1.values, s2.values)[0, 1]))
    assert 0.55 < actual_corr < 0.85, f"Expected ~0.70 corr, got {actual_corr:.3f}"

    pool = {"h1": s1, "h2": s2}
    assert effective_n(pool, rho=0.50) == 1   # merged at loose threshold
    assert effective_n(pool, rho=0.95) == 2   # kept separate at tight threshold


def test_assign_label_consistency_with_clusters():
    """
    assign_label and clusters must be consistent:
    same cluster label ↔ same group in clusters().
    """
    base = _randn(n=300, seed=3)
    pool = {
        "a": base,
        "b": _clone(base, noise=0.01, seed=4),
        "c": _randn(n=300, seed=55),
        "d": _randn(n=300, seed=66),
    }
    grps = clusters(pool, rho=0.90)
    labels = assign_label(pool, rho=0.90)

    for grp in grps:
        grp_labels = {labels[h] for h in grp}
        assert len(grp_labels) == 1, f"Members of group {grp} have different labels"

    # Different groups must have different labels
    all_root_labels = [labels[grp[0]] for grp in grps]
    assert len(all_root_labels) == len(set(all_root_labels))


def test_zero_variance_series_not_merged():
    """
    A flat (zero-variance) series cannot be correlated with anything.
    It should remain in its own cluster rather than crashing.
    """
    normal = _randn(n=100, seed=21)
    flat   = _series(np.zeros(100), n=100)

    pool = {"good": normal, "flat": flat}
    n = effective_n(pool, rho=0.90)
    assert n == 2   # no merge; no exception raised


def test_transitive_merge():
    """
    Single-linkage is transitive: if A ~ B and B ~ C (but A may not directly
    meet the rho threshold with C), all three should land in the same cluster.

    Construction:
      - A = f1 + tiny noise  (nearly pure f1)
      - B = f1 + tiny noise  (also nearly pure f1; so A~B corr ~1.0)
      - C = B + tiny noise   (nearly pure B; so B~C corr ~1.0)
      The chain is A -- B -- C (each adjacent pair corr > 0.99).
    """
    rng = np.random.default_rng(42)
    n = 500

    f1  = rng.standard_normal(n)
    eps = 0.01  # tiny noise

    a_v = f1 + eps * rng.standard_normal(n)
    b_v = f1 + eps * rng.standard_normal(n)   # A and B both track f1
    c_v = b_v + eps * rng.standard_normal(n)  # C tracks B (hence also f1)

    dates = _dates(n)
    pool = {
        "A": pd.Series(a_v, index=dates),
        "B": pd.Series(b_v, index=dates),
        "C": pd.Series(c_v, index=dates),
    }

    corr_ab = abs(float(np.corrcoef(a_v, b_v)[0, 1]))
    corr_bc = abs(float(np.corrcoef(b_v, c_v)[0, 1]))

    # Sanity-check the construction; with eps=0.01 these will be ~0.9999
    assert corr_ab >= 0.90, f"A-B corr too low: {corr_ab:.4f}"
    assert corr_bc >= 0.90, f"B-C corr too low: {corr_bc:.4f}"

    n_eff = effective_n(pool, rho=0.90)
    # Single-linkage: the chain A-B-C collapses to 1 cluster
    assert n_eff == 1, (
        f"Expected 1 cluster via transitive merge, got {n_eff}. "
        f"corr_ab={corr_ab:.4f}, corr_bc={corr_bc:.4f}"
    )


def test_large_pool_performance():
    """
    50 candidates (mix of duplicates + orthogonal) should complete quickly
    and return the correct effective_n without error.
    Proves the O(N^2) implementation is acceptable for expected pool sizes.
    """
    import time

    base_series = [_randn(n=300, seed=i) for i in range(10)]
    pool = {}
    for i, base in enumerate(base_series):
        # Each base gets 5 near-duplicate clones → 10 clusters expected
        for j in range(5):
            key = f"base{i}_clone{j}"
            pool[key] = _clone(base, noise=0.01, seed=i * 100 + j)

    t0 = time.perf_counter()
    n = effective_n(pool, rho=0.90)
    elapsed = time.perf_counter() - t0

    assert n == 10, f"Expected 10 clusters, got {n}"
    assert elapsed < 2.0, f"Took too long: {elapsed:.2f}s for 50 candidates"
