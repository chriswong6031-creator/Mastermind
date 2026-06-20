"""
loop/pbo.py — Probability of Backtest Overfitting via CSCV
(Combinatorially-Symmetric Cross-Validation, Bailey / Lopez de Prado 2014)

PURE: numpy / pandas / stdlib only.  No engine import, no I/O, no DB.
"""
from __future__ import annotations

import math
from itertools import combinations
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

# ── helpers ──────────────────────────────────────────────────────────────────

_MAX_COMBOS = 2_000          # cap for C(S, S/2) enumeration


def _sharpe_block(block: pd.Series) -> float:
    """Annualised Sharpe-like metric on one time-block (252 trading days = 1 yr)."""
    n = len(block)
    if n < 2:
        return 0.0
    mu = block.mean()
    sd = block.std(ddof=1)
    if sd == 0.0 or not np.isfinite(sd):
        return 0.0
    # annualise by sqrt(252/block_len * n) = sqrt(252) * mu/sd
    return float(mu / sd * math.sqrt(252))


def _aligned_pool(pool: Dict[str, pd.Series]) -> Optional[pd.DataFrame]:
    """
    Align all candidate series to their common date index.
    Returns a DataFrame (rows = dates, cols = spec_hashes) or None if <2 candidates.
    """
    if len(pool) < 2:
        return None
    df = pd.DataFrame(pool)
    df = df.dropna(how="all")
    return df


def _build_perf_matrix(df: pd.DataFrame, S: int) -> np.ndarray:
    """
    Split the timeline into S equal blocks; compute Sharpe-like metric per
    (block, candidate).  Returns shape (S, n_candidates).
    """
    n_rows, n_cands = df.shape
    block_size = n_rows // S
    # Trim tail rows that don't fit evenly (consistent with CSCV literature)
    n_use = block_size * S
    trimmed = df.iloc[:n_use]

    M = np.empty((S, n_cands))
    for b in range(S):
        blk = trimmed.iloc[b * block_size: (b + 1) * block_size]
        for c in range(n_cands):
            M[b, c] = _sharpe_block(blk.iloc[:, c])
    return M


def _sample_combo_indices(S: int, half: int, max_combos: int) -> List[tuple]:
    """
    Return up to max_combos index-tuples for C(S, half).
    If total <= max_combos: return all.
    Else: sample deterministically by stride.
    """
    all_combos = list(combinations(range(S), half))
    total = len(all_combos)
    if total <= max_combos:
        return all_combos
    # Deterministic stride sample (not random, reproducible)
    step = total / max_combos
    indices = [int(i * step) for i in range(max_combos)]
    return [all_combos[i] for i in indices]


# ── public API ────────────────────────────────────────────────────────────────

def cscv(
    pool: Dict[str, pd.Series],
    S: int = 16,
) -> dict:
    """
    Probability of Backtest Overfitting via CSCV.

    Parameters
    ----------
    pool : dict[spec_hash -> daily-return Series]
        At least 2 candidates with overlapping dates.
    S    : int
        Number of time blocks to partition the shared timeline into.
        Must be even (CSCV requires S/2 train + S/2 test blocks).

    Returns
    -------
    dict with keys:
        pbo        : float in [0, 1] — fraction of IS-winners that rank below
                     OOS median (logit <= 0).  None if <2 candidates or too
                     few data points.
        n_splits   : int — number of IS/OOS splits enumerated (≤ C(S, S/2),
                     capped at _MAX_COMBOS).
        lambdas    : list[float] — logit of relative OOS rank for each split;
                     useful for plotting the distribution.
        sampled    : bool — True if the full C(S,S/2) was too large and we
                     sampled a deterministic subset.
        n_blocks   : int — actual S used (may be reduced if timeline is short).
        n_candidates : int
    """
    # ── guard: need ≥ 2 candidates
    df = _aligned_pool(pool)
    if df is None:
        return {
            "pbo": None, "n_splits": 0, "lambdas": [],
            "sampled": False, "n_blocks": 0, "n_candidates": len(pool),
        }

    n_rows, n_cands = df.shape

    # ── guard: S must be even and ≥ 2; reduce if timeline too short
    if S < 2:
        S = 2
    if S % 2 != 0:
        S -= 1  # make even
    # Need at least 1 return per block; reduce S until block_size >= 1
    while S > 2 and n_rows // S < 1:
        S -= 2
    if n_rows < 2:
        return {
            "pbo": None, "n_splits": 0, "lambdas": [],
            "sampled": False, "n_blocks": S, "n_candidates": n_cands,
        }

    # ── build performance matrix  shape (S, n_cands)
    M = _build_perf_matrix(df, S)

    half = S // 2
    combos = _sample_combo_indices(S, half, _MAX_COMBOS)
    n_splits = len(combos)
    total_combos = math.comb(S, half)
    sampled = total_combos > _MAX_COMBOS

    lambdas: List[float] = []

    for train_blocks in combos:
        train_set = set(train_blocks)
        oos_set = set(range(S)) - train_set

        # IS and OOS sub-matrices: average Sharpe across blocks
        is_scores = M[list(train_set), :].mean(axis=0)   # shape (n_cands,)
        oos_scores = M[list(oos_set), :].mean(axis=0)    # shape (n_cands,)

        # IS winner
        is_best = int(np.argmax(is_scores))

        # OOS rank of IS-winner (0 = worst, n_cands-1 = best)
        oos_winner_score = oos_scores[is_best]
        oos_rank = int(np.sum(oos_scores < oos_winner_score))
        # ties: use >= so rank counts those strictly below
        # relative rank in [0, 1]:  0 = worst, 1 = best (exclusive of self)
        # r_bar = oos_rank / (n_cands - 1) to map to (0,1)
        # logit(r_bar) ≤ 0  ⟺  r_bar ≤ 0.5  ⟺  IS-best ranks below median OOS

        if n_cands == 1:
            # degenerate; always 0.5 relative rank
            lam = 0.0
        else:
            r_bar = oos_rank / (n_cands - 1)
            # Clamp away from exact 0/1 to keep logit finite
            r_bar = np.clip(r_bar, 1e-8, 1 - 1e-8)
            lam = float(np.log(r_bar / (1.0 - r_bar)))

        lambdas.append(lam)

    if not lambdas:
        return {
            "pbo": None, "n_splits": 0, "lambdas": [],
            "sampled": sampled, "n_blocks": S, "n_candidates": n_cands,
        }

    # PBO = fraction of splits where IS-best ranks strictly below OOS median
    pbo = float(np.mean(np.array(lambdas) <= 0.0))

    return {
        "pbo": pbo,
        "n_splits": n_splits,
        "lambdas": lambdas,
        "sampled": sampled,
        "n_blocks": S,
        "n_candidates": n_cands,
    }
