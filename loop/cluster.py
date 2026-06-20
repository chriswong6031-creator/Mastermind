"""
loop/cluster.py — Effective-N clustering for the self-improving backtest loop.

PURE module: numpy / pandas / stdlib only.  No engine import, no I/O, no DB.

Public API
----------
effective_n(pool, rho=0.90) -> int
    Number of distinct bets after collapsing near-duplicate return series.
    Feeds deflated_sharpe(n_trials=n_eff) — over-counting duplicates would
    dishonestly *weaken* the DSR haircut.

clusters(pool, rho=0.90) -> list[list[str]]
    The actual groups (each is a list of spec_hash strings).

assign_label(pool, rho=0.90) -> dict[str, int]
    Maps every spec_hash to its cluster label (0-based integer).

Algorithm
---------
Single-linkage agglomeration on |corr| >= rho over shared-date aligned windows.
Pairs with fewer than 30 overlapping observations are treated as uncorrelated
(kept in separate clusters, conservative — avoids spurious merges on short
overlap).

Correlation matrix is built only once (O(N²) pairs); union-find merges in O(N²)
worst-case but N is expected to stay in the low hundreds.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from typing import Dict, List


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

MIN_OVERLAP = 30  # minimum shared dates to compute a meaningful correlation


def _pairwise_corr(pool: Dict[str, pd.Series]) -> Dict[tuple, float]:
    """
    Return {(h1, h2): abs_corr} for all pairs where overlap >= MIN_OVERLAP.
    Pairs below MIN_OVERLAP threshold are omitted (treated as corr=0 / uncorrelated).
    """
    hashes = list(pool.keys())
    n = len(hashes)
    corr_map: Dict[tuple, float] = {}

    for i in range(n):
        for j in range(i + 1, n):
            ha, hb = hashes[i], hashes[j]
            sa, sb = pool[ha], pool[hb]

            # Align on shared dates
            shared = sa.index.intersection(sb.index)
            if len(shared) < MIN_OVERLAP:
                continue  # treat as uncorrelated — leave out of map

            va = sa.loc[shared].values.astype(float)
            vb = sb.loc[shared].values.astype(float)

            # Guard: zero-variance series cannot be correlated (e.g. flat mock)
            if va.std() == 0 or vb.std() == 0:
                continue

            c = float(np.corrcoef(va, vb)[0, 1])
            if np.isfinite(c):
                corr_map[(ha, hb)] = abs(c)

    return corr_map


# ---------------------------------------------------------------------------
# Union-Find (path compression + union by rank)
# ---------------------------------------------------------------------------

class _UF:
    def __init__(self, keys: List[str]) -> None:
        self._parent: Dict[str, str] = {k: k for k in keys}
        self._rank: Dict[str, int] = {k: 0 for k in keys}

    def find(self, x: str) -> str:
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]  # path halving
            x = self._parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self._rank[ra] < self._rank[rb]:
            ra, rb = rb, ra
        self._parent[rb] = ra
        if self._rank[ra] == self._rank[rb]:
            self._rank[ra] += 1

    def groups(self) -> List[List[str]]:
        from collections import defaultdict
        buckets: Dict[str, List[str]] = defaultdict(list)
        for k in self._parent:
            buckets[self.find(k)].append(k)
        return [sorted(v) for v in buckets.values()]

    def labels(self) -> Dict[str, int]:
        grps = self.groups()
        out: Dict[str, int] = {}
        for idx, grp in enumerate(grps):
            for k in grp:
                out[k] = idx
        return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def clusters(pool: Dict[str, pd.Series], rho: float = 0.90) -> List[List[str]]:
    """
    Single-linkage agglomeration: merge two candidates if |corr| >= rho.

    Returns a list of groups; each group is a list of spec_hash strings.
    Empty pool → [].  Single candidate → [[hash]].
    """
    if not pool:
        return []

    keys = list(pool.keys())
    uf = _UF(keys)

    corr_map = _pairwise_corr(pool)
    for (ha, hb), c in corr_map.items():
        if c >= rho:
            uf.union(ha, hb)

    return uf.groups()


def effective_n(pool: Dict[str, pd.Series], rho: float = 0.90) -> int:
    """
    Number of distinct clusters (effective independent bets) in pool.

    Feeds deflated_sharpe(n_trials=effective_n(pool)) so that near-duplicate
    sleeves count as ONE trial rather than many, keeping the DSR haircut honest.

    Edge cases:
      pool={}   → 0
      pool size 1 → 1
      all identical series → 1
      all orthogonal series → len(pool)
    """
    return len(clusters(pool, rho=rho))


def assign_label(pool: Dict[str, pd.Series], rho: float = 0.90) -> Dict[str, int]:
    """
    Map every spec_hash in pool to a 0-based integer cluster label.

    Two hashes with the same label are near-duplicates (|corr| >= rho).
    """
    if not pool:
        return {}

    keys = list(pool.keys())
    uf = _UF(keys)

    corr_map = _pairwise_corr(pool)
    for (ha, hb), c in corr_map.items():
        if c >= rho:
            uf.union(ha, hb)

    return uf.labels()
