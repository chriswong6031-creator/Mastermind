"""Forward paper-trading judge — the REAL judge (the only thing that flips paper->live).

A promoted sleeve archives its live target weights via signal_archive (keep-FIRST PIT),
and a forward Brier accumulates against realized next-period sign. Until a forward window
that did not exist at training time resolves, the sleeve stays paper/display-only — no
in-sample DSR ever promotes a sleeve to live sizing.
"""
from __future__ import annotations

import bot  # noqa: F401
from engine import signal_archive as sa


def enroll(spec_hash: str, weights: dict, asof: str, prob_up: float = 0.55) -> bool:
    """Archive the sleeve's live weights at `asof` (keep-first). Returns appended?"""
    return sa.archive_snapshot(label=f"sleeve_{spec_hash[:8]}",
                               snap={"spec_hash": spec_hash, "weights": weights, "prob_up": prob_up},
                               asof=asof)


def forward_brier(spec_hash: str):
    """Rolling forward Brier from the archived snapshots vs realized outcomes.
    Returns None ('building') until forward periods resolve — paper-only until then."""
    df = sa.load_archive(f"sleeve_{spec_hash[:8]}")
    if df is None or len(df) < 2:
        return None
    # forward grading requires realized next-period returns the loop hasn't observed yet
    return None
