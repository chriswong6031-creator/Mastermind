"""
tests/test_promote.py — Deterministic tests for loop/promote.gate().

Coverage:
  - Full pass  -> stage='paper'
  - budget_exhausted always -> 'refused', even with perfect metrics
  - Each individual failing check -> stage='rejected', reason names the check
  - pbo boundary (== 0.50 fails; None passes; 0.49 passes)
  - dsr_verdict fallback: dsr>=0.95 passes without SURVIVES prefix
  - checks dict is populated on rejection but empty on refused
"""

import pytest
from loop.promote import gate

# ---------------------------------------------------------------------------
# Baseline "perfect" metrics — all in-sample checks pass
# ---------------------------------------------------------------------------
PERFECT_METRICS = {
    "dsr": 0.97,
    "dsr_verdict": "SURVIVES multiple-testing (DSR>=0.95)",
    "sharpe": 1.2,
    "maxdd": -12.0,
    "beats_spy": True,
    "beats_6040": True,
    "crisis_pass": True,
    "fold_robust": True,
    "p_value": 0.01,
}

PERFECT_KWARGS = dict(
    fdr_reject=True,
    pbo=0.30,
    holdout_confirms=True,
    budget_exhausted=False,
)


def _gate(**overrides):
    """Call gate() with perfect baseline; override specific kwargs."""
    metrics = {**PERFECT_METRICS, **overrides.pop("metrics", {})}
    kwargs = {**PERFECT_KWARGS, **overrides}
    return gate(metrics, **kwargs)


# ---------------------------------------------------------------------------
# 1. Full pass -> paper
# ---------------------------------------------------------------------------
class TestFullPass:
    def test_stage_is_paper(self):
        result = _gate()
        assert result["stage"] == "paper"

    def test_all_checks_true(self):
        result = _gate()
        checks = result["checks"]
        assert all(checks.values()), f"Expected all True, got {checks}"

    def test_reason_mentions_paper(self):
        result = _gate()
        assert "paper" in result["reason"].lower()

    def test_checks_dict_has_eight_keys(self):
        result = _gate()
        assert len(result["checks"]) == 8


# ---------------------------------------------------------------------------
# 2. budget_exhausted -> 'refused' regardless of metrics / kwargs
# ---------------------------------------------------------------------------
class TestBudgetExhausted:
    def test_refused_on_perfect_metrics(self):
        result = gate(PERFECT_METRICS, **{**PERFECT_KWARGS, "budget_exhausted": True})
        assert result["stage"] == "refused"

    def test_refused_reason_contains_dsr(self):
        result = gate(PERFECT_METRICS, **{**PERFECT_KWARGS, "budget_exhausted": True})
        assert "DSR" in result["reason"]

    def test_checks_is_empty_when_refused(self):
        result = gate(PERFECT_METRICS, **{**PERFECT_KWARGS, "budget_exhausted": True})
        assert result["checks"] == {}

    def test_refused_even_with_terrible_metrics(self):
        bad = {k: False for k in PERFECT_METRICS}
        bad.update({"dsr": 0.0, "dsr_verdict": "FAILS", "sharpe": -1.0, "maxdd": -80.0, "p_value": 0.99})
        result = gate(bad, fdr_reject=False, pbo=0.99, holdout_confirms=False, budget_exhausted=True)
        assert result["stage"] == "refused"

    def test_refused_even_fdr_false(self):
        result = gate(PERFECT_METRICS, fdr_reject=False, pbo=None, holdout_confirms=True, budget_exhausted=True)
        assert result["stage"] == "refused"


# ---------------------------------------------------------------------------
# 3. Single failing check -> rejected, reason names it
# ---------------------------------------------------------------------------
class TestSingleFailingCheck:
    def test_fails_dsr_verdict(self):
        r = _gate(metrics={"dsr": 0.80, "dsr_verdict": "FAILS multiple-testing haircut (DSR<0.90)"})
        assert r["stage"] == "rejected"
        assert "dsr_survives" in r["reason"]

    def test_fails_beats_spy(self):
        r = _gate(metrics={"beats_spy": False})
        assert r["stage"] == "rejected"
        assert "beats_spy" in r["reason"]

    def test_fails_beats_6040(self):
        r = _gate(metrics={"beats_6040": False})
        assert r["stage"] == "rejected"
        assert "beats_6040" in r["reason"]

    def test_fails_crisis_pass(self):
        r = _gate(metrics={"crisis_pass": False})
        assert r["stage"] == "rejected"
        assert "crisis_pass" in r["reason"]

    def test_fails_fdr_reject(self):
        r = _gate(fdr_reject=False)
        assert r["stage"] == "rejected"
        assert "fdr_reject" in r["reason"]

    def test_fails_fold_robust(self):
        r = _gate(metrics={"fold_robust": False})
        assert r["stage"] == "rejected"
        assert "fold_robust" in r["reason"]

    def test_fails_pbo_at_boundary(self):
        # pbo == 0.50 is NOT < 0.50, so it fails
        r = _gate(pbo=0.50)
        assert r["stage"] == "rejected"
        assert "pbo_ok" in r["reason"]

    def test_fails_pbo_above_boundary(self):
        r = _gate(pbo=0.75)
        assert r["stage"] == "rejected"
        assert "pbo_ok" in r["reason"]

    def test_fails_holdout_confirms(self):
        r = _gate(holdout_confirms=False)
        assert r["stage"] == "rejected"
        assert "holdout_confirms" in r["reason"]


# ---------------------------------------------------------------------------
# 4. pbo edge cases
# ---------------------------------------------------------------------------
class TestPboBoundary:
    def test_pbo_none_passes(self):
        r = _gate(pbo=None)
        assert r["stage"] == "paper"
        assert r["checks"]["pbo_ok"] is True

    def test_pbo_just_below_boundary_passes(self):
        r = _gate(pbo=0.499)
        assert r["stage"] == "paper"

    def test_pbo_zero_passes(self):
        r = _gate(pbo=0.0)
        assert r["stage"] == "paper"


# ---------------------------------------------------------------------------
# 5. dsr_verdict fallback: dsr >= 0.95 without SURVIVES prefix
# ---------------------------------------------------------------------------
class TestDsrFallback:
    def test_dsr_095_without_survives_prefix_passes(self):
        r = _gate(metrics={"dsr": 0.95, "dsr_verdict": "some other text"})
        assert r["checks"]["dsr_survives"] is True
        assert r["stage"] == "paper"

    def test_dsr_below_095_without_survives_fails(self):
        r = _gate(metrics={"dsr": 0.94, "dsr_verdict": "MARGINAL (0.90<=DSR<0.95)"})
        assert r["checks"]["dsr_survives"] is False
        assert r["stage"] == "rejected"

    def test_survives_prefix_with_low_dsr_still_passes(self):
        # If dsr_verdict starts with SURVIVES, it passes even if dsr < 0.95
        # (shouldn't happen in practice but the gate respects either condition)
        r = _gate(metrics={"dsr": 0.90, "dsr_verdict": "SURVIVES (custom threshold)"})
        assert r["checks"]["dsr_survives"] is True


# ---------------------------------------------------------------------------
# 6. checks dict is present and populated on rejection
# ---------------------------------------------------------------------------
class TestChecksOnRejection:
    def test_checks_populated_on_rejection(self):
        r = _gate(metrics={"beats_spy": False})
        assert isinstance(r["checks"], dict)
        assert len(r["checks"]) > 0

    def test_checks_shows_false_for_failing_key(self):
        r = _gate(metrics={"crisis_pass": False})
        assert r["checks"]["crisis_pass"] is False

    def test_checks_shows_true_for_passing_keys(self):
        r = _gate(metrics={"crisis_pass": False})
        # beats_spy should still be True since it passes
        assert r["checks"]["beats_spy"] is True

    def test_first_failing_check_named(self):
        # dsr_survives is checked first in the ordered dict iteration
        r = _gate(metrics={"dsr": 0.50, "dsr_verdict": "FAILS", "beats_spy": False})
        # Python 3.7+ dicts are ordered; 'dsr_survives' comes first
        assert "dsr_survives" in r["reason"]
