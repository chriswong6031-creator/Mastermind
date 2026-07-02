"""tests/test_gate.py — guards for brain/gate.py (A6: gate wake triggers, W1).

WHAT IS UNDER TEST
------------------
state_signature():
  * stability  — identical input → identical signature (no thrashing on stable tape)
  * new fields flip it — confidence bucket, transition_state, contradicting list each
    independently change the signature when they change
  * old 3-field regime dicts produce 'na' tokens for all three new fields (backward
    compat — the only behaviour change is the first rebuild on deploy day, documented
    in the module comment)

should_run():
  * pre-existing trigger logic is unchanged (force, first_run, state_change, interval)
  * 'severity_tripwire' fires and is labeled when severity >= 2 (injected reader)
  * severity_tripwire fires even when the signature is unchanged (same-day crash event)
  * severity < 2 does NOT fire the tripwire
  * reader errors degrade gracefully (never block an otherwise-due run)
  * the 'carried' field is False when any trigger fires

All tests are fully offline — no filesystem reads, no live vendor data.
The conftest autouse fixtures isolate bot.db, runlog, and position_log automatically.
"""
from __future__ import annotations

import pytest

import bot  # noqa: F401  → puts vendor/macro on sys.path (needed by some transitive imports)
from brain import gate


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _regime(**kw) -> dict:
    """Build a regime dict with sensible defaults for fields the gate cares about.

    By default produces a full-featured A6-compatible frame; callers can override
    individual fields to test each new token independently.
    """
    base: dict = {
        "quad": "Q1",
        "quad_name": "Goldilocks",
        "liquidity_overlay": "expanding",
        "confidence": 0.45,          # bucket = round(0.45/0.15) = 3
        "transition_state": "STABLE",
        "contradicting": ["growth_copper_gold"],
        "macro_risk": {"score": 0.2},
    }
    base.update(kw)
    return base


def _old_regime(**kw) -> dict:
    """A 3-field regime dict as produced by the pre-A6 _regime_dict() copies.

    Has ONLY the three lens_row fields so all new A6 tokens must degrade to 'na'.
    """
    base: dict = {
        "quad": "Q1",
        "quad_name": "Goldilocks",
        "liquidity_overlay": "expanding",
        "macro_risk": {"score": 0.2},
    }
    base.update(kw)
    return base


def _sig(regime: dict | None = None, top_sector: str = "XLK") -> str:
    r = regime if regime is not None else _regime()
    return gate.state_signature(r, top_sector)


def _null_reader(_asof=None) -> int:
    """Severity reader that always returns 0 (no tripwire)."""
    return 0


def _sev2_reader(_asof=None) -> int:
    """Severity reader that always returns 2 (tripwire fires)."""
    return 2


def _sev1_reader(_asof=None) -> int:
    """Severity reader that returns 1 (below the tripwire threshold)."""
    return 1


# ---------------------------------------------------------------------------
# state_signature — stability
# ---------------------------------------------------------------------------

class TestStateSignatureStability:
    """Identical inputs must produce identical signatures (no spurious wake-ups)."""

    def test_identical_inputs_produce_identical_signature(self):
        r = _regime()
        assert _sig(r) == _sig(r)

    def test_top_sector_change_flips_signature(self):
        r = _regime()
        assert _sig(r, "XLK") != _sig(r, "XLE")

    def test_quad_change_flips_signature(self):
        assert _sig(_regime(quad="Q1")) != _sig(_regime(quad="Q4"))

    def test_liquidity_change_flips_signature(self):
        assert _sig(_regime(liquidity_overlay="expanding")) != _sig(_regime(liquidity_overlay="contracting"))

    def test_risk_band_lo_mid_hi(self):
        lo  = _sig(_regime(macro_risk={"score": 0.1}))   # < 0.34 → lo
        mid = _sig(_regime(macro_risk={"score": 0.5}))   # 0.34–0.66 → mid
        hi  = _sig(_regime(macro_risk={"score": 0.9}))   # > 0.66 → hi
        assert lo != mid and mid != hi and lo != hi

    def test_signature_format_has_seven_pipe_fields(self):
        """The full 7-field format must be parseable (deploy-day change documented in module)."""
        sig = _sig(_regime())
        parts = sig.split("|")
        assert len(parts) == 7, f"Expected 7 fields, got {len(parts)}: {sig!r}"


# ---------------------------------------------------------------------------
# state_signature — new A6 fields each independently flip the signature
# ---------------------------------------------------------------------------

class TestStateSignatureNewFields:
    """Each A6 field must independently change the signature when it changes."""

    # (a) quantized confidence bucket
    def test_confidence_bucket_changes_signature(self):
        """Confidence crossing a bucket boundary must flip the signature."""
        # bucket 3 = round(0.45/0.15); bucket 4 = round(0.60/0.15)
        r_low  = _regime(confidence=0.45)   # bucket 3
        r_high = _regime(confidence=0.60)   # bucket 4
        assert _sig(r_low) != _sig(r_high)

    def test_confidence_noise_within_bucket_does_not_flip(self):
        """Noise within the same bucket (±0.01) must NOT flip the signature."""
        r_a = _regime(confidence=0.45)    # bucket 3
        r_b = _regime(confidence=0.46)    # still bucket 3
        assert _sig(r_a) == _sig(r_b)

    def test_confidence_weakening_flip_moves_two_buckets(self):
        """A WEAKENING flip (e.g. 0.55 → 0.28) moves two buckets and wakes the gate."""
        r_before = _regime(confidence=0.55)  # bucket round(0.55/0.15) = 4
        r_after  = _regime(confidence=0.28)  # bucket round(0.28/0.15) = 2
        assert _sig(r_before) != _sig(r_after)

    # (b) transition_state
    def test_transition_state_change_flips_signature(self):
        r_stable    = _regime(transition_state="STABLE")
        r_weakening = _regime(transition_state="WEAKENING")
        assert _sig(r_stable) != _sig(r_weakening)

    def test_all_transition_states_produce_distinct_sigs(self):
        states = ["STABLE", "WEAKENING", "ROLLING", "DETERIORATING", "RECOVERING"]
        sigs = {_sig(_regime(transition_state=s)) for s in states}
        assert len(sigs) == len(states), "Each transition_state must produce a distinct signature"

    # (c) contradicting list
    def test_new_contradicting_leg_flips_signature(self):
        """Adding a leg to contradicting must change the signature (evidence accumulating)."""
        r_before = _regime(contradicting=["growth_copper_gold"])
        r_after  = _regime(contradicting=["growth_copper_gold", "growth_cyclical_defensive"])
        assert _sig(r_before) != _sig(r_after)

    def test_contradicting_order_does_not_flip_signature(self):
        """Changing the LIST ORDER of the same legs must NOT flip the signature (sorted join)."""
        r_a = _regime(contradicting=["growth_copper_gold", "growth_cyclical_defensive"])
        r_b = _regime(contradicting=["growth_cyclical_defensive", "growth_copper_gold"])
        assert _sig(r_a) == _sig(r_b)

    def test_empty_contradicting_list_is_stable(self):
        """An empty contradicting list must produce a stable, non-'na' token."""
        r = _regime(contradicting=[])
        sig_a = _sig(r)
        sig_b = _sig(r)
        assert sig_a == sig_b
        # and distinct from a non-empty list
        r_nonempty = _regime(contradicting=["growth_copper_gold"])
        assert sig_a != _sig(r_nonempty)


# ---------------------------------------------------------------------------
# state_signature — missing fields degrade to 'na' tokens
# ---------------------------------------------------------------------------

class TestStateSignatureMissingFieldsDegradeToNa:
    """Old 3-field regime dicts must produce 'na' tokens for all three new fields.

    This is the backward-compatibility guarantee: code that passes the pre-A6 slim
    regime dict (only quad/quad_name/liquidity_overlay) must see a STABLE signature
    on subsequent calls with the same slim dict — the 'na' tokens are literals and
    compare equal across runs.
    """

    def test_missing_confidence_produces_na_token(self):
        r = _old_regime()  # no confidence key
        sig = _sig(r)
        parts = sig.split("|")
        # field 5 (0-indexed: 4) is the confidence bucket
        assert parts[4] == "na", f"Expected 'na' for missing confidence, got {parts[4]!r}"

    def test_missing_transition_state_produces_na_token(self):
        r = _old_regime()
        sig = _sig(r)
        parts = sig.split("|")
        # field 6 (0-indexed: 5) is transition_state
        assert parts[5] == "na", f"Expected 'na' for missing transition_state, got {parts[5]!r}"

    def test_missing_contradicting_produces_na_token(self):
        r = _old_regime()
        sig = _sig(r)
        parts = sig.split("|")
        # field 7 (0-indexed: 6) is contradicting
        assert parts[6] == "na", f"Expected 'na' for missing contradicting, got {parts[6]!r}"

    def test_old_regime_dict_is_stable_across_identical_calls(self):
        """Two calls with the same old-regime dict must return the same signature."""
        r = _old_regime()
        assert _sig(r) == _sig(r)

    def test_none_confidence_produces_na_token(self):
        r = _regime(confidence=None)
        sig = _sig(r)
        parts = sig.split("|")
        assert parts[4] == "na"

    def test_none_transition_state_produces_na_token(self):
        r = _regime(transition_state=None)
        sig = _sig(r)
        parts = sig.split("|")
        assert parts[5] == "na"

    def test_none_contradicting_produces_na_token(self):
        r = _regime(contradicting=None)
        sig = _sig(r)
        parts = sig.split("|")
        assert parts[6] == "na"

    def test_non_list_contradicting_produces_na_token(self):
        """A contradicting value that is not a list/tuple (schema mismatch) must give 'na'."""
        r = _regime(contradicting="not_a_list")
        sig = _sig(r)
        parts = sig.split("|")
        assert parts[6] == "na"

    def test_first_four_fields_unchanged_by_old_regime(self):
        """The first four fields of the old dict are the same as the new; none should change."""
        r_old = _old_regime(quad="Q2", liquidity_overlay="contracting",
                            macro_risk={"score": 0.8})
        sig = _sig(r_old, top_sector="XLE")
        parts = sig.split("|")
        assert parts[0] == "Q2"
        assert parts[1] == "hi"   # score 0.8 > 0.66
        assert parts[2] == "contracting"
        assert parts[3] == "XLE"


# ---------------------------------------------------------------------------
# should_run — pre-existing trigger logic is UNCHANGED
# ---------------------------------------------------------------------------

class TestShouldRunPreExistingTriggers:
    """Pre-existing trigger logic must be byte-identical to the pre-A6 code."""

    def _run(self, sig, prev_run=None, *, force=False, interval_days=1, asof=None):
        return gate.should_run(sig, prev_run, force=force, interval_days=interval_days,
                               asof=asof, severity_reader=_null_reader)

    def test_first_run_fires(self):
        result = self._run("sig_a", prev_run=None)
        assert result["run"] is True
        assert "first_run" in result["triggers"]

    def test_force_fires(self):
        prev = {"state_sig": "sig_a", "asof": "2026-07-01"}
        result = self._run("sig_a", prev, force=True, asof="2026-07-01")
        assert "event" in result["triggers"]
        assert result["run"] is True

    def test_state_change_fires(self):
        prev = {"state_sig": "sig_old", "asof": "2026-07-01"}
        result = self._run("sig_new", prev, asof="2026-07-01")
        assert "state_change" in result["triggers"]
        assert result["run"] is True

    def test_interval_fires_after_n_days(self):
        prev = {"state_sig": "same_sig", "asof": "2026-07-01"}
        result = self._run("same_sig", prev, interval_days=1, asof="2026-07-02")
        assert "interval" in result["triggers"]
        assert result["run"] is True

    def test_interval_does_not_fire_same_day(self):
        prev = {"state_sig": "same_sig", "asof": "2026-07-01"}
        result = self._run("same_sig", prev, interval_days=1, asof="2026-07-01")
        # no state_change AND zero elapsed days < 1 interval → no run
        # BUT the severity_tripwire reader is _null_reader → no tripwire
        assert result["run"] is False
        assert result["triggers"] == []

    def test_carried_is_true_when_no_triggers(self):
        prev = {"state_sig": "same_sig", "asof": "2026-07-01"}
        result = self._run("same_sig", prev, asof="2026-07-01")
        assert result["carried"] is True

    def test_carried_is_false_when_triggers_fire(self):
        result = self._run("sig_a", prev_run=None)
        assert result["carried"] is False


# ---------------------------------------------------------------------------
# should_run — A6 severity_tripwire trigger
# ---------------------------------------------------------------------------

class TestShouldRunSeverityTripwire:
    """A6: the 'severity_tripwire' trigger fires (and is LABELED) when severity >= 2.

    The trigger is injected via severity_reader so tests are fully offline.
    """

    def test_severity2_fires_tripwire(self):
        """severity >= 2 must add 'severity_tripwire' to triggers and set run=True."""
        prev = {"state_sig": "same_sig", "asof": "2026-07-01"}
        result = gate.should_run("same_sig", prev, asof="2026-07-01",
                                 severity_reader=_sev2_reader)
        assert "severity_tripwire" in result["triggers"], (
            "severity=2 derisk artifact must wake a same-day rebuild"
        )
        assert result["run"] is True

    def test_severity1_does_not_fire_tripwire(self):
        """severity < 2 must NOT add 'severity_tripwire'."""
        prev = {"state_sig": "same_sig", "asof": "2026-07-01"}
        result = gate.should_run("same_sig", prev, asof="2026-07-01",
                                 severity_reader=_sev1_reader)
        assert "severity_tripwire" not in result["triggers"]

    def test_severity0_does_not_fire_tripwire(self):
        """severity = 0 (no artifacts) must NOT add 'severity_tripwire'."""
        prev = {"state_sig": "same_sig", "asof": "2026-07-01"}
        result = gate.should_run("same_sig", prev, asof="2026-07-01",
                                 severity_reader=_null_reader)
        assert "severity_tripwire" not in result["triggers"]

    def test_tripwire_fires_on_stable_regime_unchanged_sig(self):
        """The tripwire must fire even when the regime is unchanged (same-day crash event)."""
        prev = {"state_sig": "same_sig", "asof": "2026-07-01"}
        result = gate.should_run("same_sig", prev, asof="2026-07-01",
                                 severity_reader=_sev2_reader)
        # no state_change, no interval elapsed — but the tripwire wakes it anyway
        assert result["run"] is True
        assert "severity_tripwire" in result["triggers"]
        assert "state_change" not in result["triggers"]
        assert "interval" not in result["triggers"]

    def test_tripwire_fires_on_first_run(self):
        """The tripwire should be labeled even on first_run (additive trigger logic)."""
        result = gate.should_run("sig_a", None, asof="2026-07-01",
                                 severity_reader=_sev2_reader)
        assert "first_run" in result["triggers"]
        assert "severity_tripwire" in result["triggers"]

    def test_tripwire_is_labeled_as_severity_tripwire(self):
        """The trigger label must be the literal string 'severity_tripwire' (run-log consumers
        parse this string to distinguish a crash-event rebuild from a routine cadence rebuild)."""
        prev = {"state_sig": "same_sig", "asof": "2026-07-01"}
        result = gate.should_run("same_sig", prev, asof="2026-07-01",
                                 severity_reader=_sev2_reader)
        labels = result["triggers"]
        assert "severity_tripwire" in labels
        # must be a string in the list, not a nested dict or bool
        assert isinstance(labels[labels.index("severity_tripwire")], str)

    def test_reader_error_degrades_gracefully(self):
        """A reader that raises must never block an otherwise-scheduled run — degrade silently."""
        def _bad_reader(_asof=None):
            raise RuntimeError("filesystem unavailable")

        # first_run fires independently of the reader → the run must still proceed
        result = gate.should_run("sig_a", None, asof="2026-07-01",
                                 severity_reader=_bad_reader)
        assert result["run"] is True
        assert "first_run" in result["triggers"]
        # the tripwire itself did NOT fire (reader errored) but no exception was raised
        assert "severity_tripwire" not in result["triggers"]

    def test_reader_error_does_not_block_no_trigger_run(self):
        """When NO other trigger fires and the reader errors, the run does NOT proceed —
        the gate must not fire spuriously on reader failure."""
        def _bad_reader(_asof=None):
            raise ValueError("test error")

        prev = {"state_sig": "same_sig", "asof": "2026-07-01"}
        result = gate.should_run("same_sig", prev, asof="2026-07-01",
                                 severity_reader=_bad_reader)
        # same sig, same day, reader error → no triggers at all → no run
        assert result["run"] is False
        assert result["triggers"] == []

    def test_default_reader_reads_macro_risk_dir(self, tmp_path, monkeypatch):
        """Integration: the default _default_severity_reader finds severity from a fixture
        derisk file placed in the expected directory layout (data/macro_risk/<asof>/)."""
        asof = "2026-07-02"
        artifact_dir = tmp_path / "data" / "macro_risk" / asof
        artifact_dir.mkdir(parents=True)
        derisk = {
            "pid": "flagship",
            "asof": asof,
            "tripwire": {"trigger": True, "severity": 2, "reasons": ["test crash"]},
            "action": "hold",
        }
        (artifact_dir / "derisk_flagship.json").write_text(__import__("json").dumps(derisk))
        # Monkeypatch _ROOT so the reader finds our tmp_path tree.
        monkeypatch.setattr(gate, "_ROOT", tmp_path)
        sev = gate._default_severity_reader(asof)
        assert sev == 2

    def test_default_reader_returns_zero_on_missing_dir(self, tmp_path, monkeypatch):
        """The default reader must return 0 (not raise) when the directory does not exist."""
        monkeypatch.setattr(gate, "_ROOT", tmp_path)
        sev = gate._default_severity_reader("2099-01-01")
        assert sev == 0

    def test_default_reader_returns_max_severity_across_books(self, tmp_path, monkeypatch):
        """When multiple books fire at different severities, max must be returned."""
        import json
        asof = "2026-07-02"
        artifact_dir = tmp_path / "data" / "macro_risk" / asof
        artifact_dir.mkdir(parents=True)
        for pid, sev in (("flagship", 2), ("autonomous", 3), ("etf", 1)):
            (artifact_dir / f"derisk_{pid}.json").write_text(
                json.dumps({"tripwire": {"severity": sev}})
            )
        monkeypatch.setattr(gate, "_ROOT", tmp_path)
        assert gate._default_severity_reader(asof) == 3


# ---------------------------------------------------------------------------
# End-to-end: A6 scenario — WEAKENING flip on SOXX crash day
# ---------------------------------------------------------------------------

class TestA6Scenario:
    """Demonstrate the full A6 wake-trigger chain on a realistic crash-day scenario.

    Before A6 (stable signature):  regime didn't change quad/band/liquidity → carried.
    After A6 (new tokens):         confidence falls + transition=WEAKENING + new contradicting
                                   leg → all three new fields change → state_change fires.
                                   Additionally the severity-2 tripwire fires independently.
    """

    def _stable_run(self, sig, prev_sig, sev_reader=None):
        """Previous run with same sig → prior to A6, this would be carried (no state_change)."""
        prev = {"state_sig": prev_sig, "asof": "2026-07-01"}
        return gate.should_run(sig, prev, asof="2026-07-01",
                               severity_reader=sev_reader or _null_reader)

    def test_weakening_flip_wakes_rebuild(self):
        """Pre-crash regime (STABLE, confidence=0.55) vs crash-day regime (WEAKENING,
        confidence=0.28, new contradicting leg) must produce a state_change trigger."""
        r_pre = _regime(confidence=0.55, transition_state="STABLE",
                        contradicting=["growth_copper_gold"])
        r_crash = _regime(confidence=0.28, transition_state="WEAKENING",
                          contradicting=["growth_copper_gold", "growth_cyclical_defensive"])
        sig_pre   = gate.state_signature(r_pre, "XLK")
        sig_crash = gate.state_signature(r_crash, "XLK")
        result = self._stable_run(sig_crash, sig_pre)
        assert result["run"] is True
        assert "state_change" in result["triggers"]

    def test_crash_day_tripwire_also_fires_independently(self):
        """Even if the regime were somehow unchanged (same sig), the severity-2 tripwire
        must independently wake the rebuild — the gate is never blind on a crash day."""
        sig = gate.state_signature(_regime(), "XLK")
        result = self._stable_run(sig, sig, sev_reader=_sev2_reader)
        assert result["run"] is True
        assert "severity_tripwire" in result["triggers"]
