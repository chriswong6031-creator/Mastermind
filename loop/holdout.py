"""
loop/holdout.py — Locked one-shot holdout guard.

Each candidate spec_hash is allowed exactly ONE look at the holdout slice.
Re-tuning a spec must produce a new hash, which then burns a fresh holdout
look and increments the trial counter.  This prevents p-hacking via repeated
peeks disguised as the same spec.

All I/O (persisting `touched`) is the caller's responsibility.
This module is PURE: stdlib + pandas only, no engine imports, no I/O.
"""

from __future__ import annotations

import pandas as pd


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------

class HoldoutBurned(Exception):
    """Raised when a spec_hash tries to peek at holdout a second time."""


# ---------------------------------------------------------------------------
# Touch guard
# ---------------------------------------------------------------------------

def touch(spec_hash: str, touched: set[str]) -> None:
    """
    Mark spec_hash as having consumed its one holdout look.

    Raises HoldoutBurned if already consumed; otherwise MUTATES `touched`
    in-place (caller persists it to the store).
    """
    if not isinstance(spec_hash, str) or not spec_hash:
        raise ValueError("spec_hash must be a non-empty str")
    if spec_hash in touched:
        raise HoldoutBurned(
            f"Holdout already consumed for spec {spec_hash!r}. "
            "Re-tune → new hash → fresh holdout look."
        )
    touched.add(spec_hash)


def is_burned(spec_hash: str, touched: set[str]) -> bool:
    """Return True if spec_hash has already consumed its holdout look."""
    return spec_hash in touched


# ---------------------------------------------------------------------------
# Index splitter
# ---------------------------------------------------------------------------

def split_index(
    index: pd.DatetimeIndex,
    holdout_start: "str | pd.Timestamp",
) -> tuple[pd.DatetimeIndex, pd.DatetimeIndex]:
    """
    Split a DatetimeIndex into (in_sample_idx, holdout_idx).

    in_sample_idx  : dates STRICTLY before holdout_start
    holdout_idx    : dates >= holdout_start

    Parameters
    ----------
    index : pd.DatetimeIndex
        The full date range to split.
    holdout_start : str or Timestamp
        The first date of the holdout period.  Anything before this is
        in-sample (exclusive upper bound).

    Returns
    -------
    (in_sample_idx, holdout_idx) — both pd.DatetimeIndex, may be empty.
    """
    cutoff = pd.Timestamp(holdout_start)
    in_sample = index[index < cutoff]
    holdout = index[index >= cutoff]
    return in_sample, holdout


# ---------------------------------------------------------------------------
# OOS confirmation gate
# ---------------------------------------------------------------------------

def confirms(
    is_sample_sharpe: float,
    holdout_sharpe: float,
    min_ratio: float = 0.5,
) -> bool:
    """
    Return True when the holdout performance confirms a real edge.

    Rules (both must hold):
      1. holdout_sharpe > 0          — edge did not flip negative OOS
      2. holdout_sharpe >= min_ratio * is_sample_sharpe
                                     — OOS decay stays within the tolerance

    A genuine edge survives OOS with partial decay; overfit strategies
    produce zero or negative holdout Sharpe.

    Parameters
    ----------
    is_sample_sharpe : float
        Annualised Sharpe on the in-sample period.
    holdout_sharpe : float
        Annualised Sharpe on the locked holdout period.
    min_ratio : float
        Minimum fraction of in-sample Sharpe that holdout must achieve.
        Default 0.5 (half-life tolerance).
    """
    if min_ratio < 0:
        raise ValueError("min_ratio must be >= 0")
    return holdout_sharpe > 0 and holdout_sharpe >= min_ratio * is_sample_sharpe
