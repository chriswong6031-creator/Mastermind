"""
loop/promote.py — Promotion gate for the self-improving backtest loop.

gate(metrics, *, fdr_reject, pbo, holdout_confirms, budget_exhausted) -> dict

Decision stages
---------------
'refused'  — budget_exhausted is True; the trial budget is spent and DSR is no
             longer trustworthy regardless of how good the numbers look.  Every
             candidate gets this verdict; no score-based checks are run.

'paper'    — ALL of the following must hold:
               1. dsr_verdict == 'SURVIVES' OR dsr >= 0.95
               2. beats_spy is True
               3. beats_6040 is True
               4. crisis_pass is True
               5. fdr_reject is True  (BH FDR test rejects the null for this spec)
               6. fold_robust is True
               7. pbo is None OR pbo < 0.50  (probability of overfitting)
               8. holdout_confirms is True

'rejected' — at least one check failed; the dict includes the first failing
             check name under 'reason'.

Promotion to 'live-display' is a SEPARATE later transition gated ONLY on
forward paper Brier score — never on this in-sample gate.  Do not conflate
the two.

PURE module: numpy/pandas/stdlib only.  No engine import, no I/O, no DB.
The orchestrator wires the store and engine; this module only decides.
"""

from __future__ import annotations

_SURVIVES_PREFIX = "SURVIVES"


def gate(
    metrics: dict,
    *,
    fdr_reject: bool,
    pbo: float | None,
    holdout_confirms: bool,
    budget_exhausted: bool,
) -> dict:
    """
    Evaluate whether a candidate strategy should be promoted to paper trading.

    Parameters
    ----------
    metrics : dict
        Must contain at minimum:
            dsr         : float  — Deflated Sharpe Ratio (0-1)
            dsr_verdict : str    — result of dsr_verdict(); 'SURVIVES...' passes
            sharpe      : float
            maxdd       : float
            beats_spy   : bool
            beats_6040  : bool
            crisis_pass : bool
            fold_robust : bool
            p_value     : float
    fdr_reject : bool
        True if the BH FDR procedure rejects the null for this specification.
    pbo : float | None
        Probability of overfitting from combinatorial purged CV; None means the
        test was not run (treated as passing — unknown is not a failure).
    holdout_confirms : bool
        True if a held-out (never-touched) date range confirmed the edge.
    budget_exhausted : bool
        True if the trial budget for this search run is spent.  If True, the
        function returns 'refused' immediately without inspecting metrics.

    Returns
    -------
    dict with keys:
        stage  : 'paper' | 'refused' | 'rejected'
        reason : str describing the decision
        checks : dict[str, bool] — per-rule pass/fail for the UI
    """
    # ------------------------------------------------------------------
    # Hard gate: budget exhausted means DSR is no longer trustworthy.
    # Return immediately regardless of any score.
    # ------------------------------------------------------------------
    if budget_exhausted:
        return {
            "stage": "refused",
            "reason": "trial budget exhausted — DSR no longer trustworthy",
            "checks": {},
        }

    # ------------------------------------------------------------------
    # Build the per-rule boolean map.
    # ------------------------------------------------------------------
    dsr = metrics.get("dsr", 0.0)
    dsr_verdict_str = metrics.get("dsr_verdict", "")
    dsr_passes = dsr_verdict_str.startswith(_SURVIVES_PREFIX) or dsr >= 0.95

    checks: dict[str, bool] = {
        "dsr_survives":      dsr_passes,
        "beats_spy":         bool(metrics.get("beats_spy", False)),
        "beats_6040":        bool(metrics.get("beats_6040", False)),
        "crisis_pass":       bool(metrics.get("crisis_pass", False)),
        "fdr_reject":        bool(fdr_reject),
        "fold_robust":       bool(metrics.get("fold_robust", False)),
        "pbo_ok":            pbo is None or pbo < 0.50,
        "holdout_confirms":  bool(holdout_confirms),
    }

    # ------------------------------------------------------------------
    # All checks must pass for promotion to paper.
    # ------------------------------------------------------------------
    for name, passed in checks.items():
        if not passed:
            return {
                "stage": "rejected",
                "reason": f"failed check: {name}",
                "checks": checks,
            }

    return {
        "stage": "paper",
        "reason": "all gates passed — promoted to paper trading",
        "checks": checks,
    }
